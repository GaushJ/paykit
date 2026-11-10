"""Regression tests for the security/scalability hardening pass.

Each test names the failure it guards against.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from paykit.config import PayKitSettings
from paykit.core.errors import (
    DuplicateError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    ProviderError,
)
from paykit.core.models import Money, OrderStatus, PaymentStatus
from paykit.core.service import PaymentService, engine_options
from paykit.providers import get_provider
from paykit.providers.dummy import (
    DummyProvider,
    placeholder_order,
    placeholder_payment,
    placeholder_refund,
)
from paykit.providers.razorpay.adapter import RazorpayProvider
from paykit.storage.memory import InMemoryRepository
from paykit.storage.sqlalchemy.repository import SQLAlchemyRepository

INR_499 = Money(amount=49900, currency="INR")


def make_service() -> PaymentService:
    return PaymentService(provider=DummyProvider(), repository=InMemoryRepository())


async def make_sql_service() -> PaymentService:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    repo = SQLAlchemyRepository(engine)
    await repo.create_all()
    return PaymentService(provider=DummyProvider(), repository=repo)


@pytest.fixture(params=["memory", "sqlalchemy"])
async def service(request: pytest.FixtureRequest):
    if request.param == "memory":
        yield make_service()
        return
    svc = await make_sql_service()
    try:
        yield svc
    finally:
        await svc.aclose()


def dummy(service: PaymentService) -> DummyProvider:
    return service.provider  # type: ignore[return-value]


class TestPaymentOnlyWebhookLinksOrder:
    """A payment.captured webhook carrying only the payment entity (common
    for Razorpay) used to store the payment with order_id=None and never
    mark the order PAID - so the customer was charged but never granted."""

    async def test_webhook_before_confirm_links_and_marks_paid(self, service) -> None:
        order = await service.create_order(INR_499, customer_ref="u1")
        pp = dummy(service).simulate_payment(order.provider_order_id)
        received = []

        @service.events.on("payment.captured")
        async def handler(event):
            received.append(event)

        body, headers = dummy(service).make_webhook(
            event_id="evt_1", event_type="payment.captured", payment=placeholder_payment(pp)
        )
        await service.handle_webhook(body, headers)

        assert received[0].payment.order_id == order.id
        assert received[0].order is not None and received[0].order.id == order.id
        stored = await service.get_order(order.id)
        assert stored.status == OrderStatus.PAID

    async def test_confirm_after_webhook_does_not_duplicate(self, service) -> None:
        order = await service.create_order(INR_499, customer_ref="u1")
        provider = dummy(service)
        pp = provider.simulate_payment(order.provider_order_id)
        body, headers = provider.make_webhook(
            event_id="evt_1", event_type="payment.captured", payment=placeholder_payment(pp)
        )
        await service.handle_webhook(body, headers)

        payment = await service.confirm_checkout(
            provider.make_checkout_result(pp, provider_order_id=order.provider_order_id)
        )

        payments = await service.list_payments_for_order(order.id)
        assert [p.id for p in payments] == [payment.id]

    async def test_payment_amount_mismatch_does_not_mark_order_paid(self, service) -> None:
        order = await service.create_order(INR_499, customer_ref="u1")
        pp = dummy(service).simulate_payment(order.provider_order_id)
        cheap = pp.model_copy(update={"amount": Money(amount=100, currency="INR")})
        body, headers = dummy(service).make_webhook(
            event_id="evt_1", event_type="payment.captured", payment=placeholder_payment(cheap)
        )
        await service.handle_webhook(body, headers)

        assert (await service.get_order(order.id)).status == OrderStatus.CREATED


class TestIdempotency:
    async def test_key_is_scoped_to_customer(self, service) -> None:
        """Reusing another customer's key used to return *their* order."""
        await service.create_order(INR_499, customer_ref="victim", idempotency_key="k1")

        with pytest.raises(IdempotencyConflictError):
            await service.create_order(INR_499, customer_ref="attacker", idempotency_key="k1")

    async def test_same_customer_replay_returns_same_order(self, service) -> None:
        first = await service.create_order(INR_499, customer_ref="u1", idempotency_key="k1")
        again = await service.create_order(INR_499, customer_ref="u1", idempotency_key="k1")
        assert again.id == first.id

    async def test_lost_insert_race_returns_winner(self, service, monkeypatch) -> None:
        """Deterministic version of the race: the pre-check misses, the
        insert hits the unique constraint, the winner's order comes back
        (it used to surface as a 500)."""
        winner = await service.create_order(INR_499, customer_ref="u1", idempotency_key="race")
        repo = service.repository
        real_lookup = repo.get_order_by_idempotency_key
        calls = 0

        async def stale_first_lookup(key):
            nonlocal calls
            calls += 1
            return None if calls == 1 else await real_lookup(key)

        monkeypatch.setattr(repo, "get_order_by_idempotency_key", stale_first_lookup)
        loser = await service.create_order(INR_499, customer_ref="u1", idempotency_key="race")
        assert loser.id == winner.id

    async def test_concurrent_same_key_returns_one_order(self) -> None:
        """Real concurrency (in-memory backend; SQLite's single shared test
        connection can't host concurrent transactions)."""
        service = make_service()
        results = await asyncio.gather(
            *(
                service.create_order(INR_499, customer_ref="u1", idempotency_key="race")
                for _ in range(5)
            )
        )
        assert len({o.id for o in results}) == 1


