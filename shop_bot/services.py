"""Общие операции EXAMPLE SPLIT: баланс, жизненный цикл заказов, возвраты.

Финансовые операции выполняются по строгому контракту:
  1. Списание/начисление и запись в transactions — в одной транзакции БД;
  2. Уникальный external_id защищает от повторного зачисления/списания;
  3. Статус заказа переключается условным UPDATE — повторный вызов безопасен.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from aiogram import Bot

from . import db
from .config import (
    ACTIVE_AFTER_PAY,
    ORDER_STATUS_EMOJI,
    ORDER_STATUS_LABELS,
    PAID_STATUSES,
    PAY_METHOD_LABELS,
    RESTOCK_STATUSES,
    TX_LABELS,
)
from .helpers import fmt_rub, safe_send

fmt_ts = lambda ts: (datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
                     if ts else "—")

# Заявка на ручное пополнение закрывается, если в комментарии начисления указано
# «Пополнение #N» (именно такую команду бот присылает админу в уведомлении о заявке).
TOPUP_NOTE_RE = re.compile(r"Пополнение\s*#(\d{1,12})", re.IGNORECASE)
# Что считается «Пополнением» в статистике и карточке пользователя: новые записи
# ('topup') и старые ручные начисления с пометкой «Пополнение #N».
TOPUP_TX = "(type='topup' OR (type='admin_credit' AND note LIKE 'Пополнение #%'))"


def paid_in_sql() -> str:
    """SQL-фрагмент «статус в списке оплаченных» для запросов."""
    return "(" + ", ".join(f"'{s}'" for s in sorted(PAID_STATUSES)) + ")"


def order_number_text(order_id) -> str:
    return f"№ES-{order_id}"


def status_text(status) -> str:
    label = ORDER_STATUS_LABELS.get(status, str(status))
    return f"{ORDER_STATUS_EMOJI.get(status, '•')} {label}"


def status_label(status) -> str:
    return ORDER_STATUS_LABELS.get(status, str(status))


class BalanceError(Exception):
    pass


def topup_id_from_note(note):
    mt = TOPUP_NOTE_RE.search(note or "")
    return int(mt.group(1)) if mt else None


async def adjust_balance(admin_id, uid, amount, note):
    """Начисление/списание с записью в transactions.
    -> (новый баланс, id закрытой заявки на пополнение или None).

    Если в note есть «Пополнение #N», соответствующая заявка закрывается атомарно
    (защита от повторного зачисления по одной заявке).
    """
    topup_id = topup_id_from_note(note) if amount > 0 else None
    async with db.db().acquire() as con:
        async with con.transaction():
            if topup_id is not None:
                req = await con.fetchrow(
                    "SELECT user_id, status FROM manual_topups WHERE id=$1 FOR UPDATE", topup_id)
                if req is None or req["user_id"] != uid:
                    raise BalanceError(
                        f"Заявка #{topup_id} не найдена у пользователя {uid}. Начисление не выполнено.")
                if req["status"] != "pending":
                    raise BalanceError(
                        f"Заявка #{topup_id} уже обработана (статус: {req['status']}). "
                        "Начисление не выполнено.")
            row = await con.fetchrow(
                "UPDATE users SET balance=balance+$1 WHERE id=$2 AND balance+$1 >= -0.000000001 "
                "RETURNING balance", amount, uid)
            if row is None:
                exists = await con.fetchval("SELECT 1 FROM users WHERE id=$1", uid)
                raise BalanceError("not_found" if not exists else "insufficient")
            ts = int(datetime.now(timezone.utc).timestamp())
            if topup_id is None:
                await con.execute(
                    "INSERT INTO transactions(user_id,type,amount,currency,created_at,note,admin_id) "
                    "VALUES($1,$2,$3,'USD',$4,$5,$6)",
                    uid, "admin_credit" if amount > 0 else "admin_debit", amount,
                    ts, note, admin_id)
            else:
                await con.execute(
                    "INSERT INTO transactions(user_id,type,amount,currency,external_id,created_at,note,admin_id) "
                    "VALUES($1,'topup',$2,'USD',$3,$4,$5,$6)",
                    uid, amount, f"manual_topup:{topup_id}", ts, note, admin_id)
                await con.execute(
                    "UPDATE manual_topups SET status='completed', reviewed_at=$2, reviewed_by=$3, note=$4 "
                    "WHERE id=$1", topup_id, ts, admin_id, note)
    return float(row["balance"]), topup_id


# ---------------- заказы магазина ----------------
def now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


async def get_order(oid):
    return await db.fetchrow("SELECT * FROM shop_orders WHERE id=$1", oid)


async def order_full_row(oid):
    return await db.fetchrow(
        "SELECT o.*, u.username, u.name AS uname "
        "FROM shop_orders o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=$1", oid)


def order_title(o) -> str:
    return order_number_text(o["id"]) + " · " + str(o["product_name"])


def qty_line(o) -> str:
    return f"{o['qty']} × " if o["qty"] > 1 else ""


async def reserve_stock(oid, qty) -> bool:
    """Списывает товар со склада при подтверждении оплаты. False — товара нет."""
    o = await db.fetchrow("SELECT product_id FROM shop_orders WHERE id=$1", oid)
    if not o or o["product_id"] is None:
        return True  # товар был удалён — списывать нечего, оплату не блокируем
    row = await db.fetchrow(
        "UPDATE products SET stock=stock-$2 WHERE id=$1 AND stock>=$2 RETURNING stock",
        o["product_id"], qty)
    return row is not None


async def restock_order(o) -> int:
    """Возвращает товар на склад при отмене/возврате (пока он не отправлен)."""
    if o["status"] not in RESTOCK_STATUSES or o["product_id"] is None:
        return 0
    row = await db.fetchval(
        "UPDATE products SET stock=stock+$2 WHERE id=$1 RETURNING stock",
        o["product_id"], o["qty"])
    return o["qty"] if row is not None else 0


async def refund_charged(o, con) -> float:
    """Возвращает удержание с баланса (если оплата была с баланса) — ровно один раз."""
    if o["status"] not in ACTIVE_AFTER_PAY:
        return 0.0
    charged = float(o["charged_amount"] or 0)
    if o["pay_method"] == "balance" and charged > 0:
        res = await con.fetchrow(
            "UPDATE users SET balance=balance+$1 WHERE id=$2 RETURNING balance",
            charged, o["user_id"])
        if res is not None:
            await con.execute(
                "INSERT INTO transactions(user_id,type,amount,currency,external_id,created_at,note) "
                "VALUES($1,'shop_refund',$2,'USD',$3,$4,$5)",
                o["user_id"], charged, f"shop_refund:{o['id']}", now(),
                f"Возврат по заказу {order_number_text(o['id'])}")
            return charged
    return 0.0


async def apply_refund(oid, admin_id=None):
    """Возврат по оплаченному заказу: статус REFUNDED, товар на склад, средства на баланс.
    -> (updated: bool, refund: float, order). Повторный вызов безопасен."""
    async with db.db().acquire() as con:
        async with con.transaction():
            o = await con.fetchrow("SELECT * FROM shop_orders WHERE id=$1 FOR UPDATE", oid)
            if not o:
                return False, 0.0, None
            if o["status"] in ("COMPLETED", "CANCELLED", "REFUNDED"):
                return False, 0.0, o
            refund = await refund_charged(o, con)
            await restock_order(o)
            updated = await con.fetchrow(
                "UPDATE shop_orders SET status='REFUNDED', refunded_at=$2 WHERE id=$1 RETURNING *",
                oid, now())
    return updated is not None, refund, updated


async def apply_cancel(oid, uid=None, admin_id=None):
    """Отмена заказа.
    uid задан — пользователь: только неоплаченные заказы.
    admin_id задан — админ: неоплаченный → CANCELLED; оплаченный → REFUNDED (+возврат средств).
    -> (result, refund, order)."""
    async with db.db().acquire() as con:
        async with con.transaction():
            o = await con.fetchrow("SELECT * FROM shop_orders WHERE id=$1 FOR UPDATE", oid)
            if not o or (uid is not None and o["user_id"] != uid):
                return "not_found", 0.0, None
            if o["status"] in ("COMPLETED", "CANCELLED", "REFUNDED"):
                return "not_cancellable", 0.0, o
            if uid is not None:
                if o["status"] not in ("AWAITING_PAYMENT", "AWAITING_CONFIRM"):
                    return "not_cancellable", 0.0, o
                await con.execute(
                    "UPDATE shop_orders SET status='CANCELLED', cancelled_at=$2 WHERE id=$1",
                    oid, now())
                return "cancelled", 0.0, o
            if o["status"] in ACTIVE_AFTER_PAY:
                refund = await refund_charged(o, con)
                await restock_order(o)
                await con.execute(
                    "UPDATE shop_orders SET status='REFUNDED', refunded_at=$2 WHERE id=$1",
                    oid, now())
                return "refunded", refund, o
            await con.execute(
                "UPDATE shop_orders SET status='CANCELLED', cancelled_at=$2 WHERE id=$1",
                oid, now())
            return "cancelled", 0.0, o


_STATUS_TIME_COL = {
    "PAID": "paid_at", "CONFIRMED": "confirmed_at", "PREPARING": "preparing_at",
    "SHIPPED": "shipped_at", "DELIVERED": "delivered_at", "COMPLETED": "completed_at",
    "CANCELLED": "cancelled_at", "REFUNDED": "refunded_at",
}
VALID_STATUSES = frozenset(_STATUS_TIME_COL) | {"AWAITING_PAYMENT", "AWAITING_CONFIRM"}


async def switch_status(oid, new_status) -> bool:
    """Смена статуса с отметкой времени. -> True, если статус изменён."""
    if new_status not in VALID_STATUSES:
        return False
    col = _STATUS_TIME_COL.get(new_status)
    extra = f", {col}={now()}" if col else ""
    updated = await db.fetchrow(
        f"UPDATE shop_orders SET status=$2{extra} WHERE id=$1 RETURNING id",
        oid, new_status)
    return updated is not None


def order_details_lines(o) -> list[str]:
    """Строки карточки заказа (для пользователя и админа)."""
    lines = [
        f"🛍 Товар: <b>{o['product_name']}</b>",
        f"Количество: <b>{o['qty']}</b>",
        f"💰 Сумма: <b>{fmt_rub(o['amount'])}</b>",
        "Статус: " + status_text(o["status"]),
        f"🏙 Город: <b>{o['city']}</b>",
        f"📍 ПВЗ: <b>{o['pvz']}</b>",
    ]
    if o["pay_method"]:
        lines.append("💳 Оплата: " + str(PAY_METHOD_LABELS.get(o["pay_method"], o["pay_method"])))
    if o["charged_amount"] is not None:
        lines.append(f"Списано: {o['charged_amount']:.2f} USD"
                     + (f" (курс {float(o['rate']):.4f})" if o["rate"] else ""))
    if o["tracking_number"]:
        lines.append(f"🚚 Номер отправления: <code>{o['tracking_number']}</code>")
    if o["delivery_note"]:
        lines.append(f"ℹ️ О доставке: {o['delivery_note']}")
    lines.append(f"🕒 Создан: {fmt_ts(o['created_at'])}")
    stamps = [
        ("Оплачен", "paid_at"), ("Подтверждён", "confirmed_at"),
        ("Готовится", "preparing_at"), ("Отправлен", "shipped_at"),
        ("Доставлен", "delivered_at"), ("Завершён", "completed_at"),
        ("Отменён", "cancelled_at"), ("Возврат", "refunded_at"),
    ]
    for title, col in stamps:
        if o[col]:
            lines.append(f"{title}: {fmt_ts(o[col])}")
    return lines


async def notify_status_change(bot: Bot, oid, new_status, extra: str = "") -> bool:
    """Сообщает пользователю об изменении статуса заказа."""
    order = await get_order(oid)
    if not order:
        return False
    text = (f"{order_number_text(oid)} · {order['product_name']}\n"
            f"Статус: {status_text(new_status)}")
    if extra:
        text += f"\n\n{extra}"
    return await safe_send(bot, order["user_id"], text)


async def notify_admins(bot: Bot, admin_ids, text, markup=None) -> int:
    """Отправляет уведомление всем администраторам. -> число доставленных."""
    delivered = 0
    for admin_id in admin_ids:
        if await safe_send(bot, admin_id, text, reply_markup=markup):
            delivered += 1
    return delivered


async def tx_label(tx_type) -> str:
    return TX_LABELS.get(tx_type, str(tx_type))


__all__ = [
    "BalanceError", "TOPUP_NOTE_RE", "TOPUP_TX", "topup_id_from_note",
    "adjust_balance", "order_number_text", "status_text", "status_label",
    "now", "get_order", "order_full_row", "order_title", "qty_line",
    "reserve_stock", "restock_order", "refund_charged",
    "apply_refund", "apply_cancel", "switch_status",
    "order_details_lines", "notify_status_change", "notify_admins",
    "tx_label", "VALID_STATUSES", "paid_in_sql",
]
