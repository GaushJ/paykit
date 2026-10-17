"""§13: "Integration: full flow of create order -> simulated checkout signature
-> confirm -> webhook payment.captured -> handler called exactly once."
"""

from __future__ import annotations

from paykit.core.models import Money, OrderStatus, PaymentStatus
from paykit.core.service import PaymentService
from paykit.providers.dummy import DummyProvider, placeholder_order, placeholder_payment
from paykit.storage.memory import InMemoryRepository


async def test_create_order_confirm_checkout_then_webhook_dispatches_handler_once() -> None:
    provider = DummyProvider()
    service = PaymentService(provider=provider, repository=InMemoryRepository())

    captured_events = []

    @service.events.on("payment.captured")
    async def grant_access(event):
        captured_events.append(event)

    # 1. Create the order (server-decided amount).
    order = await service.create_order(
        Money(amount=49900, currency="INR"),
        customer_ref="user_42",
        receipt="inv_1001",
        idempotency_key="inv_1001",
    )
    assert order.status == OrderStatus.CREATED

    # 2. Client completes Razorpay Checkout; we simulate what the provider
    # would report back for that payment and what the client posts to
    # /payments/confirm.
    provider_payment = provider.simulate_payment(
        order.provider_order_id, status=PaymentStatus.CAPTURED
    )
    checkout_result = provider.make_checkout_result(
        provider_payment, provider_order_id=order.provider_order_id
    )

    # 3. confirm_checkout gives the UI a fast answer but does NOT dispatch
    # business events (design principle #3/§7.3) - only the webhook does.
    payment = await service.confirm_checkout(checkout_result)
    assert payment.status == PaymentStatus.CAPTURED
    assert captured_events == []

    # 4. The payment.captured webhook is what actually notifies handlers.
    raw_body, headers = provider.make_webhook(
        event_id="evt_captured_1",
        event_type="payment.captured",
        order=placeholder_order(await provider.fetch_order(order.provider_order_id)),
        payment=placeholder_payment(provider_payment),
    )
    await service.handle_webhook(raw_body, headers)

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.type == "payment.captured"
    assert event.payment is not None
    assert event.payment.id == payment.id
    assert event.order is not None
    assert event.order.status == OrderStatus.PAID

    # 5. A redelivery of the same webhook (Razorpay retries on non-2xx, or
    # just sends it twice) must not call the handler again.
    await service.handle_webhook(raw_body, headers)
    assert len(captured_events) == 1

    final_order = await service._repository.get_order(order.id)
    assert final_order is not None
    assert final_order.status == OrderStatus.PAID
