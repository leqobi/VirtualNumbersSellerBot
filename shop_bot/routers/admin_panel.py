"""Админ-панель EXAMPLE SPLIT: разделы, статистика, настройки, поддержка, экспорт.

Доступ только у Telegram ID из ADMIN_IDS (переменные окружения).
Разделы: заказы, каталог, пополнения, пользователи, поддержка,
статистика, настройки магазина, рассылка, экспорт CSV.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import db, services
from ..config import ACTIVE_DAYS, ADMINS, MANUAL_WALLET_ADDRESS
from ..helpers import esc, kb, money, show, USD_RUB_RATE, fmt_ts

r = Router(name="admin_panel")

ADMIN = F.from_user.id.in_(ADMINS)


class SettingsEdit(StatesGroup):
    value = State()


class AdminReply(StatesGroup):
    text = State()


def admin_kb():
    return kb(
        [("📦 Заказы", "aord:l:0"), ("🛍 Каталог", "cats")],
        [("💳 Пополнения", "apay"), ("👥 Пользователи", "adm:users")],
        [("✉️ Поддержка", "asup"), ("📊 Статистика", "adm:stats")],
        [("⚙️ Настройки", "adm:cfg"), ("📤 Экспорт CSV", "adm:export")],
        [("📢 Рассылка", "adm:bc")],
        [("⬅️ В меню", "menu")],
    )


@r.message(Command("admin"), ADMIN)
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_kb())


@r.callback_query(ADMIN, F.data == "adm:panel")
async def admin_panel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "⚙️ <b>Админ-панель</b>", admin_kb())


# ---------------- настройки магазина ----------------
@r.callback_query(ADMIN, F.data == "adm:cfg")
async def admin_settings(c: CallbackQuery):
    wallet_state = "задан" if MANUAL_WALLET_ADDRESS else "не задан (MANUAL_WALLET_ADDRESS)"
    text = (
        "⚙️ <b>Настройки магазина</b>\n\n"
        "Изменяемые здесь тексты:\n"
        "• «О магазине» — текст пункта меню «О магазине»;\n"
        "• «Приветствие» — сообщение при /start.\n\n"
        f"Кошелёк пополнений (env): {wallet_state}\n"
        f"Курс ЦБ РФ: <b>1 USD = {USD_RUB_RATE:.4f} ₽</b>\n\n"
        "Нажмите пункт и пришлите новый текст одним сообщением.")
    await show(c, text, kb(
        [("ℹ️ О магазине", "adm:cfg:about_text")],
        [("👋 Приветствие", "adm:cfg:welcome_text")],
        [("⬅️ Админ-панель", "adm:panel")]))


@r.callback_query(ADMIN, F.data.startswith("adm:cfg:"))
async def admin_settings_edit(c: CallbackQuery, state: FSMContext):
    key = c.data.rsplit(":", 1)[1]
    if key not in ("about_text", "welcome_text"):
        return await c.answer("Неизвестная настройка", show_alert=True)
    title = "ℹ️ О магазине" if key == "about_text" else "👋 Приветствие (/start)"
    current = await db.settings_get(key)
    await state.set_state(SettingsEdit.value)
    await state.update_data(setting_key=key)
    await show(
        c,
        f"✏️ <b>{title}</b>\n\nТекущий текст:\n<i>{esc(current[:1200])}</i>\n\n"
        "Пришлите новый текст одним сообщением.",
        kb([("⬅️ Отмена", "adm:cfg")]))


@r.message(StateFilter(SettingsEdit.value), ADMIN, F.text)
async def admin_settings_save(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")
    text = (m.text or "").strip()
    if key not in ("about_text", "welcome_text"):
        await state.clear()
        return await m.answer("⚠️ Настройка не найдена, попробуйте ещё раз через /admin.")
    if not 10 <= len(text) <= 3500:
        return await m.answer("Текст: от 10 до 3500 символов.")
    await db.settings_set(key, text)
    await state.clear()
    await m.answer("✅ Текст сохранён.", reply_markup=kb([("⬅️ Админ-панель", "adm:panel")]))


# ---------------- статистика ----------------
@r.callback_query(ADMIN, F.data == "adm:stats")
async def admin_stats(c: CallbackQuery):
    ts = services.now()
    day0 = ts - ts % 86400
    week, month = ts - 7 * 86400, ts - 30 * 86400
    sql = services.paid_in_sql()
    users = await db.fetchval("SELECT COUNT(*) FROM users")
    banned = await db.fetchval("SELECT COUNT(*) FROM users WHERE banned")
    by_status = await db.fetch(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(amount),0) AS s "
        "FROM shop_orders GROUP BY status ORDER BY n DESC")
    rev = await db.fetchrow(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s, "
        f"COUNT(*) FILTER (WHERE created_at>=$1) AS nd, "
        f"COALESCE(SUM(amount) FILTER (WHERE created_at>=$1),0) AS sd, "
        f"COUNT(*) FILTER (WHERE created_at>=$2) AS nw, "
        f"COALESCE(SUM(amount) FILTER (WHERE created_at>=$2),0) AS sw, "
        f"COUNT(*) FILTER (WHERE created_at>=$3) AS nm, "
        f"COALESCE(SUM(amount) FILTER (WHERE created_at>=$3),0) AS sm "
        f"FROM shop_orders WHERE status IN {sql}", day0, week, month)
    top = await db.fetch(
        f"SELECT product_name, COUNT(*) AS n, COALESCE(SUM(amount),0) AS s "
        f"FROM shop_orders WHERE status IN {sql} "
        f"GROUP BY product_name ORDER BY n DESC LIMIT 5")
    paid_sum = await db.fetchval(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE {services.TOPUP_TX}")
    lines = [
        "📊 <b>Статистика</b>",
        f"👥 Пользователей: {users} (заблокировано: {banned})",
        "",
        "<b>Заказы по статусам</b>",
    ]
    for x in by_status:
        lines.append(
            f"{services.status_text(x['status'])}: {x['n']} шт. на "
            f"{float(x['s']):,.0f} ₽".replace(",", " "))
    if rev["n"]:
        lines += [
            "",
            "<b>Оплаченные заказы (выручку)</b>",
            f"Всего: {rev['n']} шт. на {float(rev['s']):,.0f} ₽ ≈ {money(rev['s'] / USD_RUB_RATE)}".replace(",", " "),
            f"Сегодня: {rev['nd']} шт. на {float(rev['sd']):,.0f} ₽".replace(",", " "),
            f"7 дней: {rev['nw']} шт. на {float(rev['sw']):,.0f} ₽".replace(",", " "),
            f"30 дней: {rev['nm']} шт. на {float(rev['sm']):,.0f} ₽".replace(",", " "),
        ]
    if top:
        lines += ["", "<b>Оплаченные товары — топ</b>"]
        for x in top:
            lines.append(
                f"{esc(x['product_name'])}: {x['n']} шт. на "
                f"{float(x['s']):,.0f} ₽".replace(",", " "))
    lines += ["", f"Пополнений суммарно: <b>{money(float(paid_sum or 0))}</b>"]
    await show(c, "\n".join(lines), admin_kb())


# ---------------- рассылка ----------------
@r.callback_query(ADMIN, F.data == "adm:bc")
async def broadcast_hint(c: CallbackQuery):
    await show(
        c,
        "📢 <b>Рассылка</b>\n\n"
        "<code>/broadcast текст</code> — всем пользователям\n"
        f"<code>/broadcast active текст</code> — активным за {ACTIVE_DAYS} дн.\n"
        "Или ответьте <code>/broadcast</code> на готовое сообщение — "
        "оно придёт как есть (текст, фото или файл).",
        admin_kb())


@r.message(Command("broadcast"), ADMIN)
async def broadcast(m: Message, bot: Bot):
    rest = (m.text or "").partition(" ")[2].strip()
    active_only = False
    first, _, tail = rest.partition(" ")
    if first.lower() in ("active", "актив", "активные"):
        active_only = True
        rest = tail.strip()
    src = m.reply_to_message
    if not rest and not src:
        return await m.answer(
            "Формат:\n<code>/broadcast текст</code> — всем\n"
            f"<code>/broadcast active текст</code> — активным (за {ACTIVE_DAYS} д.)\n"
            "или ответьте /broadcast на готовое сообщение.")
    if active_only:
        rows = await db.fetch(
            "SELECT id FROM users WHERE NOT banned "
            "AND COALESCE(last_active, joined, 0) >= $1",
            services.now() - ACTIVE_DAYS * 86400)
    else:
        rows = await db.fetch("SELECT id FROM users WHERE NOT banned")
    users = [x["id"] for x in rows]
    await m.answer(f"📤 Рассылка на {len(users)} пользователей…")

    async def send(uid):
        if src is not None:
            await bot.copy_message(uid, src.chat.id, src.message_id)
        else:
            await bot.send_message(uid, rest)

    ok = blocked = fail = 0
    for uid in users:
        try:
            try:
                await send(uid)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await send(uid)
            ok += 1
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Доставлено: {ok}\n🚫 Заблокировали бота: {blocked}\n❌ Ошибки: {fail}")


# ---------------- экспорт CSV ----------------
EXPORT_SPECS = [
    ("users.csv", "SELECT id, username, name, balance, joined, last_active, banned, contact_phone "
                  "FROM users ORDER BY id",
     ["id", "username", "name", "balance", "joined", "last_active", "banned", "contact_phone"]),
    ("categories.csv", "SELECT id, name, position, active, created_at FROM categories ORDER BY id",
     ["id", "name", "position", "active", "created_at"]),
    ("products.csv", "SELECT id, category_id, name, description, price, stock, active, created_at "
                     "FROM products ORDER BY id",
     ["id", "category_id", "name", "description", "price", "stock", "active", "created_at"]),
    ("shop_orders.csv",
     "SELECT id, user_id, product_id, product_name, product_price, qty, amount, city, pvz, "
     "pay_method, charged_amount, charged_currency, rate, tracking_number, delivery_note, "
     "status, created_at, paid_at, shipped_at, refunded_at FROM shop_orders ORDER BY id",
     ["id", "user_id", "product_id", "product_name", "product_price", "qty", "amount", "city",
      "pvz", "pay_method", "charged_amount", "charged_currency", "rate", "tracking_number",
      "delivery_note", "status", "created_at", "paid_at", "shipped_at", "refunded_at"]),
    ("transactions.csv", "SELECT id, user_id, type, amount, currency, external_id, created_at, note "
                         "FROM transactions ORDER BY id",
     ["id", "user_id", "type", "amount", "currency", "external_id", "created_at", "note"]),
    ("manual_topups.csv", "SELECT id, user_id, amount, status, created_at, reviewed_at "
                          "FROM manual_topups ORDER BY id",
     ["id", "user_id", "amount", "status", "created_at", "reviewed_at"]),
    ("rub_topups.csv", "SELECT id, user_id, amount_rub, amount_usd, status, created_at "
                       "FROM rub_topups ORDER BY id",
     ["id", "user_id", "amount_rub", "amount_usd", "status", "created_at"]),
    ("payments.csv", "SELECT id, invoice_id, user_id, amount, asset, status, created_at, paid_at "
                     "FROM payments ORDER BY id",
     ["id", "invoice_id", "user_id", "amount", "asset", "status", "created_at", "paid_at"]),
    ("numbers.csv",
     "SELECT n.id, co.name country, l.service, n.phone, n.status, n.buyer, n.sold_at, n.sold_price "
     "FROM numbers n LEFT JOIN lots l ON l.id=n.lot_id LEFT JOIN countries co ON co.id=l.country_id "
     "ORDER BY n.id",
     ["id", "country", "service", "phone", "status", "buyer", "sold_at", "sold_price"]),
]

TS_COLUMNS = {"joined", "last_active", "created_at", "paid_at", "reviewed_at",
              "shipped_at", "refunded_at", "closed_at", "sold_at"}
NUM_LIKE = re.compile(r"^[+-]?[\d.]+$")


def _csv_cell(col, v):
    if v is None:
        return ""
    if col in TS_COLUMNS:
        try:
            return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(v)
    if isinstance(v, str) and v and (v[0] in "=@" or (v[0] in "+-" and not NUM_LIKE.match(v))):
        return "'" + v  # защита от формул при открытии в Excel
    return v


async def build_export():
    files = []
    for name, query, cols in EXPORT_SPECS:
        rows = await db.fetch(query)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for row in rows:
            w.writerow([_csv_cell(col, row[col]) for col in cols])
        files.append((name, ("\ufeff" + buf.getvalue()).encode("utf-8")))
    return files


async def send_export(bot: Bot, chat_id: int):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    for name, data in await build_export():
        await bot.send_document(chat_id, BufferedInputFile(data, filename=f"{stamp}_{name}"))


@r.message(Command("export"), ADMIN)
async def export_cmd(m: Message, bot: Bot):
    await m.answer("📤 Готовлю выгрузку…")
    try:
        await send_export(bot, m.chat.id)
    except Exception:
        logging.exception("export failed")
        await m.answer("❌ Не удалось подготовить выгрузку.")


@r.callback_query(ADMIN, F.data == "adm:export")
async def admin_export_button(c: CallbackQuery, bot: Bot):
    await c.answer("Готовлю выгрузку…")
    chat_id = c.message.chat.id if c.message is not None else c.from_user.id
    try:
        await send_export(bot, chat_id)
    except Exception:
        logging.exception("export failed")
        await bot.send_message(chat_id, "❌ Не удалось подготовить выгрузку.")


# ---------------- поддержка: список открытых диалогов ----------------
@r.callback_query(ADMIN, F.data == "asup")
async def support_queue(c: CallbackQuery):
    rows = await db.fetch(
        "SELECT user_id, COUNT(*) AS n, MAX(msg_id) AS last_msg "
        "FROM msgmap WHERE answered = FALSE GROUP BY user_id "
        "ORDER BY MAX(msg_id) DESC LIMIT 15")
    if not rows:
        return await show(c, "✉️ Открытых обращений нет.",
                          kb([("⬅️ Админ-панель", "adm:panel")]))
    btns = []
    for x in rows:
        u = await db.fetchrow("SELECT name, username FROM users WHERE id=$1", x["user_id"])
        label = x["user_id"]
        if u is not None:
            label = f"{u['name']} (@{u['username']})" if u["username"] else f"{u['name']}"
        btns.append([(f"✉️ {label} · {x['n']} сообщ.", f"asup:u:{x['user_id']}")])
    btns.append([("⬅️ Админ-панель", "adm:panel")])
    await show(
        c,
        "✉️ <b>Открытые обращения</b>\n\n"
        "Ответьте реплаем на сообщение пользователя (или откройте карточку), "
        "потом диалог закроется.",
        kb(*btns))


@r.callback_query(ADMIN, F.data.startswith("asup:u:"))
async def support_user_card(c: CallbackQuery, state: FSMContext):
    uid = int(c.data.rsplit(":", 1)[1])
    u = await db.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    if not u:
        return await c.answer("Пользователь не найден", show_alert=True)
    cnt = await db.fetchval("SELECT COUNT(*) FROM msgmap WHERE user_id=$1 AND answered=FALSE", uid)
    uname = f"@{u['username']}" if u["username"] else "—"
    await show(
        c,
        f"✉️ <b>Обращение</b>\n\n👤 {esc(u['name'] or '—')} ({esc(uname)})\n"
        f"🆔 <code>{uid}</code>\n"
        f"Непрочитанных сообщений: <b>{cnt}</b>\n\n"
        "Ответьте на сообщение пользователя или напишите ответ здесь.",
        kb([("💬 Написать ответ", f"asup:write:{uid}")],
           [("✅ Закрыть диалог", f"asup:close:{uid}")],
           [("⬅️ К обращениям", "asup")]))


@r.callback_query(ADMIN, F.data.startswith("asup:write:"))
async def support_write_start(c: CallbackQuery, state: FSMContext):
    uid = int(c.data.rsplit(":", 1)[1])
    await state.set_state(AdminReply.text)
    await state.update_data(reply_uid=uid)
    await show(c, "💬 Пришлите текст ответа одним сообщением:",
               kb([("⬅️ Отмена", f"asup:u:{uid}")]))


@r.message(StateFilter(AdminReply.text), ADMIN, F.text)
async def support_write_send(m: Message, state: FSMContext, bot: Bot):
    from .support import send_user_answer
    data = await state.get_data()
    uid = data.get("reply_uid")
    if not isinstance(uid, int):
        await state.clear()
        return await m.answer("Ответ потерялся — начните заново.")
    ok = await send_user_answer(bot, m.from_user.id, uid, (m.text or "").strip()[:3000])
    await state.clear()
    if not ok:
        return await m.answer("❌ Не доставлено.")
    await m.answer("✅ Ответ отправлен.", reply_markup=kb([("⬅️ К обращениям", "asup")]))


@r.callback_query(ADMIN, F.data.startswith("asup:close:"))
async def support_close(c: CallbackQuery):
    uid = int(c.data.rsplit(":", 1)[1])
    await db.execute("UPDATE msgmap SET answered=TRUE WHERE user_id=$1 AND answered=FALSE", uid)
    await c.answer("Диалог закрыт", show_alert=True)
    await support_queue(c)
