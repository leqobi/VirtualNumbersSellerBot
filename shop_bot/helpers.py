"""Общие хелперы EXAMPLE SPLIT: экранирование, money, клавиатуры, long-сообщения,
безопасная правка сообщений. Курс USD/RUB обновляется с сайта ЦБ РФ.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import ClientSession, ClientTimeout

from .config import TG_CHUNK, USD_RUB_RATE_FALLBACK, CBR_DAILY_URL

# ---------------- курс ЦБ (из старого проекта, проверенный) ----------------
USD_RUB_RATE = USD_RUB_RATE_FALLBACK
USD_RUB_RATE_UPDATED_AT = 0
USD_RUB_RATE_SOURCE = "fallback"


async def update_usd_rub_rate() -> float:
    """Получает последний зарегистрированный курс USD/RUB ЦБ РФ."""
    global USD_RUB_RATE, USD_RUB_RATE_UPDATED_AT, USD_RUB_RATE_SOURCE
    timeout = ClientTimeout(total=10)
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.get(CBR_DAILY_URL) as resp:
                resp.raise_for_status()
                body = await resp.text(encoding="windows-1251")
        root = ET.fromstring(body)
        for valute in root.findall("Valute"):
            if valute.findtext("CharCode") == "USD":
                value = valute.findtext("Value")
                nominal = valute.findtext("Nominal") or "1"
                rate = float(value.replace(",", ".")) / float(nominal)
                if math.isfinite(rate) and rate > 0:
                    USD_RUB_RATE = rate
                    USD_RUB_RATE_UPDATED_AT = int(time.time())
                    USD_RUB_RATE_SOURCE = "CBR"
                    logging.info("USD/RUB rate updated from CBR: %.4f", rate)
                    return rate
        raise ValueError("USD rate not found in CBR response")
    except Exception as exc:
        logging.warning("Could not update USD/RUB rate from CBR: %s; using %.4f", exc, USD_RUB_RATE)
        return USD_RUB_RATE


async def cbr_rate_worker() -> None:
    while True:
        try:
            await update_usd_rub_rate()
        except Exception:
            logging.exception("CBR rate worker error")
        await asyncio.sleep(6 * 60 * 60)


# ---------------- базовые хелперы (унаследованы из старого проекта) ----------------
def esc(s) -> str:
    return html.escape(str(s))


def fmt_ts(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def now() -> int:
    return int(time.time())


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


def clean_label(s, maxlen=60):
    s = " ".join((s or "").split())
    return s if 1 <= len(s) <= maxlen else None


def clean_text(s, maxlen=2000):
    s = (s or "").strip()
    return s[:maxlen] if len(s) <= maxlen else None


# ---------------- money ----------------
def fmt_rub(x) -> str:
    """32000 -> '32 000 ₽'; 3300.5 -> '3 300.50 ₽'."""
    x = float(x)
    if abs(x - round(x)) < 1e-9:
        return f"{round(x):,}".replace(",", " ") + " ₽"
    return f"{x:,.2f}".replace(",", " ") + " ₽"


def fmt_usd(x) -> str:
    return f"${float(x):.2f}"


def rub_money(x) -> str:
    """Эквивалент USD-суммы в рублях по текущему курсу (для показа)."""
    v = float(x) * USD_RUB_RATE
    return fmt_rub(v)


def money(x) -> str:
    return f"${float(x):.2f}"


def price_rub(x) -> str:
    """Цена рублёвая + эквивалент в долларах для показа."""
    return f"{fmt_rub(x)} ≈ {money(float(x) / USD_RUB_RATE)}"


def usd_to_rub(x) -> float:
    return round(float(x) * USD_RUB_RATE, 2)


def rub_to_usd(x, rate=None) -> float:
    return round(float(x) / float(rate or USD_RUB_RATE), 4)


def balance_text(x, currency="USD"):
    x = float(x)
    if currency == "RUB":
        return f"{x:.2f} ₽"
    return f"${x:.2f}"


def balance_both(x):
    x = float(x)
    return f"${x:.2f} ≈ {rub_money(x)}"


def signed_money(x):
    x = float(x)
    return f"{'+' if x >= 0 else '-'}${abs(x):.2f}"


def next_balance_currency(currency):
    return "RUB" if currency == "USD" else "USD"


# ---------------- клавиатуры ----------------
def _btn(text, data):
    if data.startswith(("http://", "https://")):
        return InlineKeyboardButton(text=text, url=data)
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(t, d) for t, d in row] for row in rows])


def back_kb(data="menu", text="⬅️ В меню"):
    return kb([(text, data)])


def int_arg(data, idx=1):
    try:
        return int(str(data).split(":")[idx])
    except (ValueError, IndexError):
        return None


def str_arg(data, idx=1):
    try:
        return str(data).split(":")[idx]
    except IndexError:
        return None


# ---------------- длинные сообщения (обработка из старого проекта) ----------------
def split_message(text, limit=TG_CHUNK):
    """Делит длинный текст на части <= limit символов (по строкам, затем по пробелам)."""
    text = str(text)
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
            amp = rest.rfind("&", max(0, cut - 10), cut)
            if amp > 0 and ";" not in rest[amp:cut]:
                cut = amp
            parts.append(rest[:cut])
            rest = rest[cut:]
        else:
            parts.append(rest[:cut])
            rest = rest[cut + 1:]
    parts.append(rest)
    return [x for x in parts if x.strip()] or [text[:limit] or "…"]


async def send_long(bot: Bot, chat_id, text, **kw):
    """send_message с разбиением; reply_markup крепится к последней части."""
    parts = split_message(text)
    markup = kw.pop("reply_markup", None)
    sent = []
    for i, part in enumerate(parts):
        extra = dict(kw)
        if markup is not None and i == len(parts) - 1:
            extra["reply_markup"] = markup
        sent.append(await bot.send_message(chat_id, part, **extra))
    return sent


async def answer_long(m: Message, text, **kw):
    """m.answer с разбиением; reply_markup крепится к последней части."""
    parts = split_message(text)
    markup = kw.pop("reply_markup", None)
    for i, part in enumerate(parts):
        extra = dict(kw)
        if markup is not None and i == len(parts) - 1:
            extra["reply_markup"] = markup
        await m.answer(part, **extra)


async def show(c: CallbackQuery, text, markup):
    """Редактирует сообщение inline-кнопки, при неудаче отправляет новое."""
    parts = split_message(text)
    msg = c.message
    if not isinstance(msg, Message):
        for i, part in enumerate(parts):
            await c.bot.send_message(
                c.from_user.id, part,
                reply_markup=markup if i == len(parts) - 1 else None)
    elif len(parts) == 1:
        try:
            await msg.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await msg.answer(text, reply_markup=markup)
    else:
        rest = parts[1:]
        try:
            await msg.edit_text(parts[0])
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                rest = []
            else:
                await msg.answer(parts[0])
        for i, part in enumerate(rest):
            await msg.answer(part, reply_markup=markup if i == len(rest) - 1 else None)
    try:
        await c.answer()
    except TelegramBadRequest:
        pass


# ---------------- media (карточки товаров с фото) ----------------
async def send_photo_card(m_or_c, photo_file_id, caption, markup):
    """Отправляет карточку с фото фотографией. Работает и по Message, и по CallbackQuery."""
    if isinstance(m_or_c, Message):
        await m_or_c.answer_photo(photo_file_id, caption=caption, reply_markup=markup)
    else:
        await m_or_c.bot.send_photo(
            m_or_c.from_user.id, photo_file_id, caption=caption, reply_markup=markup)


async def safe_send(bot: Bot, uid, text, **kw):
    try:
        await send_long(bot, uid, text, **kw)
        return True
    except TelegramAPIError:
        return False


async def safe_send_photo(bot: Bot, uid, photo_file_id, caption, **kw) -> bool:
    try:
        await bot.send_photo(uid, photo_file_id, caption=caption, **kw)
        return True
    except TelegramAPIError:
        return False


async def edit_markup(c: CallbackQuery, bot: Bot, markup=None):
    """Меняет/убирает кнопки у сообщения, с которого нажали кнопку. Работает и для старых
    (недоступных) сообщений — нужны только chat.id и message_id; ошибки не пробрасываются."""
    msg = c.message
    if msg is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=markup)
    except TelegramAPIError:
        pass


async def strip_markup(c: CallbackQuery, bot: Bot):
    await edit_markup(c, bot, None)


# ---------------- защита от двойных нажатий ----------------
class Busy:
    """Мутекс на пользователя внутри процесса: повторный клик игнорируется.
    Основная защита — блокировки и уникальные индексы БД."""

    def __init__(self, uid, busy: set | None = None):
        self.uid, self.ok = uid, False
        self._busy = busy if busy is not None else set()

    async def __aenter__(self):
        if self.uid not in self._busy:
            self._busy.add(self.uid)
            self.ok = True
        return self

    async def __aexit__(self, *exc):
        if self.ok:
            self._busy.discard(self.uid)
