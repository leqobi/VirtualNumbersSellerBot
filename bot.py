import asyncio
import html
import logging
import math
import os
import re
import time
from datetime import datetime, timezone

import asyncpg
from aiohttp import ClientSession, ClientTimeout, web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


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

# Новые необязательные настройки (есть значения по умолчанию).
LOW_STOCK_THRESHOLD = env_int("LOW_STOCK_THRESHOLD", 3, 1)   # уведомление, если осталось меньше
ACTIVE_DAYS = env_int("ACTIVE_DAYS", 30, 1)                  # «активный» пользователь для рассылки
PAYMENT_POLL_SECONDS = env_int("PAYMENT_POLL_SECONDS", 10, 3)

MAX_PRICE = 10000.0
MAX_ADJUST = 100000.0
MAX_PHONES_PER_BATCH = 500

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

# Только добавления (ADD COLUMN IF NOT EXISTS / CREATE ... IF NOT EXISTS) —
# существующие данные не меняются и не удаляются.
MIGRATIONS = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active BIGINT;
ALTER TABLE lots ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS refunded_at BIGINT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS admin_id BIGINT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS pay_url TEXT;
ALTER TABLE msgmap ADD COLUMN IF NOT EXISTS request_id BIGINT;

CREATE TABLE IF NOT EXISTS sms_requests(
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'open',
    created_at BIGINT NOT NULL,
    closed_at BIGINT,
    closed_by BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_open_per_order ON sms_requests(order_id) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_sms_status ON sms_requests(status, id);
CREATE INDEX IF NOT EXISTS idx_numbers_buyer ON numbers(buyer);
CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(number_id);
CREATE INDEX IF NOT EXISTS idx_transactions_type_created ON transactions(type, created_at);
CREATE INDEX IF NOT EXISTS idx_msgmap_request ON msgmap(request_id);
CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active);

-- Старые продажи, у которых не было записи в orders (чтобы они попали в «Мои заказы»).
INSERT INTO orders(user_id, number_id, amount, status, created_at)
SELECT n.buyer, n.id, COALESCE(n.sold_price, 0), 'paid',
       COALESCE(n.sold_at, EXTRACT(EPOCH FROM NOW())::BIGINT)
FROM numbers n JOIN users u ON u.id = n.buyer
WHERE n.status = 'sold' AND n.buyer IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.number_id = n.id);
"""


# ---------------- логи без секретов ----------------
_SECRETS = [s for s in (BOT_TOKEN, CRYPTOBOT_TOKEN, DATABASE_URL) if s]


class RedactFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        for secret in _SECRETS:
            if secret in text:
                text = text.replace(secret, "***")
        return text


# ---------------- хелперы ----------------
def esc(s):
    return html.escape(str(s))


def money(x):
    return f"${float(x):.2f}"


def signed_money(x):
    x = float(x)
    return f"{'+' if x >= 0 else '-'}${abs(x):.2f}"


def fmt_ts(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def parse_amount(text, lo, hi, allow_sign=False):
    """Безопасный разбор суммы. None, если ввод некорректен."""
    try:
        v = float(str(text).replace(",", ".").strip())
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    v = round(v, 2)
    check = abs(v) if allow_sign else v
    if check < lo or check > hi:
        return None
    return v


PHONE_RE = re.compile(r"^\+?\d{6,15}$")


def normalize_phone(raw):
    s = re.sub(r"[\s\-()]", "", raw or "")
    return s if PHONE_RE.fullmatch(s) else None


def parse_phone_lines(text):
    """-> (валидные уникальные номера, невалидные строки)."""
    valid, invalid = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        p = normalize_phone(line)
        if p:
            valid.append(p)
        else:
            invalid.append(line)
    return list(dict.fromkeys(valid)), invalid


def clean_label(s, maxlen=40):
    s = " ".join((s or "").split())
    return s if 1 <= len(s) <= maxlen else None


def _btn(text, data):
    if data.startswith(("http://", "https://")):
        return InlineKeyboardButton(text=text, url=data)
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(t, d) for t, d in row] for row in rows])


def main_kb():
    return kb(
        [("📱 Купить номер", "buy")],
        [("📦 Мои заказы", "history"), ("💰 Пополнить баланс", "topup")],
        [("👤 Профиль", "profile"), ("🧮 Операции", "ops")],
        [("✉️ Написать создателю", "contact")],
    )


def back_kb():
    return kb([("⬅️ В меню", "menu")])


async def show(c: CallbackQuery, text, markup):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await c.message.answer(text, reply_markup=markup)
    try:
        await c.answer()
    except TelegramBadRequest:
        pass


async def safe_send(bot, uid, text, **kw):
    try:
        await bot.send_message(uid, text, **kw)
        return True
    except TelegramAPIError:
        return False


def int_arg(data, idx=1):
    try:
        return int(data.split(":")[idx])
    except (ValueError, IndexError):
        return None


STATUS_LABELS = {"paid": "✅ Оплачен", "refunded": "↩️ Возврат"}
PAY_LABELS = {"active": "⏳ Ожидает", "paid": "✅ Оплачен", "expired": "⌛ Истёк"}
TX_LABELS = {
    "topup": "Пополнение",
    "purchase": "Покупка",
    "refund": "Возврат",
    "admin_credit": "Начисление админом",
    "admin_debit": "Списание админом",
}


class Contact(StatesGroup):
    text = State()


r = Router()
ADMIN = F.from_user.id.in_(ADMINS)
TEXT = F.text & ~F.text.startswith("/")


# ---------------- доступ: бан + активность ----------------
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        now = int(time.time())
        row = await pool.fetchrow(
            "UPDATE users SET last_active=$2 WHERE id=$1 RETURNING banned", user.id, now)
        if row is None:
            await pool.execute(
                "INSERT INTO users(id, username, name, joined, last_active) VALUES($1,$2,$3,$4,$4) "
                "ON CONFLICT (id) DO NOTHING", user.id, user.username, user.full_name, now)
        elif row["banned"] and user.id not in ADMINS:
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Доступ ограничен.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🚫 Доступ ограничен.")
            return None
        return await handler(event, data)


r.message.outer_middleware(AccessMiddleware())
r.callback_query.outer_middleware(AccessMiddleware())


@r.error()
async def on_error(event: ErrorEvent):
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc):
        return True
    logging.error("Unhandled error in handler", exc_info=exc)
    upd = event.update
    try:
        if upd.callback_query:
            await upd.callback_query.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
        elif upd.message:
            await upd.message.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.")
    except Exception:
        pass
    return True


# ---------------- старт / меню / профиль ----------------
@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    now = int(time.time())
    await pool.execute(
        "INSERT INTO users(id, username, name, joined, last_active) VALUES($1,$2,$3,$4,$4) "
        "ON CONFLICT (id) DO UPDATE SET username=$2, name=$3",
        u.id, u.username, u.full_name, now)
    await m.answer("👋 Добро пожаловать! Здесь можно купить виртуальный номер.", reply_markup=main_kb())


@r.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=main_kb())


@r.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "Главное меню:", main_kb())


@r.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    uid = c.from_user.id
    bal = await pool.fetchval("SELECT balance FROM users WHERE id=$1", uid) or 0
    n = await pool.fetchval("SELECT COUNT(*) FROM orders WHERE user_id=$1 AND status='paid'", uid)
    await show(
        c,
        f"👤 Профиль\nID: <code>{uid}</code>\n"
        f"Баланс: <b>{money(bal)}</b>\nКуплено номеров: {n}",
        kb([("💰 Пополнить", "topup"), ("🧮 Операции", "ops")],
           [("📦 Мои заказы", "history")],
           [("⬅️ В меню", "menu")]),
    )


# ---------------- история операций ----------------
@r.callback_query(F.data == "ops")
async def ops(c: CallbackQuery):
    uid = c.from_user.id
    bal = await pool.fetchval("SELECT balance FROM users WHERE id=$1", uid) or 0
    txs = await pool.fetch(
        "SELECT type, amount, created_at FROM transactions WHERE user_id=$1 "
        "ORDER BY id DESC LIMIT 15", uid)
    pays = await pool.fetch(
        "SELECT amount, asset, status, created_at FROM payments WHERE user_id=$1 "
        "ORDER BY id DESC LIMIT 5", uid)
    parts = [f"🧮 <b>Операции</b>\nБаланс: <b>{money(bal)}</b>"]
    if txs:
        parts.append("\n<b>Последние операции:</b>\n" + "\n".join(
            f"{signed_money(x['amount'])} — {TX_LABELS.get(x['type'], esc(x['type']))}, {fmt_ts(x['created_at'])}"
            for x in txs))
    else:
        parts.append("\nОпераций пока нет.")
    if pays:
        parts.append("\n<b>Счета на пополнение:</b>\n" + "\n".join(
            f"{money(x['amount'])} {esc(x['asset'])} — {PAY_LABELS.get(x['status'], esc(x['status']))}, "
            f"{fmt_ts(x['created_at'])}" for x in pays))
    await show(c, "\n".join(parts), kb([("💰 Пополнить", "topup")], [("⬅️ В меню", "menu")]))


# ---------------- оплата (Crypto Pay) ----------------
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
    async with ClientSession(timeout=ClientTimeout(total=20)) as session:
        async with session.post(
            f"{CRYPTOBOT_API}/{method}", json=payload or {}, headers=headers
        ) as resp:
            data = await resp.json(content_type=None)
    if not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error") if isinstance(data, dict) else data
        raise RuntimeError(f"Crypto Pay {method} failed: {err}")
    return data["result"]


def invoices_from(result):
    """getInvoices возвращает список или {'items': [...]} — поддерживаем оба варианта."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return result["items"]
    return []


