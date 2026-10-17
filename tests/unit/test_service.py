import pytest

from paykit.core.errors import (
    AmountMismatchError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    SignatureVerificationError,
)
from paykit.core.events import EventBus
from paykit.core.models import Money, OrderStatus, PaymentStatus, RefundStatus, SubscriptionStatus
from paykit.core.service import PaymentService
from paykit.providers.dummy import (
    DummyProvider,
    placeholder_order,
    placeholder_payment,
    placeholder_subscription,
)
from paykit.storage.memory import InMemoryRepository

INR_499 = Money(amount=49900, currency="INR")


def make_service() -> PaymentService:
    return PaymentService(provider=DummyProvider(), repository=InMemoryRepository())


class TestCreateOrder:
    async def test_creates_order(self) -> None:
        service = make_service()
        order = await service.create_order(INR_499, customer_ref="user_1", receipt="inv_1")
        assert order.status == OrderStatus.CREATED
        assert order.customer_ref == "user_1"
        assert order.provider == "dummy"

    async def test_idempotency_key_replay_returns_same_order(self) -> None:
        service = make_service()
        first = await service.create_order(INR_499, idempotency_key="inv_1")
        second = await service.create_order(INR_499, idempotency_key="inv_1")
        assert first.id == second.id

    async def test_idempotency_key_conflict_raises(self) -> None:
        service = make_service()
        await service.create_order(INR_499, idempotency_key="inv_1")
        with pytest.raises(IdempotencyConflictError):
            await service.create_order(Money(amount=100, currency="INR"), idempotency_key="inv_1")


class TestCheckoutConfig:
    async def test_delegates_to_provider(self) -> None:
        service = make_service()
        order = await service.create_order(INR_499)
        config = service.checkout_config(order, prefill={"email": "a@b.com"})
        assert config["provider"] == "dummy"
        assert config["order_id"] == order.provider_order_id


class TestConfirmCheckout:
    async def test_valid_checkout_persists_payment_and_marks_order_paid(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)

        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CAPTURED)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )

        payment = await service.confirm_checkout(checkout_result)

        assert payment.status == PaymentStatus.CAPTURED
        assert payment.order_id == order.id

        updated_order = await service._repository.get_order(order.id)
        assert updated_order is not None
        assert updated_order.status == OrderStatus.PAID

    async def test_invalid_signature_raises(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )
        tampered = checkout_result.model_copy(update={"signature": "deadbeef"})

        with pytest.raises(SignatureVerificationError):
            await service.confirm_checkout(tampered)

    async def test_amount_mismatch_raises(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id)
        # Mutate the provider's own record of the payment to a different amount,
        # simulating a scenario where the fetched payment doesn't match the order.
        provider._payments[pp.provider_payment_id] = pp.model_copy(
            update={"amount": Money(amount=1, currency="INR")}
        )
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )

        with pytest.raises(AmountMismatchError):
            await service.confirm_checkout(checkout_result)

    async def test_unknown_order_raises_not_found(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id="order_does_not_exist"
        )

        with pytest.raises(NotFoundError):
            await service.confirm_checkout(checkout_result)


class TestRefund:
    async def test_full_refund(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CAPTURED)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )
        payment = await service.confirm_checkout(checkout_result)

        refund = await service.refund(payment.id, None, reason="requested_by_customer")

        assert refund.status == RefundStatus.PROCESSED
        updated_payment = await service._repository.get_payment(payment.id)
        assert updated_payment is not None
        assert updated_payment.status == PaymentStatus.REFUNDED
        assert updated_payment.amount_refunded == INR_499.amount

    async def test_partial_refund(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CAPTURED)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )
        payment = await service.confirm_checkout(checkout_result)

        await service.refund(payment.id, Money(amount=10000, currency="INR"))

        updated_payment = await service._repository.get_payment(payment.id)
        assert updated_payment is not None
        assert updated_payment.status == PaymentStatus.PARTIALLY_REFUNDED
        assert updated_payment.amount_refunded == 10000

    async def test_refund_on_missing_payment_raises_not_found(self) -> None:
        service = make_service()
        with pytest.raises(NotFoundError):
            await service.refund("does_not_exist", None)

    async def test_refund_on_uncaptured_payment_raises_invalid_state(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CREATED)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )
        payment = await service.confirm_checkout(checkout_result)

        with pytest.raises(InvalidStateError):
            await service.refund(payment.id, None)


class TestCapturePayment:
    async def test_captures_uncaptured_payment(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.AUTHORIZED)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )
        payment = await service.confirm_checkout(checkout_result)
        assert payment.status == PaymentStatus.AUTHORIZED

        captured = await service.capture_payment(payment.id, INR_499)
        assert captured.status == PaymentStatus.CAPTURED

    async def test_capture_already_captured_raises_invalid_state(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CAPTURED)
        checkout_result = provider.make_checkout_result(
            pp, provider_order_id=order.provider_order_id
        )
        payment = await service.confirm_checkout(checkout_result)

        with pytest.raises(InvalidStateError):
            await service.capture_payment(payment.id, INR_499)


