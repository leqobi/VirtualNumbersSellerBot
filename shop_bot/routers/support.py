"""Поддержка EXAMPLE SPLIT: обращения в поддержку и ответы админа.

Механизм унаследован из старого проекта: обращение пользователя пересылается всем
админам с записью в msgmap; админ отвечает реплаем (или /reply), текст уходит
пользователю. Здесь же — пересылка «связаться с продавцом» по конкретному заказу.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, services
from ..config import ADMINS
from ..helpers import esc, kb, safe_send, show, split_message

from aiogram.filters import Command

r = Router(name="support")


class Contact(StatesGroup):
    text = State()


def _mention(u) -> str:
    return f"@{u.username}" if u.username else "без username"


# ---------------- пользователь: обращение ----------------
@r.callback_query(F.data == "cb_contact")
async def contact_cb(c: CallbackQuery, state: FSMContext):
    await state.set_state(Contact.text)
    await state.update_data(order_id=None)
    await show(c, "✉️ <b>Поддержка</b>\n\nНапишите сообщение одним сообщением:",
               kb([("⬅️ Отмена", "menu")]))


async def start_contact_reply(m: Message, state: FSMContext):
    """Из меню поддержки: просим написать сообщение (обращение без заказа)."""
    await state.set_state(Contact.text)
    await state.update_data(order_id=None)
    await m.answer(
        "✉️ <b>Поддержка</b>\n\nНапишите ваш вопрос одним сообщением.\n"
        "Ответ придёт в этот чат.",
        reply_markup=kb([("⬅️ Отмена", "cancel")]))


@r.callback_query(F.data.startswith("ordpaycontact:"))
async def contact_for_order(c: CallbackQuery, state: FSMContext):
    """Связаться с продавцом для согласования оплаты заказа."""
    try:
        oid = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        return await c.answer("Заказ не найден", show_alert=True)
    o = await services.get_order(oid)
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Заказ не найден", show_alert=True)
    if o["status"] != "AWAITING_PAYMENT":
        return await c.answer("Оплата уже сошлась — контакт не нужен.", show_alert=True)
    await state.set_state(Contact.text)
    await state.update_data(order_id=oid)
    await show(
        c,
        f"✉️ <b>Согласование оплаты {services.order_number_text(oid)}</b>\n\n"
        "Продавец ответит вам в личном чате и пришлёт реквизиты.\n"
        "Можно сразу написать вопрос, условие или нажать «Отправить» без текста:",
        kb([("✉️ Отправить запрос реквизитов", f"ordpayreq:{oid}")], [("⬅️ Назад", f"ov:{oid}")])
    )


@r.callback_query(F.data.startswith("ordpayreq:"))
async def contact_for_order_send(c: CallbackQuery, state: FSMContext, bot: Bot):
    try:
        oid = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        return await c.answer()
    o = await services.get_order(oid)
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Заказ не найден", show_alert=True)
    await state.clear()
    ok = await forward_contact(bot, c.from_user, m_text=None, order_id=oid)
    if not ok:
        return await c.answer("Не удалось отправить запрос. Попробуйте позже.", show_alert=True)
    await show(
        c,
        f"✉️ Запрос реквизитов по {services.order_number_text(oid)} отправлен продавцу.\n\n"
        "Он ответит вам в личном чате и пришлёт реквизиты.\n"
        "После оплаты нажмите «Я оплатил» в карточке заказа.",
        kb([("🧾 Открыть заказ", f"ov:{oid}")], [("📦 Мои заказы", "oh:0")]))


@r.message(StateFilter(Contact.text), F.text & ~F.text.startswith("/"))
async def contact_send(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    oid = data.get("order_id")
    await state.clear()
    ok = await forward_contact(bot, m.from_user, (m.text or "").strip()[:3000], oid)
    if not ok:
        return await m.answer("❌ Не удалось отправить обращение. Попробуйте позже.")
    if oid is not None:
        await m.answer(
            f"✉️ Обращение по {services.order_number_text(oid)} отправлено продавцу.\n\n"
            "Он ответит вам в личном чате и пришлёт реквизиты.\n"
            "После оплаты нажмите «Я оплатил» в карточке заказа.",
            reply_markup=kb([("📦 Мои заказы", "oh:0")], [("⬅️ В меню", "menu")]))
    else:
        await m.answer("✅ Отправлено! Ответ придёт в этот чат.", reply_markup=kb([("⬅️ В меню", "menu")]))


async def forward_contact(bot: Bot, user, m_text: str | None, order_id: int | None = None,
                          admin_ids=None) -> bool:
    """Пересылает обращение в поддержку всем админам, пишет msgmap. -> доставлено."""
    admin_ids = admin_ids or ADMINS
    head_lines = [
        "✉️ <b>Обращение в поддержку</b>",
        f"Пользователь: {esc(user.full_name)} ({_mention(user)})",
        f"Telegram ID: <code>{user.id}</code>",
    ]
    if order_id is not None:
        o = await services.get_order(order_id)
        if o is not None:
            head_lines += [
                "",
                f"🧾 Заказ: {services.order_number_text(o['id'])} · {esc(o['product_name'])}",
                f"💰 Сумма: {float(o['amount']):,.0f} ₽ (ожидает оплаты)".replace(",", " "),
                f"🏙 Город: {esc(o['city'])} · 📍 ПВЗ: {esc(o['pvz'])}",
            ]
    if m_text:
        head_lines += ["", "✍️ Сообщение:", esc(m_text)]
    head_lines += [
        "",
        "<i>Ответить: reply на это сообщение или /reply "
        f"{user.id} текст</i>",
    ]
    head = "\n".join(head_lines)
    delivered = 0
    for admin_id in admin_ids:
        try:
            for part in split_message(head):
                sent = await bot.send_message(admin_id, part)
                await db.execute(
                    "INSERT INTO msgmap(admin_id, msg_id, user_id, answered) "
                    "VALUES($1,$2,$3,FALSE) ON CONFLICT DO NOTHING",
                    admin_id, sent.message_id, user.id)
            delivered += 1
        except Exception:
            pass
    return delivered > 0


# ---------------- админ: ответы ----------------
@r.message(F.reply_to_message, F.text & ~F.text.startswith("/"))
async def reply_by_reply(m: Message, bot: Bot):
    if m.from_user.id not in ADMINS:
        return
    row = await db.fetchrow(
        "SELECT user_id FROM msgmap WHERE admin_id=$1 AND msg_id=$2",
        m.from_user.id, m.reply_to_message.message_id)
    if not row:
        return
    ok = await send_user_answer(bot, m.from_user.id, row["user_id"], m.text or "")
    await m.answer("✅ Доставлено" if ok else "❌ Не доставлено")


@r.message(Command("reply"))
async def reply_cmd(m: Message, bot: Bot):
    if m.from_user.id not in ADMINS:
        return
    p = (m.text or "").split(maxsplit=2)
    if len(p) < 3 or not p[1].lstrip("-").isdigit():
        return await m.answer("Формат: /reply user_id текст")
    ok = await safe_send(bot, int(p[1]), f"💬 <b>Ответ поддержки:</b>\n\n{esc(p[2])}")
    await m.answer("✅ Доставлено" if ok else "❌ Не доставлено")


async def send_user_answer(bot: Bot, admin_id, user_id, text: str) -> bool:
    """Отправляет ответ пользователю и помечает диалог решённым."""
    ok = await safe_send(bot, user_id, f"💬 <b>Ответ поддержки:</b>\n\n{esc(text)}")
    if ok:
        await db.execute(
            "UPDATE msgmap SET answered=TRUE WHERE admin_id=$1 AND user_id=$2", admin_id, user_id)
    return ok
