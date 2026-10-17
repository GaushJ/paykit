"""In-memory PaymentRepository - for tests and quick starts. Not for production
(nothing survives a process restart, and dedupe/locking only hold within one process).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime

from paykit.core.models import (
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    Refund,
    Subscription,
)


class InMemoryRepository:
    _NON_FINAL_ORDER_STATUSES = {OrderStatus.CREATED, OrderStatus.ATTEMPTED}
    _NON_FINAL_PAYMENT_STATUSES = {PaymentStatus.CREATED, PaymentStatus.AUTHORIZED}

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._payments: dict[str, Payment] = {}
        self._refunds: dict[str, Refund] = {}
        self._plans: dict[str, Plan] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._processed_events: set[str] = set()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        async with self._lock:
            snapshot = (
                dict(self._orders),
                dict(self._payments),
                dict(self._refunds),
                dict(self._plans),
                dict(self._subscriptions),
                set(self._processed_events),
            )
            try:
                yield
            except Exception:
                (
                    self._orders,
                    self._payments,
                    self._refunds,
                    self._plans,
                    self._subscriptions,
                    self._processed_events,
                ) = snapshot
                raise

    # Orders

    async def create_order(self, order: Order) -> Order:
        self._orders[order.id] = order.model_copy()
        return order.model_copy()

    async def get_order(self, order_id: str) -> Order | None:
        order = self._orders.get(order_id)
        return order.model_copy() if order else None

    async def update_order(self, order: Order) -> Order:
        if order.id not in self._orders:
            raise KeyError(f"no order with id {order.id!r}")
        self._orders[order.id] = order.model_copy()
        return order.model_copy()

    async def get_order_by_provider_id(
        self, provider_order_id: str, *, for_update: bool = False
    ) -> Order | None:
        for order in self._orders.values():
            if order.provider_order_id == provider_order_id:
                return order.model_copy()
        return None

    async def get_order_by_idempotency_key(self, idempotency_key: str) -> Order | None:
        for order in self._orders.values():
            if order.idempotency_key == idempotency_key:
                return order.model_copy()
        return None

    async def list_non_final_orders(self, since: datetime) -> list[Order]:
        return [
            order.model_copy()
            for order in self._orders.values()
            if order.status in self._NON_FINAL_ORDER_STATUSES and order.created_at >= since
        ]

    # Payments

    async def create_payment(self, payment: Payment) -> Payment:
        self._payments[payment.id] = payment.model_copy()
        return payment.model_copy()

    async def get_payment(self, payment_id: str) -> Payment | None:
        payment = self._payments.get(payment_id)
        return payment.model_copy() if payment else None

    async def update_payment(self, payment: Payment) -> Payment:
        if payment.id not in self._payments:
            raise KeyError(f"no payment with id {payment.id!r}")
        self._payments[payment.id] = payment.model_copy()
        return payment.model_copy()

    async def get_payment_by_provider_id(
        self, provider_payment_id: str, *, for_update: bool = False
    ) -> Payment | None:
        for payment in self._payments.values():
            if payment.provider_payment_id == provider_payment_id:
                return payment.model_copy()
        return None

    async def list_payments_for_order(self, order_id: str) -> list[Payment]:
        return [p.model_copy() for p in self._payments.values() if p.order_id == order_id]

    async def list_non_final_payments(self, since: datetime) -> list[Payment]:
        return [
            payment.model_copy()
            for payment in self._payments.values()
            if payment.status in self._NON_FINAL_PAYMENT_STATUSES
            and payment.created_at >= since
        ]

    # Refunds

    async def create_refund(self, refund: Refund) -> Refund:
        self._refunds[refund.id] = refund.model_copy()
        return refund.model_copy()

    async def get_refund(self, refund_id: str) -> Refund | None:
        refund = self._refunds.get(refund_id)
        return refund.model_copy() if refund else None

    async def update_refund(self, refund: Refund) -> Refund:
        if refund.id not in self._refunds:
            raise KeyError(f"no refund with id {refund.id!r}")
        self._refunds[refund.id] = refund.model_copy()
        return refund.model_copy()

    async def get_refund_by_provider_id(self, provider_refund_id: str) -> Refund | None:
        for refund in self._refunds.values():
            if refund.provider_refund_id == provider_refund_id:
                return refund.model_copy()
        return None

    # Plans

    async def create_plan(self, plan: Plan) -> Plan:
        self._plans[plan.id] = plan.model_copy()
        return plan.model_copy()

    async def get_plan(self, plan_id: str) -> Plan | None:
        plan = self._plans.get(plan_id)
        return plan.model_copy() if plan else None

    async def get_plan_by_provider_id(self, provider_plan_id: str) -> Plan | None:
        for plan in self._plans.values():
            if plan.provider_plan_id == provider_plan_id:
                return plan.model_copy()
        return None

    # Subscriptions

    async def create_subscription(self, subscription: Subscription) -> Subscription:
        self._subscriptions[subscription.id] = subscription.model_copy()
        return subscription.model_copy()

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        sub = self._subscriptions.get(subscription_id)
        return sub.model_copy() if sub else None

    async def update_subscription(self, subscription: Subscription) -> Subscription:
        if subscription.id not in self._subscriptions:
            raise KeyError(f"no subscription with id {subscription.id!r}")
        self._subscriptions[subscription.id] = subscription.model_copy()
        return subscription.model_copy()

    async def get_subscription_by_provider_id(
        self, provider_subscription_id: str, *, for_update: bool = False
    ) -> Subscription | None:
        for sub in self._subscriptions.values():
            if sub.provider_subscription_id == provider_subscription_id:
                return sub.model_copy()
        return None

    # Event dedupe

    async def record_event_if_new(self, event_id: str) -> bool:
        if event_id in self._processed_events:
            return False
        self._processed_events.add(event_id)
        return True
