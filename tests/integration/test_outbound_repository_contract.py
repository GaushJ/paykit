"""Same suite against both OutboundDeliveryRepository implementations,
mirroring tests/integration/test_repository_contract.py's approach."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from paykit.service_app.outbound import (
    DeliveryStatus,
    InMemoryOutboundDeliveryRepository,
    OutboundDelivery,
)
from paykit.service_app.storage import SQLAlchemyOutboundDeliveryRepository
from paykit.storage.sqlalchemy.repository import SQLAlchemyRepository

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_delivery(**overrides: object) -> OutboundDelivery:
    defaults: dict[str, object] = dict(
        id="delivery_1",
        event_id="evt_1",
        url="https://example.com/hook",
        payload=b'{"id":"evt_1"}',
        attempts=0,
        status=DeliveryStatus.PENDING,
        next_attempt_at=NOW,
        last_error=None,
    )
    defaults.update(overrides)
    return OutboundDelivery(**defaults)  # type: ignore[arg-type]


async def _make_memory_repo() -> InMemoryOutboundDeliveryRepository:
    return InMemoryOutboundDeliveryRepository()


async def _make_sqlalchemy_repo() -> SQLAlchemyOutboundDeliveryRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    # Reuse SQLAlchemyRepository.create_all() so outbound_deliveries (part
    # of the same PayKitTables.metadata) gets created too.
    await SQLAlchemyRepository(engine).create_all()
    return SQLAlchemyOutboundDeliveryRepository(engine)


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


async def test_create_and_due(repo) -> None:
    await repo.create(make_delivery())

    due = await repo.due(now=NOW + timedelta(seconds=1))

    assert [d.id for d in due] == ["delivery_1"]
    assert due[0].event_id == "evt_1"
    assert due[0].payload == b'{"id":"evt_1"}'


async def test_not_due_yet_is_excluded(repo) -> None:
    await repo.create(make_delivery(next_attempt_at=NOW + timedelta(hours=1)))

    due = await repo.due(now=NOW)

    assert due == []


async def test_mark_delivered_excludes_from_due(repo) -> None:
    await repo.create(make_delivery())
    await repo.mark_delivered("delivery_1")

    due = await repo.due(now=NOW + timedelta(seconds=1))

    assert due == []


async def test_mark_retry_bumps_attempts_and_next_attempt_at(repo) -> None:
    await repo.create(make_delivery())
    later = NOW + timedelta(minutes=5)

    await repo.mark_retry("delivery_1", next_attempt_at=later, error="connection reset")

    due_now = await repo.due(now=NOW + timedelta(seconds=1))
    assert due_now == []

    due_later = await repo.due(now=later + timedelta(seconds=1))
    assert len(due_later) == 1
    assert due_later[0].attempts == 1
    assert due_later[0].last_error == "connection reset"


async def test_mark_failed_excludes_from_due(repo) -> None:
    await repo.create(make_delivery())

    await repo.mark_failed("delivery_1", error="exhausted retries")

    due = await repo.due(now=NOW + timedelta(days=1))
    assert due == []
