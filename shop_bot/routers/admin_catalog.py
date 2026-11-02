"""Управление каталогом EXAMPLE SPLIT (админ): категории и товары.

Полный спектр: создавать/удалять категории и товары, менять названия,
описания, цены (₽), количество (склад/±), фото (загрузка/замена/удаление),
включать/отключать товары от продажи.

Callback-пространство админа: cats, catsp:{p}, catsadd, acat:{id},
catren/catpos/cattg/catdl/catdly, goods:{p}[:{cid}], goodsadd, gaddc:{cid},
aprod:{id}, prnm/prds/prpr/prst/prph/prphd/prtg/prdl/prdly.
Префиксы cat/catp/prod заняты пользовательским каталогом — не переиспользовать.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, services
from ..config import MAX_PRICE_RUB
from ..helpers import esc, int_arg, kb, parse_amount, show

from .admin_panel import ADMIN, admin_kb

r = Router(name="admin_catalog")

CATS_PAGE = 15
GOODS_PAGE = 8


class CatAdd(StatesGroup):
    name = State()


class CatEdit(StatesGroup):
    name = State()
    pos = State()


class ProdAdd(StatesGroup):
    name = State()
    desc = State()
    price = State()
    stock = State()
    photo = State()


class ProdEdit(StatesGroup):
    value = State()


def _rub_plain(x) -> str:
    return f"{float(x):,.0f}".replace(",", " ") + " ₽"


# ---------------- категории ----------------
@r.callback_query(ADMIN, F.data == "cats")
async def cats_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_cats(c, 0)


@r.callback_query(ADMIN, F.data.startswith("catsp:"))
async def cats_page(c: CallbackQuery):
    await _render_cats(c, int_arg(c.data, 1) or 0)


def _cats_nav_kb(page: int, pages: int) -> list[tuple[str, str]]:
    rows = []
    nav = []
    if page > 0:
        nav.append(("⬅️", f"catsp:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"catsp:{page + 1}"))
    if nav:
        rows.append(nav)
    return rows


async def _render_cats(c: CallbackQuery, page: int):
    total = await db.fetchval("SELECT COUNT(*) FROM categories")
    pages = max(1, (total + CATS_PAGE - 1) // CATS_PAGE)
    page = min(max(page, 0), pages - 1)
    rows = await db.fetch(
        "SELECT c.id, c.name, c.position, c.active, "
        "COUNT(p.id) FILTER (WHERE p.active AND p.stock>0) AS in_stock, "
        "COUNT(p.id) AS products "
        "FROM categories c LEFT JOIN products p ON p.category_id=c.id "
        "GROUP BY c.id ORDER BY c.position, c.name, c.id LIMIT $1 OFFSET $2",
        CATS_PAGE, page * CATS_PAGE)
    btns, line = [], []
    for x in rows:
        mark = "✅" if x["active"] else "⛔"
        line.append((f"{mark} {esc(x['name'])} ({x['products']})", f"acat:{x['id']}"))
        if len(line) == 3:
            btns.append(line)
            line = []
    if line:
        btns.append(line)
    btns.extend(_cats_nav_kb(page, pages))
    btns.append([("➕ Добавить категорию", "catsadd")])
    btns.append([("📦 Все товары", "goods:0")])
    btns.append([("⬅️ Админ-панель", "adm:panel")])
    text = (f"🗂 <b>Категории</b> (стр. {page + 1}/{pages})\n✅ активна · ⛔ выключена\n"
            f"В скобках — товаров в категории.")
    if not total:
        text += "\n\nКатегорий нет. Нажмите «Добавить категорию»."
    await show(c, text, kb(*btns))


@r.callback_query(ADMIN, F.data == "catsadd")
async def cats_add_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(CatAdd.name)
    await show(c, "➕ Введите название категории, например: Смартфоны",
               kb([("⬅️ Отмена", "cats")]))


@r.message(StateFilter(CatAdd.name), ADMIN, F.text)
async def cats_add_save(m: Message, state: FSMContext):
    name = " ".join((m.text or "").split())[:60]
    if not 1 <= len(name) <= 60:
        return await m.answer("Название категории: 1–60 символов.")
    try:
        cid = await db.fetchval(
            "INSERT INTO categories(name, created_at) VALUES($1,$2) RETURNING id",
            name, services.now())
    except Exception:
        logging.info("duplicate category name: %s", name)
        return await m.answer("❌ Категория с таким названием уже есть.",
                              reply_markup=kb([("🗂 Категории", "cats")]))
    await state.clear()
    card = await cat_card_data(cid)
    if card is None:
        return await m.answer("✅ Категория добавлена.", reply_markup=kb([("🗂 Категории", "cats")]))
    await m.answer(f"✅ Категория добавлена.\n\n{card[0]}", reply_markup=card[1])


async def cat_card_data(cid: int) -> tuple[str, object] | None:
    cat = await db.fetchrow(
        "SELECT c.*, COUNT(p.id) AS products FROM categories c "
        "LEFT JOIN products p ON p.category_id=c.id WHERE c.id=$1 GROUP BY c.id", cid)
    if not cat:
        return None
    text = (
        f"🗂 <b>Категория #{cat['id']}</b>\n"
        f"Название: <b>{esc(cat['name'])}</b>\n"
        f"Позиция сортировки: <b>{cat['position']}</b>\n"
        f"Товаров: <b>{cat['products']}</b>\n"
        f"Статус: {'✅ включена' if cat['active'] else '⛔ выключена'}")
    markup = kb(
        [("📦 Товары категории", f"goods:0:{cat['id']}")],
        [("✏️ Название", f"catren:{cat['id']}"), ("🔢 Позиция", f"catpos:{cat['id']}")],
        [("⛔ Выключить" if cat["active"] else "✅ Включить", f"cattg:{cat['id']}")],
        [("🗑 Удалить категорию", f"catdl:{cat['id']}")],
        [("⬅️ Категории", "cats")])
    return text, markup


@r.callback_query(ADMIN, F.data.startswith("acat:"))
async def cat_card_cb(c: CallbackQuery):
    cid = int_arg(c.data, 1)
    card = await cat_card_data(cid) if cid is not None else None
    if not card:
        return await c.answer("Категория не найдена", show_alert=True)
    await show(c, *card)


@r.callback_query(ADMIN, F.data.startswith("catren:"))
async def cat_rename_start(c: CallbackQuery, state: FSMContext):
    cid = int_arg(c.data, 1)
    if cid is None or not await db.fetchval("SELECT 1 FROM categories WHERE id=$1", cid):
        return await c.answer("Категория не найдена", show_alert=True)
    await state.set_state(CatEdit.name)
    await state.update_data(cid=cid)
    await show(c, "✏️ Пришлите новое название категории:",
               kb([("⬅️ Отмена", f"acat:{cid}")]))


@r.message(StateFilter(CatEdit.name), ADMIN, F.text)
async def cat_rename_save(m: Message, state: FSMContext):
    name = " ".join((m.text or "").split())[:60]
    if not 1 <= len(name) <= 60:
        return await m.answer("Название категории: 1–60 символов.")
    cid = (await state.get_data()).get("cid")
    if not isinstance(cid, int):
        await state.clear()
        return await m.answer("Форма потерялась — откройте категорию заново.",
                              reply_markup=kb([("🗂 Категории", "cats")]))
    try:
        await db.execute("UPDATE categories SET name=$2 WHERE id=$1", cid, name)
    except Exception:
        await state.clear()
        return await m.answer("❌ Категория с таким названием уже существует.")
    await state.clear()
    card = await cat_card_data(cid)
    if card is None:
        return await m.answer("✅ Название изменено.", reply_markup=kb([("🗂 Категории", "cats")]))
    await m.answer("✅ Название изменено.\n\n" + card[0], reply_markup=card[1])


@r.callback_query(ADMIN, F.data.startswith("catpos:"))
async def cat_pos_start(c: CallbackQuery, state: FSMContext):
    cid = int_arg(c.data, 1)
    if cid is None or not await db.fetchval("SELECT 1 FROM categories WHERE id=$1", cid):
        return await c.answer("Категория не найдена", show_alert=True)
    await state.set_state(CatEdit.pos)
    await state.update_data(cid=cid)
    await show(c, "🔢 Пришлите позицию сортировки в каталоге (число):",
               kb([("⬅️ Отмена", f"acat:{cid}")]))


@r.message(StateFilter(CatEdit.pos), ADMIN, F.text)
async def cat_pos_save(m: Message, state: FSMContext):
    raw = (m.text or "").strip()
    if not raw.lstrip("-").isdigit():
        return await m.answer("Позиция — целое число.")
    cid = (await state.get_data()).get("cid")
    if not isinstance(cid, int):
        await state.clear()
        return await m.answer("Форма потерялась — откройте категорию заново.",
                              reply_markup=kb([("🗂 Категории", "cats")]))
    await db.execute("UPDATE categories SET position=$2 WHERE id=$1", cid, int(raw))
    await state.clear()
    card = await cat_card_data(cid)
    if card is None:
        return await m.answer("✅ Позиция изменена.", reply_markup=kb([("🗂 Категории", "cats")]))
    await m.answer("✅ Позиция изменена.\n\n" + card[0], reply_markup=card[1])


@r.callback_query(ADMIN, F.data.startswith("cattg:"))
async def cat_toggle(c: CallbackQuery):
    cid = int_arg(c.data, 1)
    row = await db.fetchrow(
        "UPDATE categories SET active = NOT active WHERE id=$1 RETURNING active", cid) \
        if cid is not None else None
    if not row:
        return await c.answer("Категория не найдена", show_alert=True)
    card = await cat_card_data(cid)
    if card:
        await show(c, *card)


@r.callback_query(ADMIN, F.data.startswith("catdl:"))
async def cat_delete_confirm(c: CallbackQuery):
    cid = int_arg(c.data, 1)
    cat = await db.fetchrow(
        "SELECT c.id, c.name, COUNT(p.id) AS products FROM categories c "
        "LEFT JOIN products p ON p.category_id=c.id WHERE c.id=$1 GROUP BY c.id", cid) \
        if cid is not None else None
    if not cat:
        return await c.answer("Категория не найдена", show_alert=True)
    await show(
        c,
        f"🗑 <b>Удалить категорию?</b>\n\n{esc(cat['name'])}\n"
        f"Товаров внутри: <b>{cat['products']}</b> — они тоже будут удалены.\n"
        "История прошлых заказов сохранится.",
        kb([("✅ Да, удалить", f"catdly:{cid}")], [("⬅️ Отмена", f"acat:{cid}")]))


@r.callback_query(ADMIN, F.data.startswith("catdly:"))
async def cat_delete_do(c: CallbackQuery):
    cid = int_arg(c.data, 1)
    if cid is None:
        return await c.answer()
    row = await db.fetchrow("DELETE FROM categories WHERE id=$1 RETURNING name", cid)
    if not row:
        return await c.answer("Категория уже удалена", show_alert=True)
    await show(c, f"🗑 Категория «{esc(row['name'])}» удалена.",
               kb([("🗂 Категории", "cats")], [("⬅️ Админ-панель", "adm:panel")]))


# ---------------- товары ----------------
@r.callback_query(ADMIN, F.data.startswith("goods:"))
async def goods_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    page = int_arg(c.data, 1) or 0
    cid = int_arg(c.data, 2)
    if cid is not None and not await db.fetchval("SELECT 1 FROM categories WHERE id=$1", cid):
        return await c.answer("Категория не найдена", show_alert=True)
    await _render_goods(c, page, cid)


async def _render_goods(c: CallbackQuery, page: int, cid: int | None):
    if cid is None:
        total = await db.fetchval("SELECT COUNT(*) FROM products")
        pages = max(1, (total + GOODS_PAGE - 1) // GOODS_PAGE)
        page = min(max(page, 0), pages - 1)
        rows = await db.fetch(
            "SELECT p.*, c.name AS cat_name FROM products p "
            "LEFT JOIN categories c ON c.id=p.category_id ORDER BY p.category_id, p.id "
            "LIMIT $1 OFFSET $2", GOODS_PAGE, page * GOODS_PAGE)
    else:
        total = await db.fetchval("SELECT COUNT(*) FROM products WHERE category_id=$1", cid)
        pages = max(1, (total + GOODS_PAGE - 1) // GOODS_PAGE)
        page = min(max(page, 0), pages - 1)
        rows = await db.fetch(
            "SELECT p.*, c.name AS cat_name FROM products p "
            "JOIN categories c ON c.id=p.category_id WHERE p.category_id=$1 "
            "ORDER BY p.id LIMIT $2 OFFSET $3",
            cid, GOODS_PAGE, page * GOODS_PAGE)
    cat_name = await db.fetchval("SELECT name FROM categories WHERE id=$1", cid) if cid is not None else None
    btns = []
    for x in rows:
        mark = "✅" if x["active"] else "⛔"
        stock = "нет" if x["stock"] <= 0 else f"{x['stock']} шт."
        btns.append([(f"{mark} {esc(x['name'])} · {_rub_plain(x['price'])} · {stock}", f"aprod:{x['id']}")])
    nav = []
    if page > 0:
        nav.append(("⬅️", f"goods:{page - 1}" if cid is None else f"goods:{page - 1}:{cid}"))
    if page < pages - 1:
        nav.append(("➡️", f"goods:{page + 1}" if cid is None else f"goods:{page + 1}:{cid}"))
    if nav:
        btns.append(nav)
    btns.append([("➕ Добавить товар", "goodsadd")])
    btns.append([("🗂 Категории", "cats")])
    btns.append([("⬅️ Админ-панель", "adm:panel")])
    title = "📦 <b>Товары</b>" if cid is None else f"📦 <b>Товары: {esc(cat_name or '?')}</b>"
    text = f"{title} (стр. {page + 1}/{pages})\n✅ продаётся · ⛔ отключён"
    if not total:
        text += "\n\nТоваров нет. Нажмите «Добавить товар»."
    await show(c, text, kb(*btns))


@r.callback_query(ADMIN, F.data == "goodsadd")
async def goods_add_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    # Выбор категории для нового товара
    rows = await db.fetch("SELECT id, name FROM categories ORDER BY position, name, id LIMIT 24")
    if not rows:
        return await show(
            c, "Сначала создайте категорию.",
            kb([("➕ Добавить категорию", "catsadd")], [("⬅️ Категории", "cats")]))
    btns, line = [], []
    for x in rows:
        line.append((f"{esc(x['name'])}", f"gaddc:{x['id']}"))
        if len(line) == 2:
            btns.append(line)
            line = []
    if line:
        btns.append(line)
    btns.append([("⬅️ Отмена", "cats")])
    await show(c, "➕ В какой категории новый товар?", kb(*btns))


@r.callback_query(ADMIN, F.data.startswith("gaddc:"))
async def goods_add_cat(c: CallbackQuery, state: FSMContext):
    cid = int_arg(c.data, 1)
    if cid is None or not await db.fetchval("SELECT 1 FROM categories WHERE id=$1", cid):
        return await c.answer("Категория не найдена", show_alert=True)
    await state.set_state(ProdAdd.name)
    await state.update_data(cat_id=cid)
    await show(c, "🛍 Пришлите название товара, например: iPhone 17 Pro Max",
               kb([("⬅️ Отмена (в каталог)", "cats")]))


@r.message(StateFilter(ProdAdd.name), ADMIN, F.text)
async def goods_add_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()[:120]
    if not 1 <= len(name) <= 120:
        return await m.answer("Название товара: 1–120 символов.")
    await state.update_data(name=name)
    await state.set_state(ProdAdd.desc)
    await m.answer(
        "📝 Пришлите описание товара одним сообщением (или нажмите «Пропустить»):",
        reply_markup=kb([("⏭ Пропустить", "gskip:desc")], [("⬅️ Отмена", "cats")]))


@r.message(StateFilter(ProdAdd.desc), ADMIN, F.text)
async def goods_add_desc(m: Message, state: FSMContext):
    desc = (m.text or "").strip()[:2000]
    await state.update_data(desc=desc)
    await state.set_state(ProdAdd.price)
    await m.answer("💰 Пришлите цену в рублях, например: 32000",
                   reply_markup=kb([("⬅️ Отмена", "cats")]))


@r.callback_query(ADMIN, F.data == "gskip:desc", StateFilter(ProdAdd.desc))
async def goods_add_desc_skip(c: CallbackQuery, state: FSMContext):
    await state.update_data(desc="")
    await state.set_state(ProdAdd.price)
    await show(c, "💰 Пришлите цену в рублях, например: 32000",
               kb([("⬅️ Отмена", "cats")]))


@r.message(StateFilter(ProdAdd.price), ADMIN, F.text)
async def goods_add_price(m: Message, state: FSMContext):
    price = parse_amount(m.text, 1.0, MAX_PRICE_RUB)
    if price is None:
        return await m.answer(
            f"Цена — число от 1 до {f'{MAX_PRICE_RUB:,.0f}'.replace(',', ' ')} ₽.")
    await state.update_data(price=price)
    await state.set_state(ProdAdd.stock)
    await m.answer("📦 Пришлите количество на складе, например: 5",
                   reply_markup=kb([("⬅️ Отмена", "cats")]))


@r.message(StateFilter(ProdAdd.stock), ADMIN, F.text)
async def goods_add_stock(m: Message, state: FSMContext):
    raw = (m.text or "").strip()
    if not raw.isdigit() or int(raw) > 1_000_000:
        return await m.answer("Количество — целое число от 0.")
    await state.update_data(stock=int(raw))
    await state.set_state(ProdAdd.photo)
    await m.answer(
        "📷 Пришлите фото товара картинкой (не файлом), либо:"
        "\n• нажмите «Без фото»;\n• фото можно добавить позже в карточке.",
        reply_markup=kb([("🖼 Без фото", "gskip:photo")], [("⬅️ Отмена", "cats")]))


@r.callback_query(ADMIN, F.data == "gskip:photo", StateFilter(ProdAdd.photo))
async def goods_add_photo_skip(c: CallbackQuery, state: FSMContext):
    await state.update_data(photo=None)
    await _insert_product(c, state)


@r.message(StateFilter(ProdAdd.photo), ADMIN, F.photo)
async def goods_add_photo(m: Message, state: FSMContext):
    await state.update_data(photo=m.photo[-1].file_id)
    await _insert_product(m, state)


async def _insert_product(event: Message | CallbackQuery, state: FSMContext):
    """Создаёт товар из данных формы. event — откуда пришло последнее действие."""
    data = await state.get_data()
    await state.clear()
    cat_id = data.get("cat_id")
    name = data.get("name")
    price = data.get("price")
    stock = data.get("stock")
    if not isinstance(cat_id, int) or not name or price is None or stock is None:
        text = "Данные формы потерялись — начните заново через /admin."
        if isinstance(event, Message):
            return await event.answer(text)
        return await show(event, text, kb([("🗂 Категории", "cats")]))
    try:
        pid = await db.fetchval(
            "INSERT INTO products(category_id, name, description, price, stock, photo_file_id, created_at) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            cat_id, name, data.get("desc") or "", float(price), int(stock),
            data.get("photo"), services.now())
    except Exception:
        logging.exception("insert product failed")
        text = "❌ Не удалось сохранить товар."
        if isinstance(event, Message):
            return await event.answer(text)
        return await show(event, text, kb([("🗂 Категории", "cats")]))
    card = await prod_card_data(pid)
    if card is None:
        text = "✅ Товар добавлен."
        if isinstance(event, Message):
            return await event.answer(text, reply_markup=kb([("📦 Товары", "goods:0")]))
        return await show(event, text, kb([("📦 Товары", "goods:0")]))
    if isinstance(event, Message):
        await event.answer("✅ Товар добавлен.\n\n" + card[0], reply_markup=card[1])
    else:
        await show(event, "✅ Товар добавлен.\n\n" + card[0], card[1])


async def prod_card_data(pid: int) -> tuple[str, object] | None:
    row = await db.fetchrow(
        "SELECT p.*, c.name AS cat_name FROM products p "
        "LEFT JOIN categories c ON c.id=p.category_id WHERE p.id=$1", pid)
    if not row:
        return None
    photo_line = "📷 Фото: задано" if row["photo_file_id"] else "📷 Фото: нет"
    text = (
        f"🛍 <b>Товар #{row['id']}</b>\n"
        f"Название: <b>{esc(row['name'])}</b>\n"
        f"Категория: {esc(row['cat_name'] or '—')}\n"
        f"💰 Цена: <b>{_rub_plain(row['price'])}</b>\n"
        f"📦 На складе: <b>{row['stock']}</b>\n"
        f"{photo_line}\n"
        f"Статус: {'✅ продаётся' if row['active'] else '⛔ отключён'}")
    if row["description"]:
        text += "\n📝 Описание: " + esc(row["description"][:300])
    markup = kb(
        [("✏️ Название", f"prnm:{pid}"), ("📝 Описание", f"prds:{pid}")],
        [("💰 Цена (₽)", f"prpr:{pid}"), ("📦 Количество", f"prst:{pid}")],
        [("📷 Фото", f"prph:{pid}"), ("🗑 Удалить фото", f"prphd:{pid}")],
        [("⛔ Отключить" if row["active"] else "✅ Включить", f"prtg:{pid}")],
        [("🗑 Удалить товар", f"prdl:{pid}")],
        [("⬅️ Товары", "goods:0")])
    return text, markup


@r.callback_query(ADMIN, F.data.startswith("aprod:"))
async def prod_card_cb(c: CallbackQuery):
    pid = int_arg(c.data, 1)
    card = await prod_card_data(pid) if pid is not None else None
    if not card:
        return await c.answer("Товар не найден", show_alert=True)
    await show(c, *card)


# --- редактирование полей товара ---
PROD_FIELDS = {
    "prnm": ("name", "✏️ Пришлите новое название товара одним сообщением:"),
    "prds": ("desc", "📝 Пришлите новое описание одним сообщением (или «-» очистить):"),
}


async def prod_edit_start(c: CallbackQuery, state: FSMContext, key: str):
    """key — ключ из PROD_FIELDS; в состояние пишем имя поля (name/desc)."""
    pid = int_arg(c.data, 1)
    if pid is None or not await db.fetchval("SELECT 1 FROM products WHERE id=$1", pid):
        return await c.answer("Товар не найден", show_alert=True)
    field, prompt = PROD_FIELDS[key]
    await state.set_state(ProdEdit.value)
    await state.update_data(pid=pid, field=field)
    await show(c, prompt, kb([("⬅️ Отмена", f"aprod:{pid}")]))


@r.callback_query(ADMIN, F.data.startswith("prnm:"))
async def prnm_start(c: CallbackQuery, state: FSMContext):
    await prod_edit_start(c, state, "prnm")


@r.callback_query(ADMIN, F.data.startswith("prds:"))
async def prds_start(c: CallbackQuery, state: FSMContext):
    await prod_edit_start(c, state, "prds")


@r.message(StateFilter(ProdEdit.value), ADMIN, F.text)
async def prod_edit_save(m: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    pid = data.get("pid")
    raw = (m.text or "").strip()
    if not isinstance(pid, int) or field not in ("name", "desc", "price", "stock", "photo"):
        await state.clear()
        return await m.answer("Форма потерялась — откройте товар заново через /admin.")
    if field == "photo":
        return await m.answer(
            "📷 Пришлите фото картинкой (не файлом).",
            reply_markup=kb([("⬅️ Отмена", f"aprod:{pid}")]))
    if field == "name":
        name = raw[:120]
        if not 1 <= len(name) <= 120:
            return await m.answer("Название: 1–120 символов.")
        await db.execute("UPDATE products SET name=$2 WHERE id=$1", pid, name)
        done = "✅ Название изменено."
    elif field == "desc":
        desc = "" if raw == "-" else raw[:2000]
        await db.execute("UPDATE products SET description=$2 WHERE id=$1", pid, desc)
        done = "✅ Описание обновлено."
    elif field == "price":
        price = parse_amount(raw, 1.0, MAX_PRICE_RUB)
        if price is None:
            return await m.answer(
                f"Цена — число от 1 до {f'{MAX_PRICE_RUB:,.0f}'.replace(',', ' ')} ₽.")
        await db.execute("UPDATE products SET price=$2 WHERE id=$1", pid, price)
        done = "✅ Цена изменена."
    else:  # stock
        if not raw.isdigit() and not (raw.startswith(("+", "-")) and raw[1:].isdigit()):
            return await m.answer("Количество: число (5) или изменение (+5 / -2).")
        if raw.startswith("+"):
            row = await db.fetchrow(
                "UPDATE products SET stock=stock+$2 WHERE id=$1 RETURNING stock", pid, int(raw))
            done = f"✅ Количество изменено: {row['stock']} шт."
        elif raw.startswith("-"):
            n = int(raw)
            row = await db.fetchrow(
                "UPDATE products SET stock=stock+$2 WHERE id=$1 AND stock+$2>=0 "
                "RETURNING stock", pid, n)
            if row is None:
                return await m.answer("❌ Количество не может стать меньше нуля.")
            done = f"✅ Количество изменено: {row['stock']} шт."
        else:
            await db.execute("UPDATE products SET stock=$2 WHERE id=$1", pid, int(raw))
            done = f"✅ Количество: {int(raw)} шт."
    await state.clear()
    card = await prod_card_data(pid)
    if card is None:
        return await m.answer(done + "\n\nТовар удалён.", reply_markup=kb([("📦 Товары", "goods:0")]))
    await m.answer(done + "\n\n" + card[0], reply_markup=card[1])


@r.callback_query(ADMIN, F.data.startswith("prpr:"))
async def prpr_start(c: CallbackQuery, state: FSMContext):
    pid = int_arg(c.data, 1)
    if pid is None or not await db.fetchval("SELECT 1 FROM products WHERE id=$1", pid):
        return await c.answer("Товар не найден", show_alert=True)
    await state.set_state(ProdEdit.value)
    await state.update_data(pid=pid, field="price")
    await show(c, "💰 Пришлите новую цену в рублях одним сообщением:",
               kb([("⬅️ Отмена", f"aprod:{pid}")]))


@r.callback_query(ADMIN, F.data.startswith("prst:"))
async def prst_start(c: CallbackQuery, state: FSMContext):
    pid = int_arg(c.data, 1)
    if pid is None or not await db.fetchval("SELECT 1 FROM products WHERE id=$1", pid):
        return await c.answer("Товар не найден", show_alert=True)
    await state.set_state(ProdEdit.value)
    await state.update_data(pid=pid, field="stock")
    await show(c, "📦 Пришлите количество:\n• число (например 5) — установить;\n"
                  "• +5 / -2 — изменить на значение.", kb([("⬅️ Отмена", f"aprod:{pid}")]))


@r.callback_query(ADMIN, F.data.startswith("prph:"))
async def prph_start(c: CallbackQuery, state: FSMContext):
    pid = int_arg(c.data, 1)
    if pid is None or not await db.fetchval("SELECT 1 FROM products WHERE id=$1", pid):
        return await c.answer("Товар не найден", show_alert=True)
    await state.set_state(ProdEdit.value)
    await state.update_data(pid=pid, field="photo")
    await show(c, "📷 Пришлите фото товара картинкой (не файлом):",
               kb([("⬅️ Отмена", f"aprod:{pid}")]))


@r.callback_query(ADMIN, F.data.startswith("prphd:"))
async def prph_delete(c: CallbackQuery):
    pid = int_arg(c.data, 1)
    if pid is None:
        return await c.answer()
    await db.execute("UPDATE products SET photo_file_id=NULL WHERE id=$1", pid)
    card = await prod_card_data(pid)
    if card:
        await show(c, "✅ Фото удалено.\n\n" + card[0], card[1])


@r.message(StateFilter(ProdEdit.value), ADMIN, F.photo)
async def prod_photo_save(m: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("pid")
    if not isinstance(pid, int) or data.get("field") != "photo":
        await state.clear()
        return await m.answer("Форма потерялась — попробуйте заново через /admin.")
    await db.execute("UPDATE products SET photo_file_id=$2 WHERE id=$1",
                     pid, m.photo[-1].file_id)
    await state.clear()
    card = await prod_card_data(pid)
    if card is None:
        return await m.answer("✅ Фото сохранено.", reply_markup=kb([("📦 Товары", "goods:0")]))
    await m.answer("✅ Фото сохранено.\n\n" + card[0], reply_markup=card[1])


@r.callback_query(ADMIN, F.data.startswith("prtg:"))
async def prod_toggle(c: CallbackQuery):
    pid = int_arg(c.data, 1)
    if pid is None:
        return await c.answer()
    row = await db.fetchrow(
        "UPDATE products SET active = NOT active WHERE id=$1 RETURNING active", pid)
    if not row:
        return await c.answer("Товар не найден", show_alert=True)
    card = await prod_card_data(pid)
    if card:
        await show(c, *card)


@r.callback_query(ADMIN, F.data.startswith("prdl:"))
async def prod_delete_confirm(c: CallbackQuery):
    pid = int_arg(c.data, 1)
    row = await db.fetchrow("SELECT name FROM products WHERE id=$1", pid) if pid is not None else None
    if not row:
        return await c.answer("Товар не найден", show_alert=True)
    await show(c,
               f"🗑 <b>Удалить товар?</b>\n\n{esc(row['name'])}\n"
               "История заказов сохранится; открытые заказы останутся доступными.",
               kb([("✅ Да, удалить", f"prdly:{pid}")], [("⬅️ Отмена", f"aprod:{pid}")]))


@r.callback_query(ADMIN, F.data.startswith("prdly:"))
async def prod_delete_do(c: CallbackQuery):
    pid = int_arg(c.data, 1)
    if pid is None:
        return await c.answer()
    row = await db.fetchrow("DELETE FROM products WHERE id=$1 RETURNING name", pid)
    if not row:
        return await c.answer("Товар уже удалён", show_alert=True)
    await show(c, f"🗑 Товар «{esc(row['name'])}» удалён.",
               kb([("📦 Товары", "goods:0")], [("⬅️ Админ-панель", "adm:panel")]))
