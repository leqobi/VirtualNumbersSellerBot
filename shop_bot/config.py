"""Конфигурация EXAMPLE SPLIT. Все секреты — только из переменных окружения."""

import os


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
ADMINS: list[int] = []
try:
    ADMINS = [int(x.strip()) for x in required_env("ADMIN_IDS").split(",") if x.strip()]
except ValueError as exc:
    raise RuntimeError("ADMIN_IDS must contain only Telegram numeric IDs separated by commas") from exc
if not ADMINS:
    raise RuntimeError("ADMIN_IDS must contain at least one Telegram numeric ID")

# PostgreSQL (Render / Neon / Supabase / локальный сервер).
DATABASE_URL = required_env("DATABASE_URL")
# Render задаёт сам; локально — 8080 по умолчанию, чтобы не конфликтовать.
PORT = int(os.getenv("PORT", "8080"))

# Кошелёк USDT TRC20 для ручного пополнения баланса; задаётся в переменных окружения.
MANUAL_WALLET_ADDRESS = os.getenv("MANUAL_WALLET_ADDRESS", "").strip()

# Курс USD/RUB — стартовое значение, затем обновляется с сайта ЦБ.
USD_RUB_RATE_FALLBACK = 85.4811
CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"

# Уведомлять админа, если остаток товара меньше порога.
LOW_STOCK_THRESHOLD = env_int("LOW_STOCK_THRESHOLD", 2, 1)
# «Активный» пользователь для рассылки.
ACTIVE_DAYS = env_int("ACTIVE_DAYS", 30, 1)
# Автобэкап CSV админам; 0 = выключен.
BACKUP_INTERVAL_HOURS = env_int("BACKUP_INTERVAL_HOURS", 0, 0)

# Лимиты ввода.
MAX_PRICE_RUB = 10_000_000.0      # максимум цены товара, ₽
MAX_ADJUST_USD = 100_000.0        # максимум ручного начисления баланса, USD
MAX_TOPUP_USD = 10_000.0          # максимум суммы заявки на пополнение, USD
MAX_QTY = 20                      # максимум quantity в одном заказе
TG_CHUNK = 4000                   # безопасный размер сообщения (< 4096)

SHOP_NAME = "EXAMPLE SPLIT"

# ---------------- статусы заказа ----------------
ORDER_STATUS_EMOJI = {
    "AWAITING_PAYMENT": "💳",
    "AWAITING_CONFIRM": "⏳",
    "PAID": "✅",
    "CONFIRMED": "🧑‍💼",
    "PREPARING": "📦",
    "SHIPPED": "🚚",
    "DELIVERED": "📬",
    "COMPLETED": "🏁",
    "CANCELLED": "❌",
    "REFUNDED": "↩️",
}

ORDER_STATUS_LABELS = {
    "AWAITING_PAYMENT": "Ожидает оплаты",
    "AWAITING_CONFIRM": "Ожидает подтверждения",
    "PAID": "Оплачен",
    "CONFIRMED": "Подтверждён продавцом",
    "PREPARING": "Готовится к отправке",
    "SHIPPED": "Отправлен",
    "DELIVERED": "Доставлен",
    "COMPLETED": "Завершён",
    "CANCELLED": "Отменён",
    "REFUNDED": "Возврат",
}

# Статусы, в которых заказ считается оплаченным (в статистике выручки).
PAID_STATUSES = frozenset({
    "PAID", "CONFIRMED", "PREPARING", "SHIPPED", "DELIVERED", "COMPLETED",
})

# Статусы, в которых заказ можно отменить/оформить возврат: оплата и выдача уже были.
ACTIVE_AFTER_PAY = frozenset({"PAID", "CONFIRMED", "PREPARING"})
# Возврат товара на склад возможен, пока он ещё не отправлен.
RESTOCK_STATUSES = frozenset({"PAID", "CONFIRMED", "PREPARING"})
# Пользователь сам может отменить только неоплаченный заказ.
USER_CANCELLABLE = frozenset({"AWAITING_PAYMENT", "AWAITING_CONFIRM"})

# Разрешённые переходы статусов (админ): статус -> доступные следующий статусы.
NEXT_STATUSES = {
    "AWAITING_PAYMENT": ("PAID", "REFUNDED", "CANCELLED"),
    "AWAITING_CONFIRM": ("PAID", "AWAITING_PAYMENT", "CANCELLED", "REFUNDED"),
    "PAID": ("CONFIRMED", "CANCELLED", "REFUNDED"),
    "CONFIRMED": ("PREPARING", "CANCELLED", "REFUNDED"),
    "PREPARING": ("SHIPPED", "CANCELLED", "REFUNDED"),
    "SHIPPED": ("DELIVERED", "REFUNDED"),
    "DELIVERED": ("COMPLETED", "REFUNDED"),
    "COMPLETED": ("REFUNDED",),
    "CANCELLED": (),
    "REFUNDED": (),
}

# Способы оплаты заказа.
PAY_METHOD_LABELS = {
    "balance": "С внутреннего баланса",
    "manual": "Напрямую продавцу (подтверждено)",
    "claimed": "Ожидает подтверждения продавцом",
}

# Популярные пункты выдачи для выбора на шаге ПВЗ.
PVZ_SERVICES = ("СДЭК", "Boxberry", "Ozon", "Wildberries", "Яндекс Маркет", "ПЭК")

# Подпись типов транзакций (история операций, включая старые записи предыдущего бота).
TX_LABELS = {
    "topup": "Пополнение",
    "purchase": "Покупка",
    "refund": "Возврат",
    "admin_credit": "Начисление админом",
    "admin_debit": "Списание админом",
    "order_payment": "Оплата заказа",
    "shop_refund": "Возврат по заказу",
}

# Ключи таблицы settings и значения по умолчанию.
DEFAULT_SETTINGS = {
    "about_text": (
        "Те же техника и электроника, только в Telegram.\n\n"
        "В каталоге — смартфоны, iPhone, Android, наушники, планшеты, "
        "ноутбуки и аксессуары.\n"
        "Оформление заказа на себя: город и ПВЗ — кто удобнее.\n"
        "Оплата — с внутреннего баланса или напрямую продавцу.\n"
        "Доставка — по РФ, работает менеджер. Напишите в поддержку."
    ),
    "welcome_text": (
        "Добро пожаловать в EXAMPLE SPLIT — магазин техники и электроники.\n\n"
        "Каталог, заказы, оплата и поддержка — ниже в меню.\n"
        "Начните с раздела каталога."
    ),
}
