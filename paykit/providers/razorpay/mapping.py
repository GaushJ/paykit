"""Razorpay JSON payloads/statuses <-> paykit's normalized Provider* models.

Field names below were checked against https://razorpay.com/docs/api/ and
https://razorpay.com/docs/webhooks/payloads/ (Sep 2026). Where the docs are
silent or ambiguous, this module says so in a `# NOTE:` rather than guessing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from paykit.core.models import (
    Money,
    OrderStatus,
    PaymentStatus,
    ProviderOrder,
    ProviderPayment,
    ProviderPlan,
    ProviderRefund,
    ProviderSubscription,
    RefundStatus,
    SubscriptionStatus,
)


def _epoch_to_dt(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def _string_notes(notes: Any) -> dict[str, str]:
    if not isinstance(notes, dict):
        return {}
    return {str(k): str(v) for k, v in notes.items()}


_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "created": OrderStatus.CREATED,
    "attempted": OrderStatus.ATTEMPTED,
    "paid": OrderStatus.PAID,
}


def map_order_status(raw: str) -> OrderStatus:
    # NOTE: the Razorpay order entity's documented `status` values are only
    # created/attempted/paid. OrderStatus.EXPIRED exists for paykit's own use
    # (e.g. a reconcile job marking a stale order), never returned by the API.
    # An unrecognized value degrades to ATTEMPTED rather than PAID, so we
    # never over-claim that money moved.
    return _ORDER_STATUS_MAP.get(raw, OrderStatus.ATTEMPTED)


_PAYMENT_STATUS_MAP: dict[str, PaymentStatus] = {
    "created": PaymentStatus.CREATED,
    "authorized": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "failed": PaymentStatus.FAILED,
    "refunded": PaymentStatus.REFUNDED,
}


def map_payment_status(raw: str, *, amount: int, amount_refunded: int) -> PaymentStatus:
    # NOTE: Razorpay's payment `status` only becomes "refunded" once the full
    # amount is back; a partial refund leaves status "captured" and just bumps
    # amount_refunded (see refund_status: "partial" on the payment entity).
    # We derive PARTIALLY_REFUNDED/REFUNDED from amount_refunded vs amount
    # instead of trusting `status` alone.
    if amount_refunded > 0 and raw != "refunded":
        return (
            PaymentStatus.REFUNDED
            if amount_refunded >= amount
            else PaymentStatus.PARTIALLY_REFUNDED
        )
    return _PAYMENT_STATUS_MAP.get(raw, PaymentStatus.CREATED)


def map_refund_status(raw: str) -> RefundStatus:
    try:
        return RefundStatus(raw)
    except ValueError:
        return RefundStatus.PENDING


def map_subscription_status(raw: str) -> SubscriptionStatus:
    try:
        return SubscriptionStatus(raw)
    except ValueError:
        # "resumed" is a webhook *event* name, not a subscription `status`
        # value (the entity's status goes back to "active"). Anything else
        # unrecognized is treated the same way: don't guess, stay put.
        if raw == "resumed":
            return SubscriptionStatus.ACTIVE
        raise


def order_from_razorpay(payload: dict[str, Any]) -> ProviderOrder:
    return ProviderOrder(
        provider_order_id=payload["id"],
        amount=Money(amount=payload["amount"], currency=payload["currency"]),
        status=map_order_status(payload["status"]),
        receipt=payload.get("receipt"),
        notes=_string_notes(payload.get("notes")),
        created_at=_epoch_to_dt(payload["created_at"]),
    )


def payment_from_razorpay(
    payload: dict[str, Any], *, provider_subscription_id: str | None = None
) -> ProviderPayment:
    amount = payload["amount"]
    amount_refunded = payload.get("amount_refunded", 0)
    return ProviderPayment(
        provider_payment_id=payload["id"],
        provider_order_id=payload.get("order_id"),
        # NOTE: the Razorpay payment entity has no `subscription_id` field.
        # For subscription charges the webhook envelope carries both a
        # `subscription` and a `payment` entity side by side, so callers that
        # know the subscription id (webhooks.py) pass it in explicitly.
        provider_subscription_id=provider_subscription_id,
        amount=Money(amount=amount, currency=payload["currency"]),
        amount_refunded=amount_refunded,
        status=map_payment_status(
            payload["status"], amount=amount, amount_refunded=amount_refunded
        ),
        method=payload.get("method"),
        error_code=payload.get("error_code"),
        error_description=payload.get("error_description"),
        created_at=_epoch_to_dt(payload["created_at"]),
    )


def refund_from_razorpay(payload: dict[str, Any]) -> ProviderRefund:
    return ProviderRefund(
        provider_refund_id=payload["id"],
        provider_payment_id=payload["payment_id"],
        amount=Money(amount=payload["amount"], currency=payload["currency"]),
        status=map_refund_status(payload["status"]),
        notes=_string_notes(payload.get("notes")),
        created_at=_epoch_to_dt(payload["created_at"]),
    )


def plan_from_razorpay(payload: dict[str, Any]) -> ProviderPlan:
    item = payload["item"]
    return ProviderPlan(
        provider_plan_id=payload["id"],
        name=item["name"],
        amount=Money(amount=item["amount"], currency=item["currency"]),
        period=payload["period"],
        interval=payload["interval"],
    )


def subscription_from_razorpay(payload: dict[str, Any]) -> ProviderSubscription:
    return ProviderSubscription(
        provider_subscription_id=payload["id"],
        provider_plan_id=payload["plan_id"],
        status=map_subscription_status(payload["status"]),
        current_start=(
            _epoch_to_dt(payload["current_start"]) if payload.get("current_start") else None
        ),
        current_end=_epoch_to_dt(payload["current_end"]) if payload.get("current_end") else None,
        total_count=payload.get("total_count"),
        paid_count=payload.get("paid_count", 0),
        short_url=payload.get("short_url"),
        notes=_string_notes(payload.get("notes")),
    )
