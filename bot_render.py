import asyncio
import html
import logging
import os
import time
from datetime import datetime, timezone

import asyncpg
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


BOT_TOKEN = required_env("BOT_TOKEN")

try:
    ADMINS = [int(x.strip()) for x in required_env("ADMIN_IDS").split(",") if x.strip()]
except ValueError as exc:
    raise RuntimeError("ADMIN_IDS must contain only Telegram numeric IDs separated by commas") from exc

if not ADMINS:
    raise RuntimeError("ADMIN_IDS must contain at least one Telegram numeric ID")

DATABASE_URL = required_env("DATABASE_URL")  # PostgreSQL (Render / Neon / Supabase)
PORT = int(os.getenv("PORT", "10000"))     # Render задаёт сам

# Crypto Pay. Для безопасного теста по умолчанию используется testnet.
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_ASSET = os.getenv("CRYPTOBOT_ASSET", "USDT").upper()
CRYPTOBOT_TESTNET = os.getenv("CRYPTOBOT_TESTNET", "1") == "1"
CRYPTOBOT_API = (
    "https://testnet-pay.crypt.bot/api"
    if CRYPTOBOT_TESTNET
    else "https://pay.crypt.bot/api"
)

pool: asyncpg.Pool = None
bot_instance: Bot = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id BIGINT PRIMARY KEY, username TEXT, name TEXT,
    balance DOUBLE PRECISION DEFAULT 0, joined BIGINT);
