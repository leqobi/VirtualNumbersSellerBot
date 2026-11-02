"""Оплата EXAMPLE SPLIT: 💳 Оплата меню и пополнение баланса.

Пополнение баланса (адаптировано из проверенного старого проекта):
  • USDT TRC20 — заявка на ручное пополнение по адресу кошелька из переменных окружения;
  • Рубли — заявка владельцу: реквизиты вручную, зачисление после подтверждения.
Записи в transactions ведутся с внешним id — повторные зачисления невозможны.
"""

from __future__ import annotations

import math

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, services
from ..config import ADMINS, MANUAL_WALLET_ADDRESS, MAX_TOPUP_USD
from ..helpers import (
    USD_RUB_RATE,
    answer_long,
    esc,
    fmt_ts,
    kb,
    money,
    parse_amount,
    rub_money,
    show,
)

r = Router(name="topup")

TOPUP_STATUS_LABELS = {
    "pending": "⏳ ожидает проверки",
    "details_sent": "📨 реквизиты отправлены",
    "completed": "✅ зачислено",
    "cancelled": "❌ отменено",
}

PRESETS_USD = (1, 5, 10, 25, 50, 100)
PRESETS_RUB = (500, 1000, 2500, 5000, 10000, 25000)


class RubTopup(StatesGroup):
    amount = State()


class RubTopupAdmin(StatesGroup):
    details = State()


class TopupCustom(StatesGroup):
    amount = State()


def _fmt_presets_kb(currency: str):
    presets = PRESETS_USD if currency == "usd" else PRESETS_RUB
    btns, row = [], []
    for v in presets:
        label = f"${v}" if currency == "usd" else f"{v} ₽"
        row.append((label, f"topup:{currency}:{v}"))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([("✍️ Своя сумма", f"topc:{currency}")])
    btns.append([("⬅️ Назад", "topup")])
    return kb(*btns)


def _display_amount(currency: str, entered: float) -> str:
    if currency == "rub":
        return f"{entered:,.0f} ₽ ≈ {money(round(entered / USD_RUB_RATE, 2))}".replace(",", " ")
    return f"{money(entered)} ≈ {rub_money(entered)}"


# ---------------- 💳 Оплата (главное меню) ----------------
async def send_pay_menu(m: Message, state: FSMContext | None = None):
    row = await db.fetchrow(
        "SELECT balance, balance_currency FROM users WHERE id=$1", m.from_user.id)
    bal = float(row["balance"]) if row else 0.0
    currency = (row["balance_currency"] if row else "USD") or "USD"
    pending = await db.fetchval(
        "SELECT COUNT(*) FROM shop_orders WHERE user_id=$1 "
        "AND status IN ('AWAITING_PAYMENT','AWAITING_CONFIRM')", m.from_user.id)
    lines = [
        "💳 <b>Оплата</b>",
        f"Баланс: <b>{money(bal)}</b> ≈ {rub_money(bal)}",
        f"Заказов, ожидающих оплаты: <b>{pending}</b>",
        "",
        "Пополнение баланса — через заявки:",
        "• 💵 USDT TRC20: админы подтвердят перевод и зачислят баланс",
        "• 🇷🇺 Рубли: заявка владельцу — реквизиты и подтверждение",
        "",
        "Оплата заказов: с баланса или напрямую продавцу.",
    ]
    rows = []
    if pending:
        rows.append([(f"🧭 Заказы к оплате ({pending})", "oh:0")])
    rows += [
        [("💰 Пополнить", "topup")],
        [("🧮 История операций", "ops")],
        [("⬅️ В меню", "menu")],
    ]
    await m.answer("\n".join(lines), reply_markup=kb(*rows))


@r.callback_query(F.data == "topup")
async def topup_entry(c: CallbackQuery):
    await show(
        c,
        "💰 <b>Пополнение баланса</b>\n\n"
        "Выберите вариант пополнения.\n"
        f"Текущий курс ЦБ: <b>1 USD = {USD_RUB_RATE:.4f} ₽</b>\n\n"
        "💵 <b>Доллары/USDT</b> — перевод в USDT TRC20 на кошелёк магазина.\n"
        "🇷🇺 <b>Рубли</b> — заявка владельцу: пришлёт реквизиты, после оплаты зачислит баланс.",
        kb([("💵 Доллары / USDT", "topcur:usd")],
           [("🇷🇺 Пополнить рублями на карту", "rubrequest")],
           [("⬅️ В меню", "menu")]))


