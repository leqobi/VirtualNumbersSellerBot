"""База данных EXAMPLE SPLIT: пул PostgreSQL и идемпотентные миграции.

Миграции строго адитивные (CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS):
существующие таблицы и данные предыдущего проекта не изменяются и не удаляются.
Унаследованные таблицы остаются в базе нетронутыми; новые таблицы магазина
используются для каталога и заказов техники.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from .config import DEFAULT_SETTINGS

MIGRATION_LOCK_ID = 591833  # не пересекается со старым инстансом (727001)

pool: asyncpg.Pool | None = None


def db() -> asyncpg.Pool:
    db_instance = pool
    if db_instance is None:
        raise RuntimeError("Database pool is not initialized")
    return db_instance


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id BIGINT PRIMARY KEY, username TEXT, name TEXT,
    balance DOUBLE PRECISION DEFAULT 0, joined BIGINT, balance_currency TEXT NOT NULL DEFAULT 'USD');
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

MIGRATIONS = """
-- ---- унаследованные адитивные миграции предыдущей версии ----
ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active BIGINT;
ALTER TABLE lots ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS refunded_at BIGINT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS admin_id BIGINT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS pay_url TEXT;
ALTER TABLE msgmap ADD COLUMN IF NOT EXISTS request_id BIGINT;

CREATE TABLE IF NOT EXISTS manual_topups(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    amount DOUBLE PRECISION NOT NULL,
    wallet TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at BIGINT NOT NULL,
    reviewed_at BIGINT,
    reviewed_by BIGINT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_manual_topups_status
    ON manual_topups(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_manual_topups_user
    ON manual_topups(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS rub_topups(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    amount_rub DOUBLE PRECISION NOT NULL,
    amount_usd DOUBLE PRECISION NOT NULL,
    rate DOUBLE PRECISION,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at BIGINT NOT NULL,
    reviewed_at BIGINT,
    reviewed_by BIGINT,
    details TEXT
);
ALTER TABLE rub_topups ADD COLUMN IF NOT EXISTS rate DOUBLE PRECISION;
ALTER TABLE rub_topups ADD COLUMN IF NOT EXISTS reviewed_at BIGINT;
ALTER TABLE rub_topups ADD COLUMN IF NOT EXISTS reviewed_by BIGINT;
ALTER TABLE rub_topups ADD COLUMN IF NOT EXISTS details TEXT;
CREATE INDEX IF NOT EXISTS idx_rub_topups_status
    ON rub_topups(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rub_topups_user
    ON rub_topups(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sms_requests(
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'open',
    created_at BIGINT NOT NULL,
    closed_at BIGINT,
    closed_by BIGINT
);
ALTER TABLE sms_requests ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'sms';
ALTER TABLE sms_requests ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS replaced_from BIGINT;
CREATE INDEX IF NOT EXISTS idx_sms_status ON sms_requests(status, id);
CREATE INDEX IF NOT EXISTS idx_numbers_buyer ON numbers(buyer);
CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(number_id);
CREATE INDEX IF NOT EXISTS idx_transactions_type_created ON transactions(type, created_at);
CREATE INDEX IF NOT EXISTS idx_msgmap_request ON msgmap(request_id);
CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active);

-- ---- EXAMPLE SPLIT: каталог, заказы, настройки магазина ----
ALTER TABLE users ADD COLUMN IF NOT EXISTS contact_phone TEXT;
ALTER TABLE msgmap ADD COLUMN IF NOT EXISTS answered BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_msgmap_answered ON msgmap(answered, msg_id DESC)
    WHERE answered = FALSE;

CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories(
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    position INT NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_categories_pos ON categories(position, id);

CREATE TABLE IF NOT EXISTS products(
    id SERIAL PRIMARY KEY,
    category_id INT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price DOUBLE PRECISION NOT NULL CHECK (price > 0),
    stock INT NOT NULL DEFAULT 0 CHECK (stock >= 0),
    photo_file_id TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_cat ON products(category_id, id);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(active, stock);

-- Заказы магазина. Сумма (amount) — в рублях; charged_amount — фактически
-- списано с баланса в USD с фиксацией курса.
CREATE TABLE IF NOT EXISTS shop_orders(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    product_id INT REFERENCES products(id) ON DELETE SET NULL,
    product_name TEXT NOT NULL,
    product_price DOUBLE PRECISION NOT NULL,
    qty INT NOT NULL DEFAULT 1 CHECK (qty > 0),
    amount DOUBLE PRECISION NOT NULL CHECK (amount > 0),
    city TEXT NOT NULL,
    pvz TEXT NOT NULL,
    pay_method TEXT,
    charged_amount DOUBLE PRECISION,
    charged_currency TEXT NOT NULL DEFAULT 'USD',
    rate DOUBLE PRECISION,
    tracking_number TEXT,
    delivery_note TEXT,
    status TEXT NOT NULL DEFAULT 'AWAITING_PAYMENT' CHECK (status IN
        ('AWAITING_PAYMENT','AWAITING_CONFIRM','PAID','CONFIRMED','PREPARING',
         'SHIPPED','DELIVERED','COMPLETED','CANCELLED','REFUNDED')),
    created_at BIGINT NOT NULL,
    paid_at BIGINT,
    confirmed_at BIGINT,
    preparing_at BIGINT,
    shipped_at BIGINT,
    delivered_at BIGINT,
    completed_at BIGINT,
    cancelled_at BIGINT,
    refunded_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_shop_orders_user ON shop_orders(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_shop_orders_status ON shop_orders(status, id DESC);
-- Не более одного открытого (неоплаченного) заказа на один товар у пользователя —
-- повторное «Купить» не порождает дубликаты.
CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_orders_open
    ON shop_orders(user_id, product_id)
    WHERE status IN ('AWAITING_PAYMENT','AWAITING_CONFIRM');
"""


async def init_pool(url: str) -> asyncpg.Pool:
    """Создаёт пул соединений и сохраняет его в db.pool."""
    global pool
    new_pool = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )
    pool = new_pool
    return new_pool


async def run_migrations() -> None:
    """Идемпотентное применение схемы + значений по умолчанию.

    Advisory-lock: при перекрытии старого и нового инстансов миграции не столкнутся.
    """
    p = db()
    async with p.acquire() as con:
        await con.execute(f"SELECT pg_advisory_lock({MIGRATION_LOCK_ID})")
        try:
            await con.execute(SCHEMA)
            await con.execute(MIGRATIONS)
            for key, value in DEFAULT_SETTINGS.items():
                await con.execute(
                    "INSERT INTO settings(key, value) VALUES($1, $2) ON CONFLICT (key) DO NOTHING",
                    key, value,
                )
        finally:
            await con.execute(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_ID})")


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    return await db().fetchrow(query, *args)


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    return await db().fetch(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    return await db().fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    return await db().execute(query, *args)


async def settings_get(key: str, default: str = "") -> str:
    value = await fetchval("SELECT value FROM settings WHERE key=$1", key)
    return value if value is not None else default


async def settings_set(key: str, value: str) -> None:
    await execute(
        "INSERT INTO settings(key, value) VALUES($1,$2) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        key, value,
    )
