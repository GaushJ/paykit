from datetime import UTC, datetime

from paykit.core.events import EventBus
from paykit.core.models import PaymentEvent


def make_event(event_type: str = "payment.captured", event_id: str = "evt_1") -> PaymentEvent:
    return PaymentEvent(
        id=event_id, type=event_type, provider="dummy", occurred_at=datetime.now(UTC), raw={}
    )


async def test_dispatch_calls_registered_handler_for_matching_type() -> None:
    bus = EventBus()
    received = []

    @bus.on("payment.captured")
    async def handler(event: PaymentEvent) -> None:
        received.append(event)

    event = make_event("payment.captured")
    await bus.dispatch(event)

    assert received == [event]


async def test_dispatch_does_not_call_handler_for_other_types() -> None:
    bus = EventBus()
    received = []

    @bus.on("payment.captured")
    async def handler(event: PaymentEvent) -> None:
        received.append(event)

    await bus.dispatch(make_event("payment.failed"))

    assert received == []


async def test_subscribe_without_decorator() -> None:
    bus = EventBus()
    received = []

    async def handler(event: PaymentEvent) -> None:
        received.append(event)

    bus.subscribe("refund.processed", handler)
    await bus.dispatch(make_event("refund.processed"))

    assert len(received) == 1


async def test_wildcard_handler_runs_for_every_event_type() -> None:
    bus = EventBus()
    received = []

    @bus.on("*")
    async def catch_all(event: PaymentEvent) -> None:
        received.append(event.type)

    await bus.dispatch(make_event("payment.captured"))
    await bus.dispatch(make_event("subscription.cancelled"))

    assert received == ["payment.captured", "subscription.cancelled"]


async def test_specific_and_wildcard_handlers_both_run() -> None:
    bus = EventBus()
    calls = []

    @bus.on("payment.captured")
    async def specific(event: PaymentEvent) -> None:
        calls.append("specific")

    @bus.on("*")
    async def wildcard(event: PaymentEvent) -> None:
        calls.append("wildcard")

    await bus.dispatch(make_event("payment.captured"))

    assert sorted(calls) == ["specific", "wildcard"]


async def test_handler_exception_is_swallowed_and_logged() -> None:
    bus = EventBus()
    calls = []

    @bus.on("payment.captured")
    async def bad(event: PaymentEvent) -> None:
        raise RuntimeError("boom")

    @bus.on("payment.captured")
    async def good(event: PaymentEvent) -> None:
        calls.append("good")

    # Must not raise, and later handlers still run.
    await bus.dispatch(make_event("payment.captured"))

    assert calls == ["good"]