CREATE TABLE IF NOT EXISTS countries(id SERIAL PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS lots(
    id SERIAL PRIMARY KEY, country_id INT REFERENCES countries(id),
    service TEXT, price DOUBLE PRECISION, UNIQUE(country_id, service));
CREATE TABLE IF NOT EXISTS numbers(
    id SERIAL PRIMARY KEY, lot_id INT REFERENCES lots(id), phone TEXT,
    status TEXT DEFAULT 'free', buyer BIGINT, sold_at BIGINT,
    sold_price DOUBLE PRECISION, code TEXT, UNIQUE(lot_id, phone));
CREATE TABLE IF NOT EXISTS orders(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    number_id INT REFERENCES numbers(id),
    amount DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'paid',
    created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    type TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    external_id TEXT,
    created_at BIGINT NOT NULL,
    UNIQUE(external_id)
);
CREATE INDEX IF NOT EXISTS idx_numbers_status_lot ON numbers(lot_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS payments(
    id BIGSERIAL PRIMARY KEY,
    invoice_id BIGINT UNIQUE NOT NULL,
    user_id BIGINT REFERENCES users(id) NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    asset TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at BIGINT NOT NULL,
    paid_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE TABLE IF NOT EXISTS msgmap(
    admin_id BIGINT, msg_id BIGINT, user_id BIGINT, PRIMARY KEY(admin_id, msg_id));
"""


# ---------------- хелперы ----------------
def esc(s):
    return html.escape(str(s))


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def main_kb():
    return kb(
        [("📱 Купить номер", "buy")],
        [("💰 Пополнить баланс", "topup"), ("👤 Профиль", "profile")],
        [("🧾 История заказов", "history")],
        [("✉️ Написать создателю", "contact")],
    )


def back_kb():
    return kb([("⬅️ В меню", "menu")])


async def show(c: CallbackQuery, text, markup):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await c.message.answer(text, reply_markup=markup)
    await c.answer()


async def safe_send(bot, uid, text, **kw):
    try:
        await bot.send_message(uid, text, **kw)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


class Contact(StatesGroup):
    text = State()


r = Router()


# ---------------- старт / меню / профиль ----------------
@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    await pool.execute(
        "INSERT INTO users(id, username, name, joined) VALUES($1,$2,$3,$4) "
        "ON CONFLICT (id) DO UPDATE SET username=$2, name=$3",
        u.id, u.username, u.full_name, int(time.time()))
    await m.answer("👋 Добро пожаловать! Здесь можно купить виртуальный номер.", reply_markup=main_kb())


@r.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "Главное меню:", main_kb())


@r.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    bal = await pool.fetchval("SELECT balance FROM users WHERE id=$1", c.from_user.id) or 0
    n = await pool.fetchval("SELECT COUNT(*) FROM numbers WHERE buyer=$1", c.from_user.id)
    await show(c, f"👤 Профиль\nID: <code>{c.from_user.id}</code>\n"
                  f"Баланс: <b>${bal:.2f}</b>\nКуплено номеров: {n}", back_kb())


def topup_amount_kb():
    return kb(
        [("$1", "topup:1"), ("$5", "topup:5")],
        [("$10", "topup:10"), ("$25", "topup:25")],
        [("⬅️ В меню", "menu")]
    )


async def crypto_api(method: str, payload=None):
    if not CRYPTOBOT_TOKEN:
        raise RuntimeError("CRYPTOBOT_TOKEN is not configured")
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    async with ClientSession() as session:
        async with session.post(
            f"{CRYPTOBOT_API}/{method}",
            json=payload or {},
            headers=headers,
            timeout=20
        ) as resp:
            data = await resp.json(content_type=None)
            if not data.get("ok"):
                raise RuntimeError(str(data.get("error") or data))
            return data["result"]


@r.callback_query(F.data == "topup")
async def topup(c: CallbackQuery):
    if not CRYPTOBOT_TOKEN:
        return await show(
            c,
            "⚠️ Оплата пока не настроена владельцем.",
            back_kb()
        )
    mode = "TESTNET" if CRYPTOBOT_TESTNET else "MAINNET"
    await show(
        c,
        f"💰 <b>Пополнение баланса</b>\n"
        f"Выберите сумму.\n\n"
        f"Сеть оплаты: <b>{mode}</b>\n"
        f"Валюта: <b>{esc(CRYPTOBOT_ASSET)}</b>",
        topup_amount_kb()
    )


@r.callback_query(F.data.startswith("topup:" ))
async def create_topup_invoice(c: CallbackQuery):
    if not CRYPTOBOT_TOKEN:
        return await show(c, "⚠️ Crypto Pay не настроен.", back_kb())

    try:
        amount = float(c.data.split(":")[1])
        if amount <= 0:
            raise ValueError
    except (ValueError, IndexError):
        return await c.answer("Некорректная сумма", show_alert=True)

    uid = c.from_user.id
    try:
        invoice = await crypto_api(
            "createInvoice",
            {
                "currency_type": "crypto",
                "asset": CRYPTOBOT_ASSET,
                "amount": f"{amount:.2f}",
                "description": f"Пополнение баланса пользователя {uid}",
                "payload": str(uid),
                "allow_comments": False,
                "allow_anonymous": False,
                "expires_in": 1800,
            }
        )
        await pool.execute(
            "INSERT INTO payments(invoice_id,user_id,amount,asset,status,created_at) "
            "VALUES($1,$2,$3,$4,'active',$5) "
            "ON CONFLICT(invoice_id) DO NOTHING",
            int(invoice["invoice_id"]), uid, amount, CRYPTOBOT_ASSET, int(time.time())
        )
    except Exception as e:
        logging.exception("Crypto Pay createInvoice failed")
        return await show(
            c,
            "❌ Не удалось создать счёт. Попробуйте ещё раз позже.",
            back_kb()
        )

    await show(
        c,
        f"💳 <b>Счёт создан</b>\n\n"
        f"Сумма: <b>${amount:.2f}</b>\n"
        f"Срок: 30 минут\n\n"
        f"После оплаты баланс обновится автоматически.",
        kb(
            [("💳 Оплатить", f"payurl:{invoice['invoice_id']}")],
            [("🔄 Проверить оплату", f"checkpay:{invoice['invoice_id']}")],
            [("⬅️ Назад", "topup")]
        )
    )
    # Telegram callback_data cannot contain the payment URL itself.
    # Store the URL in FSM-less temporary DB field is unnecessary; instead
    # send a separate message with the official invoice URL.
    await c.message.answer(
        f"🔗 <a href=\"{html.escape(invoice['bot_invoice_url'], quote=True)}\">Открыть счёт Crypto Pay</a>"
    )


@r.callback_query(F.data.startswith("payurl:"))
async def payurl_legacy(c: CallbackQuery):
    await c.answer("Откройте ссылку на счёт выше.", show_alert=True)


@r.callback_query(F.data.startswith("checkpay:"))
async def check_topup(c: CallbackQuery):
    try:
        invoice_id = int(c.data.split(":")[1])
        inv = await crypto_api(
            "getInvoices",
            {"invoice_ids": str(invoice_id), "count": 1}
        )
        items = inv if isinstance(inv, list) else []
        if not items:
            return await c.answer("Счёт не найден", show_alert=True)
        invoice = items[0]
        if invoice.get("status") != "paid":
            return await c.answer("Оплата ещё не подтверждена.", show_alert=True)
        await credit_paid_invoice(invoice)
        await show(
            c,
            "✅ <b>Оплата подтверждена!</b>\n"
            "Баланс пополнен. Откройте профиль, чтобы увидеть новый баланс.",
            back_kb()
        )
    except Exception:
        logging.exception("Manual payment check failed")
        await c.answer("Не удалось проверить оплату.", show_alert=True)


@r.callback_query(F.data == "history")
async def history(c: CallbackQuery):
    rows = await pool.fetch(
        "SELECT n.phone, n.sold_price, n.sold_at, l.service, co.name country FROM numbers n "
        "JOIN lots l ON l.id=n.lot_id JOIN countries co ON co.id=l.country_id "
        "WHERE n.buyer=$1 ORDER BY n.sold_at DESC LIMIT 10", c.from_user.id)
    if not rows:
        return await show(c, "🧾 Заказов пока нет.", back_kb())
    lines = [f"<code>{esc(x['phone'])}</code> — {esc(x['service'])}, {esc(x['country'])}, "
             f"${x['sold_price']:.2f}, {datetime.fromtimestamp(x['sold_at'], tz=timezone.utc):%d.%m.%Y %H:%M} UTC" for x in rows]
    await show(c, "🧾 <b>Последние заказы:</b>\n\n" + "\n".join(lines), back_kb())


# ---------------- покупка: страна -> сервис -> подтверждение ----------------
@r.callback_query(F.data == "buy")
async def buy_countries(c: CallbackQuery):
    rows = await pool.fetch(
        "SELECT co.id, co.name, COUNT(n.id) cnt FROM countries co "
        "JOIN lots l ON l.country_id=co.id JOIN numbers n ON n.lot_id=l.id AND n.status='free' "
        "GROUP BY co.id, co.name ORDER BY co.name")
    if not rows:
        return await show(c, "😔 Сейчас номеров нет. Загляните позже.", back_kb())
    btns = [[(f"🌍 {x['name']} ({x['cnt']})", f"ct:{x['id']}")] for x in rows]
    btns.append([("⬅️ В меню", "menu")])
    await show(c, "Выберите страну:", kb(*btns))


@r.callback_query(F.data.startswith("ct:"))
async def buy_services(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    rows = await pool.fetch(
        "SELECT l.id, l.service, l.price, COUNT(n.id) cnt FROM lots l "
        "JOIN numbers n ON n.lot_id=l.id AND n.status='free' "
        "WHERE l.country_id=$1 GROUP BY l.id ORDER BY l.service", cid)
    if not rows:
        return await show(c, "Номера закончились.", kb([("⬅️ Назад", "buy")]))
    btns = [[(f"{x['service']} — ${x['price']:.2f} ({x['cnt']})", f"lot:{x['id']}")] for x in rows]
    btns.append([("⬅️ Назад", "buy")])
    await show(c, "Выберите сервис:", kb(*btns))


@r.callback_query(F.data.startswith("lot:"))
async def confirm(c: CallbackQuery):
    lid = int(c.data.split(":")[1])
    l = await pool.fetchrow("SELECT l.*, co.name country FROM lots l JOIN countries co ON co.id=l.country_id "
                            "WHERE l.id=$1", lid)
    if not l:
        return await c.answer()
    await show(c, f"Купить номер?\n🌍 {esc(l['country'])}\n📲 {esc(l['service'])}\n💵 ${l['price']:.2f}",
               kb([("✅ Купить", f"buy:{lid}")], [("⬅️ Назад", f"ct:{l['country_id']}")]))


class NoStock(Exception):
    pass


class NoMoney(Exception):
    pass


@r.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery, bot: Bot):
    lid = int(c.data.split(":")[1])
    uid = c.from_user.id
    try:
        async with pool.acquire() as con:
            async with con.transaction():
                l = await con.fetchrow("SELECT l.*, co.name country FROM lots l "
                                       "JOIN countries co ON co.id=l.country_id WHERE l.id=$1", lid)
                num = await con.fetchrow(
                    "UPDATE numbers SET status='sold', buyer=$1, sold_at=$2, sold_price=$3 WHERE id=("
                    "SELECT id FROM numbers WHERE lot_id=$4 AND status='free' ORDER BY id LIMIT 1 "
                    "FOR UPDATE SKIP LOCKED) RETURNING id, phone", uid, int(time.time()), l["price"], lid)
                if not num:
                    raise NoStock
                res = await con.execute("UPDATE users SET balance=balance-$1 WHERE id=$2 AND balance>=$1",
                                        l["price"], uid)
                if res != "UPDATE 1":
                    raise NoMoney  # откат: номер снова свободен
                await con.execute(
                    "INSERT INTO orders(user_id, number_id, amount, status, created_at) "
                    "VALUES($1,$2,$3,'paid',$4)",
                    uid, num["id"], l["price"], int(time.time())
                )
                await con.execute(
                    "INSERT INTO transactions(user_id, type, amount, currency, created_at) "
                    "VALUES($1,'purchase',$2,'USD',$3)",
                    uid, -float(l["price"]), int(time.time())
                )
    except NoStock:
        return await show(c, "😔 Номера закончились.", kb([("⬅️ Назад", "buy")]))
    except NoMoney:
        return await show(c, "❌ Недостаточно средств.", kb([("💰 Пополнить", "topup")], [("⬅️ В меню", "menu")]))
    await show(c, f"✅ Ваш номер: <code>{esc(num['phone'])}</code>\n"
                  f"{esc(l['service'])}, {esc(l['country'])}\n\nИстория заказов — в меню.", back_kb())
    for a in ADMINS:
        await safe_send(bot, a, f"🛒 Куплен номер <code>{esc(num['phone'])}</code> "
                                f"({esc(l['service'])}, {esc(l['country'])}) — "
                                f"{esc(c.from_user.full_name)}, id <code>{uid}</code>")


# ---------------- обращение к создателю ----------------
@r.callback_query(F.data == "contact")
async def contact(c: CallbackQuery, state: FSMContext):
    await state.set_state(Contact.text)
    await show(c, "✍️ Напишите сообщение создателю одним сообщением:", kb([("⬅️ Отмена", "menu")]))


@r.message(StateFilter(Contact.text), F.text)
async def contact_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    u = m.from_user
    uname = f"@{u.username}" if u.username else "без username"
    head = (f"✉️ <b>{esc(u.full_name)}</b> ({uname}), id <code>{u.id}</code>:\n\n{esc(m.text)}\n\n"
            f"<i>Ответить: reply на это сообщение или /reply {u.id} текст</i>")
    for a in ADMINS:
        try:
            sent = await bot.send_message(a, head)
            await pool.execute("INSERT INTO msgmap VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                               a, sent.message_id, u.id)
        except Exception:
            pass
    await m.answer("✅ Отправлено! Ответ придёт сюда.", reply_markup=main_kb())


# ---------------- админ-команды ----------------
ADMIN = F.from_user.id.in_(ADMINS)


@r.message(Command("reply"), ADMIN)
async def reply_cmd(m: Message, bot: Bot):
    p = (m.text or "").split(maxsplit=2)
    if len(p) < 3 or not p[1].isdigit():
        return await m.answer("Формат: /reply user_id текст")
    ok = await safe_send(bot, int(p[1]), f"💬 <b>Ответ создателя:</b>\n\n{esc(p[2])}")
    await m.answer("✅ Доставлено" if ok else "❌ Не доставлено")


@r.message(F.reply_to_message, ADMIN, F.text, ~F.text.startswith("/"))
async def reply_by_reply(m: Message, bot: Bot):
    uid = await pool.fetchval("SELECT user_id FROM msgmap WHERE admin_id=$1 AND msg_id=$2",
                              m.from_user.id, m.reply_to_message.message_id)
    if not uid:
        return
    ok = await safe_send(bot, uid, f"💬 <b>Ответ создателя:</b>\n\n{esc(m.text)}")
    await m.answer("✅ Доставлено" if ok else "❌ Не доставлено")


@r.message(Command("broadcast"), ADMIN)
async def broadcast(m: Message, bot: Bot):
    text = (m.text or "").partition(" ")[2].strip()
    src = m.reply_to_message
    if not text and not src:
        return await m.answer("Формат: /broadcast текст\nили ответьте /broadcast на готовое сообщение")
    users = [x["id"] for x in await pool.fetch("SELECT id FROM users")]
    await m.answer(f"📤 Рассылка на {len(users)} пользователей…")

    async def send(uid):
        if src:
            await bot.copy_message(uid, src.chat.id, src.message_id)
        else:
            await bot.send_message(uid, text)

    ok = fail = 0
    for uid in users:
        try:
            try:
                await send(uid)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await send(uid)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")


@r.message(Command("stats"), ADMIN)
async def stats(m: Message):
    users = await pool.fetchval("SELECT COUNT(*) FROM users")
    sold = await pool.fetchrow("SELECT COUNT(*) n, COALESCE(SUM(sold_price),0) s FROM numbers WHERE status='sold'")
    free = await pool.fetchval("SELECT COUNT(*) FROM numbers WHERE status='free'")
    await m.answer(f"📊 <b>Статистика</b>\n👥 Пользователей: {users}\n"
                   f"📱 Куплено номеров: {sold['n']} (на ${sold['s']:.2f})\n📦 В наличии: {free}")


@r.message(Command("addbalance"), ADMIN)
async def addbalance(m: Message, bot: Bot):
    p = (m.text or "").split()
    try:
        uid, amount = int(p[1]), float(p[2].replace(",", "."))
    except (IndexError, ValueError):
        return await m.answer("Формат: /addbalance user_id сумма")
    res = await pool.execute("UPDATE users SET balance=balance+$1 WHERE id=$2", amount, uid)
    if res != "UPDATE 1":
        return await m.answer("❌ Пользователь не найден (он должен нажать /start)")
    await safe_send(bot, uid, f"💰 Баланс изменён на ${amount:.2f}")
    await m.answer("✅ Готово")


class AddNumbers(StatesGroup):
    country = State()
    service = State()
    price = State()
    numbers = State()


def admin_kb():
    return kb(
        [("📱 Добавить номера", "adm:add")],
        [("📦 Склад", "adm:stock")],
        [("📊 Статистика", "adm:stats")],
        [("📢 Рассылка", "adm:broadcast")],
        [("👥 Пользователи", "adm:users")],
        [("⬅️ В меню", "menu")],
    )


@r.message(Command("admin"), ADMIN)
async def admin_cmd(m: Message):
    await m.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_kb())


@r.callback_query(F.data == "adm:add", ADMIN)
async def admin_add_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddNumbers.country)
    await show(c, "🌍 Введите страну:", back_kb())


@r.message(AddNumbers.country, ADMIN, F.text)
async def admin_add_country(m: Message, state: FSMContext):
    await state.update_data(country=m.text.strip())
    await state.set_state(AddNumbers.service)
    await m.answer("📲 Введите сервис, например Telegram:")


@r.message(AddNumbers.service, ADMIN, F.text)
async def admin_add_service(m: Message, state: FSMContext):
    await state.update_data(service=m.text.strip())
    await state.set_state(AddNumbers.price)
    await m.answer("💵 Введите цену одного номера, например 0.60:")


@r.message(AddNumbers.price, ADMIN, F.text)
async def admin_add_price(m: Message, state: FSMContext):
    try:
        price = float(m.text.replace(",", ".").strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        return await m.answer("❌ Цена должна быть положительным числом. Например: 0.60")
    await state.update_data(price=price)
    await state.set_state(AddNumbers.numbers)
    await m.answer(
        "📱 Отправьте номера одним сообщением, по одному на строку.\n\n"
        "Пример:\n<code>+79001112233\n+79001112234</code>"
    )


@r.message(AddNumbers.numbers, ADMIN, F.text)
async def admin_add_numbers(m: Message, state: FSMContext):
    data = await state.get_data()
    phones = [x.strip() for x in m.text.splitlines() if x.strip()]
    if not phones:
        return await m.answer("❌ Не найдено ни одного номера.")

    country = data["country"]
    service = data["service"]
    price = data["price"]

    async with pool.acquire() as con:
        async with con.transaction():
            cid = await con.fetchval(
                "INSERT INTO countries(name) VALUES($1) "
                "ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name "
                "RETURNING id", country
            )
            lid = await con.fetchval(
                "INSERT INTO lots(country_id, service, price) VALUES($1,$2,$3) "
                "ON CONFLICT (country_id, service) DO UPDATE SET price=EXCLUDED.price "
                "RETURNING id", cid, service, price
            )
            added = 0
            for phone in phones:
                row = await con.fetchval(
                    "INSERT INTO numbers(lot_id, phone) VALUES($1,$2) "
                    "ON CONFLICT DO NOTHING RETURNING id", lid, phone
                )
                added += bool(row)

    await state.clear()
    await m.answer(
        f"✅ <b>Номера добавлены</b>\n"
        f"🌍 {esc(country)}\n📲 {esc(service)}\n"
        f"💵 ${price:.2f}\n"
        f"📱 Добавлено: {added} из {len(phones)}",
        reply_markup=admin_kb()
    )


# Старый формат оставляем, чтобы уже привычная команда не сломалась.
@r.message(Command("addnumbers"), ADMIN)
async def addnumbers_legacy(m: Message):
    head, _, body = (m.text or "").partition("\n")
    parts = [x.strip() for x in head.partition(" ")[2].split("|")]
    phones = body.split()
    try:
        country, service, price = parts[0], parts[1], float(parts[2].replace(",", "."))
        assert country and service and phones and price > 0
    except (IndexError, ValueError, AssertionError):
        return await m.answer(
            "Формат:\n<code>/addnumbers Россия | Telegram | 0.6\n"
            "+79001112233\n+79001112234</code>"
        )
    async with pool.acquire() as con:
        async with con.transaction():
            cid = await con.fetchval(
                "INSERT INTO countries(name) VALUES($1) "
                "ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id", country
            )
            lid = await con.fetchval(
                "INSERT INTO lots(country_id, service, price) VALUES($1,$2,$3) "
                "ON CONFLICT (country_id, service) DO UPDATE SET price=EXCLUDED.price "
                "RETURNING id", cid, service, price
            )
            added = 0
            for p in phones:
                added += bool(await con.fetchval(
                    "INSERT INTO numbers(lot_id, phone) VALUES($1,$2) "
                    "ON CONFLICT DO NOTHING RETURNING id", lid, p
                ))
    await m.answer(
        f"✅ Добавлено: {added} из {len(phones)} "
        f"({esc(country)} / {esc(service)} / ${price:.2f})"
    )


@r.callback_query(F.data == "adm:stock", ADMIN)
async def admin_stock_button(c: CallbackQuery):
    rows = await pool.fetch(
        "SELECT co.name country, l.service, l.price, "
        "COUNT(n.id) FILTER (WHERE n.status='free') free "
        "FROM lots l JOIN countries co ON co.id=l.country_id "
        "LEFT JOIN numbers n ON n.lot_id=l.id "
        "GROUP BY co.name, l.service, l.price ORDER BY co.name, l.service"
    )
    if not rows:
        return await show(c, "📦 Склад пуст.", admin_kb())
    body = "\n".join(
        f"{esc(x['country'])} / {esc(x['service'])} / "
        f"${x['price']:.2f} — {x['free']} шт."
        for x in rows
    )
    await show(c, "📦 <b>Склад:</b>\n" + body, admin_kb())


@r.callback_query(F.data == "adm:stats", ADMIN)
async def admin_stats_button(c: CallbackQuery):
    users = await pool.fetchval("SELECT COUNT(*) FROM users")
    sold = await pool.fetchrow(
        "SELECT COUNT(*) n, COALESCE(SUM(sold_price),0) s "
        "FROM numbers WHERE status='sold'"
    )
    free = await pool.fetchval("SELECT COUNT(*) FROM numbers WHERE status='free'")
    revenue = await pool.fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='purchase'"
    )
    await show(
        c,
        f"📊 <b>Статистика</b>\n"
        f"👥 Пользователей: {users}\n"
        f"📱 Продано: {sold['n']}\n"
        f"💰 Продаж на: ${sold['s']:.2f}\n"
        f"📦 В наличии: {free}\n"
        f"🧾 Учтено покупок: ${abs(float(revenue or 0)):.2f}",
        admin_kb()
    )


@r.callback_query(F.data == "adm:users", ADMIN)
async def admin_users_button(c: CallbackQuery):
    users = await pool.fetchval("SELECT COUNT(*) FROM users")
    active = await pool.fetchval(
        "SELECT COUNT(*) FROM users WHERE joined >= $1",
        int(time.time()) - 86400
    )
    await show(
        c,
        f"👥 <b>Пользователи</b>\nВсего: {users}\n"
        f"Новых за 24 часа: {active}",
        admin_kb()
    )


@r.callback_query(F.data == "adm:broadcast", ADMIN)
async def admin_broadcast_button(c: CallbackQuery):
    await show(
        c,
        "📢 Для рассылки ответьте на готовое сообщение командой "
        "<code>/broadcast</code> или используйте <code>/broadcast текст</code>.",
        admin_kb()
    )


@r.message(Command("stock"), ADMIN)
async def stock(m: Message):
    rows = await pool.fetch(
        "SELECT co.name country, l.service, l.price, COUNT(n.id) FILTER (WHERE n.status='free') free "
        "FROM lots l JOIN countries co ON co.id=l.country_id LEFT JOIN numbers n ON n.lot_id=l.id "
        "GROUP BY co.name, l.service, l.price ORDER BY co.name, l.service")
    if not rows:
        return await m.answer("Склад пуст. Добавьте: /addnumbers")
    await m.answer("📦 <b>Склад:</b>\n" + "\n".join(
        f"{esc(x['country'])} / {esc(x['service'])} / ${x['price']:.2f} — {x['free']} шт." for x in rows))

async def credit_paid_invoice(invoice):
    """Atomically credit one paid invoice exactly once."""
    invoice_id = int(invoice["invoice_id"])
    # Credit only the amount that belongs to our stored invoice.
    # The row lock and all related updates are kept inside one transaction
    # so two workers/checks cannot credit the same invoice twice.
    async with pool.acquire() as con:
        async with con.transaction():
            current = await con.fetchrow(
                "SELECT id, user_id, amount, status FROM payments "
                "WHERE invoice_id=$1 FOR UPDATE",
                invoice_id
            )
            if not current or current["status"] == "paid":
                return False

            await con.execute(
                "UPDATE users SET balance=balance+$1 WHERE id=$2",
                float(current["amount"]), int(current["user_id"])
            )
            await con.execute(
                "UPDATE payments SET status='paid', paid_at=$1 WHERE invoice_id=$2",
                int(time.time()), invoice_id
            )
            await con.execute(
                "INSERT INTO transactions(user_id,type,amount,currency,external_id,created_at) "
                "VALUES($1,'topup',$2,$3,$4,$5) "
                "ON CONFLICT(external_id) DO NOTHING",
                int(current["user_id"]), float(current["amount"]),
                CRYPTOBOT_ASSET, str(invoice_id), int(time.time())
            )
    return True


async def payment_worker():
    """Poll Crypto Pay for our active invoices.
    Polling is used in Stage 2 so the Render setup does not need a webhook route.
    """
    if not CRYPTOBOT_TOKEN:
        logging.warning("CRYPTOBOT_TOKEN is not set; payment worker disabled.")
        return

    while True:
        try:
            rows = await pool.fetch(
                "SELECT invoice_id FROM payments WHERE status='active' "
                "ORDER BY created_at ASC LIMIT 100"
            )
            for row in rows:
                try:
                    result = await crypto_api(
                        "getInvoices",
                        {"invoice_ids": str(row["invoice_id"]), "count": 1}
                    )
                    invoices = result if isinstance(result, list) else []
                    if invoices and invoices[0].get("status") == "paid":
                        credited = await credit_paid_invoice(invoices[0])
                        if credited:
                            await safe_send(
                                bot_instance,
                                await pool.fetchval(
                                    "SELECT user_id FROM payments WHERE invoice_id=$1",
                                    row["invoice_id"]
                                ),
                                "✅ <b>Оплата получена!</b>\n"
                                "Ваш баланс автоматически пополнен."
                            )
                except Exception:
                    logging.exception(
                        "Payment check failed for invoice %s", row["invoice_id"]
                    )
                await asyncio.sleep(0.2)
        except Exception:
            logging.exception("Payment worker error")
        await asyncio.sleep(10)


# ---------------- запуск (веб-сервер для UptimeRobot + бот) ----------------
async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda req: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def main():
    global pool, bot_instance
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.info(
        "Starting bot | crypto=%s | asset=%s | port=%s",
        "testnet" if CRYPTOBOT_TESTNET else "mainnet",
        CRYPTOBOT_ASSET,
        PORT,
    )

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )
    try:
        await pool.execute(SCHEMA)
        await web_server()
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
        bot_instance = bot
        dp = Dispatcher()
        dp.include_router(r)
        asyncio.create_task(payment_worker())
        await dp.start_polling(bot)
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
