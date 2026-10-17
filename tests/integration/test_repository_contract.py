"""One test suite, run against every PaymentRepository implementation.

Postgres is the real production backend (see storage/sqlalchemy), but this
dev environment has no Docker daemon running, so the SQLAlchemy side of the
matrix runs against an in-memory SQLite engine here - it exercises the same
SQLAlchemy Core code path (inserts/updates/selects/FOR UPDATE branch/
savepoints), just not Postgres's actual locking behavior. If
`PAYKIT_TEST_DATABASE_URL` is set (e.g. to a postgresql+asyncpg:// URL),
that's used instead so this suite runs for real in CI/against Postgres.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from paykit.core.models import (
    Money,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    Refund,
    RefundStatus,
    Subscription,
    SubscriptionStatus,
)
from paykit.storage.memory import InMemoryRepository
from paykit.storage.sqlalchemy.repository import SQLAlchemyRepository

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_order(**overrides: object) -> Order:
    defaults: dict[str, object] = dict(
        id="ord_1",
        provider="razorpay",
        provider_order_id="order_rzp_1",
        amount=Money(amount=49900, currency="INR"),
        status=OrderStatus.CREATED,
        receipt="inv_1",
        customer_ref="user_1",
        metadata={"plan": "pro"},
        idempotency_key="inv_1",
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return Order(**defaults)  # type: ignore[arg-type]


def make_payment(**overrides: object) -> Payment:
    defaults: dict[str, object] = dict(
        id="pay_1",
        provider="razorpay",
        provider_payment_id="pay_rzp_1",
        order_id=None,
        subscription_id=None,
        amount=Money(amount=49900, currency="INR"),
        amount_refunded=0,
        status=PaymentStatus.CREATED,
        method="upi",
        error_code=None,
        error_description=None,
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return Payment(**defaults)  # type: ignore[arg-type]


def make_refund(**overrides: object) -> Refund:
    defaults: dict[str, object] = dict(
        id="rfnd_1",
        provider_refund_id="rfnd_rzp_1",
        payment_id="pay_1",
        amount=Money(amount=10000, currency="INR"),
        status=RefundStatus.PENDING,
        reason="requested_by_customer",
        created_at=NOW,
    )
    defaults.update(overrides)
    return Refund(**defaults)  # type: ignore[arg-type]


def make_plan(**overrides: object) -> Plan:
    defaults: dict[str, object] = dict(
        id="plan_1",
        provider_plan_id="plan_rzp_1",
        name="Pro",
        amount=Money(amount=49900, currency="INR"),
        period="monthly",
        interval=1,
    )
    defaults.update(overrides)
    return Plan(**defaults)  # type: ignore[arg-type]


def make_subscription(**overrides: object) -> Subscription:
    defaults: dict[str, object] = dict(
        id="sub_1",
        provider_subscription_id="sub_rzp_1",
        plan_id="plan_1",
        customer_ref="user_1",
        status=SubscriptionStatus.CREATED,
        current_start=None,
        current_end=None,
        total_count=12,
        paid_count=0,
        short_url="https://rzp.io/i/abc",
        metadata={"user_id": "user_1"},
    )
    defaults.update(overrides)
    return Subscription(**defaults)  # type: ignore[arg-type]


async def _make_memory_repo() -> InMemoryRepository:
    return InMemoryRepository()


async def _make_sqlalchemy_repo() -> SQLAlchemyRepository:
    database_url = os.environ.get("PAYKIT_TEST_DATABASE_URL")
    if database_url:
        engine = create_async_engine(database_url)
    else:
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
        )
    repo = SQLAlchemyRepository(engine)
    await repo.create_all()
    return repo


@pytest_asyncio.fixture(params=["memory", "sqlalchemy"])
async def repo(request: pytest.FixtureRequest):
    if request.param == "memory":
        yield await _make_memory_repo()
        return
    sql_repo = await _make_sqlalchemy_repo()
    try:
        yield sql_repo
    finally:
        await sql_repo.dispose()


class TestOrders:
    async def test_create_and_get(self, repo) -> None:
        await repo.create_order(make_order())
        fetched = await repo.get_order("ord_1")
        assert fetched is not None
        assert fetched.provider_order_id == "order_rzp_1"
        assert fetched.amount.amount == 49900

    async def test_get_missing_returns_none(self, repo) -> None:
        assert await repo.get_order("does_not_exist") is None

    async def test_update_requires_existing_row(self, repo) -> None:
        with pytest.raises(KeyError):
            await repo.update_order(make_order(id="ghost"))

    async def test_update_persists_changes(self, repo) -> None:
        await repo.create_order(make_order())
        updated = make_order(status=OrderStatus.PAID)
        await repo.update_order(updated)
        fetched = await repo.get_order("ord_1")
        assert fetched is not None
        assert fetched.status == OrderStatus.PAID

    async def test_get_by_provider_id(self, repo) -> None:
        await repo.create_order(make_order())
        fetched = await repo.get_order_by_provider_id("order_rzp_1")
        assert fetched is not None
        assert fetched.id == "ord_1"

    async def test_get_by_provider_id_missing(self, repo) -> None:
        assert await repo.get_order_by_provider_id("nope") is None

    async def test_get_by_idempotency_key(self, repo) -> None:
        await repo.create_order(make_order())
        fetched = await repo.get_order_by_idempotency_key("inv_1")
        assert fetched is not None
        assert fetched.id == "ord_1"

    async def test_list_non_final_excludes_paid_and_old(self, repo) -> None:
        await repo.create_order(make_order(id="ord_created", status=OrderStatus.CREATED))
        await repo.create_order(
            make_order(
                id="ord_attempted",
                provider_order_id="order_rzp_2",
                idempotency_key="inv_2",
                status=OrderStatus.ATTEMPTED,
            )
        )
        await repo.create_order(
            make_order(
                id="ord_paid",
                provider_order_id="order_rzp_3",
                idempotency_key="inv_3",
                status=OrderStatus.PAID,
            )
        )
        await repo.create_order(
            make_order(
                id="ord_too_old",
                provider_order_id="order_rzp_4",
                idempotency_key="inv_4",
                status=OrderStatus.CREATED,
                created_at=NOW - timedelta(days=10),
                updated_at=NOW - timedelta(days=10),
            )
        )

        results = await repo.list_non_final_orders(since=NOW - timedelta(hours=1))

        ids = {o.id for o in results}
        assert ids == {"ord_created", "ord_attempted"}


class TestPayments:
    async def test_create_and_get(self, repo) -> None:
        await repo.create_order(make_order())
        await repo.create_payment(make_payment(order_id="ord_1"))
        fetched = await repo.get_payment("pay_1")
        assert fetched is not None
        assert fetched.order_id == "ord_1"

    async def test_update_persists_changes(self, repo) -> None:
        await repo.create_order(make_order())
        await repo.create_payment(make_payment(order_id="ord_1"))
        await repo.update_payment(
            make_payment(order_id="ord_1", status=PaymentStatus.CAPTURED)
        )
        fetched = await repo.get_payment("pay_1")
        assert fetched is not None
        assert fetched.status == PaymentStatus.CAPTURED

    async def test_get_by_provider_id(self, repo) -> None:
        await repo.create_order(make_order())
        await repo.create_payment(make_payment(order_id="ord_1"))
        fetched = await repo.get_payment_by_provider_id("pay_rzp_1")
        assert fetched is not None
        assert fetched.id == "pay_1"

    async def test_get_by_provider_id_with_for_update(self, repo) -> None:
        # Exercises the FOR UPDATE branch (a no-op outside Postgres); must
        # not raise on SQLite or the in-memory repo.
        await repo.create_order(make_order())
        await repo.create_payment(make_payment(order_id="ord_1"))
        async with repo.transaction():
            fetched = await repo.get_payment_by_provider_id("pay_rzp_1", for_update=True)
        assert fetched is not None

    async def test_list_payments_for_order(self, repo) -> None:
        await repo.create_order(make_order())
        await repo.create_payment(make_payment(order_id="ord_1"))
        await repo.create_payment(
            make_payment(id="pay_2", provider_payment_id="pay_rzp_2", order_id="ord_1")
        )
        results = await repo.list_payments_for_order("ord_1")
        assert {p.id for p in results} == {"pay_1", "pay_2"}

    async def test_list_non_final_payments(self, repo) -> None:
        await repo.create_order(make_order())
        await repo.create_payment(
            make_payment(id="pay_created", status=PaymentStatus.CREATED, order_id="ord_1")
        )
        await repo.create_payment(
            make_payment(
                id="pay_captured",
                provider_payment_id="pay_rzp_2",
                status=PaymentStatus.CAPTURED,
                order_id="ord_1",
            )
        )
        results = await repo.list_non_final_payments(since=NOW - timedelta(hours=1))
        assert {p.id for p in results} == {"pay_created"}


class TestRefunds:
    async def test_create_and_get(self, repo) -> None:
        await repo.create_order(make_order())
        await repo.create_payment(make_payment(order_id="ord_1"))
        await repo.create_refund(make_refund())
        fetched = await repo.get_refund("rfnd_1")
        assert fetched is not None
        assert fetched.payment_id == "pay_1"

    async def test_update_persists_changes(self, repo) -> None:
        await repo.create_order(make_order())
        await repo.create_payment(make_payment(order_id="ord_1"))
        await repo.create_refund(make_refund())
        await repo.update_refund(make_refund(status=RefundStatus.PROCESSED))
        fetched = await repo.get_refund("rfnd_1")
        assert fetched is not None
        assert fetched.status == RefundStatus.PROCESSED

    async def test_get_by_provider_id(self, repo) -> None:
        await repo.create_order(make_order())
        await repo.create_payment(make_payment(order_id="ord_1"))
        await repo.create_refund(make_refund())
        fetched = await repo.get_refund_by_provider_id("rfnd_rzp_1")
        assert fetched is not None
        assert fetched.id == "rfnd_1"


class TestPlans:
    async def test_create_and_get(self, repo) -> None:
        await repo.create_plan(make_plan())
        fetched = await repo.get_plan("plan_1")
        assert fetched is not None
        assert fetched.name == "Pro"

    async def test_get_by_provider_id(self, repo) -> None:
        await repo.create_plan(make_plan())
        fetched = await repo.get_plan_by_provider_id("plan_rzp_1")
        assert fetched is not None
        assert fetched.id == "plan_1"


class TestSubscriptions:
    async def test_create_and_get(self, repo) -> None:
        await repo.create_plan(make_plan())
        await repo.create_subscription(make_subscription())
        fetched = await repo.get_subscription("sub_1")
        assert fetched is not None
        assert fetched.plan_id == "plan_1"

    async def test_update_persists_changes(self, repo) -> None:
        await repo.create_plan(make_plan())
        await repo.create_subscription(make_subscription())
        await repo.update_subscription(
            make_subscription(status=SubscriptionStatus.ACTIVE, paid_count=1)
        )
        fetched = await repo.get_subscription("sub_1")
        assert fetched is not None
        assert fetched.status == SubscriptionStatus.ACTIVE
        assert fetched.paid_count == 1

    async def test_get_by_provider_id(self, repo) -> None:
        await repo.create_plan(make_plan())
        await repo.create_subscription(make_subscription())
        fetched = await repo.get_subscription_by_provider_id("sub_rzp_1")
        assert fetched is not None
        assert fetched.id == "sub_1"


class TestEventDedupe:
    async def test_first_call_records_new(self, repo) -> None:
        assert await repo.record_event_if_new("evt_1") is True

    async def test_duplicate_returns_false(self, repo) -> None:
        await repo.record_event_if_new("evt_1")
        assert await repo.record_event_if_new("evt_1") is False

    async def test_different_ids_are_independent(self, repo) -> None:
        assert await repo.record_event_if_new("evt_a") is True
        assert await repo.record_event_if_new("evt_b") is True

    async def test_duplicate_inside_transaction_does_not_abort_it(self, repo) -> None:
        """On Postgres, an unhandled IntegrityError poisons the whole
        transaction until rollback; record_event_if_new must isolate its
        own insert (e.g. via a savepoint) so a duplicate doesn't kill
        other work done in the same `transaction()` block."""
        await repo.record_event_if_new("evt_dup")

        async with repo.transaction():
            is_new = await repo.record_event_if_new("evt_dup")
            assert is_new is False
            await repo.create_order(make_order(id="ord_after_dup"))

        assert await repo.get_order("ord_after_dup") is not None


class TestTransactionRollback:
    async def test_exception_rolls_back_writes(self, repo) -> None:
        with pytest.raises(RuntimeError):
            async with repo.transaction():
                await repo.create_order(make_order(id="ord_rollback"))
                raise RuntimeError("boom")

        assert await repo.get_order("ord_rollback") is None

    async def test_successful_transaction_commits(self, repo) -> None:
        async with repo.transaction():
            await repo.create_order(make_order(id="ord_committed"))

        assert await repo.get_order("ord_committed") is not None