class TestConfirmOwnership:
    async def test_other_customers_order_is_not_found(self, service) -> None:
        order = await service.create_order(INR_499, customer_ref="victim")
        provider = dummy(service)
        pp = provider.simulate_payment(order.provider_order_id)
        result = provider.make_checkout_result(pp, provider_order_id=order.provider_order_id)

        with pytest.raises(NotFoundError):
            await service.confirm_checkout(result, customer_ref="attacker")

        payment = await service.confirm_checkout(result, customer_ref="victim")
        assert payment.order_id == order.id


class TestRefundSafety:
    async def _captured(self, service):
        order = await service.create_order(INR_499, customer_ref="u1")
        provider = dummy(service)
        pp = provider.simulate_payment(order.provider_order_id)
        payment = await service.confirm_checkout(
            provider.make_checkout_result(pp, provider_order_id=order.provider_order_id)
        )
        return payment, pp

    async def test_refund_exceeding_balance_rejected_before_provider_call(self, service) -> None:
        payment, _ = await self._captured(service)
        await service.refund(payment.id, Money(amount=40000, currency="INR"))

        with pytest.raises(InvalidStateError, match="exceeds"):
            await service.refund(payment.id, Money(amount=10000, currency="INR"))

    async def test_refund_currency_mismatch_rejected(self, service) -> None:
        payment, _ = await self._captured(service)
        with pytest.raises(InvalidStateError, match="currency"):
            await service.refund(payment.id, Money(amount=100, currency="USD"))

    async def test_webhook_first_refund_not_double_counted(self, service) -> None:
        """If the refund webhook (with the provider's authoritative
        amount_refunded) lands before refund() records it, refund() must not
        add the amount a second time."""
        payment, pp = await self._captured(service)
        provider = dummy(service)
        real_refund = provider.refund_payment

        async def refund_then_webhook(*args, **kwargs):
            pr = await real_refund(*args, **kwargs)
            refunded_pp = await provider.fetch_payment(pp.provider_payment_id)
            body, headers = provider.make_webhook(
                event_id="evt_refund",
                event_type="refund.processed",
                payment=placeholder_payment(refunded_pp),
                refund=placeholder_refund(pr),
            )
            await service.handle_webhook(body, headers)
            return pr

        provider.refund_payment = refund_then_webhook  # type: ignore[method-assign]
        await service.refund(payment.id, Money(amount=10000, currency="INR"))

        stored = await service.get_payment(payment.id)
        assert stored.amount_refunded == 10000
        assert stored.status == PaymentStatus.PARTIALLY_REFUNDED


class TestDuplicateErrors:
    async def test_repositories_raise_duplicate_error(self, service) -> None:
        order = await service.create_order(INR_499, customer_ref="u1")
        clone = order.model_copy(update={"id": "another-id"})
        with pytest.raises(DuplicateError):
            await service.repository.create_order(clone)

    async def test_duplicate_inside_transaction_keeps_outer_transaction_usable(
        self, service
    ) -> None:
        order = await service.create_order(INR_499, customer_ref="u1")
        clone = order.model_copy(update={"id": "another-id"})
        async with service.repository.transaction():
            with pytest.raises(DuplicateError):
                await service.repository.create_order(clone)
            assert await service.repository.record_event_if_new("evt_after_dup")
        assert not await service.repository.record_event_if_new("evt_after_dup")