def invoice_url(invoice):
    return invoice.get("bot_invoice_url") or invoice.get("pay_url") or ""


@r.callback_query(F.data == "topup")
async def topup(c: CallbackQuery):
    if not CRYPTOBOT_TOKEN:
        return await show(c, "⚠️ Оплата пока не настроена владельцем.", back_kb())
    mode = "TESTNET" if CRYPTOBOT_TESTNET else "MAINNET"
    await show(
        c,
        f"💰 <b>Пополнение баланса</b>\n"
        f"Выберите сумму.\n\n"
        f"Сеть оплаты: <b>{mode}</b>\n"
        f"Валюта: <b>{esc(CRYPTOBOT_ASSET)}</b>",
        topup_amount_kb()
    )


@r.callback_query(F.data.startswith("topup:"))
async def create_topup_invoice(c: CallbackQuery):
    if not CRYPTOBOT_TOKEN:
        return await show(c, "⚠️ Crypto Pay не настроен.", back_kb())

    try:
        amount = float(c.data.split(":")[1])
        if not math.isfinite(amount) or amount <= 0 or amount > MAX_PRICE:
            raise ValueError
    except (ValueError, IndexError):
        return await c.answer("Некорректная сумма", show_alert=True)

    uid = c.from_user.id
    pending = await pool.fetchval(
        "SELECT COUNT(*) FROM payments WHERE user_id=$1 AND status='active' AND created_at>$2",
        uid, int(time.time()) - 1800)
    if pending >= 5:
        return await c.answer(
            "Слишком много неоплаченных счетов. Оплатите или дождитесь истечения.", show_alert=True)

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
        url = invoice_url(invoice)
        if not url:
            raise RuntimeError("Crypto Pay returned no invoice URL")
        await pool.execute(
            "INSERT INTO payments(invoice_id,user_id,amount,asset,status,created_at,pay_url) "
            "VALUES($1,$2,$3,$4,'active',$5,$6) "
            "ON CONFLICT(invoice_id) DO NOTHING",
            int(invoice["invoice_id"]), uid, amount, CRYPTOBOT_ASSET, int(time.time()), url
        )
    except Exception:
        logging.exception("Crypto Pay createInvoice failed")
        return await show(c, "❌ Не удалось создать счёт. Попробуйте ещё раз позже.", back_kb())

    await show(
        c,
        f"💳 <b>Счёт создан</b>\n\n"
        f"Сумма: <b>${amount:.2f}</b>\n"
        f"Срок: 30 минут\n\n"
        f"После оплаты баланс обновится автоматически.",
        kb(
            [("💳 Оплатить", url)],
            [("🔄 Проверить оплату", f"checkpay:{invoice['invoice_id']}")],
            [("⬅️ Назад", "topup")]
        )
    )


@r.callback_query(F.data.startswith("payurl:"))
async def payurl_legacy(c: CallbackQuery):
    """Кнопка из старых сообщений: достаём ссылку на счёт и присылаем её кнопкой."""
    invoice_id = int_arg(c.data)
    row = None
    if invoice_id is not None:
        row = await pool.fetchrow(
            "SELECT pay_url, status FROM payments WHERE invoice_id=$1 AND user_id=$2",
            invoice_id, c.from_user.id)
    if not row:
        return await c.answer("Счёт не найден.", show_alert=True)
    if row["status"] != "active":
        return await c.answer("Этот счёт уже неактивен.", show_alert=True)
    url = row["pay_url"]
    if not url:
        try:
            items = invoices_from(await crypto_api(
                "getInvoices", {"invoice_ids": str(invoice_id), "count": 1}))
            url = invoice_url(items[0]) if items else ""
        except Exception:
            logging.exception("payurl lookup failed")
    if not url:
        return await c.answer("Не удалось получить ссылку. Создайте новый счёт.", show_alert=True)
    await c.message.answer("💳 Оплата счёта:", reply_markup=kb([("💳 Оплатить", url)]))
    await c.answer()


@r.callback_query(F.data.startswith("checkpay:"))
async def check_topup(c: CallbackQuery):
    try:
        invoice_id = int(c.data.split(":")[1])
        own = await pool.fetchval(
            "SELECT 1 FROM payments WHERE invoice_id=$1 AND user_id=$2", invoice_id, c.from_user.id)
        if not own:
            return await c.answer("Счёт не найден", show_alert=True)
        items = invoices_from(await crypto_api(
            "getInvoices", {"invoice_ids": str(invoice_id), "count": 1}))
        if not items:
            return await c.answer("Счёт не найден", show_alert=True)
        invoice = items[0]
        status = invoice.get("status")
        if status == "expired":
            await mark_expired(invoice_id)
            return await show(c, "⌛ Срок счёта истёк. Создайте новый.", kb([("💰 Пополнить", "topup")]))
        if status != "paid":
            return await c.answer("Оплата ещё не подтверждена.", show_alert=True)
        credited = await credit_paid_invoice(invoice)
        if credited:
            text = ("✅ <b>Оплата подтверждена!</b>\n"
                    "Баланс пополнен. Откройте профиль, чтобы увидеть новый баланс.")
        else:
            text = "✅ Этот счёт уже оплачен и зачислен."
        await show(c, text, back_kb())
    except Exception:
        logging.exception("Manual payment check failed")
        await c.answer("Не удалось проверить оплату.", show_alert=True)


async def mark_expired(invoice_id):
    await pool.execute(
        "UPDATE payments SET status='expired' WHERE invoice_id=$1 AND status='active'", invoice_id)


async def credit_paid_invoice(invoice):
    """Atomically credit one paid invoice exactly once. Returns user_id if credited now, else None."""
    invoice_id = int(invoice["invoice_id"])
    # Зачисляем только сумму, сохранённую у нас в БД. Блокировка строки и все
    # обновления — в одной транзакции, чтобы повторная проверка не начислила дважды.
    async with pool.acquire() as con:
        async with con.transaction():
            current = await con.fetchrow(
                "SELECT id, user_id, amount, status FROM payments "
                "WHERE invoice_id=$1 FOR UPDATE",
                invoice_id
            )
            if not current or current["status"] == "paid":
                return None
            uid = int(current["user_id"])
            amount = float(current["amount"])
            now = int(time.time())
            await con.execute("UPDATE users SET balance=balance+$1 WHERE id=$2", amount, uid)
            await con.execute(
                "UPDATE payments SET status='paid', paid_at=$1 WHERE invoice_id=$2", now, invoice_id)
            await con.execute(
                "INSERT INTO transactions(user_id,type,amount,currency,external_id,created_at) "
                "VALUES($1,'topup',$2,$3,$4,$5) ON CONFLICT(external_id) DO NOTHING",
                uid, amount, CRYPTOBOT_ASSET, str(invoice_id), now
            )
    return uid