@r.callback_query(F.data.startswith("topcur:"))
async def topup_currency(c: CallbackQuery):
    currency = c.data[str(c.data).rfind(":") + 1:]
    if currency not in ("usd", "rub"):
        return await c.answer("Некорректная валюта", show_alert=True)
    title = "💵 Сумма в долларах" if currency == "usd" else "🇷🇺 Сумма в рублях"
    await show(
        c,
        f"Выберите {title}. Курс: <b>1 USD = {USD_RUB_RATE:.2f} ₽</b>.",
        _fmt_presets_kb(currency))


@r.callback_query(F.data.startswith("topc:"))
async def topup_custom_start(c: CallbackQuery, state: FSMContext):
    currency = c.data.split(":")[1]
    if currency not in ("usd", "rub"):
        return await c.answer("Некорректная валюта", show_alert=True)
    await state.set_state(TopupCustom.amount)
    await state.update_data(currency=currency)
    hi = f"${MAX_TOPUP_USD:,.0f}" if currency == "usd" \
        else f"{MAX_TOPUP_USD * USD_RUB_RATE:,.0f} ₽".replace(",", " ")
    example = "5" if currency == "usd" else "1000"
    await show(
        c,
        f"✍️ <b>Своя сумма пополнения</b>\n\n"
        f"Введите сумму, например: <code>{example}</code>.\n"
        f"Диапазон: до <b>{hi}</b>.",
        kb([("⬅️ Отмена", "topup")]))


