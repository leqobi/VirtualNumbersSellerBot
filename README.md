# EXAMPLE SPLIT — Telegram-магазин электроники

Магазин на **aiogram 3**. Основная поставка собрана в `bot-manual-topup.py` и `requirements.txt`; модульная версия `shop_bot/` оставлена как исходный ориентир.

## Стек
- **Python 3.10+** (рекомендуется 3.11/3.12)
- **aiogram 3.31** — Telegram Bot API
- **asyncpg 0.32** — PostgreSQL пул, транзакции, advisory-lock миграций
- **aiohttp 3.14** — keepalive-HTTP на `PORT` (Render/Healthcheck)
- **python-dotenv** — секреты только из `.env` / окружения

## Требования
- **PostgreSQL 14+** (Render / Neon / Supabase / локальный)
- **Telegram Bot Token** (от @BotFather)
- **Администраторы** — числовые Telegram ID через переменную окружения

## Установка и запуск

```bash
# 1) Клонирование репозитория
git clone <repo-url>
cd "Example Split"

# 2) Виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

# 3) Зависимости
pip install -r requirements.txt

# 4) Переменные окружения — создайте файл .env
cat > .env <<'EOF'
# Обязательные
BOT_TOKEN=123456:ABC-DEF...           # токен бота от @BotFather
ADMIN_IDS=111222333,444555666        # числовые Telegram ID админов через запятую
OWNER_ID=111222333                   # владелец, принимает решения о возвратах; по умолчанию первый ADMIN_IDS
DATABASE_URL=postgresql://user:pass@host:5432/db  # строка подключения к PostgreSQL

# Опциональные (есть дефолты)
PORT=8080                             # keepalive-порт (Render задаёт сам)
MANUAL_WALLET_ADDRESS=TXYZ...         # USDT TRC20 кошелёк для пополнений (пусто = кнопка скрыта)
LOW_STOCK_THRESHOLD=2                 # уведомлять админа при остатке < N
ACTIVE_DAYS=30                        # окно активности для рассылки
BACKUP_INTERVAL_HOURS=24              # 0 = отключить авто-бэкап CSV админам
EOF

# 5) Запуск
python bot-manual-topup.py
```

## Как добавить администратора
Администраторы задаются **только** через переменную окружения `ADMIN_IDS` — числовые Telegram ID через запятую. В базе данных отдельной роли «админ» нет, проверка идёт по списку ID.

Пример: `ADMIN_IDS=123456789,987654321`

Узнать свой ID можно у бота @userinfobot или @getmyid_bot.

## Инициализация базы данных
Миграции применяются **автоматически при первом запуске** бота:
- Создаются таблицы: `users`, `categories`, `products`, `shop_orders`, `settings`, `manual_topups`, `rub_topups`, `transactions`, `msgmap` и др.
- Создаются индексы, в т.ч. уникальный `uq_shop_orders_open` (защита от дублей заказов)
- Заливаются дефолтные настройки (`about_text`, `welcome_text`)
- Advisory-lock `591833` предотвращает конфликты при параллельном запуске

Старые таблицы legacy-проекта (`countries`, `lots`, `numbers`, `orders`, `sms_requests`, `payments`) **не трогаются**.

## Архитектура
```
shop_bot/
├── config.py          # env, статусы (10 шт.), константы, лимиты, PVZ_SERVICES
├── db.py              # пул asyncpg + идемпотентные миграции (advisory-lock 591833)
├── helpers.py         # UI, money, CBR-курс (каждые 6ч), длинные сообщения, Busy
├── services.py        # баланс, заказы, склад, возвраты, ledger, уведомления
├── main.py            # entry point: пул, middleware, keepalive, диспетчер, воркеры
└── routers/           # 11 роутеров (5 админ + 6 пользовательских)
    ├── admin_panel.py     # /admin, настройки, статистика, рассылка, экспорт, поддержка
    ├── admin_catalog.py   # CRUD категорий/товаров, фото, склад, активность
    ├── admin_orders.py    # заказы: список, фильтры, поиск, подтверждение оплаты, статусы, трекинг, возврат
    ├── admin_payments.py  # заявки на пополнение: USDT (mtpay/mtdeny) + рубли (ref)
    ├── admin_users.py     # пользователи: поиск, карточка, бан, ±баланс, /user, /addbalance
    ├── catalog.py         # пользовательский каталог: категории → товары → карточка с фото
    ├── checkout.py        # FSM покупки: кол-во → город → ПВЗ → конфирм → оплата
    ├── common.py          # /start, reply-меню (6 кнопок), профиль
    ├── orders.py          # мои заказы (пользователь): список, карточка, отмена
    ├── support.py         # поддержка: msgmap, ответы админа (reply/FSM)
    └── topup.py           # 💳 Оплата: баланс, пополнения USDT/рубли, история
```