async def payment_worker():
    """Polling Crypto Pay: один запрос на пачку активных счетов."""
    if not CRYPTOBOT_TOKEN:
        logging.warning("CRYPTOBOT_TOKEN is not set; payment worker disabled.")
        return

    while True:
        try:
            rows = await pool.fetch(
                "SELECT invoice_id, created_at FROM payments WHERE status='active' "
                "ORDER BY created_at ASC LIMIT 100"
            )
            if rows:
                ids = [x["invoice_id"] for x in rows]
                result = await crypto_api(
                    "getInvoices", {"invoice_ids": ",".join(map(str, ids)), "count": len(ids)})
                found = {}
                for inv in invoices_from(result):
                    try:
                        found[int(inv["invoice_id"])] = inv
                    except (KeyError, TypeError, ValueError):
                        continue
                now = int(time.time())
                for row in rows:
                    inv = found.get(row["invoice_id"])
                    try:
                        if inv is None:
                            if now - row["created_at"] > 3 * 86400:
                                await mark_expired(row["invoice_id"])
                            continue
                        status = inv.get("status")
                        if status == "paid":
                            uid = await credit_paid_invoice(inv)
                            if uid:
                                await safe_send(
                                    bot_instance, uid,
                                    "✅ <b>Оплата получена!</b>\nВаш баланс автоматически пополнен.")
                        elif status == "expired":
                            await mark_expired(row["invoice_id"])
                    except Exception:
                        logging.exception("Payment handling failed for invoice %s", row["invoice_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Payment worker error")
        await asyncio.sleep(PAYMENT_POLL_SECONDS)


# ---------------- заказы (пользователь) ----------------
ORDERS_PAGE = 5


async def orders_view(uid, page):
    total = await pool.fetchval("SELECT COUNT(*) FROM orders WHERE user_id=$1", uid)
    if not total:
        return "🧾 Заказов пока нет.", back_kb()
    pages = max(1, (total + ORDERS_PAGE - 1) // ORDERS_PAGE)
    page = min(max(page, 0), pages - 1)
    rows = await pool.fetch(
        "SELECT o.id, o.amount, o.status, o.created_at, n.phone, l.service, co.name country "
        "FROM orders o JOIN numbers n ON n.id=o.number_id JOIN lots l ON l.id=n.lot_id "
        "JOIN countries co ON co.id=l.country_id "
        "WHERE o.user_id=$1 ORDER BY o.id DESC LIMIT $2 OFFSET $3",
        uid, ORDERS_PAGE, page * ORDERS_PAGE)
    lines, btns = [], []
    for x in rows:
        lines.append(
            f"<b>#{x['id']}</b> · {esc(x['country'])} / {esc(x['service'])}\n"
            f"<code>{esc(x['phone'])}</code> · {money(x['amount'])} · "
            f"{STATUS_LABELS.get(x['status'], esc(x['status']))}\n{fmt_ts(x['created_at'])}")
        btns.append([(f"#{x['id']} · {x['phone']}", f"order:{x['id']}")])
    nav = []
    if page > 0:
        nav.append(("⬅️", f"hist:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"hist:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([("⬅️ В меню", "menu")])
    return f"📦 <b>Мои заказы</b> (стр. {page + 1}/{pages})\n\n" + "\n\n".join(lines), kb(*btns)


@r.callback_query(F.data == "history")
async def history(c: CallbackQuery):
    text, markup = await orders_view(c.from_user.id, 0)
    await show(c, text, markup)


@r.callback_query(F.data.startswith("hist:"))
async def history_page(c: CallbackQuery):
    page = int_arg(c.data) or 0
    text, markup = await orders_view(c.from_user.id, page)
    await show(c, text, markup)


@r.message(Command("orders"))
async def orders_cmd(m: Message):
    text, markup = await orders_view(m.from_user.id, 0)
    await m.answer(text, reply_markup=markup)


async def order_row(oid):
    return await pool.fetchrow(
        "SELECT o.id, o.user_id, o.amount, o.status, o.created_at, o.refunded_at, "
        "n.phone, l.service, co.name country, u.username, u.name uname "
        "FROM orders o JOIN numbers n ON n.id=o.number_id JOIN lots l ON l.id=n.lot_id "
        "JOIN countries co ON co.id=l.country_id LEFT JOIN users u ON u.id=o.user_id "
        "WHERE o.id=$1", oid)


@r.callback_query(F.data.startswith("order:"))
async def order_card(c: CallbackQuery):
    oid = int_arg(c.data)
    o = await order_row(oid) if oid is not None else None
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Заказ не найден", show_alert=True)
    open_req = await pool.fetchval(
        "SELECT id FROM sms_requests WHERE order_id=$1 AND status='open'", oid)
    text = (f"🧾 <b>Заказ #{o['id']}</b>\n"
            f"🌍 {esc(o['country'])}\n📲 {esc(o['service'])}\n"
            f"📱 <code>{esc(o['phone'])}</code>\n"
            f"💵 {money(o['amount'])}\n"
            f"Статус: {STATUS_LABELS.get(o['status'], esc(o['status']))}\n"
            f"Создан: {fmt_ts(o['created_at'])}")
    rows = []
    if o["status"] == "paid":
        if open_req:
            text += "\n\n📩 Запрос SMS отправлен, ожидайте ответ."
        else:
            rows.append([("📩 Запросить SMS", f"smsreq:{oid}")])
    rows.append([("⬅️ К заказам", "history")])
    await show(c, text, kb(*rows))


# ---------------- запрос SMS (ответ админа вручную) ----------------
@r.callback_query(F.data.startswith("smsreq:"))
async def sms_request(c: CallbackQuery, bot: Bot):
    oid = int_arg(c.data)
    o = await order_row(oid) if oid is not None else None
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Заказ не найден", show_alert=True)
    if o["status"] != "paid":
        return await c.answer("По этому заказу запрос недоступен.", show_alert=True)
    try:
        req_id = await pool.fetchval(
            "INSERT INTO sms_requests(order_id, user_id, created_at) VALUES($1,$2,$3) RETURNING id",
            oid, o["user_id"], int(time.time()))
    except asyncpg.UniqueViolationError:
        return await c.answer("Запрос уже отправлен. Ожидайте ответ администратора.", show_alert=True)

    uname = f"@{o['username']}" if o["username"] else "без username"
    text = (f"📩 <b>Запрос SMS #{req_id}</b>\n"
            f"Заказ: <b>#{oid}</b>\n"
            f"Пользователь: {esc(o['uname'] or '—')} ({esc(uname)})\n"
            f"ID: <code>{o['user_id']}</code>\n"
            f"Номер: <code>{esc(o['phone'])}</code>\n"
            f"Страна: {esc(o['country'])}\nСервис: {esc(o['service'])}\n\n"
            f"<i>Ответьте реплаем на это сообщение — текст уйдёт пользователю.</i>")
    delivered = 0
    for a in ADMINS:
        try:
            sent = await bot.send_message(
                a, text, reply_markup=kb([("✅ Закрыть запрос", f"smsclose:{req_id}")]))
            await pool.execute(
                "INSERT INTO msgmap(admin_id, msg_id, user_id, request_id) VALUES($1,$2,$3,$4) "
                "ON CONFLICT DO NOTHING", a, sent.message_id, o["user_id"], req_id)
            delivered += 1
        except TelegramAPIError:
            logging.warning("Could not deliver SMS request %s to admin %s", req_id, a)
    if not delivered:
        await pool.execute("DELETE FROM sms_requests WHERE id=$1", req_id)
        return await c.answer("Не удалось отправить запрос. Попробуйте позже.", show_alert=True)
    await show(
        c,
        f"📩 Запрос по заказу <b>#{oid}</b> отправлен администратору.\n"
        f"Ответ придёт в этот чат.",
        kb([("📦 Мои заказы", "history")], [("⬅️ В меню", "menu")]))


async def close_sms_request(req_id, admin_id, bot):
    row = await pool.fetchrow(
        "UPDATE sms_requests SET status='closed', closed_at=$2, closed_by=$3 "
        "WHERE id=$1 AND status='open' RETURNING user_id, order_id",
        req_id, int(time.time()), admin_id)
    if not row:
        return False
    await safe_send(bot, row["user_id"], f"✅ Запрос SMS по заказу #{row['order_id']} закрыт.")
    return True


@r.callback_query(F.data.startswith("smsclose:"), ADMIN)
async def sms_close(c: CallbackQuery, bot: Bot):
    req_id = int_arg(c.data)
    if req_id is None or not await close_sms_request(req_id, c.from_user.id, bot):
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return await c.answer("Запрос уже закрыт.", show_alert=True)
    try:
        await c.message.edit_text((c.message.html_text or "") + "\n\n✅ <b>Запрос закрыт</b>",
                                  reply_markup=None)
    except TelegramBadRequest:
        pass
    await c.answer("Закрыто")


@r.callback_query(F.data.startswith("adm:smsc:"), ADMIN)
async def admin_sms_close_from_list(c: CallbackQuery, bot: Bot):
    req_id = int_arg(c.data, 2)
    if req_id is not None:
        await close_sms_request(req_id, c.from_user.id, bot)
    await admin_sms_list(c)


@r.callback_query(F.data == "adm:sms", ADMIN)
async def admin_sms_list(c: CallbackQuery):
    rows = await pool.fetch(
        "SELECT s.id, s.order_id, s.user_id, s.created_at, n.phone FROM sms_requests s "
        "JOIN orders o ON o.id=s.order_id JOIN numbers n ON n.id=o.number_id "
        "WHERE s.status='open' ORDER BY s.id LIMIT 10")
    if not rows:
        return await show(c, "📩 Открытых запросов SMS нет.", admin_kb())
    lines = [f"#{x['id']} · заказ #{x['order_id']} · <code>{esc(x['phone'])}</code> · "
             f"user <code>{x['user_id']}</code> · {fmt_ts(x['created_at'])}" for x in rows]
    btns = [[(f"✅ Закрыть #{x['id']}", f"adm:smsc:{x['id']}")] for x in rows]
    btns.append([("⬅️ Админ-панель", "adm:panel")])
    await show(c, "📩 <b>Открытые запросы SMS:</b>\n\n" + "\n".join(lines)
               + "\n\n<i>Отвечайте реплаем на исходное сообщение запроса.</i>", kb(*btns))


# ---------------- покупка: страна -> сервис -> подтверждение ----------------
@r.callback_query(F.data == "buy")
async def buy_countries(c: CallbackQuery):
    rows = await pool.fetch(
        "SELECT co.id, co.name, COUNT(n.id) cnt FROM countries co "
        "JOIN lots l ON l.country_id=co.id AND l.active "
        "JOIN numbers n ON n.lot_id=l.id AND n.status='free' "
        "GROUP BY co.id, co.name ORDER BY co.name")
    if not rows:
        return await show(c, "😔 Сейчас номеров нет. Загляните позже.", back_kb())
    btns = [[(f"🌍 {x['name']} ({x['cnt']})", f"ct:{x['id']}")] for x in rows]
    btns.append([("⬅️ В меню", "menu")])
    await show(c, "Выберите страну:", kb(*btns))


@r.callback_query(F.data.startswith("ct:"))
async def buy_services(c: CallbackQuery):
    cid = int_arg(c.data)
    if cid is None:
        return await c.answer()
    rows = await pool.fetch(
        "SELECT l.id, l.service, l.price, COUNT(n.id) cnt FROM lots l "
        "JOIN numbers n ON n.lot_id=l.id AND n.status='free' "
        "WHERE l.country_id=$1 AND l.active GROUP BY l.id ORDER BY l.service", cid)
    if not rows:
        return await show(c, "Номера закончились.", kb([("⬅️ Назад", "buy")]))
    btns = [[(f"{x['service']} — ${x['price']:.2f} ({x['cnt']})", f"lot:{x['id']}")] for x in rows]
    btns.append([("⬅️ Назад", "buy")])
    await show(c, "Выберите сервис:", kb(*btns))


@r.callback_query(F.data.startswith("lot:"))
async def confirm(c: CallbackQuery):
    lid = int_arg(c.data)
    if lid is None:
        return await c.answer()
    l = await pool.fetchrow("SELECT l.*, co.name country FROM lots l JOIN countries co ON co.id=l.country_id "
                            "WHERE l.id=$1", lid)
    if not l or not l["active"]:
        return await show(c, "Этот товар сейчас недоступен.", kb([("⬅️ Назад", "buy")]))
    await show(c, f"Купить номер?\n🌍 {esc(l['country'])}\n📲 {esc(l['service'])}\n💵 ${l['price']:.2f}",
               kb([("✅ Купить", f"buy:{lid}")], [("⬅️ Назад", f"ct:{l['country_id']}")]))


class NoStock(Exception):
    pass


class NoMoney(Exception):
    pass


_buying = set()  # защита от двойного нажатия «Купить» (в рамках процесса)


@r.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery, bot: Bot):
    lid = int_arg(c.data)
    if lid is None:
        return await c.answer()
    uid = c.from_user.id
    if uid in _buying:
        return await c.answer("⏳ Покупка уже обрабатывается…")
    _buying.add(uid)
    try:
        async with pool.acquire() as con:
            async with con.transaction():
                # Блокируем строку пользователя — параллельные покупки одного человека идут по очереди.
                urow = await con.fetchrow("SELECT balance FROM users WHERE id=$1 FOR UPDATE", uid)
                if not urow:
                    raise NoMoney
                l = await con.fetchrow("SELECT l.*, co.name country FROM lots l "
                                       "JOIN countries co ON co.id=l.country_id WHERE l.id=$1", lid)
                if not l or not l["active"]:
                    raise NoStock
                now = int(time.time())
                num = await con.fetchrow(
                    "UPDATE numbers SET status='sold', buyer=$1, sold_at=$2, sold_price=$3 WHERE id=("
                    "SELECT id FROM numbers WHERE lot_id=$4 AND status='free' ORDER BY id LIMIT 1 "
                    "FOR UPDATE SKIP LOCKED) RETURNING id, phone", uid, now, l["price"], lid)
                if not num:
                    raise NoStock
                res = await con.execute("UPDATE users SET balance=balance-$1 WHERE id=$2 AND balance>=$1",
                                        l["price"], uid)
                if res != "UPDATE 1":
                    raise NoMoney  # откат: номер снова свободен
                order_id = await con.fetchval(
                    "INSERT INTO orders(user_id, number_id, amount, status, created_at) "
                    "VALUES($1,$2,$3,'paid',$4) RETURNING id",
                    uid, num["id"], l["price"], now
                )
                await con.execute(
                    "INSERT INTO transactions(user_id, type, amount, currency, external_id, created_at) "
                    "VALUES($1,'purchase',$2,'USD',$3,$4)",
                    uid, -float(l["price"]), f"purchase:{order_id}", now
                )
                left = await con.fetchval(
                    "SELECT COUNT(*) FROM numbers WHERE lot_id=$1 AND status='free'", lid)
    except NoStock:
        return await show(c, "😔 Номера закончились.", kb([("⬅️ Назад", "buy")]))
    except NoMoney:
        return await show(c, "❌ Недостаточно средств.", kb([("💰 Пополнить", "topup")], [("⬅️ В меню", "menu")]))
    finally:
        _buying.discard(uid)

    await show(c, f"✅ Ваш номер: <code>{esc(num['phone'])}</code>\n"
                  f"{esc(l['service'])}, {esc(l['country'])}\n"
                  f"Заказ: <b>#{order_id}</b>\n\n"
                  f"Нужен код из SMS? Нажмите кнопку ниже — запрос уйдёт администратору.",
               kb([("📩 Запросить SMS", f"smsreq:{order_id}")],
                  [("📦 Мои заказы", "history"), ("⬅️ В меню", "menu")]))
    for a in ADMINS:
        await safe_send(bot, a, f"🛒 Заказ #{order_id}: куплен номер <code>{esc(num['phone'])}</code> "
                                f"({esc(l['service'])}, {esc(l['country'])}) — "
                                f"{esc(c.from_user.full_name)}, id <code>{uid}</code>")
    if left < LOW_STOCK_THRESHOLD:
        for a in ADMINS:
            await safe_send(bot, a, f"⚠️ Мало номеров: {esc(l['country'])} / {esc(l['service'])} — "
                                    f"осталось {left} шт.")


# ---------------- обращение к создателю (поддержка) ----------------
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
            await pool.execute(
                "INSERT INTO msgmap(admin_id, msg_id, user_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                a, sent.message_id, u.id)
        except Exception:
            pass
    await m.answer("✅ Отправлено! Ответ придёт сюда.", reply_markup=main_kb())


# ---------------- ответы админа ----------------
@r.message(Command("reply"), ADMIN)
async def reply_cmd(m: Message, bot: Bot):
    p = (m.text or "").split(maxsplit=2)
    if len(p) < 3 or not p[1].isdigit():
        return await m.answer("Формат: /reply user_id текст")
    ok = await safe_send(bot, int(p[1]), f"💬 <b>Ответ создателя:</b>\n\n{esc(p[2])}")
    await m.answer("✅ Доставлено" if ok else "❌ Не доставлено")


@r.message(F.reply_to_message, ADMIN, F.text, ~F.text.startswith("/"))
async def reply_by_reply(m: Message, bot: Bot):
    row = await pool.fetchrow(
        "SELECT user_id, request_id FROM msgmap WHERE admin_id=$1 AND msg_id=$2",
        m.from_user.id, m.reply_to_message.message_id)
    if not row:
        return
    if row["request_id"]:
        req = await pool.fetchrow("SELECT status, order_id FROM sms_requests WHERE id=$1", row["request_id"])
        if not req or req["status"] != "open":
            return await m.answer("⚠️ Этот запрос уже закрыт — сообщение не отправлено.")
        text = f"📩 <b>Ответ по заказу #{req['order_id']}:</b>\n\n{esc(m.text)}"
    else:
        text = f"💬 <b>Ответ создателя:</b>\n\n{esc(m.text)}"
    ok = await safe_send(bot, row["user_id"], text)
    await m.answer("✅ Доставлено" if ok else "❌ Не доставлено")


# ---------------- рассылка ----------------
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
            "<code>/broadcast active текст</code> — только активным "
            f"(за {ACTIVE_DAYS} дн.)\nили ответьте /broadcast на готовое сообщение.")
    if active_only:
        rows = await pool.fetch(
            "SELECT id FROM users WHERE NOT banned AND COALESCE(last_active, joined, 0) >= $1",
            int(time.time()) - ACTIVE_DAYS * 86400)
    else:
        rows = await pool.fetch("SELECT id FROM users WHERE NOT banned")
    users = [x["id"] for x in rows]
    await m.answer(f"📤 Рассылка на {len(users)} пользователей"
                   f"{' (активные)' if active_only else ''}…")

    async def send(uid):
        if src:
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
    await m.answer(f"✅ Доставлено: {ok}\n🚫 Бот заблокирован: {blocked}\n❌ Ошибки: {fail}")


# ---------------- статистика ----------------
async def stats_text():
    now = int(time.time())
    day0 = now - now % 86400  # начало суток UTC
    week, month = now - 7 * 86400, now - 30 * 86400
    users = await pool.fetchval("SELECT COUNT(*) FROM users")
    banned = await pool.fetchval("SELECT COUNT(*) FROM users WHERE banned")
    s = await pool.fetchrow(
        "SELECT "
        "COUNT(*) FILTER (WHERE status='paid') n, "
        "COALESCE(SUM(amount) FILTER (WHERE status='paid'),0) s, "
        "COUNT(*) FILTER (WHERE status='paid' AND created_at>=$1) nd, "
        "COALESCE(SUM(amount) FILTER (WHERE status='paid' AND created_at>=$1),0) sd, "
        "COUNT(*) FILTER (WHERE status='paid' AND created_at>=$2) nw, "
        "COALESCE(SUM(amount) FILTER (WHERE status='paid' AND created_at>=$2),0) sw, "
        "COUNT(*) FILTER (WHERE status='paid' AND created_at>=$3) nm, "
        "COALESCE(SUM(amount) FILTER (WHERE status='paid' AND created_at>=$3),0) sm, "
        "COUNT(*) FILTER (WHERE status='refunded') nr, "
        "COALESCE(SUM(amount) FILTER (WHERE status='refunded'),0) sr "
        "FROM orders", day0, week, month)
    t = await pool.fetchrow(
        "SELECT COALESCE(SUM(amount),0) s, "
        "COALESCE(SUM(amount) FILTER (WHERE created_at>=$1),0) sd, "
        "COALESCE(SUM(amount) FILTER (WHERE created_at>=$2),0) sw, "
        "COALESCE(SUM(amount) FILTER (WHERE created_at>=$3),0) sm "
        "FROM transactions WHERE type='topup'", day0, week, month)
    free = await pool.fetchval("SELECT COUNT(*) FROM numbers WHERE status='free'")
    open_sms = await pool.fetchval("SELECT COUNT(*) FROM sms_requests WHERE status='open'")
    return (
        f"📊 <b>Статистика</b>\n"
        f"👥 Пользователей: {users} (заблокировано: {banned})\n"
        f"📦 Свободных номеров: {free}\n"
        f"📩 Открытых запросов SMS: {open_sms}\n\n"
        f"<b>Продажи (оплаченные заказы)</b>\n"
        f"Всего: {s['n']} на {money(s['s'])}\n"
        f"Сегодня (UTC): {s['nd']} на {money(s['sd'])}\n"
        f"7 дней: {s['nw']} на {money(s['sw'])}\n"
        f"30 дней: {s['nm']} на {money(s['sm'])}\n"
        f"Возвраты: {s['nr']} на {money(s['sr'])}\n\n"
        f"<b>Пополнения</b>\n"
        f"Всего: {money(t['s'])}\n"
        f"Сегодня (UTC): {money(t['sd'])}\n"
        f"7 дней: {money(t['sw'])}\n"
        f"30 дней: {money(t['sm'])}"
    )


@r.message(Command("stats"), ADMIN)
async def stats(m: Message):
    await m.answer(await stats_text())


# ---------------- баланс (админ) ----------------
async def adjust_balance(admin_id, uid, amount, note):
    """Начисление/списание с записью в transactions. Возвращает новый баланс."""
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "UPDATE users SET balance=balance+$1 WHERE id=$2 AND balance+$1 >= -0.000000001 "
                "RETURNING balance", amount, uid)
            if row is None:
                exists = await con.fetchval("SELECT 1 FROM users WHERE id=$1", uid)
                raise ValueError("not_found" if not exists else "insufficient")
            await con.execute(
                "INSERT INTO transactions(user_id,type,amount,currency,created_at,note,admin_id) "
                "VALUES($1,$2,$3,'USD',$4,$5,$6)",
                uid, "admin_credit" if amount > 0 else "admin_debit", amount,
                int(time.time()), note, admin_id)
    return row["balance"]


async def apply_adjust(m: Message, bot: Bot, uid, amount, note):
    try:
        new_bal = await adjust_balance(m.from_user.id, uid, amount, note)
    except ValueError as e:
        if str(e) == "not_found":
            return False, "❌ Пользователь не найден (он должен нажать /start)"
        return False, "❌ Недостаточно средств на балансе для списания"
    await safe_send(bot, uid, f"💰 Баланс изменён: {signed_money(amount)}")
    return True, f"✅ Готово. Новый баланс: {money(new_bal)}"


@r.message(Command("addbalance"), ADMIN)
async def addbalance(m: Message, bot: Bot):
    p = (m.text or "").split(maxsplit=3)
    try:
        uid = int(p[1])
        amount = parse_amount(p[2], 0.01, MAX_ADJUST, allow_sign=True)
        if amount is None:
            raise ValueError
    except (IndexError, ValueError):
        return await m.answer("Формат: /addbalance user_id сумма [комментарий]\n"
                              "Отрицательная сумма — списание, например <code>/addbalance 123 -2.5</code>")
    note = p[3][:200] if len(p) > 3 else None
    _, text = await apply_adjust(m, bot, uid, amount, note)
    await m.answer(text)


# ---------------- админ-панель ----------------
class AddNumbers(StatesGroup):
    country = State()
    service = State()
    price = State()
    numbers = State()


class AddToLot(StatesGroup):
    numbers = State()


class DelNumbers(StatesGroup):
    numbers = State()


class EditLot(StatesGroup):
    price = State()
    country = State()
    service = State()


class UserSearch(StatesGroup):
    uid = State()


class BalanceAdj(StatesGroup):
    amount = State()


def admin_kb():
    return kb(
        [("📱 Добавить номера", "adm:add"), ("🛠 Товары", "adm:goods")],
        [("📦 Склад", "adm:stock"), ("📊 Статистика", "adm:stats")],
        [("👥 Пользователи", "adm:users"), ("📩 Запросы SMS", "adm:sms")],
        [("📢 Рассылка", "adm:broadcast")],
        [("⬅️ В меню", "menu")],
    )


@r.message(Command("admin"), ADMIN)
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_kb())


@r.callback_query(F.data == "adm:panel", ADMIN)
async def admin_panel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "⚙️ <b>Админ-панель</b>", admin_kb())


