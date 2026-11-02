"""Мои заказы EXAMPLE SPLIT (пользователь): список, карточка, отмена, трекинг."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .. import db, services
from ..helpers import esc, int_arg, kb, show

r = Router(name="orders")

ORDERS_PER_PAGE = 5


async def _orders_view(uid: int, page: int = 0):
    total = await db.fetchval("SELECT COUNT(*) FROM shop_orders WHERE user_id=$1", uid)
    if not total:
        return ("📦 <b>Мои заказы</b>\n\nЗаказов пока нет. Откройте каталог и выберите товар.",
                kb([("🛍 Каталог", "cat")], [("⬅️ В меню", "menu")]))
    pages = max(1, (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    rows = await db.fetch(
        "SELECT id, product_name, qty, amount, status, city, tracking_number, created_at "
        "FROM shop_orders WHERE user_id=$1 ORDER BY id DESC LIMIT $2 OFFSET $3",
        uid, ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    lines, btns = [], []
    for x in rows:
        qty_line = f"{x['qty']} × " if x["qty"] > 1 else ""
        track = f" · 🚚 {esc(x['tracking_number'])}" if x["tracking_number"] else ""
        lines.append(
            f"{services.order_number_text(x['id'])}\n"
            f"{qty_line}{esc(x['product_name'])} — {float(x['amount']):,.0f} ₽{track}".replace(",", " ") +
            f"\nСтатус: {services.status_label(x['status'])}")
        btns.append([(services.order_number_text(x["id"]), f"ov:{x['id']}")])
    nav = []
    if page > 0:
        nav.append(("⬅️", f"oh:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"oh:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([("🛍 Каталог", "cat")])
    btns.append([("⬅️ В меню", "menu")])
    text = f"📦 <b>Мои заказы</b> (стр. {page + 1}/{pages})\n\n" + "\n\n".join(lines)
    return text, kb(*btns)


async def send_orders_list(m: Message):
    text, markup = await _orders_view(m.from_user.id, 0)
    await m.answer(text, reply_markup=markup)
    # Навигация из карточки: списком обрабатываем callback «oh:».


@r.callback_query(F.data.startswith("oh:"))
async def orders_page_cb(c: CallbackQuery):
    text, markup = await _orders_view(c.from_user.id, int_arg(c.data, 1) or 0)
    await show(c, text, markup)


@r.callback_query(F.data.startswith("ov:"))
async def order_open_cb(c: CallbackQuery):
    oid = int_arg(c.data, 1)
    if oid is None:
        return await c.answer()
    await send_order_card(c, oid)


async def send_order_card(c: CallbackQuery, oid: int):
    """Карточка заказа пользователя: товар, город, ПВЗ, статус, трек."""
    o = await services.order_full_row(oid)
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Заказ не найден", show_alert=True)
    text = (
        f"🧾 <b>Заказ {services.order_number_text(o['id'])}</b>\n\n" +
        "\n".join(services.order_details_lines(o)))
    rows = []
    row = await db.fetchrow("SELECT balance FROM users WHERE id=$1", c.from_user.id)
    from ..config import USD_RUB_RATE
    need = round(float(o["amount"]) / float(USD_RUB_RATE), 4)
    bal = float(row["balance"]) if row else 0.0
    if o["status"] == "AWAITING_PAYMENT":
        rows.append([
            ("💰 Оплатить с баланса" if bal >= need
             else f"💰 Оплатить с баланса ({bal:.2f}/{need:.2f})",
             f"ckpay:{o['id']}"),
        ])
        rows.append([("✉️ Связаться с продавцом", f"ordpaycontact:{o['id']}")])
        rows.append([("✅ Я оплатил — подтвердите", f"ckclaim:{o['id']}")])
        rows.append([("❌ Отменить заказ", f"orcx:{o['id']}")])
    elif o["status"] == "AWAITING_CONFIRM":
        rows.append([("⏳ Ожидает подтверждения продавца", "verif")])
        rows.append([("❌ Отменить", f"orcx:{o['id']}")])
    elif o["status"] in ("PAID", "CONFIRMED", "PREPARING", "SHIPPED", "DELIVERED"):
        rows.append([("🚚 Отслеживание", "tracking_info")])
        rows.append([("✉️ Написать в поддержку", "cb_contact")])
    else:
        rows.append([("🛍 В каталог", "cat")])
    rows.append([("📦 Мои заказы", "oh:0")])
    rows.append([("⬅️ В меню", "menu")])
    await show(c, text, kb(*rows))


@r.callback_query(F.data == "tracking_info")
async def tracking_info_hint(c: CallbackQuery):
    await c.answer("Номер отправления появится в статусе «Отправлен».", show_alert=True)


@r.callback_query(F.data.startswith("orcx:"))
async def user_cancel_order(c: CallbackQuery, bot):
    oid = int_arg(c.data, 1)
    if oid is None:
        return await c.answer()
    res, refund, order = await services.apply_cancel(oid, uid=c.from_user.id)
    if res == "not_found":
        return await show(c, "❌ Заказ не найден.", kb([("📦 Мои заказы", "oh:0")]))
    if res == "not_cancellable":
        o = await services.get_order(oid)
        return await show(
            c,
            "ℹ️ Этот заказ уже закрыт — отменить не получится.\n" +
            ("\n".join(services.order_details_lines(o)) if o else ""),
            kb([("📦 Мои заказы", "oh:0")], [("⬅️ В меню", "menu")]))
    await show(
        c,
        f"❌ Заказ {services.order_number_text(oid)} отменён.",
        kb([("🛍 Каталог", "cat")], [("📦 Мои заказы", "oh:0")], [("⬅️ В меню", "menu")]))
