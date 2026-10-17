"""Webhook event parsing + normalization (§8.5).

paykit's normalized event *type* strings are identical to Razorpay's own
event names for every event we recognize - so normalization is really just
"is this one we know about". Anything else is passed through as
`provider.<original_name>` so apps can still opt in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from paykit.core.models import (
    Order,
    Payment,
    PaymentEvent,
    ProviderOrder,
    ProviderPayment,
    ProviderRefund,
    ProviderSubscription,
    Refund,
    Subscription,
)
from paykit.providers.razorpay.mapping import (
    order_from_razorpay,
    payment_from_razorpay,
    refund_from_razorpay,
    subscription_from_razorpay,
)

KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "payment.authorized",
        "payment.captured",
        "payment.failed",
        "order.paid",
        "refund.created",
        "refund.processed",
        "refund.failed",
        "subscription.authenticated",
        "subscription.activated",
        "subscription.charged",
        "subscription.pending",
        "subscription.halted",
        "subscription.paused",
        "subscription.resumed",
        "subscription.cancelled",
        "subscription.completed",
    }
)


def normalize_event_type(razorpay_event: str) -> str:
    if razorpay_event in KNOWN_EVENTS:
        return razorpay_event
    return f"provider.{razorpay_event}"


# The adapter layer has no repository access, so it can't know paykit's
# internal ids for these objects - only the provider's. These conversions
# use the provider id as a stand-in domain id (and `created_at` as a
# stand-in `updated_at`, since we don't know the real one either).
# PaymentService.handle_webhook (core/service.py) looks each object up by
# its provider id, carries over the *real* internal id/timestamps when one
# already exists, and only dispatches to app handlers after that
# reconciliation - so handlers never see these placeholders.


def _order_to_domain(po: ProviderOrder) -> Order:
    return Order(
        id=po.provider_order_id,
        provider="razorpay",
        provider_order_id=po.provider_order_id,
        amount=po.amount,
        status=po.status,
        receipt=po.receipt,
        customer_ref=None,
        metadata=po.notes,
        idempotency_key=None,
        created_at=po.created_at,
        updated_at=po.created_at,
    )


def _payment_to_domain(pp: ProviderPayment) -> Payment:
    return Payment(
        id=pp.provider_payment_id,
        provider="razorpay",
        provider_payment_id=pp.provider_payment_id,
        order_id=pp.provider_order_id,
        subscription_id=pp.provider_subscription_id,
        amount=pp.amount,
        amount_refunded=pp.amount_refunded,
        status=pp.status,
        method=pp.method,
        error_code=pp.error_code,
        error_description=pp.error_description,
        created_at=pp.created_at,
        updated_at=pp.created_at,
    )


def _refund_to_domain(pr: ProviderRefund) -> Refund:
    return Refund(
        id=pr.provider_refund_id,
        provider_refund_id=pr.provider_refund_id,
        payment_id=pr.provider_payment_id,
        amount=pr.amount,
        status=pr.status,
        reason=pr.notes.get("reason"),
        created_at=pr.created_at,
    )


def _subscription_to_domain(ps: ProviderSubscription) -> Subscription:
    return Subscription(
        id=ps.provider_subscription_id,
        provider_subscription_id=ps.provider_subscription_id,
        plan_id=ps.provider_plan_id,
        customer_ref=None,
        status=ps.status,
        current_start=ps.current_start,
        current_end=ps.current_end,
        total_count=ps.total_count,
        paid_count=ps.paid_count,
        short_url=ps.short_url,
        metadata=ps.notes,
    )


def build_payment_event(body: dict[str, Any], *, event_id: str) -> PaymentEvent:
    """Build a normalized PaymentEvent from a parsed webhook body.

    Caller is responsible for verifying the signature against the raw
    bytes *before* parsing JSON and calling this.
    """
    razorpay_event = body["event"]
    payload = body.get("payload", {})

    subscription_entity = payload.get("subscription", {}).get("entity")
    provider_subscription_id = subscription_entity["id"] if subscription_entity else None

    order = None
    if "order" in payload:
        order = _order_to_domain(order_from_razorpay(payload["order"]["entity"]))

    payment = None
    if "payment" in payload:
        payment = _payment_to_domain(
            payment_from_razorpay(
                payload["payment"]["entity"], provider_subscription_id=provider_subscription_id
            )
        )

    refund = None
    if "refund" in payload:
        refund = _refund_to_domain(refund_from_razorpay(payload["refund"]["entity"]))

    subscription = None
    if subscription_entity:
        subscription = _subscription_to_domain(subscription_from_razorpay(subscription_entity))

    return PaymentEvent(
        id=event_id,
        type=normalize_event_type(razorpay_event),
        provider="razorpay",
        occurred_at=datetime.fromtimestamp(body["created_at"], tz=UTC),
        order=order,
        payment=payment,
        refund=refund,
        subscription=subscription,
        raw=body,
    )