async def upsert_lot(con, country, service, price):
    cid = await con.fetchval(
        "INSERT INTO countries(name) VALUES($1) "
        "ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id", country)
    return await con.fetchval(
        "INSERT INTO lots(country_id, service, price) VALUES($1,$2,$3) "
        "ON CONFLICT (country_id, service) DO UPDATE SET price=EXCLUDED.price "
        "RETURNING id", cid, service, price)


async def add_numbers(con, lid, phones):
    """Добавляет номера. 'removed' номера возвращаются в продажу. -> число добавленных."""
    added = 0
    for phone in phones:
        row = await con.fetchval(
            "INSERT INTO numbers(lot_id, phone) VALUES($1,$2) "
            "ON CONFLICT (lot_id, phone) DO UPDATE SET status='free', buyer=NULL, sold_at=NULL, "
            "sold_price=NULL, code=NULL WHERE numbers.status='removed' RETURNING id", lid, phone)
        added += bool(row)
    return added


def invalid_note(invalid):
    if not invalid:
        return ""
    shown = ", ".join(esc(x[:20]) for x in invalid[:5])
    more = f" и ещё {len(invalid) - 5}" if len(invalid) > 5 else ""
    return f"\n⚠️ Пропущено неверных строк: {len(invalid)} ({shown}{more})"