@r.message(StateFilter(TopupCustom.amount), F.text)
async def topup_custom_amount(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    currency = data.get("currency") or "usd"
    raw = (m.text or "").strip().replace(",", ".").replace(" ", "")
    raw = raw.lstrip("$").rstrip("₽").rstrip("r").rstrip("р") or raw
    if currency == "usd":
        amount = parse_amount(raw, 0.01, MAX_TOPUP_USD)
    else:
        amount = parse_amount(raw, 1.0, MAX_TOPUP_USD * USD_RUB_RATE)
    if amount is None:
        hi = f"${MAX_TOPUP_USD:,.0f}" if currency == "usd" \
            else f"{MAX_TOPUP_USD * USD_RUB_RATE:,.0f} ₽".replace(",", " ")
        return await m.answer(f"❌ Введите число до {hi}.")
    await _make_manual_topup(m, bot, currency=currency, entered=float(amount))


# ---------------- заявка: USDT TRC20 (адаптировано из старого проекта) ----------------
@r.callback_query(F.data.startswith("topup:"))
async def topup_preset(c: CallbackQuery, bot: Bot):
    try:
        _, currency, value = c.data.split(":")
        if currency not in ("usd", "rub"):
            raise ValueError
        entered = float(value)
        if not math.isfinite(entered) or entered <= 0:
            raise ValueError
    except (ValueError, IndexError):
        return await c.answer("Некорректная сумма", show_alert=True)
    await _make_manual_topup(c, bot, currency=currency, entered=entered)


async def _make_manual_topup(tap, bot: Bot, currency, entered):
    """Создает заявку на ручное пополнение USDT TRC20. tap — Message или CallbackQuery."""
    if not MANUAL_WALLET_ADDRESS:
        if isinstance(tap, Message):
            return await tap.answer("⚠️ Кошелёк для пополнения ещё не настроен владельцем.",
                                    reply_markup=kb([("⬅️ В меню", "menu")]))
        return await show(tap, "⚠️ Кошелёк для пополнения ещё не настроен владельцем.",
                          kb([("⬅️ В меню", "menu")]))
    if currency == "rub":
        amount = round(entered / USD_RUB_RATE, 2)
    else:
        amount = round(entered, 2)
    if amount <= 0 or amount > MAX_TOPUP_USD:
        msg = "❌ Некорректная сумма."
        if isinstance(tap, Message):
            return await tap.answer(msg)
        return await tap.answer(msg, show_alert=True)
    uid = tap.from_user.id
    ts = services.now()
    existing = await db.fetchrow(
        "SELECT id, amount FROM manual_topups "
        "WHERE user_id=$1 AND status='pending' AND created_at>$2 ORDER BY id DESC LIMIT 1",
        uid, ts - 1800)
    if existing:
        text = (f"⏳ У вас уже есть заявка <b>#{existing['id']}</b> "
                f"на <b>{money(existing['amount'])}</b>.\n\n"
                "Дождитесь её проверки администрацией.")
    else:
        req_id = await db.fetchval(
            "INSERT INTO manual_topups(user_id, amount, wallet, status, created_at) "
            "VALUES($1,$2,$3,'pending',$4) RETURNING id",
            uid, amount, MANUAL_WALLET_ADDRESS, ts)
        uname = f"@{tap.from_user.username}" if tap.from_user.username else "без username"
        admin_text = (
            f"💰 <b>Новая заявка на пополнение #{req_id}</b>\n\n"
            f"👤 Пользователь: {esc(tap.from_user.full_name)} ({esc(uname)})\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"💵 Сумма: <b>{money(amount)} ≈ {rub_money(amount)}</b>\n"
            f"🕒 Время: {fmt_ts(ts)}\n\n"
            "⚠️ <b>Проверьте фактическое поступление на кошелёк.</b>\n"
            "После проверки администратор произвести начисление вручную:\n"
            f"<code>/addbalance {uid} {amount:.2f} Пополнение #{req_id}</code>\n"
            "<i>Внутри бот хранит сумму в USD. Баланс автоматически не начисляется.</i>"
        )
        delivered = await services.notify_admins(bot, ADMINS, admin_text)
        if not delivered:
            await db.execute("UPDATE manual_topups SET status='cancelled', reviewed_at=$2 WHERE id=$1",
                             req_id, ts)
            text = "❌ Не удалось уведомить администрацию. Попробуйте позже."
        else:
            text = (f"💳 <b>Заявка #{req_id} создана</b>\n\n"
                    f"Вы выбрали: <b>{esc(_display_amount(currency, entered))}</b>\n\n"
                    f"Переведите <b>{money(amount)} USDT в сети TRC20</b> на кошелёк:\n"
                    f"<code>{esc(MANUAL_WALLET_ADDRESS)}</code>\n\n"
                    "После перевода ничего делать не нужно — админы проверят поступление "
                    "и зачислят баланс.\n\n"
                    "⚠️ Отправляйте только USDT по сети TRC20.\n"
                    "⚠️ Не отправляйте деньги повторно, если заявка уже создана.")
    markup = kb([("⬅️ В меню", "menu")])
    if isinstance(tap, Message):
        await answer_long(tap, text, reply_markup=markup)
    else:
        await show(tap, text, markup)


# ---------------- заявка рублями (унаследована из старого проекта) ----------------
@r.callback_query(F.data == "rubrequest")
async def rub_request_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(RubTopup.amount)
    await show(
        c,
        "🇷🇺 <b>Пополнение рублями</b>\n\n"
        "Введите сумму: например, <code>1000</code>.\n"
        f"Это примерно {money(round(1000 / USD_RUB_RATE, 2))} по курсу ЦБ: 1 USD = {USD_RUB_RATE:.4f} ₽.\n\n"
        "После заявки владелец пришлёт реквизиты для перевода (карта/телефон).",
        kb([("⬅️ Отмена", "menu")]))


@r.message(RubTopup.amount, F.text)
async def rub_request_create(m: Message, state: FSMContext, bot: Bot):
    raw = (m.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount_rub = float(raw.replace("₽", "").replace("руб", "").replace("р", ""))
        if not math.isfinite(amount_rub) or amount_rub <= 0 or amount_rub > MAX_TOPUP_USD * USD_RUB_RATE:
            raise ValueError
    except ValueError:
        return await m.answer(
            f"❌ Введите сумму от 1 до {MAX_TOPUP_USD * USD_RUB_RATE:,.0f} ₽. Например: 1000".replace(",", " "))
    amount_rub = round(amount_rub, 2)
    locked_rate = float(USD_RUB_RATE)
    amount_usd = round(amount_rub / locked_rate, 2)
    if amount_usd <= 0 or amount_usd > MAX_TOPUP_USD:
        return await m.answer("❌ Слишком маленькая сумма.")
    uid = m.from_user.id
    ts = services.now()
    existing = await db.fetchrow(
        "SELECT id, amount_rub, amount_usd FROM rub_topups "
        "WHERE user_id=$1 AND status IN ('pending','details_sent') AND created_at>$2 "
        "ORDER BY id DESC LIMIT 1", uid, ts - 1800)
    if existing:
        await state.clear()
        return await m.answer(
            f"⏳ У вас уже есть заявка <b>#{existing['id']}</b> на "
            f"<b>{existing['amount_rub']:,.2f} ₽</b> (≈ {money(existing['amount_usd'])}).\n\n"
            "Дождитесь реквизитов или ответа владельца.".replace(",", " "),
            reply_markup=kb([("⬅️ В меню", "menu")]))
    req_id = await db.fetchval(
        "INSERT INTO rub_topups(user_id, amount_rub, amount_usd, rate, status, created_at) "
        "VALUES($1,$2,$3,$4,'pending',$5) RETURNING id",
        uid, amount_rub, amount_usd, locked_rate, ts)
    await state.clear()
    uname = f"@{m.from_user.username}" if m.from_user.username else "без username"
    admin_text = (
        f"🇷🇺 <b>Новая заявка на пополнение рублями #{req_id}</b>\n\n"
        f"👤 Пользователь: {esc(m.from_user.full_name)} ({esc(uname)})\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"💰 Сумма: <b>{amount_rub:,.2f} ₽</b> ≈ <b>{money(amount_usd)}</b>\n"
        f"💱 Зафиксированный курс: <b>1 USD = {locked_rate:.4f} ₽</b>\n"
        f"🕒 Время: {fmt_ts(ts)}\n\n"
        "Нажмите «Реквизиты» и отправьте пользователю данные для оплаты, "
        "после этого нажмите «Зачислить».".replace(",", " "))
    admin_kb = kb(
        [("📨 Реквизиты", f"rubdetails:{req_id}"), ("💰 Зачислить", f"rubconfirm:{req_id}")],
        [("❌ Отменить", f"rubcancel:{req_id}")])
    delivered = await services.notify_admins(bot, ADMINS, admin_text, admin_kb)
    if not delivered:
        await db.execute("UPDATE rub_topups SET status='cancelled', reviewed_at=$2 WHERE id=$1",
                         req_id, ts)
        return await m.answer("❌ Не удалось отправить заявку владельцу. Попробуйте позже.",
                              reply_markup=kb([("⬅️ В меню", "menu")]))
    await m.answer(
        f"✅ <b>Заявка #{req_id} отправлена владельцу.</b>\n\n"
        f"Сумма: <b>{amount_rub:,.2f} ₽</b> ≈ <b>{money(amount_usd)}</b>.\n"
        "Реквизиты придут сюда. После оплаты владелец зачислит баланс.".replace(",", " "),
        reply_markup=kb([("⬅️ В меню", "menu")]))


# ---------------- админ: заявки в рублях (унаследованное подтверждение) ----------------
async def _edit_markup(c, bot, markup=None):
    from ..helpers import edit_markup
    await edit_markup(c, bot, markup)


@r.callback_query(F.data.startswith("rubdetails:"))
async def rub_details_start(c: CallbackQuery, state: FSMContext, bot: Bot):
    if c.from_user.id not in ADMINS:
        return await c.answer("Команда доступна админам", show_alert=True)
    try:
        req_id = int(c.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return await c.answer("Некорректная заявка", show_alert=True)
    req = await db.fetchrow("SELECT user_id, amount_rub, amount_usd, status FROM rub_topups WHERE id=$1", req_id)
    if not req or req["status"] not in ("pending", "details_sent"):
        return await c.answer("Заявка уже обработана или не найдена", show_alert=True)
    await state.set_state(RubTopupAdmin.details)
    await state.update_data(rub_req_id=req_id)
    await bot.send_message(
        c.from_user.id,
        f"📨 Заявка #{req_id}: отправьте одним сообщением реквизиты для оплаты.\n"
        "Например: номер карты, номер телефона, СБП и инструкции.\n\n"
        "Это сообщение будет отправлено пользователю.",
        reply_markup=kb([("⬅️ Отмена", "adm:panel")]))
    await c.answer()


@r.message(RubTopupAdmin.details, F.text)
async def rub_details_send(m: Message, state: FSMContext, bot: Bot):
    if m.from_user.id not in ADMINS:
        return
    data = await state.get_data()
    try:
        req_id = int(data.get("rub_req_id"))
    except (TypeError, ValueError):
        await state.clear()
        return await m.answer("❌ Заявка не найдена.")
    details = (m.text or "").strip()[:2000]
    req = await db.fetchrow("SELECT user_id, amount_rub, status FROM rub_topups WHERE id=$1", req_id)
    if not req or req["status"] not in ("pending", "details_sent"):
        await state.clear()
        return await m.answer("❌ Заявка уже обработана или не найдена.")
    from ..helpers import safe_send
    sent = await safe_send(
        bot, req["user_id"],
        f"💳 <b>Реквизиты для пополнения по заявке #{req_id}</b>\n\n"
        f"Сумма: <b>{req['amount_rub']:,.2f} ₽</b>\n\n{esc(details)}\n\n"
        f"После перевода дождитесь подтверждения владельца.".replace(",", " "))
    if not sent:
        return await m.answer("❌ Не удалось отправить реквизиты пользователю.")
    await db.execute("UPDATE rub_topups SET status='details_sent', details=$2 WHERE id=$1",
                     req_id, details)
    await state.clear()
    await m.answer(f"✅ Реквизиты по заявке #{req_id} отправлены пользователю.")


@r.callback_query(F.data.startswith("rubconfirm:"))
async def rub_confirm(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMINS:
        return await c.answer("Команда доступна админам", show_alert=True)
    try:
        req_id = int(c.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return await c.answer("Некорректная заявка", show_alert=True)
    req = await db.fetchrow("SELECT amount_usd, rate, status FROM rub_topups WHERE id=$1", req_id)
    if not req or req["status"] not in ("pending", "details_sent"):
        return await c.answer("Заявка уже обработана или не найдена", show_alert=True)
    rate = float(req["rate"] or USD_RUB_RATE)
    await _edit_markup(c, bot, kb(
        [("✅ Да, зачислить", f"rubcomplete:{req_id}")],
        [("⬅️ Отмена", f"rubback:{req_id}")]))
    await c.answer(f"К зачислению: {money(float(req['amount_usd']))} (курс {rate:.4f})", show_alert=True)


@r.callback_query(F.data.startswith("rubback:"))
async def rub_back(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMINS:
        return await c.answer("Команда доступна админам", show_alert=True)
    try:
        req_id = int(c.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return await c.answer("Некорректная заявка", show_alert=True)
    req = await db.fetchrow("SELECT status FROM rub_topups WHERE id=$1", req_id)
    if not req or req["status"] not in ("pending", "details_sent"):
        return await c.answer("Заявка уже обработана или не найдена", show_alert=True)
    await _edit_markup(c, bot, kb(
        [("📨 Реквизиты", f"rubdetails:{req_id}"), ("💰 Зачислить", f"rubconfirm:{req_id}")],
        [("❌ Отменить", f"rubcancel:{req_id}")]))
    await c.answer("Отмена")


@r.callback_query(F.data.startswith("rubcomplete:"))
async def rub_complete(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMINS:
        return await c.answer("Команда доступна админам", show_alert=True)
    try:
        req_id = int(c.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return await c.answer("Некорректная заявка", show_alert=True)
    try:
        async with db.db().acquire() as con:
            async with con.transaction():
                req = await con.fetchrow(
                    "SELECT user_id, amount_rub, amount_usd, rate, status FROM rub_topups "
                    "WHERE id=$1 FOR UPDATE", req_id)
                if not req or req["status"] not in ("pending", "details_sent"):
                    raise services.BalanceError("Заявка уже обработана или не найдена.")
                row = await con.fetchrow(
                    "UPDATE users SET balance=balance+$1 WHERE id=$2 RETURNING balance",
                    req["amount_usd"], req["user_id"])
                if row is None:
                    raise services.BalanceError("Пользователь не найден.")
                ts = services.now()
                await con.execute(
                    "INSERT INTO transactions(user_id,type,amount,currency,external_id,created_at,note,admin_id) "
                    "VALUES($1,'topup',$2,'USD',$3,$4,$5,$6)",
                    req["user_id"], req["amount_usd"], f"rub_topup:{req_id}", ts,
                    f"Пополнение рублями #{req_id}: {req['amount_rub']:,.2f} ₽ → "
                    f"{money(req['amount_usd'])} по курсу {float(req['rate'] or USD_RUB_RATE):.4f}".replace(",", " "),
                    c.from_user.id)
                await con.execute(
                    "UPDATE rub_topups SET status='completed', reviewed_at=$2, reviewed_by=$3 WHERE id=$1",
                    req_id, ts, c.from_user.id)
        from ..helpers import safe_send
        await safe_send(bot, req["user_id"],
                        f"✅ <b>Пополнение #{req_id} зачислено!</b>\n\n"
                        f"Сумма: <b>{req['amount_rub']:,.2f} ₽</b> ≈ <b>{money(req['amount_usd'])}</b>"
                        .replace(",", " "))
        await _edit_markup(c, bot)
        await c.answer("Зачислено", show_alert=True)
    except services.BalanceError as e:
        await c.answer(str(e), show_alert=True)


@r.callback_query(F.data.startswith("rubcancel:"))
async def rub_cancel(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMINS:
        return await c.answer("Команда доступна админам", show_alert=True)
    try:
        req_id = int(c.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return await c.answer("Некорректная заявка", show_alert=True)
    req = await db.fetchrow("SELECT user_id, status FROM rub_topups WHERE id=$1", req_id)
    if not req or req["status"] in ("completed", "cancelled"):
        return await c.answer("Заявка уже обработана или не найдена", show_alert=True)
    await db.execute(
        "UPDATE rub_topups SET status='cancelled', reviewed_at=$2, reviewed_by=$3 WHERE id=$1",
        req_id, services.now(), c.from_user.id)
    from ..helpers import safe_send
    await safe_send(bot, req["user_id"],
                    f"❌ Заявка на пополнение рублями #{req_id} отменена владельцем.")
    await _edit_markup(c, bot)
    await c.answer("Заявка отменена", show_alert=True)


# ---------------- 🧮 история операций (унаследована) ----------------
@r.callback_query(F.data == "ops")
async def ops(c: CallbackQuery):
    uid = c.from_user.id
    row = await db.fetchrow("SELECT balance, balance_currency FROM users WHERE id=$1", uid)
    bal = float(row["balance"]) if row else 0.0
    currency = (row["balance_currency"] if row else "USD") or "USD"
    txs = await db.fetch(
        "SELECT type, amount, created_at FROM transactions WHERE user_id=$1 "
        "ORDER BY id DESC LIMIT 15", uid)
    topups_rub = await db.fetch(
        "SELECT id, amount_rub, status, created_at FROM rub_topups WHERE user_id=$1 "
        "ORDER BY id DESC LIMIT 3", uid)
    parts = ["🧮 <b>История операций</b>"]
    if txs:
        parts.append("")
        lines = []
        for x in txs:
            sign = "+" if float(x["amount"]) >= 0 else "−"
            lines.append(f"{sign}{abs(float(x['amount'])):,.2f} USD — "
                         f"{services.tx_label(x['type'])}, {fmt_ts(x['created_at'])}".replace(",", " "))
        parts.append("Последние операции:")
        parts.extend(lines)
    else:
        parts.append("\nОпераций пока нет.")
    if topups_rub:
        parts.append("")
        parts.append("Заявки на пополнение рублями:")
        parts.extend(
            f"#{x['id']}: {x['amount_rub']:,.0f} ₽ — "
            f"{TOPUP_STATUS_LABELS.get(x['status'], x['status'])}".replace(",", " ")
            for x in topups_rub)
    await show(c, "\n".join(parts),
               kb([("💰 Пополнить", "topup")], [("💳 Заказы к оплате", "oh:0")],
                  [("⬅️ В меню", "menu")]))