## Основные фичи
| Область | Что внутри |
|--------|------------|
| **Каталог** | Товары в БД (`categories` / `products`): название, описание, цена (₽), фото, склад, активность. Админ управляет через inline-меню. |
| **Заказы** | 10 статусов (`AWAITING_PAYMENT`…`REFUNDED`), уникальный `№ES-{id}`, частичный индекс `uq_shop_orders_open` — не более одного незакрытого заказа на товар у пользователя. Город + ПВЗ (служба: СДЭК/Boxberry/Ozon/Wildberries/Яндекс Маркет/ПЭК + адрес). |
| **Оплата** | Только внутренний баланс (USD) **или** «Я оплатил» → админ подтверждает (`AWAITING_CONFIRM` → `PAID`, `pay_method='manual'`, `reserve_stock`). Данные карт не принимаются. |
| **Баланс** | В USD. Пополнение: USDT TRC20 (ручная заявка + админ-подтверждение) или рубли (заявка владельцу → реквизиты → зачисление). Все операции — транзакции с `external_id` (`manual_topup:N`, `rub_topup:N`, `shop:pay:{oid}`, `shop_refund:{oid}`) — двойное списание невозможно. |
| **Склад** | Списывается при подтверждении оплаты (`reserve_stock`). Возврат на склад — только для `PAID/CONFIRMED/PREPARING` (`RESTOCK_STATUSES`). |
| **Админ-панель** | Доступ строго по `ADMIN_IDS`. Защита от двойных действий: `Busy` (процесс) + `FOR UPDATE` + уникальные индексы. Разделы: Заказы, Каталог, Пополнения, Пользователи, Поддержка, Статистика, Настройки, Рассылка, Экспорт CSV. |
| **Поддержка** | Обращения через msgmap; админ отвечает реплаем на пересланное сообщение или `/reply`. |
| **Наследие** | Сохранены: msgmap-поддержка, рассылка, CSV-экспорт (9 таблиц), бэкап-воркер, CBR-курс (каждые 6ч), keepalive-сервер, ручные пополнения USDT/рубли. Старые таблицы не тронуты. |

## Переменные окружения (полный список)

| Переменная | Обязательна | Дефолт | Описание |
|------------|-------------|--------|----------|
| `BOT_TOKEN` | ✅ | — | Токен бота от @BotFather |
| `ADMIN_IDS` | ✅ | — | Числовые Telegram ID админов через запятую |
| `OWNER_ID` | — | первый `ADMIN_IDS` | Telegram ID владельца, который подтверждает возвраты |
| `DATABASE_URL` | ✅ | — | PostgreSQL connection string |
| `PORT` | — | `8080` | Keepalive-порт (Render задаёт сам) |
| `MANUAL_WALLET_ADDRESS` | — | `""` | USDT TRC20 кошелёк (пусто = кнопка скрыта) |
| `LOW_STOCK_THRESHOLD` | — | `2` | Порог уведомления админа о низком остатке |
| `ACTIVE_DAYS` | — | `30` | Окно активности для рассылки |
| `BACKUP_INTERVAL_HOURS` | — | `0` | Интервал авто-бэкапа CSV (0 = выкл) |

## Деплой на Render

1. Создайте **PostgreSQL** базу (Render PostgreSQL / Neon / Supabase)
2. Создайте **Web Service**:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot-manual-topup.py`
3. Добавьте Environment Variables (все из `.env`)
4. Render сам выставит `PORT` — бот подхватит его автоматически
4. Keepalive эндпоинт доступен на `https://<app>.onrender.com/` → возвращает `OK`

## Тестирование
- `ast.parse` — все 18 `.py` файлов синтаксически валидны (0 ошибок)
- Импорт-смок в venv с фейковым `DATABASE_URL` — успешно (диспетчер собирается, 11 роутеров)
- Реальные миграции и run-time требуют живой PostgreSQL и Telegram

## Legacy-файл
`bot-manual-topup-1.py` (3666 строк) оставлен нетронутым в корне проекта как референс старых механизмов.