# --- добавление номеров (мастер) ---
@r.callback_query(F.data == "adm:add", ADMIN)
async def admin_add_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddNumbers.country)
    await show(c, "🌍 Введите страну:", kb([("⬅️ Отмена", "adm:panel")]))


@r.message(AddNumbers.country, ADMIN, TEXT)
async def admin_add_country(m: Message, state: FSMContext):
    country = clean_label(m.text)
    if not country:
        return await m.answer("❌ Название страны: от 1 до 40 символов.")
    await state.update_data(country=country)
    await state.set_state(AddNumbers.service)
    await m.answer("📲 Введите сервис, например Telegram:")


@r.message(AddNumbers.service, ADMIN, TEXT)
async def admin_add_service(m: Message, state: FSMContext):
    service = clean_label(m.text)
    if not service:
        return await m.answer("❌ Название сервиса: от 1 до 40 символов.")
    await state.update_data(service=service)
    await state.set_state(AddNumbers.price)
    await m.answer("💵 Введите цену одного номера, например 0.60:")


@r.message(AddNumbers.price, ADMIN, TEXT)
async def admin_add_price(m: Message, state: FSMContext):
    price = parse_amount(m.text, 0.01, MAX_PRICE)
    if price is None:
        return await m.answer(f"❌ Цена должна быть числом от 0.01 до {MAX_PRICE:.0f}. Например: 0.60")
    await state.update_data(price=price)
    await state.set_state(AddNumbers.numbers)
    await m.answer(
        "📱 Отправьте номера одним сообщением, по одному на строку.\n\n"
        "Пример:\n<code>+79001112233\n+79001112234</code>"
    )


@r.message(AddNumbers.numbers, ADMIN, TEXT)
async def admin_add_numbers(m: Message, state: FSMContext):
    data = await state.get_data()
    phones, invalid = parse_phone_lines(m.text)
    if not phones:
        return await m.answer("❌ Не найдено ни одного корректного номера (6–15 цифр, можно с +).")
    if len(phones) > MAX_PHONES_PER_BATCH:
        return await m.answer(f"❌ Слишком много номеров за раз (максимум {MAX_PHONES_PER_BATCH}).")

    country, service, price = data["country"], data["service"], data["price"]
    async with pool.acquire() as con:
        async with con.transaction():
            lid = await upsert_lot(con, country, service, price)
            added = await add_numbers(con, lid, phones)

    await state.clear()
    await m.answer(
        f"✅ <b>Номера добавлены</b>\n"
        f"🌍 {esc(country)}\n📲 {esc(service)}\n"
        f"💵 ${price:.2f}\n"
        f"📱 Добавлено: {added} из {len(phones)}" + invalid_note(invalid),
        reply_markup=admin_kb()
    )


# Старый формат оставляем, чтобы уже привычная команда не сломалась.
@r.message(Command("addnumbers"), ADMIN)
async def addnumbers_legacy(m: Message):
    head, _, body = (m.text or "").partition("\n")
    parts = [x.strip() for x in head.partition(" ")[2].split("|")]
    usage = ("Формат:\n<code>/addnumbers Россия | Telegram | 0.6\n"
             "+79001112233\n+79001112234</code>")
    try:
        country = clean_label(parts[0])
        service = clean_label(parts[1])
        price = parse_amount(parts[2], 0.01, MAX_PRICE)
        if not (country and service and price):
            raise ValueError
    except (IndexError, ValueError):
        return await m.answer(usage)
    phones, invalid = [], []
    for token in body.split():
        p = normalize_phone(token)
        (phones if p else invalid).append(p or token)
    phones = list(dict.fromkeys(phones))
    if not phones:
        return await m.answer(usage)
    if len(phones) > MAX_PHONES_PER_BATCH:
        return await m.answer(f"❌ Слишком много номеров за раз (максимум {MAX_PHONES_PER_BATCH}).")
    async with pool.acquire() as con:
        async with con.transaction():
            lid = await upsert_lot(con, country, service, price)
            added = await add_numbers(con, lid, phones)
    await m.answer(
        f"✅ Добавлено: {added} из {len(phones)} "
        f"({esc(country)} / {esc(service)} / ${price:.2f})" + invalid_note(invalid)
    )


