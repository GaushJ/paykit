"""Allowed state transitions for domain statuses.

Webhooks can arrive out of order or be redelivered. `can_transition` is the
single source of truth for whether a status change should be applied.
Illegal or backward transitions are not exceptions - callers should check
`can_transition` first, apply if True, and otherwise log-and-ignore. The
same-status "transition" (e.g. a redelivered webhook) is always allowed as
a no-op.
"""

from __future__ import annotations

from typing import Any

from paykit.core.models import OrderStatus, PaymentStatus, RefundStatus, SubscriptionStatus

PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset(
        {PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.FAILED}
    ),
    PaymentStatus.AUTHORIZED: frozenset({PaymentStatus.CAPTURED, PaymentStatus.FAILED}),
    PaymentStatus.CAPTURED: frozenset(
        {PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}
    ),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset(
        {PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}
    ),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.REFUNDED: frozenset(),
}

ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {OrderStatus.ATTEMPTED, OrderStatus.PAID, OrderStatus.EXPIRED}
    ),
    OrderStatus.ATTEMPTED: frozenset(
        {OrderStatus.ATTEMPTED, OrderStatus.PAID, OrderStatus.EXPIRED}
    ),
    OrderStatus.PAID: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}

REFUND_TRANSITIONS: dict[RefundStatus, frozenset[RefundStatus]] = {
    RefundStatus.PENDING: frozenset({RefundStatus.PROCESSED, RefundStatus.FAILED}),
    RefundStatus.PROCESSED: frozenset(),
    RefundStatus.FAILED: frozenset(),
}

SubStatus = SubscriptionStatus
SUBSCRIPTION_TRANSITIONS: dict[SubscriptionStatus, frozenset[SubscriptionStatus]] = {
    SubStatus.CREATED: frozenset(
        {SubStatus.AUTHENTICATED, SubStatus.CANCELLED, SubStatus.EXPIRED}
    ),
    SubStatus.AUTHENTICATED: frozenset(
        {SubStatus.ACTIVE, SubStatus.CANCELLED, SubStatus.EXPIRED}
    ),
    SubStatus.ACTIVE: frozenset(
        {
            SubStatus.PENDING,
            SubStatus.HALTED,
            SubStatus.PAUSED,
            SubStatus.CANCELLED,
            SubStatus.COMPLETED,
            SubStatus.EXPIRED,
        }
    ),
    SubStatus.PENDING: frozenset(
        {
            SubStatus.ACTIVE,
            SubStatus.HALTED,
            SubStatus.CANCELLED,
            SubStatus.COMPLETED,
            SubStatus.EXPIRED,
        }
    ),
    SubStatus.HALTED: frozenset({SubStatus.ACTIVE, SubStatus.CANCELLED, SubStatus.EXPIRED}),
    SubStatus.PAUSED: frozenset({SubStatus.ACTIVE, SubStatus.CANCELLED, SubStatus.EXPIRED}),
    SubStatus.CANCELLED: frozenset(),
    SubStatus.COMPLETED: frozenset(),
    SubStatus.EXPIRED: frozenset(),
}

_TRANSITION_TABLES: dict[type, dict[Any, frozenset[Any]]] = {
    PaymentStatus: PAYMENT_TRANSITIONS,
    OrderStatus: ORDER_TRANSITIONS,
    RefundStatus: REFUND_TRANSITIONS,
    SubscriptionStatus: SUBSCRIPTION_TRANSITIONS,
}


def can_transition(old: object, new: object) -> bool:
    """Return whether moving from `old` to `new` is allowed.

    `old` and `new` must be the same status enum type. Same-status is
    always allowed (idempotent redelivery). Unknown enum types raise
    TypeError - that's a programming error, not a runtime data issue.
    """
    if type(old) is not type(new):
        raise TypeError(f"status types differ: {type(old)!r} vs {type(new)!r}")
    if old == new:
        return True
    table = _TRANSITION_TABLES.get(type(old))
    if table is None:
        raise TypeError(f"no transition table for {type(old)!r}")
    return new in table.get(old, frozenset())
