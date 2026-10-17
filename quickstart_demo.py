import asyncio
from paykit import Money, PaymentService
from paykit.providers.dummy import DummyProvider, placeholder_payment
from paykit.storage.memory import InMemoryRepository


async def main():
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())

    @service.events.on("payment.captured")
    async def grant_access(event):
        print("granted:", event.payment.order_id)

    order = await service.create_order(Money(amount=49900, currency="INR"), customer_ref="user_42")
    provider = service._provider
    payment = provider.simulate_payment(order.provider_order_id)
    result = provider.make_checkout_result(payment, provider_order_id=order.provider_order_id)
    await service.confirm_checkout(result)
    raw_body, headers = provider.make_webhook(
        event_id="evt_1", event_type="payment.captured", payment=placeholder_payment(payment)
    )
    await service.handle_webhook(raw_body, headers)


asyncio.run(main())
