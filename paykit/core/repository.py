"""Persistence contract. Implementations: storage/memory.py, storage/sqlalchemy/repository.py."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from paykit.core.models import Order, Payment, Plan, Refund, Subscription


class PaymentRepository(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[None]:
        """Unit-of-work context manager: `async with repo.transaction(): ...`."""
        ...

    # `for_update=True` requests a row lock (`SELECT ... FOR UPDATE` on
    # Postgres) so concurrent webhook deliveries for the same object
    # serialize instead of racing. Only meaningful inside `transaction()`;
    # backends that can't lock (SQLite, the in-memory repo) ignore it.
    #
    # `create_*` methods must raise `paykit.core.errors.DuplicateError` when
    # an insert violates a uniqueness constraint (provider ids, idempotency
    # key), without aborting an enclosing `transaction()` (use a SAVEPOINT).

    # Orders
    async def create_order(self, order: Order) -> Order: ...
    async def get_order(self, order_id: str) -> Order | None: ...
    async def update_order(self, order: Order) -> Order: ...
    async def get_order_by_provider_id(
        self, provider_order_id: str, *, for_update: bool = False
    ) -> Order | None: ...
    async def get_order_by_idempotency_key(self, idempotency_key: str) -> Order | None: ...
    async def list_non_final_orders(
        self, since: datetime, *, limit: int | None = None
    ) -> list[Order]: ...

    # Payments
    async def create_payment(self, payment: Payment) -> Payment: ...
    async def get_payment(
        self, payment_id: str, *, for_update: bool = False
    ) -> Payment | None: ...
    async def update_payment(self, payment: Payment) -> Payment: ...
    async def get_payment_by_provider_id(
        self, provider_payment_id: str, *, for_update: bool = False
    ) -> Payment | None: ...
    async def list_payments_for_order(self, order_id: str) -> list[Payment]: ...
    async def list_non_final_payments(
        self, since: datetime, *, limit: int | None = None
    ) -> list[Payment]: ...

    # Refunds
    async def create_refund(self, refund: Refund) -> Refund: ...
    async def get_refund(self, refund_id: str) -> Refund | None: ...
    async def update_refund(self, refund: Refund) -> Refund: ...
    async def get_refund_by_provider_id(self, provider_refund_id: str) -> Refund | None: ...

    # Plans
    async def create_plan(self, plan: Plan) -> Plan: ...
    async def get_plan(self, plan_id: str) -> Plan | None: ...
    async def get_plan_by_provider_id(self, provider_plan_id: str) -> Plan | None: ...

    # Subscriptions
    async def create_subscription(self, subscription: Subscription) -> Subscription: ...
    async def get_subscription(self, subscription_id: str) -> Subscription | None: ...
    async def update_subscription(self, subscription: Subscription) -> Subscription: ...
    async def get_subscription_by_provider_id(
        self, provider_subscription_id: str, *, for_update: bool = False
    ) -> Subscription | None: ...

    # Event dedupe
    async def record_event_if_new(self, event_id: str) -> bool:
        """Atomically record that `event_id` was processed.

        Returns False if it was already recorded (duplicate delivery),
        True if this call recorded it for the first time. Must be backed
        by a unique DB constraint, not an in-memory set, so it's safe
        across processes.
        """
        ...

    async def prune_processed_events(self, older_than: datetime) -> int:
        """Delete dedupe records older than `older_than`; return how many.

        Keep comfortably longer than the provider's redelivery window
        (Razorpay retries for ~24h) - see PaymentService.prune_processed_events.
        """
        ...
