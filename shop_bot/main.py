"""EXAMPLE SPLIT бот: точка входа.

Здесь собирается всё приложение:
• пул PostgreSQL (db.init_pool) и идемпотентные миграции (db.run_migrations);
• курс ЦБ (helpers.update_usd_rub_rate + cbr_rate_worker);
• keepalive-HTTP-сервер на PORT (для платформ типа Render);
• глобальный middleware: апсерт пользователя и блокировка забаненных;
• Dispatcher с MemoryStorage и глобальным обработчиком ошибок;
• роутеры: админ-модули → пользовательские разделы;
• фоновый worker автоматических CSV-бэкапов админам (BACKUP_INTERVAL_HOURS).

Запуск: `python -m shop_bot.main`
Переменные окружения: BOT_TOKEN, ADMIN_IDS, DATABASE_URL, PORT,
MANUAL_WALLET_ADDRESS, LOW_STOCK_THRESHOLD, BACKUP_INTERVAL_HOURS.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, Update

from . import config as cfg
from . import db
from .helpers import cbr_rate_worker, update_usd_rub_rate
from .routers import (
    admin_catalog,
    admin_orders,
    admin_panel,
    admin_payments,
    admin_users,
    catalog,
    checkout,
    common,
    orders,
    support,
    topup,
)
from .routers.admin_panel import send_export

logger = logging.getLogger("shop_bot")

USER_FIELDS = ("message", "edited_message", "callback_query", "inline_query",
               "pre_checkout_query", "shipping_query", "my_chat_member",
               "chat_member", "chat_join_request")


def _scan_user(update: Update):
    """Достаёт from_user как можно раньше (не полагаясь на aiogram-данные)."""
    for field in USER_FIELDS:
        inner = getattr(update, field, None)
        if inner is None:
            continue
        user = getattr(inner, "from_user", None)
        if user is not None:
            return user
    return None


def _ts() -> int:
    return int(time.time())


class UserGate:
    """Outer-middleware на Update: апсерт пользователя и защита от банов.

    Апсерт идемпотентный (ON CONFLICT); сохранённая структура users не меняется.
    Забаненным пользователям обработчики недоступны; админам — без ограничений.
    """

    async def _ensure(self, user) -> bool | None:
        ts = _ts()
        try:
            row = await db.fetchrow(
                "INSERT INTO users(id, username, name, joined, last_active) "
                "VALUES($1,$2,$3,$4,$4) "
                "ON CONFLICT (id) DO UPDATE SET username=$2, name=$3, last_active=$4 "
                "RETURNING banned",
                user.id, user.username, user.full_name, ts)
            return None if row is None else bool(row["banned"])
        except Exception:
            logger.exception("user upsert failed for %s", getattr(user, "id", "?"))
            return False

    @staticmethod
    async def _deny(update: Update) -> None:
        try:
            if update.callback_query is not None:
                await update.callback_query.answer("⛔ Доступ к боту заблокирован.",
                                                   show_alert=True)
            elif update.message is not None:
                await update.message.answer("⛔ Ваш доступ к боту ограничен.")
        except Exception:
            pass

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user") or _scan_user(event)
        if user is None or user.is_bot or user.id in cfg.ADMINS:
            return await handler(event, data)
        banned = await self._ensure(user)
        if banned:
            await self._deny(event)
            return None
        return await handler(event, data)


# ---------------- прочие вспомогательные модули ----------------
async def _start_keepalive() -> web.AppRunner:
    """HTTP-сервер «/» → 200 на PORT — healthcheck для хостингов."""
    app = web.Application()
    async def ok(_request):
        return web.Response(text="OK")
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.PORT)
    await site.start()
    logger.info("Keepalive server listening on 0.0.0.0:%s", cfg.PORT)
    return runner


async def backup_worker(bot: Bot) -> None:
    """Периодическая выгрузка CSV обособленным воркером (задан в env)."""
    if not cfg.BACKUP_INTERVAL_HOURS:
        return
    while True:
        await asyncio.sleep(cfg.BACKUP_INTERVAL_HOURS * 3600)
        try:
            for admin_id in cfg.ADMINS:
                await send_export(bot, admin_id)
        except Exception:
            logger.exception("backup worker error")


def build_dispatcher() -> Dispatcher:
    """Собирает Dispatch с роутерами; глобальный обработчик ошибок."""
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UserGate())

    @dp.error()
    async def on_error(event: ErrorEvent) -> None:
        logger.exception("Unhandled error: %r", event.exception)
        update = event.update
        chat_id = None
        if update.message is not None:
            chat_id = update.message.chat.id
        elif update.callback_query is not None and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id
        if chat_id is not None:
            try:
                await event.bot.send_message(chat_id, "⚠️ Произошла ошибка при обработке. "
                                                "Попробуйте ещё раз позже.")
            except Exception:
                pass

    for module in (admin_panel, admin_catalog, admin_orders, admin_payments,
                   admin_users, common, topup, catalog, checkout, orders, support):
        dp.include_router(module.r)
    return dp


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    bot = Bot(token=cfg.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    pool = await db.init_pool(cfg.DATABASE_URL)
    logger.info("Database pool created")
    try:
        await db.run_migrations()
        logger.info("Migrations applied")
    except Exception:
        logger.exception("Migrations failed")
        await pool.close()
        raise
    await update_usd_rub_rate()

    dp = build_dispatcher()

    runner = None
    tasks: list[asyncio.Task] = []
    try:
        runner = await _start_keepalive()
        tasks.append(asyncio.create_task(cbr_rate_worker()))
        if cfg.BACKUP_INTERVAL_HOURS:
            tasks.append(asyncio.create_task(backup_worker(bot)))
        logger.info("Bot polling started")
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        if runner is not None:
            await runner.cleanup()
        await pool.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