class TestWebhookRetryOnInsertRace:
    async def test_retries_once_on_duplicate(self, service, monkeypatch) -> None:
        order = await service.create_order(INR_499, customer_ref="u1")
        provider = dummy(service)
        pp = provider.simulate_payment(order.provider_order_id)
        body, headers = provider.make_webhook(
            event_id="evt_1",
            event_type="order.paid",
            order=placeholder_order(await provider.fetch_order(order.provider_order_id)),
            payment=placeholder_payment(pp),
        )
        real = service._reconcile_event
        calls = 0

        async def flaky(event):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DuplicateError("simulated concurrent insert")
            return await real(event)

        monkeypatch.setattr(service, "_reconcile_event", flaky)
        await service.handle_webhook(body, headers)

        assert calls == 2
        assert (await service.get_order(order.id)).status == OrderStatus.PAID


class TestPruneProcessedEvents:
    async def test_prunes_only_old_records(self, service) -> None:
        assert await service.repository.record_event_if_new("old")
        deleted = await service.repository.prune_processed_events(
            datetime.now(UTC) + timedelta(seconds=1)
        )
        assert deleted == 1
        assert await service.prune_processed_events() == 0

    async def test_refuses_retention_shorter_than_redelivery_window(self, service) -> None:
        with pytest.raises(ValueError):
            await service.prune_processed_events(older_than=timedelta(hours=1))


class TestReconcile:
    async def test_limit_and_failure_isolation(self) -> None:
        service = make_service()
        provider = dummy(service)
        orders = [await service.create_order(INR_499, customer_ref="u1") for _ in range(3)]
        for o in orders:
            pp = provider.simulate_payment(o.provider_order_id, status=PaymentStatus.AUTHORIZED)
            await service.repository.create_payment(
                placeholder_payment(pp).model_copy(update={"id": f"p-{o.id}", "order_id": o.id})
            )
        # Provider now reports them captured.
        for pid, pp in list(provider._payments.items()):
            provider._payments[pid] = pp.model_copy(update={"status": PaymentStatus.CAPTURED})

        real_fetch = provider.fetch_payment
        first = sorted(provider._payments)[0]

        async def flaky_fetch(pid):
            if pid == first:
                raise ProviderError("boom")
            return await real_fetch(pid)

        provider.fetch_payment = flaky_fetch  # type: ignore[method-assign]
        since = datetime.now(UTC) - timedelta(hours=1)
        updated = await service.reconcile(since, limit=2)
        assert 1 <= updated <= 2  # one of the two in the batch failed and was skipped
        await service.reconcile(since, limit=10)
        captured = [
            p for p in provider._payments
            if (await service.repository.get_payment_by_provider_id(p)).status
            == PaymentStatus.CAPTURED
        ]
        assert len(captured) == 2  # the flaky one stays non-final until the provider recovers


class TestProviderHardening:
    def test_dummy_provider_secret_is_random(self) -> None:
        a, b = DummyProvider(), DummyProvider()
        assert a._secret != b._secret
        assert a._secret != "dummy_secret"

    def test_dummy_from_settings_needs_no_razorpay_keys(self) -> None:
        settings = PayKitSettings(_env_file=None, provider="dummy")  # type: ignore[call-arg]
        assert isinstance(get_provider("dummy", settings), DummyProvider)

    async def test_razorpay_rejects_path_traversal_ids(self) -> None:
        provider = RazorpayProvider("rzp_test_x", "s", "w")
        try:
            for bad in ("../orders", "pay_1/refund", "", "a" * 65, "pay_1?x=1"):
                with pytest.raises(ProviderError, match="invalid Razorpay id"):
                    await provider.fetch_payment(bad)
        finally:
            await provider.aclose()

    def test_engine_options_pool_settings_only_for_server_dbs(self) -> None:
        sqlite = PayKitSettings(
            _env_file=None, provider="dummy", database_url="sqlite+aiosqlite:///x.db"
        )  # type: ignore[call-arg]
        pg = PayKitSettings(
            _env_file=None, provider="dummy", database_url="postgresql+asyncpg://u:p@h/db"
        )  # type: ignore[call-arg]
        assert engine_options(sqlite) == {}
        assert engine_options(pg)["pool_pre_ping"] is True