# --- склад ---
async def stock_text():
    rows = await pool.fetch(
        "SELECT co.name country, l.service, l.price, l.active, "
        "COUNT(n.id) FILTER (WHERE n.status='free') free "
        "FROM lots l JOIN countries co ON co.id=l.country_id "
        "LEFT JOIN numbers n ON n.lot_id=l.id "
        "GROUP BY l.id, co.name ORDER BY co.name, l.service")
    if not rows:
        return None
    return "📦 <b>Склад:</b>\n" + "\n".join(
        f"{'' if x['active'] else '⛔ '}{esc(x['country'])} / {esc(x['service'])} / "
        f"${x['price']:.2f} — {x['free']} шт." for x in rows)


@r.callback_query(F.data == "adm:stock", ADMIN)
async def admin_stock_button(c: CallbackQuery):
    text = await stock_text()
    await show(c, text or "📦 Склад пуст.", admin_kb())


@r.message(Command("stock"), ADMIN)
async def stock(m: Message):
    text = await stock_text()
    await m.answer(text or "Склад пуст. Добавьте: /addnumbers")


@r.callback_query(F.data == "adm:stats", ADMIN)
async def admin_stats_button(c: CallbackQuery):
    await show(c, await stats_text(), admin_kb())


@r.callback_query(F.data == "adm:broadcast", ADMIN)
async def admin_broadcast_button(c: CallbackQuery):
    await show(
        c,
        "📢 <b>Рассылка</b>\n"
        "<code>/broadcast текст</code> — всем\n"
        "<code>/broadcast active текст</code> — только активным\n"
        "Или ответьте <code>/broadcast</code> (или <code>/broadcast active</code>) на готовое сообщение.",
        admin_kb()
    )


# --- товары (лоты) ---
GOODS_PAGE = 8


