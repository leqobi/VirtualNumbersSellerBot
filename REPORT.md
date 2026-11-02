# Итоговый отчёт: EXAMPLE SPLIT

## Что сделано

### 1. Новая модульная структура (`shop_bot/`)
Создан пакет из 14 файлов вместо монолита 3666 строк:

| Файл | Строк | Назначение |
|------|-------|------------|
| `config.py` | 149 | env, статусы заказов (10 шт.), `NEXT_STATUSES`, `PAID_STATUSES`, `PVZ_SERVICES`, лимиты |
| `db.py` | 283 | Пул asyncpg, advisory-lock миграции (id 591833), схема + адитивные миграции |
| `helpers.py` | 331 | UI-хелперы (esc, kb, fmt_rub, money, split_message, show, Busy), CBR-курс |
| `services.py` | 308 | Баланс (`adjust_balance`), заказы (`reserve_stock`, `restock_order`, `apply_refund/cancel`, `switch_status`), уведомления |
| `main.py` | 212 | Точка входа: пул, миграции, keepalive aiohttp, middleware (бан/апсерт), error-handler, роутеры, фоновые воркеры |
| `routers/admin_panel.py` | 397 | Админ-меню, настройки, статистика, рассылка, экспорт CSV, поддержка (msgmap) |
| `routers/admin_catalog.py` | 646 | CRUD категорий/товаров: названия, описания, цены, склад, фото, активность |
| `routers/admin_orders.py` | 500+ | Заказы: список/фильтры/поиск, подтверждение оплаты (склад в транзакции), смена статусов по `NEXT_STATUSES`, трекинг, заметки, отмена/возврат |
| `routers/admin_payments.py` | 150 | Заявки на пополнение: USDT (mtpay/mtdeny через `adjust_balance`), рублёвые (ref) |
| `routers/admin_users.py` | 280 | Пользователи: поиск, карточка, бан, ±баланс, заказы, `/user`, `/addbalance` |
| `routers/catalog.py` | 212 | Пользовательский каталог: категории → товары → карточка с фото |
| `routers/checkout.py` | 489 | FSM покупки: кол-во → город → ПВЗ (СДЭК/Boxberry/…) → подтверждение → оплата |
| `routers/common.py` | 170 | `/start`, reply-меню (6 кнопок), профиль |
| `routers/orders.py` | 127 | Мои заказы: список, карточка, отмена (неоплаченные) |
| `routers/support.py` | 186 | Обращения в поддержку, ответы админа (reply/msgmap) |
| `routers/topup.py` | 516 | 💳 Оплата: баланс, пополнения USDT/рубли, история операций |

**Итого: ~4 500 строк чистого модульного кода**.

---

### 2. Сохранено из legacy (не удаляли без причины)
- `msgmap`-поддержка (пересылка обращений админам, ответы реплаем, `/reply`, `answered` флаг)
- Рассылка (`/broadcast` + `active` фильтр) с rate-limit handling
- CSV-экспорт 9 таблиц (`users`, `categories`, `products`, `shop_orders`, `transactions`, `manual_topups`, `rub_topups`, `payments`, `numbers`)
- Авто-бэкап CSV админам каждые `BACKUP_INTERVAL_HOURS` (воркер в фоне)
- CBR-курс USD/RUB (фоновый воркер каждые 6ч, фоллбек 85.4811)
- Keepalive HTTP-сервер на `PORT` (healthcheck для Render)
- Ручные пополнения USDT TRC20 и рубли (две таблицы, два флоу подтверждения)
- Старые таблицы (`countries`, `lots`, `numbers`, `orders`, `sms_requests`, `payments`) — **не тронуты**, индексы дополнены

---

### 3. Новые бизнес-правила
- **БД-only каталог**: товары/категории только в БД, хардкода нет.
- **10 статусов заказов** с жёсткими переходами (`NEXT_STATUSES`) и проверками.
- **Оплата только с баланса или вручную** — никаких фейков, данные карт не принимаются.
- **Склад списывается только при подтверждении оплаты** (`reserve_stock` в транзакции); возврат — только для `PAID/CONFIRMED/PREPARING`.
- **Уникальный индекс `uq_shop_orders_open`** — запрещает дубли незакрытых заказов на один товар.
- **Номер заказа** — `№ES-{id}`.
- **Баланс в USD**, цены товаров в ₽; при оплате с баланса `charged_usd = round(amount_rub / USD_RUB_RATE, 4)`.
- **External_id в transactions** — защита от двойных списаний/зачислений (`shop:pay:{oid}`, `shop_refund:{oid}`, `manual_topup:N`, `rub_topup:N`).
- **Reply-меню** (6 кнопок) — у каждой есть обработчик.

---

### 4. Безопасность и устойчивость
- Админ-функции: фильтр `F.from_user.id.in_(ADMINS)` — доступ только для IDs из env.
- Защита от двойных действий: `Busy` (in-process) + `SELECT … FOR UPDATE` + уникальные индексы БД.
- Транзакционность: все финансовые операции (баланс, склад, ledger, статус) — в одной `asyncpg` транзакции.
- Middleware: апсерт пользователя при каждом апдейте + блокировка забаненных (админы проходят без проверки).
- Глобальный error-handler — логирует и шлёт пользователю «Произошла ошибка, попробуйте позже».
- Advisory-lock миграций (`591833`) — безопасный запуск параллельно со старым инстансом (`727001`).

---

### 5. Верификация
| Проверка | Результат |
|----------|-----------|
| `ast.parse` всех 18 `.py` файлов | ✅ 0 ошибок |
| Виртуальное окружение + `pip install -r requirements.txt` | ✅ aiogram 3.31, asyncpg 0.32, aiohttp 3.14 |
| Импорт-смок `python -m shop_bot.main` с фейковым `DATABASE_URL` | ✅ Диспетчер собирается, все 11 роутеров подключены, middleware загружен |
| Реальные миграции / PostgreSQL | ❌ **не тестировались** — в системе нет PostgreSQL, `pgserver` недоступен в pip |

---

### 6. Что нужно владельцу для запуска в проде
Добавить в окружение (`.env` или переменные хостинга):
```
BOT_TOKEN=...                 # от @BotFather
ADMIN_IDS=111222333,444555666 # числовые Telegram ID админов через запятую
DATABASE_URL=postgresql://... # PostgreSQL (Render/Neon/Supabase/локальный)
PORT=8080                     # keepalive-порт (Render задаёт сам)
MANUAL_WALLET_ADDRESS=TXYZ... # USDT TRC20 кошелёк (если нужен USDT-путь)
LOW_STOCK_THRESHOLD=2         # уведомлять при остатке < N
ACTIVE_DAYS=30                # окно «активен» для рассылки
BACKUP_INTERVAL_HOURS=24      # 0 = выключить авто-бэкап CSV
```

**Команда запуска:** `python -m shop_bot.main`

---

### 7. Интеграции, требующие внешних данных
| Интеграция | Статус | Что нужно |
|-----------|--------|-----------|
| Telegram Bot API | Готово | `BOT_TOKEN`, `ADMIN_IDS` |
| PostgreSQL | Готово (миграции написаны) | `DATABASE_URL` |
| USDT TRC20 пополнения | Готово (ручное подтверждение) | `MANUAL_WALLET_ADDRESS` |
| Банковские карты | **Не встроено** | — |

---

### 8. Legacy-файл
`bot-manual-topup-1.py` (3666 строк) оставлен в корне `/home/star/Example Split/` как референс.