class TestSubscriptions:
    async def test_create_and_cancel_subscription(self) -> None:
        service = make_service()
        plan = await service.create_plan("Pro", INR_499, "monthly", 1)

        sub = await service.create_subscription(plan.id, customer_ref="user_1", total_count=12)
        assert sub.status == SubscriptionStatus.CREATED
        assert sub.plan_id == plan.id

        cancelled = await service.cancel_subscription(sub.id, at_cycle_end=False)
        assert cancelled.status == SubscriptionStatus.CANCELLED

    async def test_create_subscription_unknown_plan_raises_not_found(self) -> None:
        service = make_service()
        with pytest.raises(NotFoundError):
            await service.create_subscription("no_such_plan", total_count=12)


class TestHandleWebhook:
    async def test_bad_signature_raises(self) -> None:
        service = make_service()
        with pytest.raises(SignatureVerificationError):
            await service.handle_webhook(b"{}", {"X-Dummy-Signature": "bad"})

    async def test_captured_event_creates_payment_and_marks_order_paid(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CAPTURED)

        received = []

        @service.events.on("payment.captured")
        async def handler(event):
            received.append(event)

        raw_body, headers = provider.make_webhook(
            event_id="evt_1",
            event_type="payment.captured",
            order=placeholder_order(await provider.fetch_order(order.provider_order_id)),
            payment=placeholder_payment(pp),
        )
        await service.handle_webhook(raw_body, headers)

        assert len(received) == 1
        dispatched_payment = received[0].payment
        assert dispatched_payment.id != pp.provider_payment_id  # got a real internal id
        assert dispatched_payment.order_id == order.id

        stored_order = await service._repository.get_order(order.id)
        assert stored_order is not None
        assert stored_order.status == OrderStatus.PAID

    async def test_duplicate_delivery_processed_once(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CAPTURED)

        call_count = 0

        @service.events.on("payment.captured")
        async def handler(event):
            nonlocal call_count
            call_count += 1

        raw_body, headers = provider.make_webhook(
            event_id="evt_dup",
            event_type="payment.captured",
            payment=placeholder_payment(pp),
        )
        await service.handle_webhook(raw_body, headers)
        await service.handle_webhook(raw_body, headers)

        assert call_count == 1

    async def test_out_of_order_authorized_after_captured_is_ignored(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CAPTURED)

        captured_body, captured_headers = provider.make_webhook(
            event_id="evt_captured",
            event_type="payment.captured",
            payment=placeholder_payment(pp),
        )
        await service.handle_webhook(captured_body, captured_headers)

        stale_authorized = pp.model_copy(update={"status": PaymentStatus.AUTHORIZED})
        authorized_body, authorized_headers = provider.make_webhook(
            event_id="evt_authorized_late",
            event_type="payment.authorized",
            payment=placeholder_payment(stale_authorized),
        )
        await service.handle_webhook(authorized_body, authorized_headers)

        payment = await service._repository.get_payment_by_provider_id(pp.provider_payment_id)
        assert payment is not None
        assert payment.status == PaymentStatus.CAPTURED

    async def test_handler_exception_does_not_propagate(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        order = await service.create_order(INR_499)
        pp = provider.simulate_payment(order.provider_order_id, status=PaymentStatus.CAPTURED)

        @service.events.on("payment.captured")
        async def bad_handler(event):
            raise RuntimeError("handler blew up")

        raw_body, headers = provider.make_webhook(
            event_id="evt_bad_handler",
            event_type="payment.captured",
            payment=placeholder_payment(pp),
        )
        # Must not raise - a handler exception must not turn this into a
        # non-2xx response (the provider would retry and double-apply).
        await service.handle_webhook(raw_body, headers)

    async def test_subscription_webhook_creates_subscription_and_payment(self) -> None:
        service = make_service()
        provider: DummyProvider = service._provider  # type: ignore[assignment]
        plan = await service.create_plan("Pro", INR_499, "monthly", 1)
        provider_sub = await provider.create_subscription(
            plan.provider_plan_id, total_count=12, customer_notify=True, metadata={}
        )
        provider_sub = provider_sub.model_copy(update={"status": SubscriptionStatus.ACTIVE})

        received = []

        @service.events.on("subscription.activated")
        async def handler(event):
            received.append(event)

        raw_body, headers = provider.make_webhook(
            event_id="evt_sub_activated",
            event_type="subscription.activated",
            subscription=placeholder_subscription(provider_sub),
        )
        await service.handle_webhook(raw_body, headers)

        assert len(received) == 1
        stored_sub = await service._repository.get_subscription_by_provider_id(
            provider_sub.provider_subscription_id
        )
        assert stored_sub is not None
        assert stored_sub.plan_id == plan.id  # resolved from provider plan id to our id
        assert stored_sub.status == SubscriptionStatus.ACTIVE


class TestDummyProviderProvesCoreIsProviderAgnostic:
    async def test_full_flow_works_with_no_razorpay_involved(self) -> None:
        events = EventBus()
        service = PaymentService(
            provider=DummyProvider(), repository=InMemoryRepository(), events=events
        )
        order = await service.create_order(INR_499, idempotency_key="inv_x")
        assert order.provider == "dummy"
        config = service.checkout_config(order)
        assert config["provider"] == "dummy"
