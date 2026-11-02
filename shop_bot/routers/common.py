"""EXAMPLE SPLIT: /start, главное меню, навигация, профиль пользователя.

Главное меню — reply-клавиатура (текстовое меню Telegram):
  🛍 Каталог · 📦 Мои заказы · 💳 Оплата · 👤 Профиль · 📞 Поддержка · ℹ️ О магазине
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

from .. import db, services
from ..config import PAID_STATUSES, SHOP_NAME
from ..helpers import balance_text, esc, fmt_ts, kb, money, rub_money, show

r = Router(name="common")

MENU_CATALOG = "🛍 Каталог"
MENU_ORDERS = "📦 Мои заказы"
MENU_PAY = "💳 Оплата"
MENU_PROFILE = "👤 Профиль"
MENU_SUPPORT = "📞 Поддержка"
MENU_ABOUT = "ℹ️ О магазине"

MENU_TEXT = "Выберите раздел"


def main_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=MENU_CATALOG)],
        [KeyboardButton(text=MENU_ORDERS), KeyboardButton(text=MENU_PAY)],
        [KeyboardButton(text=MENU_PROFILE), KeyboardButton(text=MENU_SUPPORT)],
        [KeyboardButton(text=MENU_ABOUT)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите раздел…")


def paid_sql() -> str:
    return "(" + ", ".join(f"'{s}'" for s in sorted(PAID_STATUSES)) + ")"


# ---------------- /start и навигация ----------------
@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    ts = services.now()
    await db.execute(
        "INSERT INTO users(id, username, name, joined, last_active) VALUES($1,$2,$3,$4,$4) "
        "ON CONFLICT (id) DO UPDATE SET username=$2, name=$3",
        u.id, u.username, u.full_name, ts)
    welcome = await db.settings_get("welcome_text")
    await m.answer(f"🛍 {esc(SHOP_NAME)}\n\n{esc(welcome)}", reply_markup=main_menu_kb())


@r.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(MENU_TEXT, reply_markup=main_menu_kb())


@r.message(Command("menu"))
async def menu_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(MENU_TEXT, reply_markup=main_menu_kb())


@r.callback_query(F.data == "menu")
async def menu_cb(c: CallbackQuery, state: FSMContext):
    """Inline «В меню» внутри новых разделов; данные совпадают со старыми кнопками."""
    await state.clear()
    await show(c, MENU_TEXT, kb([("ℹ️ О магазине", "cb_about")], [("🛍 Каталог", "cat")]))


# ---------------- разделы главного меню (reply-кнопки) ----------------
@r.message(F.text == MENU_CATALOG)
async def menu_catalog(m: Message, state: FSMContext):
    await state.clear()
    from .catalog import send_catalog
    await send_catalog(m, 0)


@r.message(F.text == MENU_ORDERS)
async def menu_orders(m: Message, state: FSMContext):
    await state.clear()
    from .orders import send_orders_list
    await send_orders_list(m)


@r.message(F.text == MENU_PAY)
async def menu_pay(m: Message, state: FSMContext):
    await state.clear()
    from .topup import send_pay_menu
    await send_pay_menu(m)


@r.message(F.text == MENU_PROFILE)
async def menu_profile(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await profile_view(m.from_user)
    await m.answer(text, reply_markup=markup)


@r.message(F.text == MENU_SUPPORT)
async def menu_support(m: Message, state: FSMContext):
    await state.clear()
    from .support import start_contact_reply
    await start_contact_reply(m, state)


@r.message(F.text == MENU_ABOUT)
async def menu_about(m: Message, state: FSMContext):
    await state.clear()
    about = await db.settings_get("about_text")
    await m.answer(
        f"ℹ️ <b>{esc(SHOP_NAME)}</b>\n\n{esc(about)}",
        reply_markup=kb([("📞 Поддержка", "cb_contact")], [("⬅️ В меню", "menu")]))


@r.callback_query(F.data == "cb_about")
async def cb_about(c: CallbackQuery):
    about = await db.settings_get("about_text")
    await show(c, f"ℹ️ <b>{esc(SHOP_NAME)}</b>\n\n{esc(about)}",
               kb([("📞 Поддержка", "cb_contact")], [("⬅️ В меню", "menu")]))


# ---------------- профиль ----------------
async def profile_view(user) -> tuple[str, InlineKeyboardMarkup]:
    """Профиль: Telegram ID, имя, заказы, контакт. Минимум персональных данных."""
    uid = user.id
    row = await db.fetchrow(
        "SELECT balance, balance_currency, contact_phone, joined FROM users WHERE id=$1", uid)
    bal = float(row["balance"]) if row else 0.0
    currency = (row["balance_currency"] if row else "USD") or "USD"
    orders_n = await db.fetchval(
        f"SELECT COUNT(*) FROM shop_orders WHERE user_id=$1 AND status IN {paid_sql()}", uid)
    spend = await db.fetchval(
        f"SELECT COALESCE(SUM(amount),0) FROM shop_orders WHERE user_id=$1 AND status IN {paid_sql()}",
        uid)
    phone = row["contact_phone"] if row else None
    joined = fmt_ts(row["joined"]) if row and row["joined"] else "—"
    spend_rub = float(spend or 0)
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"Telegram ID: <code>{uid}</code>\n"
        f"Имя: {esc(user.full_name)}\n"
        f"Username: {('@' + esc(user.username)) if user.username else '—'}\n"
        f"В боте с: {joined}\n\n"
        f"Заказов: <b>{orders_n}</b>\n"
        f"Сумма покупок: <b>{spend_rub:,.0f}".replace(",", " ") + " ₽</b>\n"
        f"Баланс: <b>{balance_text(bal, currency)}</b> ≈ {rub_money(bal)}\n"
        f"Контакт: {('+' + esc(str(phone))) if phone else '—'}\n\n"
        "<i>Баланс — внутренние средства для оплаты заказов.</i>"
    )
    rows = [
        [("🧮 История операций", "ops")],
        [("💰 Пополнить баланс", "topup")],
        [("📦 Мои заказы", "oh:0")],
        [("⬅️ В меню", "menu")],
    ]
    return text, kb(*rows)


@r.callback_query(F.data == "cb_profile")
async def cb_profile(c: CallbackQuery):
    text, markup = await profile_view(c.from_user)
    await show(c, text, markup)
