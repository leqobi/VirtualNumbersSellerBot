"""Пользователи EXAMPLE SPLIT (админ): обзор, поиск, карточка, бан,
начисление/списание баланса, заказы пользователя, команды /user и /addbalance.

Начисление/списание — только через services.adjust_balance (одна транзакция,
ledger-запись, поддержка закрытия заявок «Пополнение #N»).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, services
from ..config import ADMINS, MAX_ADJUST_USD
from ..helpers import esc, fmt_ts, kb, money, parse_amount, rub_money, show

from .admin_panel import ADMIN, admin_kb

r = Router(name="admin_users")

LIST_PAGE = 12


class UserSearch(StatesGroup):
    query = State()


class UserAdjust(StatesGroup):
    value = State()


def _int_of(data, idx: int = 1):
    try:
        return int(str(data).split(":")[idx])
    except (ValueError, IndexError):
        return None


# ---------------- обзор ----------------
@r.callback_query(ADMIN, F.data == "adm:users")
async def users_overview(c: CallbackQuery, state: FSMContext):
    await state.clear()
    total = await db.fetchval("SELECT COUNT(*) FROM users")
    banned = await db.fetchval("SELECT COUNT(*) FROM users WHERE banned")
    active = await db.fetchval(
        "SELECT COUNT(*) FROM users WHERE COALESCE(last_active, joined, 0) >= $1",
        services.now() - 30 * 86400)
    rows = await db.fetch(
        "SELECT u.id, u.name, u.username, u.balance FROM users u "
        "ORDER BY COALESCE(u.last_active, u.joined, 0) DESC LIMIT $1", LIST_PAGE)
    lines = [
        "👥 <b>Пользователи</b>",
        f"Всего: <b>{total}</b> · активных за 30 дн.: <b>{active}</b> · "
        f"заблокировано: <b>{banned}</b>",
        "",
        "Последние по активности:",
    ]
    btns, line = [], []
    for x in rows:
        label = x["name"] or "—"
        if x["username"]:
            label += f" @{x['username']}"
        line.append((f"{esc(label)[:24]}", f"adm:u:{x['id']}"))
        if len(line) == 2:
            btns.append(line)
            line = []
    if line:
        btns.append(line)
    if not rows:
        lines.append("Пока никого нет.")
    btns.append([("🔎 Найти пользователя", "adm:usearch")])
    btns.append([("⬅️ Админ-панель", "adm:panel")])
    await show(c, "\n".join(lines), kb(*btns))


# ---------------- поиск ----------------
@r.callback_query(ADMIN, F.data == "adm:usearch")
async def search_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserSearch.query)
    await show(
        c,
        "🔎 <b>Поиск пользователя</b>\n\nПришлите одним сообщением:\n"
        "• Telegram ID, например <code>123456789</code>;\n"
        "• @username;\n• часть имени.",
        kb([("⬅️ Отмена", "adm:users")]))


async def _find_users(raw: str) -> list:
    like = f"%{raw}%"
    return await db.fetch(
        "SELECT * FROM users WHERE LOWER(username)=$1 OR name ILIKE $2 "
        "ORDER BY id LIMIT 6", raw.lower(), like)


@r.message(StateFilter(UserSearch.query), ADMIN, F.text)
async def search_do(m: Message, state: FSMContext):
    await state.clear()
    raw = (m.text or "").strip()
    if not raw:
        return await m.answer("Пустой запрос.", reply_markup=kb([("⬅️ Пользователи", "adm:users")]))
    if raw.isdigit():
        text, markup = await admin_user_card(int(raw))
        if text is None:
            return await m.answer("Пользователь не найден.",
                                  reply_markup=kb([("🔎 Ещё раз", "adm:usearch")],
                                                  [("⬅️ Пользователи", "adm:users")]))
        return await m.answer(text, reply_markup=markup)
    if raw.startswith("@"):
        raw = raw[1:].lstrip("@")
    rows = await _find_users(raw)
    if not rows:
        return await m.answer("Ничего не найдено.",
                              reply_markup=kb([("🔎 Ещё раз", "adm:usearch")],
                                              [("⬅️ Пользователи", "adm:users")]))
    lines, btns = [], []
    for x in rows:
        lines.append(
            f"👤 {esc(x['name'] or '—')} ({('@' + x['username']) if x['username'] else 'без username'})"
            f"\n🆔 <code>{x['id']}</code> · Баланс: {money(x['balance'])}")
        btns.append([(f"👤 {x['id']}", f"adm:u:{x['id']}")])
    btns.append([("⬅️ Пользователи", "adm:users")])
    await m.answer("🔍 <b>Найденные пользователи</b>\n\n" + "\n\n".join(lines),
                   reply_markup=kb(*btns))


# ---------------- карточка ----------------
async def admin_user_card(uid: int):
    """Карточка пользователя для админа. -> (text, markup) или (None, None)."""
    u = await db.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    if not u:
        return None, None
    paid_cnt = await db.fetchval(
        f"SELECT COUNT(*) FROM shop_orders WHERE user_id=$1 AND status IN {services.paid_in_sql()}",
        uid)
    paid_sum = await db.fetchval(
        f"SELECT COALESCE(SUM(amount),0) FROM shop_orders "
        f"WHERE user_id=$1 AND status IN {services.paid_in_sql()}", uid)
    orders_cnt = await db.fetchval("SELECT COUNT(*) FROM shop_orders WHERE user_id=$1", uid)
    open_cnt = await db.fetchval(
        "SELECT COUNT(*) FROM shop_orders WHERE user_id=$1 "
        "AND status IN ('AWAITING_PAYMENT','AWAITING_CONFIRM')", uid)
    pending_topups = await db.fetchval(
        "SELECT COUNT(*) FROM manual_topups WHERE user_id=$1 AND status='pending'", uid)
    pending_rub = await db.fetchval(
        "SELECT COUNT(*) FROM rub_topups WHERE user_id=$1 AND status IN ('pending','details_sent')",
        uid)
    paid_topups = await db.fetchval(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=$1 AND {services.TOPUP_TX}",
        uid)
    balance = float(u["balance"] or 0)
    text = (
        f"👤 <b>Пользователь {uid}</b>\n\n"
        f"Имя: {esc(u['name'] or '—')}\n"
        f"Username: {('@' + esc(u['username'])) if u['username'] else '—'}\n"
        f"Контакт: {('+' + esc(str(u['contact_phone']))) if u['contact_phone'] else '—'}\n"
        f"Регистрация: {fmt_ts(u['joined'])}\n"
        f"Активность: {fmt_ts(u['last_active'])}\n"
        f"Статус: {'🚫 заблокирован' if u['banned'] else '✅ активен'}\n\n"
        f"💰 Баланс: <b>{money(balance)}</b> ≈ {rub_money(balance)}\n"
        f"Пополнений всего: <b>{money(paid_topups or 0)}</b>\n\n"
        f"📦 Заказов: <b>{orders_cnt}</b> (оплаченных {paid_cnt} на "
        f"{f'{float(paid_sum or 0):,.0f}'.replace(',', ' ')} ₽)\n"
        f"Открытых заказов: <b>{open_cnt}</b>\n"
        f"Ожидают подтверждения пополнения: USDT <b>{pending_topups}</b>, "
        f"₽ <b>{pending_rub}</b>")
    btns = [
        [("💳 ± Баланс", f"adm:ua:{uid}")],
        [("🚫 Заблокировать" if not u["banned"] else "✅ Разблокировать", f"adm:ub:{uid}")],
        [("📦 Заказы пользователя", f"adm:uo:{uid}")],
        [("✉️ Написать в поддержку", f"asup:write:{uid}")],
        [("⬅️ Пользователи", "adm:users")],
        [("⬅️ Админ-панель", "adm:panel")],
    ]
    return text, kb(*btns)


@r.callback_query(ADMIN, F.data.startswith("adm:u:"))
async def user_card_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = _int_of(c.data)
    text, markup = await admin_user_card(uid) if uid is not None else (None, None)
    if text is None:
        return await c.answer("Пользователь не найден", show_alert=True)
    await show(c, text, markup)


# ---------------- бан ----------------
@r.callback_query(ADMIN, F.data.startswith("adm:ub:"))
async def ban_toggle(c: CallbackQuery):
    uid = _int_of(c.data)
    if uid is None:
        return await c.answer()
    if uid in ADMINS:
        return await c.answer("Администратора нельзя заблокировать.", show_alert=True)
    row = await db.fetchrow(
        "UPDATE users SET banned=NOT banned WHERE id=$1 RETURNING banned", uid)
    if not row:
        return await c.answer("Пользователь ещё не открывал бота.", show_alert=True)
    text, markup = await admin_user_card(uid)
    if text is not None:
        await show(c, ("🚫 Пользователь заблокирован.\n"
                       if row["banned"] else "✅ Пользователь разблокирован.\n") + text, markup)


# ---------------- начисление/списание ----------------
@r.callback_query(ADMIN, F.data.startswith("adm:ua:"))
async def adjust_start(c: CallbackQuery, state: FSMContext):
    uid = _int_of(c.data)
    if uid is None:
        return await c.answer()
    u = await db.fetchrow("SELECT balance, banned FROM users WHERE id=$1", uid)
    if not u:
        return await c.answer("Пользователь ещё не открывал бота.", show_alert=True)
    await state.set_state(UserAdjust.value)
    await state.update_data(uid=uid)
    await show(
        c,
        f"💳 <b>Начисление/списание баланса</b>\n\n"
        f"Пользователь: <code>{uid}</code> · баланс: <b>{money(u['balance'])}</b>\n\n"
        f"Пришлите сообщение:\n<code>+5</code> — начислить $5\n"
        f"<code>-10</code> — списать $10\n"
        f"<code>+25 Пополнение #7</code> — с пометкой для закрытия заявки\n\n"
        f"Максимум: {MAX_ADJUST_USD:,.0f} $ за операцию.".replace(",", " "),
        kb([("⬅️ Отмена", f"adm:u:{uid}")]))


@r.message(StateFilter(UserAdjust.value), ADMIN, F.text)
async def adjust_save(m: Message, state: FSMContext, bot=None):
    data = await state.get_data()
    await state.clear()
    uid = data.get("uid")
    raw = (m.text or "").strip()
    head, _, tail = raw.partition(" ")
    amount = parse_amount(head, 0.01, MAX_ADJUST_USD, allow_sign=True)
    if not isinstance(uid, int) or amount is None:
        return await m.answer(
            "Формат: +5 / -10 / «+25 Пополнение #7». Максимум "
            f"{MAX_ADJUST_USD:,.0f}.".replace(",", " "),
            reply_markup=kb([("⬅️ Отмена", f"adm:u:{uid}" if isinstance(uid, int) else "adm:users")]))
    note = tail.strip()[:200] or (
        "Начисление админом" if amount > 0 else "Списание админом")
    try:
        balance, closed = await services.adjust_balance(m.from_user.id, uid, amount, note)
    except services.BalanceError as e:
        msg = {
            "not_found": "❌ Пользователь не найден.",
            "insufficient": "❌ Этого недостаточно на балансе для списания.",
        }.get(str(e), f"❌ {e}")
        return await m.answer(msg, reply_markup=kb([("👤 Карточка", f"adm:u:{uid}")]))
    extra_note = f"\nЗаявка #{closed} закрыта." if closed else ""
    reply = kb([("👤 Карточка пользователя", f"adm:u:{uid}")],
               [("👥 Пользователи", "adm:users")])
    await m.answer(
        f"✅ Операция выполнена.\n"
        f"Пользователь: <code>{uid}</code>\n"
        f"Сумма: <b>{money(amount)}</b>\n"
        f"Новый баланс: <b>{money(balance)}</b> ≈ {rub_money(balance)}\n"
        f"Комментарий: {esc(note)}{extra_note}", reply_markup=reply)
    from ..helpers import safe_send
    sign = "+" if amount > 0 else "−"
    await safe_send(bot, uid,
                    f"💳 Администратор {sign}{abs(amount):.2f} USD на ваш баланс.\n"
                    f"Баланс: <b>{money(balance)}</b>")


# ---------------- заказы пользователя ----------------
@r.callback_query(ADMIN, F.data.startswith("adm:uo:"))
async def user_orders_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = _int_of(c.data)
    if uid is None:
        return await c.answer("Пользователь не найден", show_alert=True)
    from .admin_orders import render_user_orders
    await render_user_orders(c, uid)


# ---------------- команды ----------------
@r.message(Command("user"), ADMIN)
async def user_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        return await m.answer("Формат: /user user_id")
    text, markup = await admin_user_card(int(parts[1].strip()))
    if text is None:
        return await m.answer("Пользователь не найден.")
    await m.answer(text, reply_markup=markup)


@r.message(Command("addbalance"), ADMIN)
async def addbalance_cmd(m: Message, bot=None):
    parts = (m.text or "").split(maxsplit=3)
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
        return await m.answer(
            "Формат: /addbalance user_id +СУММА [комментарий]\n"
            "Например: /addbalance 123456 25 Пополнение #7")
    uid = int(parts[1])
    amount = parse_amount(parts[2], 0.01, MAX_ADJUST_USD, allow_sign=True)
    if amount is None:
        return await m.answer(
            f"Сумма — число по модулю до {MAX_ADJUST_USD:,.0f} $.".replace(",", " "))
    note = (parts[3].strip()[:200] if len(parts) > 3 else
            "Начисление админом" if amount > 0 else "Списание админом")
    try:
        balance, closed = await services.adjust_balance(m.from_user.id, uid, amount, note)
    except services.BalanceError as e:
        msg = {
            "not_found": "Пользователь не найден.",
            "insufficient": "На балансе недостаточно средств для списания.",
        }.get(str(e), str(e))
        return await m.answer(f"❌ {msg}")
    extra_note = f"\nЗаявка #{closed} закрыта." if closed else ""
    await m.answer(
        f"✅ Готово: {uid} → {money(amount)}\n"
        f"Новый баланс: <b>{money(balance)}</b> ≈ {rub_money(balance)}{extra_note}")
    from ..helpers import safe_send
    await safe_send(bot, uid,
                    f"💳 Баланс обновлён: {money(amount)} ({esc(note)}).\n"
                    f"Баланс: <b>{money(balance)}</b>")
