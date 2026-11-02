"""Заказы EXAMPLE SPLIT (админ): список с фильтрами, поиск, карточка,
подтверждение оплаты, смена статусов, трекинг, заметка о доставке,
отмена и возврат средств.

Только для ID из ADMIN_IDS (фильтр ADMIN из admin_panel).
Подтверждение оплаты списывает товар со склада в одной транзакции;
если товара нет — отказ без списания и без записей.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import db, services
from ..config import (
    ACTIVE_AFTER_PAY,
    NEXT_STATUSES,
    ORDER_STATUS_EMOJI,
    ORDER_STATUS_LABELS,
    PAID_STATUSES,
    PAY_METHOD_LABELS,
)
from ..helpers import Busy, esc, kb, money, show

from .admin_panel import ADMIN, admin_kb

r = Router(name="admin_orders")

PAGE = 5
_busy: set[int] = set()

AWAITING_STATUSES = ("AWAITING_PAYMENT", "AWAITING_CONFIRM")


class AOrdSearch(StatesGroup):
    query = State()


class AOrdTrack(StatesGroup):
    value = State()


class AOrdNote(StatesGroup):
    value = State()


def _plain_rub(x) -> str:
    return f"{float(x):,.0f}".replace(",", " ") + " ₽"


def _status_chip(status: str, current: str | None) -> tuple[str, str]:
    mark = "● " if status == current else ""
    return (f"{mark}{ORDER_STATUS_EMOJI.get(status, '•')} "
            f"{ORDER_STATUS_LABELS.get(status, status)}", f"aord:fl:{status}:0")


# ---------------- список заказов ----------------
@r.callback_query(ADMIN, F.data == "aord:l:0")
async def orders_list_root(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_orders(c, 0, None)


@r.callback_query(ADMIN, F.data.startswith("aord:l:"))
async def orders_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_orders(c, int_arg(c.data, 2) or 0, None)


@r.callback_query(ADMIN, F.data.startswith("aord:fl:"))
async def orders_filtered(c: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = str(c.data).split(":")
    status = parts[2] if len(parts) > 2 and parts[2] in ORDER_STATUS_LABELS else None
    page = int_arg(c.data, 3) or 0
    await render_orders(c, page, status)


async def render_orders(c: CallbackQuery, page: int, status: str | None):
    if status is None:
        total = await db.fetchval("SELECT COUNT(*) FROM shop_orders")
    else:
        total = await db.fetchval("SELECT COUNT(*) FROM shop_orders WHERE status=$1", status)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    if status is None:
        rows = await db.fetch(
            "SELECT o.*, u.username, u.name AS uname FROM shop_orders o "
            "LEFT JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT $1 OFFSET $2",
            PAGE, page * PAGE)
    else:
        rows = await db.fetch(
            "SELECT o.*, u.username, u.name AS uname FROM shop_orders o "
            "LEFT JOIN users u ON u.id=o.user_id WHERE o.status=$1 "
            "ORDER BY o.id DESC LIMIT $2 OFFSET $3",
            status, PAGE, page * PAGE)

    lines, btns = [], []
    for x in rows:
        uname = f"@{x['username']}" if x["username"] else (x["uname"] or "—")
        lines.append(
            f"{services.order_number_text(x['id'])} · {esc(str(x['product_name'])[:32])} — "
            f"{_plain_rub(x['amount'])}\n"
            f"{ORDER_STATUS_EMOJI.get(x['status'], '•')} "
            f"{ORDER_STATUS_LABELS.get(x['status'], x['status'])} · 👤 {esc(uname)}")
        btns.append([(f"🧾 {services.order_number_text(x['id'])}", f"aord:v:{x['id']}")])

    nav = []
    if page > 0:
        nav.append(("⬅️", _page_cb(page - 1, status)))
    if page < pages - 1:
        nav.append(("➡️", _page_cb(page + 1, status)))
    if nav:
        btns.append(nav)
    if status is None:
        title = "📦 <b>Заказы — все</b>"
    else:
        title = (f"📦 <b>Заказы: {ORDER_STATUS_EMOJI.get(status, '•')} "
                 f"{ORDER_STATUS_LABELS.get(status, status)}</b>")
    text = f"{title} (стр. {page + 1}/{pages} · всего {total})\n\n"
    text += "\n\n".join(lines) if lines else "Заказов нет."
    chips = [_status_chip(s, status) for s in ORDER_STATUS_LABELS]
    btns += [chips[i:i + 3] for i in range(0, len(chips), 3)]
    btns.append([("🔎 Поиск заказа", "aord:srch")])
    btns.append([("💳 Заявки на пополнение", "apay")])
    btns.append([("⬅️ Админ-панель", "adm:panel")])
    await show(c, text, kb(*btns))


def _page_cb(page: int, status: str | None) -> str:
    return f"aord:l:{page}" if status is None else f"aord:fl:{status}:{page}"


# ---------------- поиск ----------------
@r.callback_query(ADMIN, F.data == "aord:srch")
async def search_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AOrdSearch.query)
    await show(
        c,
        "🔎 <b>Поиск заказа</b>\n\nПришлите одним сообщением:\n"
        "• номер заказа, например <code>12</code>;\n"
        "• ID или @username пользователя;\n"
        "• часть названия товара.",
        kb([("⬅️ Отмена", "aord:l:0")]))


@r.message(StateFilter(AOrdSearch.query), ADMIN, F.text)
async def search_do(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    raw = (m.text or "").strip()
    if not raw:
        return await m.answer("Пустой запрос.",
                              reply_markup=kb([("📦 К заказам", "aord:l:0")]))
    if raw.isdigit():
        order = await services.order_full_row(int(raw))
        if order is not None:
            text, markup = admin_card_content(order)
            return await m.answer(text, reply_markup=markup)
        exists = await db.fetchval("SELECT 1 FROM users WHERE id=$1", int(raw))
        if exists:
            from .admin_users import admin_user_card
            text, markup = await admin_user_card(int(raw))
            if text is not None:
                return await m.answer(text, reply_markup=markup)
        return await m.answer("Ничего не найдено.",
                              reply_markup=kb([("🔎 Ещё раз", "aord:srch")],
                                              [("📦 К заказам", "aord:l:0")]))
    if raw.startswith("@"):
        uname = raw[1:].lstrip("@").lower()
        rows = await db.fetch(
            "SELECT id, name, username FROM users WHERE LOWER(username)=$1 LIMIT 5", uname)
        if rows:
            cards = [_user_order_buttons(int(x["id"])) for x in rows]
            btns = [row for card in cards for row in card]
            btns.append([("⬅️ К заказам", "aord:l:0")])
            return await m.answer(
                f"🔎 Пользователи по <code>{esc(raw)}</code>:",
                reply_markup=kb(*btns))
        return await m.answer("Пользователи с таким username не найдены.",
                              reply_markup=kb([("🔎 Ещё раз", "aord:srch")],
                                              [("📦 К заказам", "aord:l:0")]))
    like = f"%{raw}%"
    rows = await db.fetch(
        "SELECT * FROM shop_orders WHERE product_name ILIKE $1 ORDER BY id DESC LIMIT 10", like)
    if not rows:
        return await m.answer("Ничего не найдено.",
                              reply_markup=kb([("🔎 Ещё раз", "aord:srch")],
                                              [("📦 К заказам", "aord:l:0")]))
    lines, btns = [], []
    for x in rows:
        lines.append(
            f"{services.order_number_text(x['id'])} · {esc(x['product_name'])} — "
            f"{_plain_rub(x['amount'])}\n{ORDER_STATUS_EMOJI.get(x['status'], '•')} "
            f"{ORDER_STATUS_LABELS.get(x['status'], x['status'])}")
        btns.append([(f"🧾 {services.order_number_text(x['id'])}", f"aord:v:{x['id']}")])
    btns.append([("⬅️ К заказам", "aord:l:0")])
    await m.answer("🔍 <b>Найденные заказы</b>\n\n" + "\n\n".join(lines), reply_markup=kb(*btns))


def _user_order_buttons(uid: int) -> list:
    return [(f"🛒 Заказы пользователя {uid}", f"aord:usr:{uid}"),
            (f"👤 Карточка пользователя", f"adm:u:{uid}")]


@r.callback_query(ADMIN, F.data.startswith("aord:usr:"))
async def user_orders_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = int_arg(c.data, 2)
    if uid is None:
        return await c.answer("Пользователь не найден", show_alert=True)
    await render_user_orders(c, uid)


async def render_user_orders(c: CallbackQuery, uid: int, page: int = 0):
    per = 10
    total = await db.fetchval("SELECT COUNT(*) FROM shop_orders WHERE user_id=$1", uid)
    if not total:
        return await show(c, "У пользователя пока нет заказов.",
                          kb([("⬅️ К поискам", "aord:l:0")]))
    pages = max(1, (total + per - 1) // per)
    page = min(max(page, 0), pages - 1)
    rows = await db.fetch(
        "SELECT * FROM shop_orders WHERE user_id=$1 ORDER BY id DESC LIMIT $2 OFFSET $3",
        uid, per, page * per)
    lines, btns = [], []
    for x in rows:
        lines.append(
            f"{services.order_number_text(x['id'])} · {esc(str(x['product_name'])[:32])} — "
            f"{_plain_rub(x['amount'])}\n{ORDER_STATUS_EMOJI.get(x['status'], '•')} "
            f"{ORDER_STATUS_LABELS.get(x['status'], x['status'])}")
        btns.append([(f"🧾 {services.order_number_text(x['id'])}", f"aord:v:{x['id']}")])
    nav = []
    if page > 0:
        nav.append(("⬅️", f"aord:usrp:{uid}:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"aord:usrp:{uid}:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([("👤 Карточка пользователя", f"adm:u:{uid}")])
    btns.append([("⬅️ К заказам", "aord:l:0")])
    await show(c, f"📦 <b>Заказы пользователя {uid}</b>\n\n" + "\n\n".join(lines), kb(*btns))


@r.callback_query(ADMIN, F.data.startswith("aord:usrp:"))
async def user_orders_page(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = int_arg(c.data, 2)
    page = int_arg(c.data, 3) or 0
    if uid is None:
        return await c.answer("Пользователь не найден", show_alert=True)
    await render_user_orders(c, uid, page)


# ---------------- карточка заказа ----------------
@r.callback_query(ADMIN, F.data.startswith("aord:v:"))
async def order_card_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    oid = int_arg(c.data, 1)
    o = await services.order_full_row(oid) if oid is not None else None
    if not o:
        return await c.answer("Заказ не найден", show_alert=True)
    text, markup = admin_card_content(o)
    await show(c, text, markup)


def admin_card_content(o) -> tuple[str, InlineKeyboardMarkup]:
    oid = o["id"]
    status = o["status"]
    uname = f"@{o['username']}" if o["username"] else "—"
    if o["uname"]:
        uname = f"{o['uname']} ({uname})" if o["username"] else o["uname"]
    lines = [
        f"🧾 <b>Заказ {services.order_number_text(oid)}</b>",
        f"👤 Покупатель: {esc(uname)}",
        f"🆔 ID: <code>{o['user_id']}</code>",
        "",
    ]
    lines += services.order_details_lines(o)
    text = "\n".join(lines)
    btns = []
    if status in AWAITING_STATUSES:
        btns.append([("✅ Подтвердить оплату", f"aord:cfy:{oid}")])
        if status == "AWAITING_CONFIRM":
            btns.append([("💳 Вернуть в ожидание оплаты", f"aord:sts:{oid}:AWAITING_PAYMENT")])
        btns.append([("❌ Отменить заказ", f"aord:rfy:{oid}")])
    else:
        allowed = [s for s in NEXT_STATUSES.get(status, ())
                   if s not in ("CANCELLED", "REFUNDED") and s != "PAID"]
        i = 0
        while i < len(allowed):
            batch = allowed[i:i + 2]
            btns.append([(
                f"{ORDER_STATUS_EMOJI.get(s, '•')} {ORDER_STATUS_LABELS.get(s, s)}",
                f"aord:sts:{oid}:{s}") for s in batch])
            i += 2
        if any(s in ("CANCELLED", "REFUNDED") for s in NEXT_STATUSES.get(status, ())):
            btns.append([("❌ Отмена или возврат средств", f"aord:rfy:{oid}")])
    if status in PAID_STATUSES:
        btns.append([("🚚 Трек-номер", f"aord:tr:{oid}"),
                     ("ℹ️ Заметка о доставке", f"aord:dn:{oid}")])
    btns.append([("👤 Карточка пользователя", f"adm:u:{o['user_id']}")])
    btns.append([("📦 К заказам", "aord:l:0"), ("⚙️ Админ-панель", "adm:panel")])
    return text, kb(*btns)


# ---------------- подтверждение оплаты ----------------
@r.callback_query(ADMIN, F.data.startswith("aord:cfy:"))
async def confirm_payment(c: CallbackQuery, bot: Bot):
    oid = int_arg(c.data, 1)
    if oid is None:
        return await c.answer()
    async with Busy(c.from_user.id, _busy) as busy:
        if not busy.ok:
            return await c.answer("⏳ Уже обрабатывается…")
        res, o, left = await _confirm_payment_row(oid)
    if res == "not_found":
        return await c.answer("Заказ не найден", show_alert=True)
    if res == "closed":
        return await c.answer("Заказ уже обработан (не в ожидании оплаты).", show_alert=True)
    if res == "no_stock":
        return await c.answer(
            "Товар отсутствует на складе — оплата не подтверждена, ничего не изменено. "
            "Пополните склад.", show_alert=True)
    await services.notify_status_change(
        bot, oid, "PAID", extra="Оплата подтверждена продавцом ✅")
    from ..config import ADMINS, LOW_STOCK_THRESHOLD
    if left is not None and left < LOW_STOCK_THRESHOLD:
        await services.notify_admins(
            bot, ADMINS,
            f"⚠️ Товар заканчивается: {esc(o['product_name'])} — осталось {left} шт.")
    await show(c, f"✅ Оплата подтверждена: {services.order_number_text(oid)}"
                  f"\nТовар списан со склада: "
                  f"{'осталось ' + str(left) + ' шт.' if left is not None else '—'}\n"
                  f"Статус заказа: ✅ Оплачен.",
              kb([("🧾 Открыть заказ", f"aord:v:{oid}")], [("📦 К заказам", "aord:l:0")]))


async def _confirm_payment_row(oid: int):
    """AWAITING_* → PAID (pay_method='manual') + склад в одной транзакции.

    -> (result, order, left_stock). При нехватке товара ничего не меняется.
    """
    async with db.db().acquire() as con:
        async with con.transaction():
            o = await con.fetchrow("SELECT * FROM shop_orders WHERE id=$1 FOR UPDATE", oid)
            if o is None:
                return "not_found", None, None
            if o["status"] not in AWAITING_STATUSES:
                return "closed", o, None
            product = None
            if o["product_id"] is not None:
                product = await con.fetchrow(
                    "SELECT id, stock FROM products WHERE id=$1 FOR UPDATE", o["product_id"])
            if product is not None and product["stock"] < o["qty"]:
                return "no_stock", o, None
            left = None
            if product is not None:
                left = await con.fetchval(
                    "UPDATE products SET stock=stock-$2 WHERE id=$1 RETURNING stock",
                    product["id"], o["qty"])
            order = await con.fetchrow(
                "UPDATE shop_orders SET status='PAID', pay_method='manual', paid_at=$2 "
                "WHERE id=$1 RETURNING *", oid, services.now())
    return "ok", order, left


# ---------------- смена статуса ----------------
@r.callback_query(ADMIN, F.data.startswith("aord:sts:"))
async def switch_status_do(c: CallbackQuery, bot: Bot):
    oid = int_arg(c.data, 1)
    parts = str(c.data).split(":")
    new_status = parts[3] if len(parts) > 3 else ""
    o = await services.get_order(oid) if oid is not None else None
    if not o:
        return await c.answer("Заказ не найден", show_alert=True)
    if o["status"] in AWAITING_STATUSES and new_status == "PAID":
        return await c.answer("Используйте кнопку «Подтвердить оплату» — "
                              "нужно списание со склада.", show_alert=True)
    if new_status not in NEXT_STATUSES.get(o["status"], ()):
        return await c.answer("Такой переход статуса недоступен.", show_alert=True)
    ok = await services.switch_status(oid, new_status)
    if not ok:
        return await c.answer("Не удалось изменить статус.", show_alert=True)
    await services.notify_status_change(bot, oid, new_status)
    o = await services.order_full_row(oid)
    text, markup = admin_card_content(o)
    await show(c, f"↪️ Статус обновлён: {services.status_text(new_status)}\n\n" + text, markup)


# ---------------- трек-номер и заметка о доставке ----------------
@r.callback_query(ADMIN, F.data.startswith("aord:tr:"))
async def tracking_start(c: CallbackQuery, state: FSMContext):
    oid = int_arg(c.data, 1)
    o = await services.get_order(oid) if oid is not None else None
    if not o:
        return await c.answer("Заказ не найден", show_alert=True)
    await state.set_state(AOrdTrack.value)
    await state.update_data(oid=oid)
    current = f"Текущий номер: <code>{esc(o['tracking_number'])}</code>\n\n" \
        if o["tracking_number"] else ""
    await show(c, f"🚚 Номер отправления для {services.order_number_text(oid)}\n\n"
                  f"{current}Пришлите номер одним сообщением (до 64 символов). Пользователь "
                  f"увидит его в карточке заказа.",
               kb([("⬅️ Отмена", f"aord:v:{oid}")]))


@r.message(StateFilter(AOrdTrack.value), ADMIN, F.text)
async def tracking_save(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    oid = data.get("oid")
    value = " ".join((m.text or "").split())[:64]
    if not oid or not value:
        return await m.answer("Форма потерялась — откройте заказ заново через /admin.")
    row = await db.fetchrow("SELECT status, product_name FROM shop_orders WHERE id=$1", oid)
    if not row:
        return await m.answer("Заказ не найден.")
    await db.execute("UPDATE shop_orders SET tracking_number=$2 WHERE id=$1", oid, value)
    note = ""
    if row["status"] in PAID_STATUSES:
        ok = await services.notify_status_change(
            bot, oid, row["status"],
            extra=f"🚚 Номер отправления: <code>{esc(value)}</code>")
        note = "\nПользователь уведомлён." if ok else "\nПользователю не доставлено."
    await m.answer(
        f"✅ Номер отправления сохранён: <code>{esc(value)}</code>{note}",
        reply_markup=kb([("🧾 Открыть заказ", f"aord:v:{oid}")],
                        [("📦 К заказам", "aord:l:0")]))


@r.callback_query(ADMIN, F.data.startswith("aord:dn:"))
async def delivery_note_start(c: CallbackQuery, state: FSMContext):
    oid = int_arg(c.data, 1)
    o = await services.get_order(oid) if oid is not None else None
    if not o:
        return await c.answer("Заказ не найден", show_alert=True)
    await state.set_state(AOrdNote.value)
    await state.update_data(oid=oid)
    current = f"Текущая заметка: {esc(o['delivery_note'])}\n\n" if o["delivery_note"] else ""
    await show(c, f"ℹ️ Заметка о доставке для {services.order_number_text(oid)}\n\n"
                  f"{current}Пришлите текст одним сообщением (до 200 символов): "
                  "например, сроки или нюансы ПВЗ. «-» — удалить.",
               kb([("⬅️ Отмена", f"aord:v:{oid}")]))


@r.message(StateFilter(AOrdNote.value), ADMIN, F.text)
async def delivery_note_save(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    oid = data.get("oid")
    raw = (m.text or "").strip()
    if not oid:
        return await m.answer("Форма потерялась — откройте заказ заново через /admin.")
    value = "" if raw == "-" else " ".join(raw.split())[:200]
    await db.execute("UPDATE shop_orders SET delivery_note=$2 WHERE id=$1", oid, value or None)
    if value:
        row = await db.fetchrow("SELECT status FROM shop_orders WHERE id=$1", oid)
        if row and row["status"] in PAID_STATUSES:
            await services.notify_status_change(
                bot, oid, row["status"], extra=f"ℹ️ О доставке: {esc(value)}")
    await m.answer("✅ Заметка о доставке обновлена.",
                   reply_markup=kb([("🧾 Открыть заказ", f"aord:v:{oid}")],
                                   [("📦 К заказам", "aord:l:0")]))


# ---------------- отмена и возврат ----------------
@r.callback_query(ADMIN, F.data.startswith("aord:rfy:"))
async def cancel_or_refund_screen(c: CallbackQuery):
    oid = int_arg(c.data, 1)
    o = await services.get_order(oid) if oid is not None else None
    if not o:
        return await c.answer("Заказ не найден", show_alert=True)
    status = o["status"]
    charged = float(o["charged_amount"] or 0)
    if status in AWAITING_STATUSES:
        text = (
            f"❌ <b>Отменить заказ {services.order_number_text(oid)}?</b>\n"
            f"{esc(o['product_name'])} × {o['qty']} — {_plain_rub(o['amount'])}\n\n"
            "Заказ ещё не оплачен: деньги возвращать не нужно, склад не задействован.")
        rows = [
            [("✅ Да, отменить заказ", f"aord:cancel:{oid}")],
            [("⬅️ Назад", f"aord:v:{oid}")],
        ]
        return await show(c, text, kb(*rows))
    refund_note = ""
    if o["pay_method"] == "balance" and charged > 0 and status in ACTIVE_AFTER_PAY:
        refund_note = f"\n\n↩️ На баланс покупателя вернётся <b>{money(charged)}</b>, " \
                      "товар вернётся на склад."
    elif status in ACTIVE_AFTER_PAY:
        refund_note = "\n\nОплата была не с баланса — вернуть деньги вручную напрямую."
    elif status in ("SHIPPED", "DELIVERED", "COMPLETED"):
        refund_note = ("\n\nЗаказ уже в пути/выдан: списанного с баланса нет — "
                       "товар и средства не возвращаются автоматически.")
    text = (
        f"↩️ <b>Возврат по заказу {services.order_number_text(oid)}</b>\n"
        f"{esc(o['product_name'])} × {o['qty']} — {_plain_rub(o['amount'])}\n"
        f"Статус: {services.status_text(status)}"
        f"{refund_note}")
    rows = [
        [("↩️ Да, оформить возврат", f"aord:refund:{oid}")],
        [("⬅️ Назад", f"aord:v:{oid}")],
    ]
    await show(c, text, kb(*rows))


@r.callback_query(ADMIN, F.data.startswith("aord:cancel:"))
async def cancel_do(c: CallbackQuery, bot: Bot):
    oid = int_arg(c.data, 1)
    async with Busy(c.from_user.id, _busy) as busy:
        if not busy.ok:
            return await c.answer("⏳ Уже обрабатывается…")
        result, _refund, _order = await services.apply_cancel(oid, admin_id=c.from_user.id)
    if result == "not_found":
        return await c.answer("Заказ не найден", show_alert=True)
    if result == "not_cancellable":
        return await c.answer("Заказ уже закрыт (завершён/отменён/возврат).", show_alert=True)
    await services.notify_status_change(bot, oid, "CANCELLED", extra="Заказ отменён продавцом.")
    await show(c, f"❌ Заказ {services.order_number_text(oid)} отменён.\n"
                  f"Пользователь уведомлён.",
               kb([("🧾 Открыть заказ", f"aord:v:{oid}")], [("📦 К заказам", "aord:l:0")]))


@r.callback_query(ADMIN, F.data.startswith("aord:refund:"))
async def refund_do(c: CallbackQuery, bot: Bot):
    oid = int_arg(c.data, 1)
    async with Busy(c.from_user.id, _busy) as busy:
        if not busy.ok:
            return await c.answer("⏳ Уже обрабатывается…")
        updated, refund, _o = await services.apply_refund(oid)
    if not updated:
        return await c.answer("Возврат не выполнен: заказ уже закрыт.", show_alert=True)
    extra = f"↩️ Возвращаемая сумма на баланс: {money(refund)}\n" if refund else ""
    await services.notify_status_change(
        bot, oid, "REFUNDED", extra=f"{extra}Заказ закрыт возвратом.")
    await show(c, f"↩️ Возврат по заказу {services.order_number_text(oid)} оформлен."
                  f"{' На баланс возвращено ' + money(refund) + '.' if refund else ''}\n"
                  f"Пользователь уведомлён.",
               kb([("🧾 Открыть заказ", f"aord:v:{oid}")], [("📦 К заказам", "aord:l:0")]))
