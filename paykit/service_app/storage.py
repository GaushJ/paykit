"""SQLAlchemy-backed OutboundDeliveryRepository.

Persists retry state to the `outbound_deliveries` table so a process
restart mid-backoff doesn't silently drop a pending delivery.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from paykit.service_app.outbound import DeliveryStatus, OutboundDelivery
from paykit.storage.sqlalchemy.tables import PayKitTables


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SQLAlchemyOutboundDeliveryRepository:
    def __init__(self, engine: AsyncEngine, *, table_prefix: str = "paykit_") -> None:
        self._engine = engine
        self._table = PayKitTables(table_prefix).outbound_deliveries

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def create(self, delivery: OutboundDelivery) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                self._table.insert().values(
                    id=delivery.id,
                    event_id=delivery.event_id,
                    url=delivery.url,
                    payload=delivery.payload.decode("utf-8"),
                    attempts=delivery.attempts,
                    status=delivery.status.value,
                    next_attempt_at=delivery.next_attempt_at,
                    last_error=delivery.last_error,
                )
            )

    async def due(self, *, now: datetime) -> list[OutboundDelivery]:
        async with self._engine.begin() as conn:
            stmt = self._table.select().where(
                self._table.c.status == DeliveryStatus.PENDING.value,
                self._table.c.next_attempt_at <= now,
            )
            result = await conn.execute(stmt)
            return [self._row_to_delivery(row._mapping) for row in result]

    async def mark_delivered(self, delivery_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                self._table.update()
                .where(self._table.c.id == delivery_id)
                .values(status=DeliveryStatus.DELIVERED.value)
            )

    async def mark_retry(
        self, delivery_id: str, *, next_attempt_at: datetime, error: str
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                self._table.update()
                .where(self._table.c.id == delivery_id)
                .values(
                    attempts=self._table.c.attempts + 1,
                    next_attempt_at=next_attempt_at,
                    last_error=error,
                )
            )

    async def mark_failed(self, delivery_id: str, *, error: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                self._table.update()
                .where(self._table.c.id == delivery_id)
                .values(status=DeliveryStatus.FAILED.value, last_error=error)
            )

    @staticmethod
    def _row_to_delivery(row: RowMapping) -> OutboundDelivery:
        return OutboundDelivery(
            id=row["id"],
            event_id=row["event_id"],
            url=row["url"],
            payload=row["payload"].encode("utf-8"),
            attempts=row["attempts"],
            status=DeliveryStatus(row["status"]),
            next_attempt_at=_ensure_utc(row["next_attempt_at"]),
            last_error=row["last_error"],
        )
