"""Каталог EXAMPLE SPLIT (пользовательская часть): категории → товары → карточка.

Товары хранятся в БД (categories/products). Ничего не хардкодится.
Категория: name, position, active. Товар: name, description, price (₽),
stock, photo_file_id, active.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext

from .. import db
from ..config import SHOP_NAME
from ..helpers import (
    esc,
    int_arg,
    kb,
    money,
    price_rub,
    send_photo_card,
    show,
    USD_RUB_RATE,
)

r = Router(name="catalog")

CATS_PER_PAGE = 9       # 3 колонки × 3 ряда
PRODUCTS_PER_PAGE = 8


def _cats_kb(rows, page, pages, tail=None) -> InlineKeyboardMarkup:
    btns, line = [], []
    for x in rows:
        label = f"{x['name']} · {x['cnt']}"
        line.append((label, f"ccat:{x['id']}"))
        if len(line) == 3:
            btns.append(line)
            line = []
    if line:
        btns.append(line)
    nav = []
    if page > 0:
        nav.append(("⬅️", f"catp:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"catp:{page + 1}"))
    if nav:
        btns.append(nav)
    if tail:
        btns.extend(tail)
    return kb(*btns)


async def render_catalog(page=0):
    """Каталог: список активных категорий с количеством товаров в наличии."""
    rows = await db.fetch(
        "SELECT c.id, c.name, COUNT(p.id) AS cnt "
        "FROM categories c LEFT JOIN products p ON p.category_id=c.id AND p.active AND p.stock>0 "
        "WHERE c.active GROUP BY c.id ORDER BY c.position, c.name, c.id")
    rows = [x for x in rows if x["cnt"] > 0]
    if not rows:
        text = ("🛍 <b>Каталог</b>\n\n😔 Товаров пока нет — каталог склада пополняется. "
                "Загляните позже.")
        return text, kb([("⬅️ В меню", "menu")])
    total = len(rows)
    pages = max(1, (total + CATS_PER_PAGE - 1) // CATS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = rows[page * CATS_PER_PAGE:(page + 1) * CATS_PER_PAGE]
    suffix = f" (стр. {page + 1}/{pages})" if pages > 1 else ""
    preview = "\n".join(f"• {esc(x['name'])} — {x['cnt']} в наличии" for x in chunk)
    text = (
        f"🛍 <b>Каталог {esc(SHOP_NAME)}</b>{suffix}\n\n"
        f"{preview}\n\n"
        "Выберите категорию на кнопках ниже.")
    tail = [[("⬅️ В меню", "menu")]]
    return text, _cats_kb(chunk, page, pages, tail)


async def send_catalog(m: Message, page=0):
    text, markup = await render_catalog(page)
    await m.answer(text, reply_markup=markup)


@r.callback_query(F.data == "cat")
async def catalog_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await render_catalog(0)
    await show(c, text, markup)


@r.callback_query(F.data.startswith("catp:"))
async def catalog_page_cb(c: CallbackQuery):
    page = int_arg(c.data, 1) or 0
    text, markup = await render_catalog(page)
    await show(c, text, markup)


@r.callback_query(F.data.startswith("ccatp:"))
async def category_page_cb(c: CallbackQuery):
    page = int_arg(c.data, 2) or 0
    cid = int_arg(c.data, 1)
    if cid is None:
        return await c.answer("Категория не найдена", show_alert=True)
    text, markup = await render_category(cid, page)
    if markup is None:
        return await c.answer("Категория не найдена", show_alert=True)
    await show(c, text, markup)


@r.callback_query(F.data.startswith("ccat:"))
async def category_cb(c: CallbackQuery):
    cid = int_arg(c.data, 1)
    if cid is None:
        return await c.answer("Категория не найдена", show_alert=True)
    text, markup = await render_category(cid, 0)
    if markup is None:
        return await c.answer("Категория не найдена", show_alert=True)
    await show(c, text, markup)


async def render_category(cat_id, page=0):
    """Товары категории: только включённые товары; нет в наличии — показаны без кнопки."""
    cat = await db.fetchrow(
        "SELECT id, name FROM categories WHERE id=$1 AND active", cat_id)
    if not cat:
        return None, None
    total = await db.fetchval(
        "SELECT COUNT(*) FROM products WHERE category_id=$1 AND active", cat_id)
    pages = max(1, (total + PRODUCTS_PER_PAGE - 1) // PRODUCTS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    rows = await db.fetch(
        "SELECT id, name, price, stock FROM products "
        "WHERE category_id=$1 AND active ORDER BY stock DESC, id "
        "LIMIT $2 OFFSET $3", cat_id, PRODUCTS_PER_PAGE, page * PRODUCTS_PER_PAGE)
    if not rows:
        return (f"🛍 {esc(cat['name'])}\n\n😔 Товаров в этой категории пока нет.",
                kb([("⬅️ В каталог", "cat")], [("⬅️ В меню", "menu")]))
    lines, btns = [], []
    for x in rows:
        stock_label = f"{x['stock']} шт." if x["stock"] > 0 else "нет в наличии"
        lines.append(f"• {esc(x['name'])} — {price_rub_plain(x['price'])} ({stock_label})")
        btns.append([(f"{esc(x['name'])} · {price_rub_plain(x['price'])}", f"prod:{x['id']}")])
    nav = []
    if page > 0:
        nav.append(("⬅️", f"ccatp:{cat_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"ccatp:{cat_id}:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([("⬅️ В каталог", "cat")])
    btns.append([("⬅️ В меню", "menu")])
    text = (f"🛍 <b>{esc(cat['name'])}</b> (стр. {page + 1}/{pages})\n\n" + "\n".join(lines))
    return text, kb(*btns)


def price_rub_plain(x) -> str:
    """32000 -> '32 000 ₽' (без эквивалента) для списков."""
    v = float(x)
    return f"{v:,.0f}".replace(",", " ") + " ₽"


@r.callback_query(F.data.startswith("prod:"))
async def product_cb(c: CallbackQuery):
    pid = int_arg(c.data, 1)
    if pid is None:
        return await c.answer("Товар не найден", show_alert=True)
    p = await db.fetchrow(
        "SELECT p.*, c.name AS cat_name FROM products p "
        "JOIN categories c ON c.id=p.category_id WHERE p.id=$1 AND p.active", pid)
    if not p:
        return await show(c, "😔 Товар больше не продаётся.", kb([("⬅️ В каталог", "cat")]))
    await _send_product_card(c, p)


async def _send_product_card(c: CallbackQuery, p):
    """Карточка товара: фото (если задано) с описанием либо текст."""
    cap_lines = [
        f"🛍 <b>{esc(p['name'])}</b>",
        f"📁 Категория: {esc(p['cat_name'])}",
        f"💰 Цена: <b>{price_rub(p['price'])}</b>",
    ]
    if p["stock"] > 0:
        cap_lines.append(f"📦 В наличии: <b>{p['stock']} шт.</b>")
    else:
        cap_lines.append("📦 <b>Нет в наличии</b>")
    cap_lines.append("")
    if p["description"]:
        cap_lines.append(esc(p["description"])[:2500])
        cap_lines.append("")
    caption = "\n".join(cap_lines) + "\n\nНажмите «Купить», чтобы оформить."
    if p["stock"] > 0:
        buy_rows = [[("🛒 Купить", f"ckstart:{p['id']}")]]
    else:
        buy_rows = [[("😔 Нет в наличии", "cat")]]
    markup = kb(
        *buy_rows,
        [("⬅️ Назад к товарам", f"ccat:{p['category_id']}")],
        [("🛍 В каталог", "cat")],
        [("⬅️ В меню", "menu")])
    if p["photo_file_id"]:
        try:
            await c.bot.send_photo(c.from_user.id, p["photo_file_id"],
                                   caption=caption, reply_markup=markup)
            return await c.answer()
        except Exception:
            pass  # фотка могла «протухнуть» — отправим текст
    try:
        await c.message.edit_text(caption, reply_markup=markup)
    except Exception:
        await c.message.answer(caption, reply_markup=markup)
    await c.answer()
