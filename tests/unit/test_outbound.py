import hashlib
import hmac
from datetime import UTC, datetime

import httpx
import pytest
import respx

from paykit.core.models import PaymentEvent
from paykit.service_app.outbound import (
    RETRY_DELAYS_SECONDS,
    DeliveryStatus,
    InMemoryOutboundDeliveryRepository,
    OutboundWebhookSender,
    sign,
)

SECRET = "outbound_secret"
URL = "https://client-backend.example.com/paykit/webhook"


def make_event(event_id: str = "evt_1") -> PaymentEvent:
    return PaymentEvent(
        id=event_id,
        type="payment.captured",
        provider="dummy",
        occurred_at=datetime.now(UTC),
        raw={},
    )


def make_sender(**kwargs) -> OutboundWebhookSender:
    return OutboundWebhookSender(url=URL, secret=SECRET, **kwargs)


class TestSign:
    def test_matches_manual_hmac(self) -> None:
        body = b'{"a":1}'
        expected = hmac.new(SECRET.encode(), b"123." + body, hashlib.sha256).hexdigest()
        assert sign(SECRET, "123", body) == expected


@respx.mock
async def test_successful_delivery_marks_delivered_with_correct_headers() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200))
    repo = InMemoryOutboundDeliveryRepository()
    sender = make_sender(repository=repo)

    await sender.handle_event(make_event("evt_1"))

    sent = route.calls.last.request
    assert sent.headers["X-PayKit-Event-Id"] == "evt_1"
    assert "X-PayKit-Signature" in sent.headers
    assert "X-PayKit-Timestamp" in sent.headers

    timestamp = sent.headers["X-PayKit-Timestamp"]
    expected_sig = sign(SECRET, timestamp, sent.content)
    assert sent.headers["X-PayKit-Signature"] == expected_sig

    deliveries = list(repo._deliveries.values())
    assert len(deliveries) == 1
    assert deliveries[0].status == DeliveryStatus.DELIVERED

    await sender.aclose()


@respx.mock
async def test_failed_delivery_schedules_retry() -> None:
    respx.post(URL).mock(return_value=httpx.Response(500))
    repo = InMemoryOutboundDeliveryRepository()
    sender = make_sender(repository=repo)

    await sender.handle_event(make_event("evt_1"))

    delivery = next(iter(repo._deliveries.values()))
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempts == 1
    assert delivery.last_error is not None
    assert delivery.next_attempt_at > datetime.now(UTC)

    await sender.aclose()


@respx.mock
async def test_exhausted_retries_marks_failed() -> None:
    respx.post(URL).mock(return_value=httpx.Response(500))
    repo = InMemoryOutboundDeliveryRepository()
    sender = make_sender(repository=repo)

    await sender.handle_event(make_event("evt_1"))
    delivery = next(iter(repo._deliveries.values()))

    # Exhaust the remaining retries directly via _attempt (bypassing the
    # real backoff delays, which would make this test take hours).
    for _ in range(len(RETRY_DELAYS_SECONDS)):
        await sender._attempt(delivery)

    assert delivery.status == DeliveryStatus.FAILED
    await sender.aclose()


@respx.mock
async def test_run_worker_delivers_due_items_and_stops_on_cancel() -> None:
    import asyncio

    route = respx.post(URL).mock(return_value=httpx.Response(200))
    repo = InMemoryOutboundDeliveryRepository()
    sender = make_sender(repository=repo)

    await sender.handle_event(make_event("evt_1"))
    # handle_event already attempts delivery once and succeeds, so nothing
    # should be "due" - simulate a delivery that's due right now instead.
    from paykit.service_app.outbound import OutboundDelivery

    pending = OutboundDelivery(
        id="delivery_2",
        event_id="evt_2",
        url=URL,
        payload=b'{"id":"evt_2"}',
        next_attempt_at=datetime.now(UTC),
    )
    await repo.create(pending)

    task = asyncio.create_task(sender.run_worker(poll_interval_seconds=0.01))
    for _ in range(50):
        if repo._deliveries["delivery_2"].status == DeliveryStatus.DELIVERED:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert repo._deliveries["delivery_2"].status == DeliveryStatus.DELIVERED
    assert route.call_count >= 2
    await sender.aclose()
