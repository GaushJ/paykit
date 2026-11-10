"""Signed outbound webhooks to client backends (§10).

After each normalized paykit event, POST it as JSON to
`PAYKIT_OUTBOUND_WEBHOOK_URL`, retrying on failure with backoff
(1m, 5m, 30m, 2h, 6h). Deliveries are tracked in an `outbound_deliveries`
table so retries survive a process restart.

`handle_event` only *enqueues* (one INSERT); it never makes the HTTP call
inline, so a slow or unreachable client backend can't slow paykit's response
to the payment provider's webhook. `run_worker` delivers in the background.
Deliveries are *claimed* with a short lease before sending, so several
service replicas can run workers against one database without sending the
same delivery twice. A crashed worker's lease simply expires and another
replica picks the delivery up.

**Receivers verify the signature like this:**

    expected = hmac_sha256(outbound_secret, f"{timestamp}.{raw_body}")
    if not hmac.compare_digest(expected, headers["X-PayKit-Signature"]):
        reject()
    if abs(now() - int(headers["X-PayKit-Timestamp"])) > 300:
        reject()  # replay protection: 5 minutes

Use `headers["X-PayKit-Event-Id"]` to dedupe redelivered events, the same
way paykit itself dedupes provider webhooks. Delivery is at-least-once.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

import httpx

from paykit.core.models import PaymentEvent

logger = logging.getLogger("paykit.service_app.outbound")

RETRY_DELAYS_SECONDS = [60, 300, 1800, 7200, 21600]  # 1m, 5m, 30m, 2h, 6h
SIGNATURE_HEADER = "X-PayKit-Signature"
EVENT_ID_HEADER = "X-PayKit-Event-Id"
TIMESTAMP_HEADER = "X-PayKit-Timestamp"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"  # exhausted retries


@dataclass
class OutboundDelivery:
    id: str
    event_id: str
    url: str
    payload: bytes
    attempts: int = 0
    status: DeliveryStatus = DeliveryStatus.PENDING
    next_attempt_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error: str | None = None


class OutboundDeliveryRepository(Protocol):
    async def create(self, delivery: OutboundDelivery) -> None: ...
    async def due(self, *, now: datetime) -> list[OutboundDelivery]: ...
    async def claim_due(
        self, *, now: datetime, limit: int, lease_seconds: float
    ) -> list[OutboundDelivery]:
        """Atomically claim up to `limit` due deliveries (oldest first) by
        pushing their `next_attempt_at` to `now + lease_seconds`. Each
        delivery is returned to at most one concurrent caller per lease."""
        ...

    async def mark_delivered(self, delivery_id: str) -> None: ...
    async def mark_retry(
        self, delivery_id: str, *, next_attempt_at: datetime, error: str
    ) -> None: ...
    async def mark_failed(self, delivery_id: str, *, error: str) -> None: ...


class InMemoryOutboundDeliveryRepository:
    """For tests / quick starts. See service_app/storage.py for a persistent one."""

    def __init__(self) -> None:
        self._deliveries: dict[str, OutboundDelivery] = {}

    async def create(self, delivery: OutboundDelivery) -> None:
        self._deliveries[delivery.id] = delivery

    async def due(self, *, now: datetime) -> list[OutboundDelivery]:
        return [
            d
            for d in self._deliveries.values()
            if d.status == DeliveryStatus.PENDING and d.next_attempt_at <= now
        ]

    async def claim_due(
        self, *, now: datetime, limit: int, lease_seconds: float
    ) -> list[OutboundDelivery]:
        claimed = sorted(await self.due(now=now), key=lambda d: d.next_attempt_at)[:limit]
        lease_until = now + timedelta(seconds=lease_seconds)
        for delivery in claimed:
            delivery.next_attempt_at = lease_until
        return claimed

    async def mark_delivered(self, delivery_id: str) -> None:
        self._deliveries[delivery_id].status = DeliveryStatus.DELIVERED

    async def mark_retry(
        self, delivery_id: str, *, next_attempt_at: datetime, error: str
    ) -> None:
        delivery = self._deliveries[delivery_id]
        delivery.attempts += 1
        delivery.next_attempt_at = next_attempt_at
        delivery.last_error = error

    async def mark_failed(self, delivery_id: str, *, error: str) -> None:
        delivery = self._deliveries[delivery_id]
        delivery.status = DeliveryStatus.FAILED
        delivery.last_error = error


def sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    message = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class OutboundWebhookSender:
    """Subscribe via `service.events.subscribe("*", sender.handle_event)` to
    enqueue every normalized event, and run `run_worker()` as a background
    task to deliver them to the client backend's webhook URL."""

    def __init__(
        self,
        *,
        url: str,
        secret: str,
        repository: OutboundDeliveryRepository | None = None,
        http_client: httpx.AsyncClient | None = None,
        batch_size: int = 50,
        concurrency: int = 10,
        lease_seconds: float = 60.0,
    ) -> None:
        self._url = url
        self._secret = secret
        self._repository = repository or InMemoryOutboundDeliveryRepository()
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            # A redirect must not bounce signed payment events to another host.
            follow_redirects=False,
            limits=httpx.Limits(max_connections=concurrency),
        )
        self._batch_size = batch_size
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lease_seconds = lease_seconds
        self._wakeup = asyncio.Event()

    async def handle_event(self, event: PaymentEvent) -> None:
        raw_body = event.model_dump_json(exclude={"raw"}).encode("utf-8")
        delivery = OutboundDelivery(
            id=str(uuid4()), event_id=event.id, url=self._url, payload=raw_body
        )
        await self._repository.create(delivery)
        self._wakeup.set()

    async def process_due(self) -> int:
        """Claim and attempt one batch of due deliveries; return how many
        were attempted. `run_worker` calls this in a loop. Tests and
        cron-style deployments can call it directly."""
        due = await self._repository.claim_due(
            now=datetime.now(UTC), limit=self._batch_size, lease_seconds=self._lease_seconds
        )
        if due:
            await asyncio.gather(*(self._attempt_bounded(d) for d in due))
        return len(due)

    async def _attempt_bounded(self, delivery: OutboundDelivery) -> None:
        async with self._semaphore:
            try:
                await self._attempt(delivery)
            except Exception:  # one bad delivery must not kill the batch/worker
                logger.exception("outbound delivery %s crashed", delivery.id)

    async def _attempt(self, delivery: OutboundDelivery) -> None:
        timestamp = str(int(time.time()))
        signature = sign(self._secret, timestamp, delivery.payload)
        headers = {
            EVENT_ID_HEADER: delivery.event_id,
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: signature,
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.post(
                delivery.url, content=delivery.payload, headers=headers
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            await self._schedule_retry(delivery, error=str(exc)[:500])
            return
        await self._repository.mark_delivered(delivery.id)

    async def _schedule_retry(self, delivery: OutboundDelivery, *, error: str) -> None:
        if delivery.attempts >= len(RETRY_DELAYS_SECONDS):
            await self._repository.mark_failed(delivery.id, error=error)
            logger.error("outbound delivery %s exhausted retries: %s", delivery.id, error)
            return
        delay_seconds = RETRY_DELAYS_SECONDS[delivery.attempts]
        next_attempt_at = datetime.fromtimestamp(time.time() + delay_seconds, tz=UTC)
        await self._repository.mark_retry(
            delivery.id, next_attempt_at=next_attempt_at, error=error
        )

    async def run_worker(self, *, poll_interval_seconds: float = 5.0) -> None:
        """Background delivery loop. Start with
        `asyncio.create_task(sender.run_worker())` and cancel it at shutdown.

        Wakes immediately when `handle_event` enqueues; otherwise polls, to
        pick up scheduled retries and other replicas' expired leases. Survives
        transient errors such as the database being briefly unavailable."""
        while True:
            # Clear *before* processing: an event enqueued mid-batch re-sets
            # the flag, so the wait below returns at once instead of missing it.
            self._wakeup.clear()
            try:
                while await self.process_due() >= self._batch_size:
                    pass  # backlog: keep draining before sleeping
            except Exception:
                logger.exception("outbound worker iteration failed; will retry")
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        await self._client.aclose()