@r.callback_query(F.data.startswith("adm:goods"), ADMIN)
async def admin_goods(c: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = c.data.split(":")
    try:
        page = max(0, int(parts[2])) if len(parts) > 2 else 0
    except ValueError:
        page = 0
    total = await pool.fetchval("SELECT COUNT(*) FROM lots")
    if not total:
        return await show(c, "🛠 Товаров пока нет. Добавьте номера.", admin_kb())
    pages = max(1, (total + GOODS_PAGE - 1) // GOODS_PAGE)
    page = min(page, pages - 1)
    rows = await pool.fetch(
        "SELECT l.id, l.service, l.price, l.active, co.name country, "
        "COUNT(n.id) FILTER (WHERE n.status='free') free "
        "FROM lots l JOIN countries co ON co.id=l.country_id "
        "LEFT JOIN numbers n ON n.lot_id=l.id "
        "GROUP BY l.id, co.name ORDER BY co.name, l.service LIMIT $1 OFFSET $2",
        GOODS_PAGE, page * GOODS_PAGE)
    btns = [[(f"{'✅' if x['active'] else '⛔'} {x['country']} / {x['service']} · "
              f"${x['price']:.2f} · {x['free']} шт.", f"adm:lot:{x['id']}")] for x in rows]
    nav = []
    if page > 0:
        nav.append(("⬅️", f"adm:goods:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"adm:goods:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([("📱 Добавить номера", "adm:add")])
    btns.append([("⬅️ Админ-панель", "adm:panel")])
    await show(c, f"🛠 <b>Товары</b> (стр. {page + 1}/{pages})\n✅ включён · ⛔ отключён", kb(*btns))


async def lot_card(lid):
    l = await pool.fetchrow(
        "SELECT l.id, l.service, l.price, l.active, co.name country, "
        "COUNT(n.id) FILTER (WHERE n.status='free') free, "
        "COUNT(n.id) FILTER (WHERE n.status='sold') sold "
        "FROM lots l JOIN countries co ON co.id=l.country_id "
        "LEFT JOIN numbers n ON n.lot_id=l.id WHERE l.id=$1 GROUP BY l.id, co.name", lid)
    if not l:
        return None
    text = (f"🛠 <b>Лот #{l['id']}</b>\n"
            f"🌍 {esc(l['country'])}\n📲 {esc(l['service'])}\n💵 ${l['price']:.2f}\n"
            f"Статус: {'✅ включён' if l['active'] else '⛔ отключён'}\n"
            f"📦 Свободно: {l['free']} · продано: {l['sold']}")
    markup = kb(
        [("⛔ Отключить" if l["active"] else "✅ Включить", f"adm:lt:{lid}")],
        [("💵 Цена", f"adm:lp:{lid}"), ("🌍 Страна", f"adm:lc:{lid}"), ("📲 Сервис", f"adm:ls:{lid}")],
        [("➕ Номера", f"adm:la:{lid}"), ("🗑 Удалить номера", f"adm:ld:{lid}")],
        [("📋 Список номеров", f"adm:ln:{lid}:0")],
        [("⬅️ К товарам", "adm:goods")],
    )
    return text, markup


@r.callback_query(F.data.startswith("adm:lot:"), ADMIN)
async def admin_lot(c: CallbackQuery, state: FSMContext):
    await state.clear()
    lid = int_arg(c.data, 2)
    card = await lot_card(lid) if lid is not None else None
    if not card:
        return await c.answer("Лот не найден", show_alert=True)
    await show(c, *card)


@r.callback_query(F.data.startswith("adm:lt:"), ADMIN)
async def admin_lot_toggle(c: CallbackQuery):
    lid = int_arg(c.data, 2)
    if lid is not None:
        await pool.execute("UPDATE lots SET active = NOT active WHERE id=$1", lid)
    card = await lot_card(lid) if lid is not None else None
    if not card:
        return await c.answer("Лот не найден", show_alert=True)
    await show(c, *card)


async def start_edit(c, state, st, lid, prompt):
    exists = lid is not None and await pool.fetchval("SELECT 1 FROM lots WHERE id=$1", lid)
    if not exists:
        return await c.answer("Лот не найден", show_alert=True)
    await state.clear()
    await state.update_data(lot_id=lid)
    await state.set_state(st)
    await show(c, prompt, kb([("⬅️ Отмена", f"adm:lot:{lid}")]))


@r.callback_query(F.data.startswith("adm:lp:"), ADMIN)
async def admin_lot_price_start(c: CallbackQuery, state: FSMContext):
    await start_edit(c, state, EditLot.price, int_arg(c.data, 2), "💵 Введите новую цену (например 0.60):")


@r.callback_query(F.data.startswith("adm:lc:"), ADMIN)
async def admin_lot_country_start(c: CallbackQuery, state: FSMContext):
    await start_edit(c, state, EditLot.country, int_arg(c.data, 2), "🌍 Введите новую страну:")


@r.callback_query(F.data.startswith("adm:ls:"), ADMIN)
async def admin_lot_service_start(c: CallbackQuery, state: FSMContext):
    await start_edit(c, state, EditLot.service, int_arg(c.data, 2), "📲 Введите новый сервис:")


async def finish_edit(m, state, lid, done_text):
    await state.clear()
    card = await lot_card(lid)
    if not card:
        return await m.answer("Лот не найден", reply_markup=admin_kb())
    await m.answer(done_text + "\n\n" + card[0], reply_markup=card[1])


@r.message(EditLot.price, ADMIN, TEXT)
async def admin_lot_price(m: Message, state: FSMContext):
    price = parse_amount(m.text, 0.01, MAX_PRICE)
    if price is None:
        return await m.answer(f"❌ Цена — число от 0.01 до {MAX_PRICE:.0f}.")
    lid = (await state.get_data()).get("lot_id")
    await pool.execute("UPDATE lots SET price=$1 WHERE id=$2", price, lid)
    await finish_edit(m, state, lid, "✅ Цена изменена (действует для новых покупок).")


@r.message(EditLot.country, ADMIN, TEXT)
async def admin_lot_country(m: Message, state: FSMContext):
    country = clean_label(m.text)
    if not country:
        return await m.answer("❌ Название страны: от 1 до 40 символов.")
    lid = (await state.get_data()).get("lot_id")
    try:
        async with pool.acquire() as con:
            async with con.transaction():
                cid = await con.fetchval(
                    "INSERT INTO countries(name) VALUES($1) "
                    "ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id", country)
                await con.execute("UPDATE lots SET country_id=$1 WHERE id=$2", cid, lid)
    except asyncpg.UniqueViolationError:
        return await m.answer("❌ Лот с такой страной и сервисом уже существует.")
    await finish_edit(m, state, lid, "✅ Страна изменена.")


@r.message(EditLot.service, ADMIN, TEXT)
async def admin_lot_service(m: Message, state: FSMContext):
    service = clean_label(m.text)
    if not service:
        return await m.answer("❌ Название сервиса: от 1 до 40 символов.")
    lid = (await state.get_data()).get("lot_id")
    try:
        await pool.execute("UPDATE lots SET service=$1 WHERE id=$2", service, lid)
    except asyncpg.UniqueViolationError:
        return await m.answer("❌ Лот с такой страной и сервисом уже существует.")
    await finish_edit(m, state, lid, "✅ Сервис изменён.")


@r.callback_query(F.data.startswith("adm:la:"), ADMIN)
async def admin_lot_add_start(c: CallbackQuery, state: FSMContext):
    await start_edit(c, state, AddToLot.numbers, int_arg(c.data, 2),
                     "📱 Отправьте номера одним сообщением, по одному на строку:")


@r.message(AddToLot.numbers, ADMIN, TEXT)
async def admin_lot_add_numbers(m: Message, state: FSMContext):
    phones, invalid = parse_phone_lines(m.text)
    if not phones:
        return await m.answer("❌ Не найдено ни одного корректного номера (6–15 цифр, можно с +).")
    if len(phones) > MAX_PHONES_PER_BATCH:
        return await m.answer(f"❌ Слишком много номеров за раз (максимум {MAX_PHONES_PER_BATCH}).")
    lid = (await state.get_data()).get("lot_id")
    async with pool.acquire() as con:
        async with con.transaction():
            added = await add_numbers(con, lid, phones)
    await finish_edit(m, state, lid, f"✅ Добавлено: {added} из {len(phones)}" + invalid_note(invalid))


@r.callback_query(F.data.startswith("adm:ld:"), ADMIN)
async def admin_lot_del_start(c: CallbackQuery, state: FSMContext):
    await start_edit(c, state, DelNumbers.numbers, int_arg(c.data, 2),
                     "🗑 Отправьте номера для удаления, по одному на строку.\n"
                     "Удаляются только свободные. Проданные остаются в истории заказов.")


@r.message(DelNumbers.numbers, ADMIN, TEXT)
async def admin_lot_del_numbers(m: Message, state: FSMContext):
    phones, invalid = parse_phone_lines(m.text)
    if not phones:
        return await m.answer("❌ Не найдено ни одного корректного номера.")
    lid = (await state.get_data()).get("lot_id")
    deleted = sold = missing = 0
    async with pool.acquire() as con:
        async with con.transaction():
            for p in phones:
                row = await con.fetchrow(
                    "SELECT id, status FROM numbers WHERE lot_id=$1 AND phone=$2 FOR UPDATE", lid, p)
                if not row or row["status"] == "removed":
                    missing += 1
                elif row["status"] != "free":
                    sold += 1
                else:
                    used = await con.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM orders WHERE number_id=$1)", row["id"])
                    if used:  # есть история заказов — не удаляем физически
                        await con.execute("UPDATE numbers SET status='removed' WHERE id=$1", row["id"])
                    else:
                        await con.execute("DELETE FROM numbers WHERE id=$1", row["id"])
                    deleted += 1
    await finish_edit(
        m, state, lid,
        f"🗑 Удалено: {deleted}\nПродано (не тронуты): {sold}\nНе найдено: {missing}" + invalid_note(invalid))


NUMS_PAGE = 20


@r.callback_query(F.data.startswith("adm:ln:"), ADMIN)
async def admin_lot_numbers(c: CallbackQuery):
    lid = int_arg(c.data, 2)
    page = max(0, int_arg(c.data, 3) or 0)
    if lid is None:
        return await c.answer()
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM numbers WHERE lot_id=$1 AND status<>'removed'", lid)
    pages = max(1, (total + NUMS_PAGE - 1) // NUMS_PAGE)
    page = min(page, pages - 1)
    rows = await pool.fetch(
        "SELECT phone, status, buyer FROM numbers WHERE lot_id=$1 AND status<>'removed' "
        "ORDER BY id LIMIT $2 OFFSET $3", lid, NUMS_PAGE, page * NUMS_PAGE)
    lines = [f"{'🟢' if x['status'] == 'free' else '🔴'} <code>{esc(x['phone'])}</code>"
             + (f" — user <code>{x['buyer']}</code>" if x["status"] != "free" and x["buyer"] else "")
             for x in rows] or ["Номеров нет."]
    nav = []
    if page > 0:
        nav.append(("⬅️", f"adm:ln:{lid}:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"adm:ln:{lid}:{page + 1}"))
    btns = ([nav] if nav else []) + [[("⬅️ К лоту", f"adm:lot:{lid}")]]
    await show(c, f"📋 <b>Номера лота #{lid}</b> (всего {total}, стр. {page + 1}/{pages})\n"
                  f"🟢 свободен · 🔴 продан\n\n" + "\n".join(lines), kb(*btns))


# --- пользователи ---
@r.callback_query(F.data == "adm:users", ADMIN)
async def admin_users_button(c: CallbackQuery, state: FSMContext):
    await state.clear()
    now = int(time.time())
    users = await pool.fetchval("SELECT COUNT(*) FROM users")
    new = await pool.fetchval("SELECT COUNT(*) FROM users WHERE joined >= $1", now - 86400)
    active = await pool.fetchval(
        "SELECT COUNT(*) FROM users WHERE COALESCE(last_active, joined, 0) >= $1",
        now - ACTIVE_DAYS * 86400)
    banned = await pool.fetchval("SELECT COUNT(*) FROM users WHERE banned")
    await show(
        c,
        f"👥 <b>Пользователи</b>\nВсего: {users}\n"
        f"Новых за 24 часа: {new}\n"
        f"Активных за {ACTIVE_DAYS} дн.: {active}\n"
        f"Заблокировано: {banned}",
        kb([("🔎 Найти по Telegram ID", "adm:usearch")], [("⬅️ Админ-панель", "adm:panel")])
    )


@r.callback_query(F.data == "adm:usearch", ADMIN)
async def admin_user_search_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(UserSearch.uid)
    await show(c, "🔎 Отправьте Telegram ID пользователя:", kb([("⬅️ Отмена", "adm:users")]))


async def user_card(uid):
    u = await pool.fetchrow(
        "SELECT id, username, name, balance, joined, last_active, banned FROM users WHERE id=$1", uid)
    if not u:
        return None
    st = await pool.fetchrow(
        "SELECT COUNT(*) FILTER (WHERE status='paid') n, "
        "COALESCE(SUM(amount) FILTER (WHERE status='paid'),0) s FROM orders WHERE user_id=$1", uid)
    top = await pool.fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=$1 AND type='topup'", uid)
    uname = f"@{u['username']}" if u["username"] else "—"
    text = (f"👤 <b>{esc(u['name'] or '—')}</b> ({esc(uname)})\n"
            f"ID: <code>{u['id']}</code>\n"
            f"Баланс: <b>{money(u['balance'] or 0)}</b>\n"
            f"Покупок: {st['n']} на {money(st['s'])}\n"
            f"Пополнений: {money(top)}\n"
            f"Регистрация: {fmt_ts(u['joined'])}\n"
            f"Последняя активность: {fmt_ts(u['last_active'])}\n"
            f"Статус: {'🚫 заблокирован' if u['banned'] else '✅ активен'}")
    markup = kb(
        [("✅ Разблокировать" if u["banned"] else "🚫 Заблокировать", f"adm:ub:{uid}")],
        [("💰 Баланс ±", f"adm:ua:{uid}")],
        [("🧾 Покупки", f"adm:uo:{uid}"), ("💳 Платежи", f"adm:up:{uid}")],
        [("⬅️ Пользователи", "adm:users")],
    )
    return text, markup


@r.message(UserSearch.uid, ADMIN, TEXT)
async def admin_user_search(m: Message, state: FSMContext):
    raw = m.text.strip()
    if not raw.lstrip("-").isdigit():
        return await m.answer("❌ Нужен числовой Telegram ID.")
    card = await user_card(int(raw))
    if not card:
        return await m.answer("❌ Пользователь не найден (он должен нажать /start).")
    await state.clear()
    await m.answer(card[0], reply_markup=card[1])


@r.message(Command("user"), ADMIN)
async def user_cmd(m: Message):
    p = (m.text or "").split()
    if len(p) < 2 or not p[1].lstrip("-").isdigit():
        return await m.answer("Формат: /user telegram_id")
    card = await user_card(int(p[1]))
    if not card:
        return await m.answer("❌ Пользователь не найден")
    await m.answer(card[0], reply_markup=card[1])


@r.callback_query(F.data.startswith("adm:u:"), ADMIN)
async def admin_user_open(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = int_arg(c.data, 2)
    card = await user_card(uid) if uid is not None else None
    if not card:
        return await c.answer("Пользователь не найден", show_alert=True)
    await show(c, *card)


@r.callback_query(F.data.startswith("adm:ub:"), ADMIN)
async def admin_user_ban(c: CallbackQuery, bot: Bot):
    uid = int_arg(c.data, 2)
    if uid is None:
        return await c.answer()
    if uid in ADMINS:
        return await c.answer("Администратора заблокировать нельзя.", show_alert=True)
    banned = await pool.fetchval("UPDATE users SET banned = NOT banned WHERE id=$1 RETURNING banned", uid)
    if banned is None:
        return await c.answer("Пользователь не найден", show_alert=True)
    await safe_send(bot, uid, "🚫 Доступ к боту ограничен." if banned else "✅ Доступ к боту восстановлен.")
    card = await user_card(uid)
    await show(c, *card)


@r.callback_query(F.data.startswith("adm:ua:"), ADMIN)
async def admin_user_adjust_start(c: CallbackQuery, state: FSMContext):
    uid = int_arg(c.data, 2)
    exists = uid is not None and await pool.fetchval("SELECT 1 FROM users WHERE id=$1", uid)
    if not exists:
        return await c.answer("Пользователь не найден", show_alert=True)
    await state.clear()
    await state.update_data(uid=uid)
    await state.set_state(BalanceAdj.amount)
    await show(c, "💰 Введите сумму со знаком и (по желанию) комментарий.\n"
                  "Пример: <code>+5 бонус</code> или <code>-2.5 ошибка</code>",
               kb([("⬅️ Отмена", f"adm:u:{uid}")]))


@r.message(BalanceAdj.amount, ADMIN, TEXT)
async def admin_user_adjust(m: Message, state: FSMContext, bot: Bot):
    p = m.text.split(maxsplit=1)
    amount = parse_amount(p[0], 0.01, MAX_ADJUST, allow_sign=True)
    if amount is None:
        return await m.answer(f"❌ Сумма — число от 0.01 до {MAX_ADJUST:.0f}, например +5 или -2.5.")
    uid = (await state.get_data()).get("uid")
    note = p[1][:200] if len(p) > 1 else None
    ok, text = await apply_adjust(m, bot, uid, amount, note)
    if not ok:
        return await m.answer(text)
    await state.clear()
    card = await user_card(uid)
    await m.answer(text + ("\n\n" + card[0] if card else ""), reply_markup=card[1] if card else admin_kb())


@r.callback_query(F.data.startswith("adm:uo:"), ADMIN)
async def admin_user_orders(c: CallbackQuery):
    uid = int_arg(c.data, 2)
    if uid is None:
        return await c.answer()
    rows = await pool.fetch(
        "SELECT o.id, o.amount, o.status, o.created_at, n.phone, l.service, co.name country "
        "FROM orders o JOIN numbers n ON n.id=o.number_id JOIN lots l ON l.id=n.lot_id "
        "JOIN countries co ON co.id=l.country_id WHERE o.user_id=$1 ORDER BY o.id DESC LIMIT 10", uid)
    if not rows:
        return await show(c, "🧾 Покупок нет.", kb([("⬅️ К пользователю", f"adm:u:{uid}")]))
    lines = [f"<b>#{x['id']}</b> {esc(x['country'])}/{esc(x['service'])} · <code>{esc(x['phone'])}</code> · "
             f"{money(x['amount'])} · {STATUS_LABELS.get(x['status'], esc(x['status']))} · "
             f"{fmt_ts(x['created_at'])}" for x in rows]
    btns = [[(f"Заказ #{x['id']}", f"adm:ord:{x['id']}")] for x in rows]
    btns.append([("⬅️ К пользователю", f"adm:u:{uid}")])
    await show(c, f"🧾 <b>Покупки пользователя {uid}</b> (последние 10)\n\n" + "\n".join(lines), kb(*btns))


@r.callback_query(F.data.startswith("adm:up:"), ADMIN)
async def admin_user_payments(c: CallbackQuery):
    uid = int_arg(c.data, 2)
    if uid is None:
        return await c.answer()
    rows = await pool.fetch(
        "SELECT invoice_id, amount, asset, status, created_at FROM payments "
        "WHERE user_id=$1 ORDER BY id DESC LIMIT 15", uid)
    back = kb([("⬅️ К пользователю", f"adm:u:{uid}")])
    if not rows:
        return await show(c, "💳 Платежей нет.", back)
    lines = [f"#{x['invoice_id']} · {money(x['amount'])} {esc(x['asset'])} · "
             f"{PAY_LABELS.get(x['status'], esc(x['status']))} · {fmt_ts(x['created_at'])}" for x in rows]
    await show(c, f"💳 <b>Платежи пользователя {uid}</b>\n\n" + "\n".join(lines), back)


# --- заказы / возвраты (админ) ---
class RefundError(Exception):
    pass


async def admin_order_view(oid):
    o = await order_row(oid)
    if not o:
        return None
    uname = f"@{o['username']}" if o["username"] else "—"
    text = (f"🧾 <b>Заказ #{o['id']}</b>\n"
            f"Пользователь: {esc(o['uname'] or '—')} ({esc(uname)}), <code>{o['user_id']}</code>\n"
            f"🌍 {esc(o['country'])} · 📲 {esc(o['service'])}\n"
            f"📱 <code>{esc(o['phone'])}</code>\n"
            f"💵 {money(o['amount'])}\n"
            f"Статус: {STATUS_LABELS.get(o['status'], esc(o['status']))}\n"
            f"Создан: {fmt_ts(o['created_at'])}"
            + (f"\nВозврат: {fmt_ts(o['refunded_at'])}" if o["refunded_at"] else ""))
    rows = []
    if o["status"] == "paid":
        rows.append([("↩️ Возврат + номер на склад", f"adm:rf:{oid}:1")])
        rows.append([("↩️ Возврат (номер остаётся проданным)", f"adm:rf:{oid}:0")])
    rows.append([("⬅️ К пользователю", f"adm:u:{o['user_id']}")])
    return text, kb(*rows)


@r.callback_query(F.data.startswith("adm:ord:"), ADMIN)
async def admin_order_open(c: CallbackQuery):
    oid = int_arg(c.data, 2)
    view = await admin_order_view(oid) if oid is not None else None
    if not view:
        return await c.answer("Заказ не найден", show_alert=True)
    await show(c, *view)


@r.message(Command("order", "refund"), ADMIN)
async def order_cmd(m: Message):
    p = (m.text or "").split()
    if len(p) < 2 or not p[1].isdigit():
        return await m.answer("Формат: /order id_заказа")
    view = await admin_order_view(int(p[1]))
    if not view:
        return await m.answer("❌ Заказ не найден")
    await m.answer(view[0], reply_markup=view[1])


@r.callback_query(F.data.startswith("adm:rf:"), ADMIN)
async def admin_refund_confirm(c: CallbackQuery):
    oid, flag = int_arg(c.data, 2), int_arg(c.data, 3)
    if oid is None or flag not in (0, 1):
        return await c.answer()
    what = "и вернуть номер в продажу" if flag else "(номер останется проданным)"
    await show(c, f"❓ Вернуть деньги по заказу <b>#{oid}</b> {what}?",
               kb([("✅ Да, вернуть", f"adm:rfy:{oid}:{flag}")], [("⬅️ Отмена", f"adm:ord:{oid}")]))


async def do_refund(oid, admin_id, restock):
    async with pool.acquire() as con:
        async with con.transaction():
            o = await con.fetchrow(
                "SELECT id, user_id, number_id, amount, status FROM orders WHERE id=$1 FOR UPDATE", oid)
            if not o:
                raise RefundError("Заказ не найден")
            if o["status"] != "paid":
                raise RefundError("Заказ уже возвращён")
            now = int(time.time())
            amount = float(o["amount"])
            await con.execute("UPDATE orders SET status='refunded', refunded_at=$2 WHERE id=$1", oid, now)
            await con.execute("UPDATE users SET balance=balance+$1 WHERE id=$2", amount, o["user_id"])
            await con.execute(
                "INSERT INTO transactions(user_id,type,amount,currency,external_id,created_at,note,admin_id) "
                "VALUES($1,'refund',$2,'USD',$3,$4,$5,$6)",
                o["user_id"], amount, f"refund:{oid}", now, f"Возврат по заказу #{oid}", admin_id)
            if restock:
                await con.execute(
                    "UPDATE numbers SET status='free', buyer=NULL, sold_at=NULL, sold_price=NULL, code=NULL "
                    "WHERE id=$1 AND status='sold'", o["number_id"])
            await con.execute(
                "UPDATE sms_requests SET status='closed', closed_at=$2, closed_by=$3 "
                "WHERE order_id=$1 AND status='open'", oid, now, admin_id)
    return o


@r.callback_query(F.data.startswith("adm:rfy:"), ADMIN)
async def admin_refund_do(c: CallbackQuery, bot: Bot):
    oid, flag = int_arg(c.data, 2), int_arg(c.data, 3)
    if oid is None or flag not in (0, 1):
        return await c.answer()
    try:
        o = await do_refund(oid, c.from_user.id, bool(flag))
    except RefundError as e:
        return await c.answer(f"❌ {e}", show_alert=True)
    await safe_send(bot, o["user_id"],
                    f"↩️ Возврат по заказу #{oid}: {money(o['amount'])} зачислены на баланс.")
    view = await admin_order_view(oid)
    await show(c, "✅ Возврат выполнен.\n\n" + view[0], view[1])


# ---------------- запуск (веб-сервер для UptimeRobot + бот) ----------------
async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda req: web.Response(text="ok"))
    app.router.add_get("/health", lambda req: web.Response(text="ok"))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    return runner


async def run_migrations():
    # Advisory-lock: при перекрытии старого и нового инстансов на Render миграции не столкнутся.
    async with pool.acquire() as con:
        await con.execute("SELECT pg_advisory_lock(727001)")
        try:
            await con.execute(SCHEMA)
            await con.execute(MIGRATIONS)
        finally:
            await con.execute("SELECT pg_advisory_unlock(727001)")


async def main():
    global pool, bot_instance
    handler = logging.StreamHandler()
    handler.setFormatter(RedactFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), handlers=[handler])
    logging.info(
        "Starting bot | crypto=%s | asset=%s | port=%s",
        "testnet" if CRYPTOBOT_TESTNET else "mainnet",
        CRYPTOBOT_ASSET,
        PORT,
    )

    runner = None
    worker = None
    bot = None
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )
    try:
        await run_migrations()
        runner = await web_server()
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
        bot_instance = bot
        dp = Dispatcher()
        dp.include_router(r)
        worker = asyncio.create_task(payment_worker())
        # aiogram сам перехватывает SIGTERM/SIGINT (Render шлёт SIGTERM при деплое)
        await dp.start_polling(bot)
    finally:
        logging.info("Shutting down…")
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if bot:
            try:
                await bot.session.close()
            except Exception:
                pass
        if runner:
            await runner.cleanup()
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
