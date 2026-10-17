from datetime import UTC, datetime, timedelta

from paykit.core.models import Money, OrderStatus, PaymentStatus
from paykit.core.service import PaymentService
from paykit.providers.dummy import DummyProvider
from paykit.storage.memory import InMemoryRepository

INR_499 = Money(amount=49900, currency="INR")


def make_service() -> PaymentService:
    return PaymentService(provider=DummyProvider(), repository=InMemoryRepository())


class TestReconcile:
    async def test_updates_stale_order_status(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)

        # Simulate the order having been paid at the provider without a
        # webhook ever arriving (the thing reconcile exists to fix).
        provider._orders[order.provider_order_id] = provider._orders[
            order.provider_order_id
        ].model_copy(update={"status": OrderStatus.PAID})

        await service.reconcile(since=datetime.now(UTC) - timedelta(hours=1))

        updated = await service._repository.get_order(order.id)
        assert updated is not None
        assert updated.status == OrderStatus.PAID

    async def test_updates_stale_payment_and_dispatches_once(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.AUTHORIZED)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )
        payment = await service.confirm_checkout(checkout_result)
        assert payment.status == PaymentStatus.AUTHORIZED

        # Provider-side the payment actually captured, but no webhook arrived.
        provider._payments[pp.provider_payment_id] = provider._payments[
            pp.provider_payment_id
        ].model_copy(update={"status": PaymentStatus.CAPTURED})

        received = []

        @service.events.on("payment.captured")
        async def handler(event):
            received.append(event)

        since = datetime.now(UTC) - timedelta(hours=1)
        await service.reconcile(since=since)

        updated = await service._repository.get_payment(payment.id)
        assert updated is not None
        assert updated.status == PaymentStatus.CAPTURED
        assert len(received) == 1

        # Running it again with nothing changed must not re-dispatch.
        await service.reconcile(since=since)
        assert len(received) == 1

    async def test_ignores_orders_outside_the_window(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        provider._orders[order.provider_order_id] = provider._orders[
            order.provider_order_id
        ].model_copy(update={"status": OrderStatus.PAID})

        # `since` is in the future relative to the order's created_at, so it
        # should be excluded from the non-final list entirely.
        await service.reconcile(since=datetime.now(UTC) + timedelta(days=1))

        updated = await service._repository.get_order(order.id)
        assert updated is not None
        assert updated.status == OrderStatus.CREATED
