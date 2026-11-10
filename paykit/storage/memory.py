"""In-memory PaymentRepository - for tests and quick starts. Not for production
(nothing survives a process restart, and dedupe/locking only hold within one process).

Mirrors the SQL backend's unique constraints (raising DuplicateError) and keeps
secondary indexes so provider-id lookups are O(1) rather than full scans.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from paykit.core.errors import DuplicateError
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
        self._processed_events: dict[str, datetime] = {}
        # Secondary unique indexes: natural key -> internal id.
        self._order_by_provider_id: dict[str, str] = {}
        self._order_by_idempotency_key: dict[str, str] = {}
        self._payment_by_provider_id: dict[str, str] = {}
        self._refund_by_provider_id: dict[str, str] = {}
        self._plan_by_provider_id: dict[str, str] = {}
        self._subscription_by_provider_id: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._in_tx: contextvars.ContextVar[bool] = contextvars.ContextVar(
            f"paykit_memory_tx_{id(self)}", default=False
        )

    def _state(self) -> dict[str, Any]:
        skip = ("_lock", "_in_tx")
        return {k: v for k, v in vars(self).items() if k.startswith("_") and k not in skip}

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        if self._in_tx.get():
            yield  # nested: join the outer transaction (same as the SQL backend)
            return
        async with self._lock:
            token = self._in_tx.set(True)
            # Containers hold immutable-by-convention model copies, so a
            # shallow copy of each container is a complete snapshot.
            snapshot = {k: copy.copy(v) for k, v in self._state().items()}
            try:
                yield
            except BaseException:
                for key, value in snapshot.items():
                    setattr(self, key, value)
                raise
            finally:
                self._in_tx.reset(token)

    @staticmethod
    def _claim(index: dict[str, str], key: str | None, obj_id: str, what: str) -> None:
        if key is None:
            return
        owner = index.get(key)
        if owner is not None and owner != obj_id:
            raise DuplicateError(f"duplicate {what} {key!r}")
        index[key] = obj_id

    # Orders

    async def create_order(self, order: Order) -> Order:
        if order.id in self._orders:
            raise DuplicateError(f"duplicate order id {order.id!r}")
        if order.provider_order_id in self._order_by_provider_id:
            raise DuplicateError(f"duplicate provider_order_id {order.provider_order_id!r}")
        if order.idempotency_key and order.idempotency_key in self._order_by_idempotency_key:
            raise DuplicateError(f"duplicate idempotency_key {order.idempotency_key!r}")
        self._claim(self._order_by_provider_id, order.provider_order_id, order.id, "order")
        self._claim(self._order_by_idempotency_key, order.idempotency_key, order.id, "key")
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
        order_id = self._order_by_provider_id.get(provider_order_id)
        return await self.get_order(order_id) if order_id else None

    async def get_order_by_idempotency_key(self, idempotency_key: str) -> Order | None:
        order_id = self._order_by_idempotency_key.get(idempotency_key)
        return await self.get_order(order_id) if order_id else None

    async def list_non_final_orders(
        self, since: datetime, *, limit: int | None = None
    ) -> list[Order]:
        orders = sorted(
            (
                o
                for o in self._orders.values()
                if o.status in self._NON_FINAL_ORDER_STATUSES and o.created_at >= since
            ),
            key=lambda o: o.created_at,
        )
        return [o.model_copy() for o in orders[:limit]]

    # Payments

    async def create_payment(self, payment: Payment) -> Payment:
        if payment.id in self._payments:
            raise DuplicateError(f"duplicate payment id {payment.id!r}")
        self._claim(
            self._payment_by_provider_id, payment.provider_payment_id, payment.id, "payment"
        )
        self._payments[payment.id] = payment.model_copy()
        return payment.model_copy()

    async def get_payment(self, payment_id: str, *, for_update: bool = False) -> Payment | None:
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
        payment_id = self._payment_by_provider_id.get(provider_payment_id)
        return await self.get_payment(payment_id) if payment_id else None

    async def list_payments_for_order(self, order_id: str) -> list[Payment]:
        return [p.model_copy() for p in self._payments.values() if p.order_id == order_id]

    async def list_non_final_payments(
        self, since: datetime, *, limit: int | None = None
    ) -> list[Payment]:
        payments = sorted(
            (
                p
                for p in self._payments.values()
                if p.status in self._NON_FINAL_PAYMENT_STATUSES and p.created_at >= since
            ),
            key=lambda p: p.created_at,
        )
        return [p.model_copy() for p in payments[:limit]]

    # Refunds

    async def create_refund(self, refund: Refund) -> Refund:
        if refund.id in self._refunds:
            raise DuplicateError(f"duplicate refund id {refund.id!r}")
        self._claim(self._refund_by_provider_id, refund.provider_refund_id, refund.id, "refund")
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
        refund_id = self._refund_by_provider_id.get(provider_refund_id)
        return await self.get_refund(refund_id) if refund_id else None

    # Plans

    async def create_plan(self, plan: Plan) -> Plan:
        if plan.id in self._plans:
            raise DuplicateError(f"duplicate plan id {plan.id!r}")
        self._claim(self._plan_by_provider_id, plan.provider_plan_id, plan.id, "plan")
        self._plans[plan.id] = plan.model_copy()
        return plan.model_copy()

    async def get_plan(self, plan_id: str) -> Plan | None:
        plan = self._plans.get(plan_id)
        return plan.model_copy() if plan else None

    async def get_plan_by_provider_id(self, provider_plan_id: str) -> Plan | None:
        plan_id = self._plan_by_provider_id.get(provider_plan_id)
        return await self.get_plan(plan_id) if plan_id else None

    # Subscriptions

    async def create_subscription(self, subscription: Subscription) -> Subscription:
        if subscription.id in self._subscriptions:
            raise DuplicateError(f"duplicate subscription id {subscription.id!r}")
        self._claim(
            self._subscription_by_provider_id,
            subscription.provider_subscription_id,
            subscription.id,
            "subscription",
        )
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
        sub_id = self._subscription_by_provider_id.get(provider_subscription_id)
        return await self.get_subscription(sub_id) if sub_id else None

    # Event dedupe

    async def record_event_if_new(self, event_id: str) -> bool:
        if event_id in self._processed_events:
            return False
        self._processed_events[event_id] = datetime.now(UTC)
        return True

    async def prune_processed_events(self, older_than: datetime) -> int:
        stale = [e for e, at in self._processed_events.items() if at < older_than]
        for event_id in stale:
            del self._processed_events[event_id]
        return len(stale)
