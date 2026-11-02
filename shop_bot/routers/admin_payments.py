"""Пополнения EXAMPLE SPLIT (админ): экран заявок на пополнение баланса.

Показывает pending-заявки двух типов (унаследованные механизмы):
• USDT TRC20 (manual_topups) — кнопки «Зачислить»/«Отклонить»;
• рубли (rub_topups) — ссылки на готовые подтверждения из topup.py.

Зачисление идёт через services.adjust_balance: одна транзакция БД, запись
в transactions с external_id, заявка закрывается атомарно (повторное
зачисление по одной заявке невозможно).
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from .. import db, services
from ..helpers import Busy, esc, fmt_ts, kb, money, rub_money, show, USD_RUB_RATE

from .admin_panel import ADMIN, admin_kb

r = Router(name="admin_payments")

PAGE = 8
_busy: set[int] = set()

RUB_STATUS_LABELS = {
    "pending": "⏳ не обработана",
    "details_sent": "📨 реквизиты отправлены",
    "completed": "✅ зачислено",
    "cancelled": "❌ отменено",
}


def _int_of(data, idx: int = 1):
    try:
        return int(str(data).split(":")[idx])
    except (ValueError, IndexError):
        return None


# ---------------- экран заявок ----------------
@r.callback_query(ADMIN, F.data == "apay")
async def payments_screen(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_payments(c)


async def render_payments(c: CallbackQuery):
    usdt_rows = await db.fetch(
        "SELECT t.*, u.name AS uname, u.username FROM manual_topups t "
        "LEFT JOIN users u ON u.id=t.user_id "
        "WHERE t.status='pending' ORDER BY t.id DESC LIMIT $1", PAGE)
    usdt_cnt = await db.fetchval("SELECT COUNT(*) FROM manual_topups WHERE status='pending'")
    rub_rows = await db.fetch(
        "SELECT t.*, u.name AS uname, u.username FROM rub_topups t "
        "LEFT JOIN users u ON u.id=t.user_id "
        "WHERE t.status IN ('pending','details_sent') ORDER BY t.id DESC LIMIT $1", PAGE)
    rub_cnt = await db.fetchval(
        "SELECT COUNT(*) FROM rub_topups WHERE status IN ('pending','details_sent')")
    ts = services.now()
    today_usd = await db.fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM transactions "
        "WHERE type='topup' AND created_at>=$1", ts - ts % 86400)

    lines = [
        "💳 <b>Заявки на пополнение</b>",
        "",
        f"💵 USDT-заявок в ожидании: <b>{usdt_cnt}</b>",
        f"🇷🇺 Рублёвых заявок в ожидании: <b>{rub_cnt}</b>",
        f"🕒 Зачислено пополнений за сегодня: <b>{money(today_usd or 0)}</b>",
        "",
    ]
    btns = []
    if not usdt_rows and not rub_rows:
        lines.append("✅ Активных заявок нет.")
    for x in usdt_rows:
        uname = f"@{x['username']}" if x["username"] else (x["uname"] or "—")
        lines.append(
            f"💵 Заявка <b>#{x['id']}</b> · <b>{money(x['amount'])}</b> "
            f"≈ {rub_money(x['amount'])}"
            f"\n👤 {esc(uname)} · id <code>{x['user_id']}</code> · {fmt_ts(x['created_at'])}")
        btns.append([
            (f"✅ Зачислить #{x['id']}", f"mtpay:{x['id']}"),
            ("❌ Отклонить", f"mtdeny:{x['id']}"),
        ])
    if rub_rows:
        lines.append("")
        lines.append("🇷🇺 <b>Заявки пополнения рублями</b>")
    for x in rub_rows:
        uname = f"@{x['username']}" if x["username"] else (x["uname"] or "—")
        amount_rub_text = f"{float(x['amount_rub']):,.2f}".replace(",", " ")
        lines.append(
            f"🇷🇺 Заявка <b>#{x['id']}</b> · <b>{amount_rub_text} ₽</b> "
            f"≈ {money(x['amount_usd'])}"
            f"\nКурс: {float(x['rate'] or USD_RUB_RATE):.4f} · "
            f"{RUB_STATUS_LABELS.get(x['status'], x['status'])}"
            f"\n👤 {esc(uname)} · id <code>{x['user_id']}</code> · {fmt_ts(x['created_at'])}")
        btns.append([
            (f"📨 Реквизиты #{x['id']}", f"rubdetails:{x['id']}"),
            ("💰 Зачислить", f"rubconfirm:{x['id']}"),
        ])
        btns.append([("❌ Отменить заявку", f"rubcancel:{x['id']}")])
    btns.append([("🔄 Обновить", "apay")])
    btns.append([("⬅️ Админ-панель", "adm:panel")])
    await show(c, "\n".join(lines), kb(*btns))


# ---------------- USDT: зачисление ----------------
@r.callback_query(ADMIN, F.data.startswith("mtpay:"))
async def manual_topup_confirm(c: CallbackQuery, bot: Bot):
    """Зачисление USDT-заявки: атомарно баланс + ledger + закрытие заявки."""
    tid = _int_of(c.data)
    if tid is None:
        return await c.answer()
    req = await db.fetchrow(
        "SELECT t.*, u.username, u.name AS uname FROM manual_topups t "
        "LEFT JOIN users u ON u.id=t.user_id WHERE t.id=$1", tid)
    if not req:
        return await c.answer("Заявка не найдена", show_alert=True)
    if req["status"] != "pending":
        return await c.answer(
            f"Заявка уже обработана (статус: {req['status']}).", show_alert=True)
    async with Busy(c.from_user.id, _busy) as busy:
        if not busy.ok:
            return await c.answer("⏳ Уже обрабатывается…")
        try:
            balance, _closed = await services.adjust_balance(
                c.from_user.id, req["user_id"], float(req["amount"]),
                f"Пополнение #{tid}")
        except services.BalanceError as e:
            return await c.answer(str(e), show_alert=True)
    uname = f"@{req['username']}" if req["username"] else "—"
    from ..helpers import safe_send
    await safe_send(bot, req["user_id"],
                    f"✅ <b>Пополнение #{tid} зачислено!</b>\n\n"
                    f"Сумма: <b>{money(req['amount'])}</b>\n"
                    f"Баланс: <b>{money(balance)}</b> ≈ {rub_money(balance)}")
    await show(
        c,
        f"✅ Заявка #{tid} закрыта.\n\n"
        f"👤 {esc(uname)} · id <code>{req['user_id']}</code>\n"
        f"Начислено: <b>{money(req['amount'])}</b>\n"
        f"Новый баланс: <b>{money(balance)}</b> ≈ {rub_money(balance)}",
        kb([("🔄 Ещё заявки", "apay")], [("⬅️ Админ-панель", "adm:panel")]))


# ---------------- USDT: отклонение ----------------
@r.callback_query(ADMIN, F.data.startswith("mtdeny:"))
async def manual_topup_deny(c: CallbackQuery, bot: Bot):
    tid = _int_of(c.data)
    if tid is None:
        return await c.answer()
    row = await db.fetchrow(
        "UPDATE manual_topups SET status='cancelled', reviewed_at=$2, reviewed_by=$3 "
        "WHERE id=$1 AND status='pending' RETURNING user_id, amount",
        tid, services.now(), c.from_user.id)
    if not row:
        return await c.answer("Заявка не найдена или уже обработана.", show_alert=True)
    from ..helpers import safe_send
    await safe_send(bot, row["user_id"],
                    f"❌ Заявка на пополнение #{tid} отклонена администрацией.\n"
                    "Если вы переводили средства — напишите в поддержку.")
    await show(c, f"❌ Заявка #{tid} отклонена.",
               kb([("🔄 Ещё заявки", "apay")], [("⬅️ Админ-панель", "adm:panel")]))
