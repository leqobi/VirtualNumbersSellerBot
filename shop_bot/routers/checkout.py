"""Оформление заказа EXAMPLE SPLIT (FSM).

Сценарий: товар → количество → город → ПВЗ (служба + адрес или свой) →
подтверждение → создание заказа → выбор способа оплаты.

Оплата: с внутреннего баланса (разовое списание + неизменяемая запись в ledger)
или напрямую продавцу («Я оплатил» → админ подтверждает). Данные карт в боте
не принимаются, в фейковых успехов нет.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, services
from ..config import ADMINS, MAX_QTY, PVZ_SERVICES
from ..helpers import Busy, esc, kb, money, price_rub, show, USD_RUB_RATE

r = Router(name="checkout")

QTY_PRESETS = (1, 2, 3)

_busy: set[int] = set()


class Buy(StatesGroup):
    qty = State()
    city = State()
    pvz = State()


def _charged_estimate(amount_rub) -> float:
    return round(float(amount_rub) / USD_RUB_RATE, 4)


def _plain_rub(x) -> str:
    return f"{float(x):,.0f}".replace(",", " ") + " ₽"


def _qty_line(qty) -> str:
    return "1 шт." if qty == 1 else f"{qty} шт."


async def _load_product(pid):
    return await db.fetchrow("SELECT * FROM products WHERE id=$1", pid)


def _int_of(data: str, idx: int = 1) -> int | None:
    try:
        return int(str(data).split(":")[idx])
    except (ValueError, IndexError):
        return None


# ---------------- шаг 1: количество ----------------
@r.callback_query(F.data.startswith("ckstart:"))
async def buy_start(c: CallbackQuery, state: FSMContext):
    pid = _int_of(c.data)
    if pid is None:
        return await c.answer("Товар не найден", show_alert=True)
    p = await _load_product(pid)
    if not p or not p["active"]:
        return await show(c, "😔 Товар больше не продаётся.", kb([("⬅️ В каталог", "cat")]))
    if p["stock"] <= 0:
        return await show(c, "😔 Товар закончился, загляните позже.",
                          kb([("⬅️ В каталог", "cat")]))
    async with Busy(c.from_user.id, _busy) as busy:
        if not busy.ok:
            return await c.answer("⏳ Уже обрабатывается…")
        existing = await db.fetchrow(
            "SELECT * FROM shop_orders WHERE user_id=$1 AND product_id=$2 "
            "AND status IN ('AWAITING_PAYMENT','AWAITING_CONFIRM') ORDER BY id DESC LIMIT 1",
            c.from_user.id, pid)
        if existing:
            from .orders import send_order_card
            await send_order_card(c, existing["id"])
            return
        await state.set_state(Buy.qty)
        await state.update_data(pid=pid)
        max_n = min(MAX_QTY, p["stock"])
        rows = kb(
            [("× 1", f"ckqty:{pid}:1"), ("× 2", f"ckqty:{pid}:2")],
            [("× 3", f"ckqty:{pid}:3")],
            [("✍️ Другое количество", "ckqcustom")],
            [("⬅️ Отмена", "ckcancel")])
        await show(
            c,
            f"🛍 <b>{esc(p['name'])}</b>\n"
            f"💰 Цена: {price_rub(p['price'])}\n"
            f"📦 В наличии: {p['stock']} шт.\n\n"
            f"Выберите количество (от 1 до {max_n}):",
            rows)


@r.callback_query(F.data == "ckqcustom", StateFilter(Buy.qty))
async def buy_qty_custom(c: CallbackQuery):
    await show(c, "✍️ Введите количество числом одним сообщением:",
               kb([("⬅️ Отмена", "ckcancel")]))


async def _go_city(state: FSMContext, m: Message | None, c: CallbackQuery | None):
    await state.set_state(Buy.city)
    text = ("🏙 <b>Город доставки</b>\n\n"
            "Введите город получения, например: <code>Москва</code>")
    markup = kb([("⬅️ Отмена", "ckcancel")])
    if m is not None:
        await m.answer(text, reply_markup=markup)
    else:
        await show(c, text, markup)


@r.callback_query(F.data.startswith("ckqty:"), StateFilter(Buy.qty))
async def buy_qty_preset(c: CallbackQuery, state: FSMContext):
    qty = _int_of(c.data, 2)  # ckqty:{pid}:{qty}
    if qty is None or qty < 1:
        return await c.answer("Некорректное количество", show_alert=True)
    data = await state.get_data()
    p = await _load_product(data.get("pid")) if data.get("pid") is not None else None
    if not p:
        await state.clear()
        return await show(c, "Товар больше не доступен.",
                          kb([("🛍 Каталог", "cat")], [("⬅️ В меню", "menu")]))
    if qty > p["stock"]:
        return await c.answer(f"В наличии только {p['stock']} шт.", show_alert=True)
    await state.update_data(qty=qty)
    await _go_city(state, None, c)


@r.message(Buy.qty, F.text)
async def buy_qty_text(m: Message, state: FSMContext):
    raw = (m.text or "").strip()
    if not raw.isdigit():
        return await m.answer("Введите число, например: 1")
    qty = int(raw)
    data = await state.get_data()
    p = await _load_product(data.get("pid")) if data.get("pid") is not None else None
    if not p:
        await state.clear()
        return await m.answer("Товар больше не доступен. Откройте каталог заново.",
                              reply_markup=kb([("🛍 Каталог", "cat")], [("⬅️ В меню", "menu")]))
    max_n = min(MAX_QTY, max(p["stock"], 1))
    if not 1 <= qty <= max_n:
        return await m.answer(f"Количество — от 1 до {max_n}.")
    await state.update_data(qty=qty)
    await _go_city(state, m, None)


# ---------------- шаг 2: город ----------------
@r.message(Buy.city, F.text)
async def buy_city(m: Message, state: FSMContext):
    city = (m.text or "").strip()
    if not 2 <= len(city) <= 60:
        return await m.answer("Город: от 2 до 60 символов. Попробуйте ещё раз или нажмите «Отмена».")
    await state.update_data(city=city)
    await state.set_state(Buy.pvz)
    btns, row = [], []
    for svc in PVZ_SERVICES:
        row.append((svc, f"ckpvz:{svc}"))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([("✍️ Свой пункт выдачи", "ckpvzcustom")])
    btns.append([("⬅️ Отмена", "ckcancel")])
    await m.answer(
        "📍 <b>Пункт выдачи (ПВЗ)</b>\n\nВыберите службу доставки, "
        "затем укажите адрес или номер пункта.\nЛибо сразу напишите свой ПВЗ.",
        reply_markup=kb(*btns))


# ---------------- шаг 3: ПВЗ ----------------
@r.callback_query(F.data == "ckpvzcustom", StateFilter(Buy.pvz))
async def buy_pvz_custom(c: CallbackQuery):
    await show(c, "✍️ Введите адрес или номер пункта выдачи одним сообщением:",
               kb([("⬅️ Отмена", "ckcancel")]))


@r.callback_query(F.data.startswith("ckpvz:"), StateFilter(Buy.pvz))
async def buy_pvz_service(c: CallbackQuery, state: FSMContext):
    svc = c.data.split(":", 1)[1][:60]
    await state.update_data(pvzsvc=svc)
    await show(
        c,
        f"📍 Служба: <b>{esc(svc)}</b>\n\n"
        "Введите адрес или номер пункта выдачи одним сообщением:",
        kb([("⬅️ Отмена", "ckcancel")]))


@r.message(Buy.pvz, F.text)
async def buy_pvz_text(m: Message, state: FSMContext):
    pvz_text = (m.text or "").strip()
    if not 2 <= len(pvz_text) <= 200:
        return await m.answer("Пункт выдачи: от 2 до 200 символов. Или нажмите «Отмена».")
    data = await state.get_data()
    svc = data.get("pvzsvc")
    pvz = f"{svc} — {pvz_text}" if svc else pvz_text
    if len(pvz) > 200:
        pvz = pvz[:200]
    await state.update_data(pvz=pvz)
    await state.set_state(None)
    pid = data.get("pid")
    qty = int(data.get("qty") or 1)
    city = data.get("city") or "—"
    p = await _load_product(pid) if pid is not None else None
    if not p:
        await state.clear()
        return await m.answer("Товар больше не доступен. Откройте каталог заново.",
                              reply_markup=kb([("🛍 Каталог", "cat")], [("⬅️ В меню", "menu")]))
    total = round(float(p["price"]) * qty, 2)
    lines = [
        "🧾 <b>Подтверждение заказа</b>", "",
        f"🛍 Товар: <b>{esc(p['name'])}</b>",
        f"Количество: <b>{_qty_line(qty)}</b>",
        f"💰 Сумма: <b>{_plain_rub(total)}</b>",
        f"🏙 Город: <b>{esc(city)}</b>",
        f"📍 ПВЗ: <b>{esc(pvz)}</b>", "",
        "Всё верно? Подтвердите заказ — далее выбор способа оплаты.",
    ]
    markup = kb(
        [("✅ Подтвердить", "ckyes")],
        [("✍️ Изменить город", "ckreedit")],
        [("❌ Отмена", "ckcancel")])
    await m.answer("\n".join(lines), reply_markup=markup)


@r.callback_query(F.data == "ckreedit")
async def buy_edit_city(c: CallbackQuery, state: FSMContext):
    await state.set_state(Buy.city)
    await show(c, "🏙 Введите город заново одним сообщением:",
               kb([("⬅️ Отмена", "ckcancel")]))


@r.callback_query(F.data == "ckcancel")
async def buy_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "Оформление отменено.",
               kb([("🛍 Каталог", "cat")], [("⬅️ В меню", "menu")]))


@r.callback_query(F.data == "ckyes")
async def buy_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    pid, qty, city, pvz = data.get("pid"), data.get("qty"), data.get("city"), data.get("pvz")
    if pid is None or not qty or not city or not pvz:
        await state.clear()
        return await show(c, "Оформление не удалось — начните заново.",
                          kb([("🛍 Каталог", "cat")]))
    async with Busy(c.from_user.id, _busy) as busy:
        if not busy.ok:
            return await c.answer("⏳ Уже обрабатывается…")
        p = await _load_product(int(pid))
        if not p or not p["active"] or p["stock"] < int(qty):
            await state.clear()
            return await show(c, "😔 Товара больше нет в наличии в нужном количестве.",
                              kb([("🛍 Каталог", "cat")], [("⬅️ В меню", "menu")]))
        order = await _create_order(p, c.from_user.id, int(qty), str(city).strip(), str(pvz).strip())
        await state.clear()
        if order is None:
            return await show(c, "❌ Не удалось создать заказ. Попробуйте ещё раз.",
                              kb([("🛍 Каталог", "cat")]))
    await _notify_new_order(bot, order, c.from_user)
    await show_payment_options(c, order["id"])


async def _create_order(p, uid: int, qty: int, city: str, pvz: str):
    """Создаёт заказ (или возвращает существующий незакрытый по этому товару)."""
    if not 2 <= len(city) <= 60 or not 2 <= len(pvz) <= 200:
        return None
    ts = services.now()
    amount = round(float(p["price"]) * qty, 2)
    for _ in range(2):
        existing = await db.fetchrow(
            "SELECT * FROM shop_orders WHERE user_id=$1 AND product_id=$2 "
            "AND status IN ('AWAITING_PAYMENT','AWAITING_CONFIRM') ORDER BY id DESC LIMIT 1",
            uid, p["id"])
        if existing:
            return existing
        try:
            return await db.fetchrow(
                "INSERT INTO shop_orders(user_id, product_id, product_name, product_price, qty, "
                "amount, city, pvz, status, created_at) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,'AWAITING_PAYMENT',$9) RETURNING *",
                uid, p["id"], p["name"], float(p["price"]), qty, amount, city, pvz, ts)
        except Exception:
            continue
    return None


async def _notify_new_order(bot: Bot, order, user):
    """Сообщает администрации о новом (неоплаченном) заказе."""
    uname = f"@{user.username}" if user.username else "без username"
    text = (
        f"🆕 <b>Новый заказ {services.order_number_text(order['id'])}</b>\n\n"
        f"🛍 {esc(order['product_name'])} × {order['qty']}\n"
        f"💰 Сумма: <b>{_plain_rub(order['amount'])}</b>\n"
        f"🏙 Город: {esc(order['city'])} · 📍 ПВЗ: {esc(order['pvz'])}\n\n"
        f"👤 Покупатель: {esc(user.full_name)} ({esc(uname)}), id <code>{user.id}</code>")
    await services.notify_admins(
        bot, ADMINS, text,
        kb([("🧾 Открыть заказ", f"aord:v:{order['id']}")]))


# ---------------- экран способов оплаты ----------------
async def show_payment_options(c: CallbackQuery, oid: int, prefix: str = ""):
    """Экран выбора способа оплаты заказа (по заказу с актуальными данными)."""
    from .orders import send_order_card
    o = await services.get_order(oid)
    if not o:
        return await show(c, "❌ Заказ не найден.", kb([("🛍 Каталог", "cat")]))
    if o["status"] not in ("AWAITING_PAYMENT", "AWAITING_CONFIRM"):
        return await send_order_card(c, oid)
    need = _charged_estimate(o["amount"])
    row = await db.fetchrow("SELECT balance FROM users WHERE id=$1", o["user_id"])
    bal = float(row["balance"]) if row else 0.0
    if o["status"] == "AWAITING_PAYMENT":
        if bal >= need:
            pay_label = f"💰 Оплатить с баланса ({money(need)})"
        else:
            pay_label = f"💰 Баланс {money(bal)} — не хватает {money(need - bal)}"
        rows = [
            [ (pay_label, f"ckpaybal:{o['id']}"), ],
            [ ("✉️ Связаться с продавцом", f"ordpaycontact:{o['id']}") ],
            [ ("✅ Я оплатил — подтвердите", f"ckclaim:{o['id']}") ],
            [ ("❌ Отменить заказ", f"orcx:{o['id']}") ],
        ]
    else:
        rows = [
            [ ("⏳ Заказ уже ожидает подтверждения продавца", "verif") ],
            [ ("❌ Отменить заказ", f"orcx:{o['id']}") ],
        ]
    rows.append([("🧾 Информация о заказе", f"ov:{o['id']}")])
    rows.append([("⬅️ В меню", "menu")])
    text = (
        prefix + f"💳 <b>Оплата заказа</b>\n\n{services.order_number_text(o['id'])}\n"
        f"🛍 {esc(o['product_name'])} × {o['qty']}\n"
        f"💰 Сумма: <b>{_plain_rub(o['amount'])}</b>\n"
        f"Статус: {services.status_text(o['status'])}\n\n"
        "Способы оплаты:\n"
        "• 💰 Баланс — разовое списание с внутреннего баланса (пополнить можно в «Оплата»).\n"
        "• ✉️ Продавец — отправит реквизиты лично; данные карт в боте не принимаются.\n"
        "• ✅ Уже заплатили напрямую? Нажмите «Я оплатил» — продавец подтвердит.")
    await show(c, text, kb(*rows))


# ---------------- действия оплаты ----------------
@r.callback_query(F.data == "verif")
async def verif_note(c: CallbackQuery):
    await c.answer("Продавец проверит оплату — ожидайте подтверждения.", show_alert=True)


@r.callback_query(F.data.startswith("ckpaybal:"))
async def pay_from_balance(c: CallbackQuery, bot: Bot):
    oid = _int_of(c.data)
    if oid is None:
        return await c.answer()
    async with Busy(c.from_user.id, _busy) as busy:
        if not busy.ok:
            return await c.answer("⏳ Оплата уже обрабатывается…")
        res = await _pay_order_balance(c.bot, oid, c.from_user.id)
    if res["result"] == "paid":
        o = res["order"]
        await show(
            c,
            f"✅ <b>Оплата получена</b> — {services.order_number_text(o['id'])}\n\n"
            f"🛍 {esc(o['product_name'])} × {o['qty']}\n"
            f"💳 Списано: {money(res['charged'])} ≈ {float(o['amount']):,.0f} ₽".replace(",", " ") +
            "\n\nПродавец подтвердит и подготовит отправку.",
            kb([("🧾 Открыть заказ", f"ov:{o['id']}")], [("⬅️ В меню", "menu")]))
        await services.notify_admins(
            bot, ADMINS,
            f"💰 Заказ {services.order_number_text(o['id'])} оплачен с баланса: "
            f"{money(res['charged'])}",
            kb([("🧾 Открыть заказ", f"aord:v:{o['id']}")]))
        from ..config import LOW_STOCK_THRESHOLD
        if res["left"] < LOW_STOCK_THRESHOLD:
            await services.notify_admins(
                bot, ADMINS,
                f"⚠️ Товар заканчивается: {esc(o['product_name'])} — осталось {res['left']} шт.")
    else:
        text_result = {
            "not_found": "❌ Заказ не найден.",
            "closed": "ℹ️ Заказ уже обработан.",
            "unavailable": "😔 Товар по заказу больше не в наличии. Напишите в поддержку.",
            "no_stock": "😔 Товар закончился — списание не выполнено.",
            "insufficient": "❌ Средств недостаточно: пополните баланс в разделе «Оплата».",
        }.get(res["result"], "Произошла ошибка. Попробуйте позже.")
        await show(c, text_result, kb([("💳 Оплата заказа", f"ckpay:{oid}")],
                                      [("📦 Мои заказы", "oh:0")], [("⬅️ В меню", "menu")]))


@r.callback_query(F.data.startswith("ckpay:"))
async def pay_options_cb(c: CallbackQuery):
    oid = _int_of(c.data)
    if oid is None:
        return await c.answer()
    await show_payment_options(c, oid)


@r.callback_query(F.data.startswith("ckclaim:"))
async def claim_paid(c: CallbackQuery, bot: Bot):
    """Пользователь заявляет, что оплатил напрямую; статус → AWAITING_CONFIRM."""
    oid = _int_of(c.data)
    if oid is None:
        return await c.answer()
    o = await services.get_order(oid)
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Заказ не найден", show_alert=True)
    if o["status"] != "AWAITING_PAYMENT":
        return await c.answer("Подтверждение уже запрошено.", show_alert=True)
    async with Busy(c.from_user.id, _busy) as busy:
        if not busy.ok:
            return await c.answer("⏳ Уже обрабатывается…")
        updated = await db.fetchrow(
            "UPDATE shop_orders SET status='AWAITING_CONFIRM', pay_method='claimed' "
            "WHERE id=$1 AND status='AWAITING_PAYMENT' RETURNING *", oid)
    if not updated:
        return await c.answer("Не удалось обновить заказ. Попробуйте позже.", show_alert=True)
    uname = f"@{c.from_user.username}" if c.from_user.username else "без username"
    await services.notify_admins(
        bot, ADMINS,
        f"✅ <b>Пользователь заявляет: оплатил напрямую</b>\n"
        f"🧾 Заказ {services.order_number_text(oid)} — "
        f"{esc(updated['product_name'])} × {updated['qty']}, {float(updated['amount']):,.0f} ₽"
        .replace(",", " "),
        kb([("✅ Подтвердить оплату", f"aord:cfy:{oid}"),
            ("🧾 Открыть заказ", f"aord:v:{oid}")]))
    await services.notify_admins(
        bot, ADMINS,
        f"👤 Пользователь: {esc(c.from_user.full_name)} ({esc(uname)}), "
        f"id <code>{c.from_user.id}</code>")
    await show(
        c,
        f"⏳ <b>Ожидает подтверждения</b> — {services.order_number_text(oid)}\n\n"
        "Продавец проверит поступление и подтвердит заказ.\n"
        "Статус в «Мои заказы» обновится автоматически.",
        kb([("🧾 Открыть заказ", f"ov:{oid}")], [("⬅️ В меню", "menu")]))


async def _pay_order_balance(bot: Bot, oid: int, uid: int) -> dict:
    """Оплата заказа с внутреннего баланса.

    Вся операция в одной транзакции; порядок блокировок: заказ → товар → пользователь.
    Повторный вызов по оплаченному заказу ничего не списывает.
    """
    async with db.db().acquire() as con:
        async with con.transaction():
            o = await con.fetchrow(
                "SELECT * FROM shop_orders WHERE id=$1 AND user_id=$2 FOR UPDATE", oid, uid)
            if o is None:
                return {"result": "not_found"}
            if o["status"] not in ("AWAITING_PAYMENT", "AWAITING_CONFIRM"):
                return {"result": "closed"}
            product = None
            if o["product_id"] is not None:
                product = await con.fetchrow(
                    "SELECT id, stock, active FROM products WHERE id=$1 FOR UPDATE", o["product_id"])
            if product is not None and (not product["active"] or product["stock"] < o["qty"]):
                return {"result": "no_stock"}
            rate = float(USD_RUB_RATE)
            charged = round(float(o["amount"]) / rate, 4)
            res = await con.execute(
                "UPDATE users SET balance=balance-$1 WHERE id=$2 AND balance>=$1", charged, uid)
            if res != "UPDATE 1":
                return {"result": "insufficient"}
            if product is not None:
                await con.execute(
                    "UPDATE products SET stock=stock-$2 WHERE id=$1", product["id"], o["qty"])
                left = product["stock"] - o["qty"]
            else:
                left = 0
            order = await con.fetchrow(
                "UPDATE shop_orders SET status='PAID', pay_method='balance', "
                "charged_amount=$3, charged_currency='USD', rate=$4, paid_at=$5 "
                "WHERE id=$1 AND status IN ('AWAITING_PAYMENT','AWAITING_CONFIRM') RETURNING *",
                oid, uid, charged, rate, services.now())
            if order is None:
                raise RuntimeError("order state race")  # откатывает транзакцию целиком
            await con.execute(
                "INSERT INTO transactions(user_id, type, amount, currency, external_id, created_at, note) "
                "VALUES($1,'order_payment',$2,'USD',$3,$4,$5)",
                uid, -charged, f"shop:pay:{oid}", services.now(),
                f"Оплата заказа {services.order_number_text(oid)}")
            return {"result": "paid", "order": order, "charged": charged, "left": left}

