"""SQLAlchemy 2.0 async PaymentRepository (Postgres/SQLite).

Uses Core (not the ORM) - domain objects are plain Pydantic models, so
mapping rows <-> models by hand is simpler than maintaining a parallel set
of ORM-mapped classes.
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import Table, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from paykit.core.models import Money, Order, Payment, Plan, Refund, Subscription
from paykit.storage.sqlalchemy.tables import PayKitTables

T = TypeVar("T")

_active_connection: contextvars.ContextVar[AsyncConnection | None] = contextvars.ContextVar(
    "paykit_active_connection", default=None
)


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SQLAlchemyRepository:
    def __init__(self, engine: AsyncEngine, *, table_prefix: str = "paykit_") -> None:
        self._engine = engine
        self._tables = PayKitTables(table_prefix)

    async def create_all(self) -> None:
        """Create tables directly (tests / quickstarts). Production apps should
        use Alembic migrations (`paykit db upgrade`) instead."""
        async with self._engine.begin() as conn:
            await conn.run_sync(self._tables.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        if _active_connection.get() is not None:
            yield  # nested: reuse the outer connection/transaction
            return
        async with self._engine.begin() as conn:
            token = _active_connection.set(conn)
            try:
                yield
            finally:
                _active_connection.reset(token)

    @asynccontextmanager
    async def _connection(self) -> AsyncGenerator[AsyncConnection]:
        conn = _active_connection.get()
        if conn is not None:
            yield conn
            return
        async with self._engine.begin() as conn:
            yield conn

    def _maybe_for_update(self, stmt: Any, *, for_update: bool) -> Any:
        if for_update and self._engine.dialect.name == "postgresql":
            return stmt.with_for_update()
        return stmt

    # -- Orders -----------------------------------------------------------

    @staticmethod
    def _order_to_row(order: Order) -> dict[str, Any]:
        return {
            "id": order.id,
            "provider": order.provider,
            "provider_order_id": order.provider_order_id,
            "amount": order.amount.amount,
            "currency": order.amount.currency,
            "status": order.status.value,
            "receipt": order.receipt,
            "customer_ref": order.customer_ref,
            "metadata_json": order.metadata,
            "idempotency_key": order.idempotency_key,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
        }

    @staticmethod
    def _row_to_order(row: RowMapping) -> Order:
        return Order(
            id=row["id"],
            provider=row["provider"],
            provider_order_id=row["provider_order_id"],
            amount=Money(amount=row["amount"], currency=row["currency"]),
            status=row["status"],
            receipt=row["receipt"],
            customer_ref=row["customer_ref"],
            metadata=row["metadata_json"] or {},
            idempotency_key=row["idempotency_key"],
            created_at=_ensure_utc(row["created_at"]),
            updated_at=_ensure_utc(row["updated_at"]),
        )

    async def create_order(self, order: Order) -> Order:
        async with self._connection() as conn:
            await conn.execute(self._tables.orders.insert().values(**self._order_to_row(order)))
        return order.model_copy()

    async def get_order(self, order_id: str) -> Order | None:
        return await self._get_one(self._tables.orders, self._row_to_order, id=order_id)

    async def update_order(self, order: Order) -> Order:
        async with self._connection() as conn:
            result = await conn.execute(
                update(self._tables.orders)
                .where(self._tables.orders.c.id == order.id)
                .values(**self._order_to_row(order))
            )
            if result.rowcount == 0:
                raise KeyError(f"no order with id {order.id!r}")
        return order.model_copy()

    async def get_order_by_provider_id(
        self, provider_order_id: str, *, for_update: bool = False
    ) -> Order | None:
        return await self._get_one(
            self._tables.orders,
            self._row_to_order,
            for_update=for_update,
            provider_order_id=provider_order_id,
        )

    async def get_order_by_idempotency_key(self, idempotency_key: str) -> Order | None:
        return await self._get_one(
            self._tables.orders, self._row_to_order, idempotency_key=idempotency_key
        )

    async def list_non_final_orders(self, since: datetime) -> list[Order]:
        non_final = {"created", "attempted"}
        async with self._connection() as conn:
            stmt = select(self._tables.orders).where(
                self._tables.orders.c.status.in_(non_final),
                self._tables.orders.c.created_at >= since,
            )
            result = await conn.execute(stmt)
            return [self._row_to_order(row._mapping) for row in result]

    # -- Payments -----------------------------------------------------------

    @staticmethod
    def _payment_to_row(payment: Payment) -> dict[str, Any]:
        return {
            "id": payment.id,
            "provider": payment.provider,
            "provider_payment_id": payment.provider_payment_id,
            "order_id": payment.order_id,
            "subscription_id": payment.subscription_id,
            "amount": payment.amount.amount,
            "currency": payment.amount.currency,
            "amount_refunded": payment.amount_refunded,
            "status": payment.status.value,
            "method": payment.method,
            "error_code": payment.error_code,
            "error_description": payment.error_description,
            "created_at": payment.created_at,
            "updated_at": payment.updated_at,
        }

    @staticmethod
    def _row_to_payment(row: RowMapping) -> Payment:
        return Payment(
            id=row["id"],
            provider=row["provider"],
            provider_payment_id=row["provider_payment_id"],
            order_id=row["order_id"],
            subscription_id=row["subscription_id"],
            amount=Money(amount=row["amount"], currency=row["currency"]),
            amount_refunded=row["amount_refunded"],
            status=row["status"],
            method=row["method"],
            error_code=row["error_code"],
            error_description=row["error_description"],
            created_at=_ensure_utc(row["created_at"]),
            updated_at=_ensure_utc(row["updated_at"]),
        )

    async def create_payment(self, payment: Payment) -> Payment:
        async with self._connection() as conn:
            await conn.execute(
                self._tables.payments.insert().values(**self._payment_to_row(payment))
            )
        return payment.model_copy()

    async def get_payment(self, payment_id: str) -> Payment | None:
        return await self._get_one(self._tables.payments, self._row_to_payment, id=payment_id)

    async def update_payment(self, payment: Payment) -> Payment:
        async with self._connection() as conn:
            result = await conn.execute(
                update(self._tables.payments)
                .where(self._tables.payments.c.id == payment.id)
                .values(**self._payment_to_row(payment))
            )
            if result.rowcount == 0:
                raise KeyError(f"no payment with id {payment.id!r}")
        return payment.model_copy()

    async def get_payment_by_provider_id(
        self, provider_payment_id: str, *, for_update: bool = False
    ) -> Payment | None:
        return await self._get_one(
            self._tables.payments,
            self._row_to_payment,
            for_update=for_update,
            provider_payment_id=provider_payment_id,
        )

    async def list_payments_for_order(self, order_id: str) -> list[Payment]:
        async with self._connection() as conn:
            stmt = select(self._tables.payments).where(
                self._tables.payments.c.order_id == order_id
            )
            result = await conn.execute(stmt)
            return [self._row_to_payment(row._mapping) for row in result]

    async def list_non_final_payments(self, since: datetime) -> list[Payment]:
        non_final = {"created", "authorized"}
        async with self._connection() as conn:
            stmt = select(self._tables.payments).where(
                self._tables.payments.c.status.in_(non_final),
                self._tables.payments.c.created_at >= since,
            )
            result = await conn.execute(stmt)
            return [self._row_to_payment(row._mapping) for row in result]

    # -- Refunds -----------------------------------------------------------

    @staticmethod
    def _refund_to_row(refund: Refund) -> dict[str, Any]:
        return {
            "id": refund.id,
            "provider_refund_id": refund.provider_refund_id,
            "payment_id": refund.payment_id,
            "amount": refund.amount.amount,
            "currency": refund.amount.currency,
            "status": refund.status.value,
            "reason": refund.reason,
            "created_at": refund.created_at,
        }

    @staticmethod
    def _row_to_refund(row: RowMapping) -> Refund:
        return Refund(
            id=row["id"],
            provider_refund_id=row["provider_refund_id"],
            payment_id=row["payment_id"],
            amount=Money(amount=row["amount"], currency=row["currency"]),
            status=row["status"],
            reason=row["reason"],
            created_at=_ensure_utc(row["created_at"]),
        )

    async def create_refund(self, refund: Refund) -> Refund:
        async with self._connection() as conn:
            await conn.execute(
                self._tables.refunds.insert().values(**self._refund_to_row(refund))
            )
        return refund.model_copy()

    async def get_refund(self, refund_id: str) -> Refund | None:
        return await self._get_one(self._tables.refunds, self._row_to_refund, id=refund_id)

    async def update_refund(self, refund: Refund) -> Refund:
        async with self._connection() as conn:
            result = await conn.execute(
                update(self._tables.refunds)
                .where(self._tables.refunds.c.id == refund.id)
                .values(**self._refund_to_row(refund))
            )
            if result.rowcount == 0:
                raise KeyError(f"no refund with id {refund.id!r}")
        return refund.model_copy()

    async def get_refund_by_provider_id(self, provider_refund_id: str) -> Refund | None:
        return await self._get_one(
            self._tables.refunds, self._row_to_refund, provider_refund_id=provider_refund_id
        )

    # -- Plans -----------------------------------------------------------

    @staticmethod
    def _plan_to_row(plan: Plan) -> dict[str, Any]:
        return {
            "id": plan.id,
            "provider_plan_id": plan.provider_plan_id,
            "name": plan.name,
            "amount": plan.amount.amount,
            "currency": plan.amount.currency,
            "period": plan.period,
            "interval": plan.interval,
        }

    @staticmethod
    def _row_to_plan(row: RowMapping) -> Plan:
        return Plan(
            id=row["id"],
            provider_plan_id=row["provider_plan_id"],
            name=row["name"],
            amount=Money(amount=row["amount"], currency=row["currency"]),
            period=row["period"],
            interval=row["interval"],
        )

    async def create_plan(self, plan: Plan) -> Plan:
        async with self._connection() as conn:
            await conn.execute(self._tables.plans.insert().values(**self._plan_to_row(plan)))
        return plan.model_copy()

    async def get_plan(self, plan_id: str) -> Plan | None:
        return await self._get_one(self._tables.plans, self._row_to_plan, id=plan_id)

    async def get_plan_by_provider_id(self, provider_plan_id: str) -> Plan | None:
        return await self._get_one(
            self._tables.plans, self._row_to_plan, provider_plan_id=provider_plan_id
        )

    # -- Subscriptions -----------------------------------------------------------

    @staticmethod
    def _subscription_to_row(subscription: Subscription) -> dict[str, Any]:
        return {
            "id": subscription.id,
            "provider_subscription_id": subscription.provider_subscription_id,
            "plan_id": subscription.plan_id,
            "customer_ref": subscription.customer_ref,
            "status": subscription.status.value,
            "current_start": subscription.current_start,
            "current_end": subscription.current_end,
            "total_count": subscription.total_count,
            "paid_count": subscription.paid_count,
            "short_url": subscription.short_url,
            "metadata_json": subscription.metadata,
        }

    @staticmethod
    def _row_to_subscription(row: RowMapping) -> Subscription:
        current_start = row["current_start"]
        current_end = row["current_end"]
        return Subscription(
            id=row["id"],
            provider_subscription_id=row["provider_subscription_id"],
            plan_id=row["plan_id"],
            customer_ref=row["customer_ref"],
            status=row["status"],
            current_start=_ensure_utc(current_start) if current_start else None,
            current_end=_ensure_utc(current_end) if current_end else None,
            total_count=row["total_count"],
            paid_count=row["paid_count"],
            short_url=row["short_url"],
            metadata=row["metadata_json"] or {},
        )

    async def create_subscription(self, subscription: Subscription) -> Subscription:
        async with self._connection() as conn:
            await conn.execute(
                self._tables.subscriptions.insert().values(
                    **self._subscription_to_row(subscription)
                )
            )
        return subscription.model_copy()

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        return await self._get_one(
            self._tables.subscriptions, self._row_to_subscription, id=subscription_id
        )

    async def update_subscription(self, subscription: Subscription) -> Subscription:
        async with self._connection() as conn:
            result = await conn.execute(
                update(self._tables.subscriptions)
                .where(self._tables.subscriptions.c.id == subscription.id)
                .values(**self._subscription_to_row(subscription))
            )
            if result.rowcount == 0:
                raise KeyError(f"no subscription with id {subscription.id!r}")
        return subscription.model_copy()

    async def get_subscription_by_provider_id(
        self, provider_subscription_id: str, *, for_update: bool = False
    ) -> Subscription | None:
        return await self._get_one(
            self._tables.subscriptions,
            self._row_to_subscription,
            for_update=for_update,
            provider_subscription_id=provider_subscription_id,
        )

    # -- Event dedupe -----------------------------------------------------------

    async def record_event_if_new(self, event_id: str) -> bool:
        async with self._connection() as conn:
            try:
                async with conn.begin_nested():
                    await conn.execute(
                        self._tables.processed_events.insert().values(
                            event_id=event_id, processed_at=datetime.now(UTC)
                        )
                    )
            except IntegrityError:
                return False
            return True

    # -- Shared helper -----------------------------------------------------------

    async def _get_one(
        self,
        table: Table,
        row_mapper: Callable[[RowMapping], T],
        *,
        for_update: bool = False,
        **where: Any,
    ) -> T | None:
        async with self._connection() as conn:
            stmt = select(table).filter_by(**where)
            stmt = self._maybe_for_update(stmt, for_update=for_update)
            result = await conn.execute(stmt)
            row = result.first()
            return row_mapper(row._mapping) if row is not None else None
