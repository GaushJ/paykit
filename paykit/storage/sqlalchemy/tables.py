"""Core (non-ORM) table definitions. Table prefix is configurable so paykit
can live inside an existing app database without name clashes.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)


class PayKitTables:
    """Holds one prefixed set of paykit tables bound to a MetaData."""

    def __init__(self, prefix: str = "paykit_") -> None:
        self.prefix = prefix
        self.metadata = MetaData()

        self.orders = Table(
            f"{prefix}orders",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("provider", String, nullable=False),
            Column("provider_order_id", String, nullable=False),
            Column("amount", BigInteger, nullable=False),
            Column("currency", String(3), nullable=False),
            Column("status", String, nullable=False),
            Column("receipt", String, nullable=True),
            Column("customer_ref", String, nullable=True),
            Column("metadata_json", JSON, nullable=False, default=dict),
            Column("idempotency_key", String, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint(
                "provider", "provider_order_id", name=f"uq_{prefix}orders_provider_order_id"
            ),
            UniqueConstraint("idempotency_key", name=f"uq_{prefix}orders_idempotency_key"),
        )

        self.payments = Table(
            f"{prefix}payments",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("provider", String, nullable=False),
            Column("provider_payment_id", String, nullable=False),
            Column("order_id", String, ForeignKey(f"{prefix}orders.id"), nullable=True),
            Column(
                "subscription_id",
                String,
                ForeignKey(f"{prefix}subscriptions.id"),
                nullable=True,
            ),
            Column("amount", BigInteger, nullable=False),
            Column("currency", String(3), nullable=False),
            Column("amount_refunded", BigInteger, nullable=False, default=0),
            Column("status", String, nullable=False),
            Column("method", String, nullable=True),
            Column("error_code", String, nullable=True),
            Column("error_description", String, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint(
                "provider", "provider_payment_id", name=f"uq_{prefix}payments_provider_payment_id"
            ),
        )

        self.refunds = Table(
            f"{prefix}refunds",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("provider_refund_id", String, nullable=False),
            Column("payment_id", String, ForeignKey(f"{prefix}payments.id"), nullable=False),
            Column("amount", BigInteger, nullable=False),
            Column("currency", String(3), nullable=False),
            Column("status", String, nullable=False),
            Column("reason", String, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint(
                "provider_refund_id", name=f"uq_{prefix}refunds_provider_refund_id"
            ),
        )

        self.plans = Table(
            f"{prefix}plans",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("provider_plan_id", String, nullable=False),
            Column("name", String, nullable=False),
            Column("amount", BigInteger, nullable=False),
            Column("currency", String(3), nullable=False),
            Column("period", String, nullable=False),
            Column("interval", Integer, nullable=False),
            UniqueConstraint("provider_plan_id", name=f"uq_{prefix}plans_provider_plan_id"),
        )

        self.subscriptions = Table(
            f"{prefix}subscriptions",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("provider_subscription_id", String, nullable=False),
            Column("plan_id", String, ForeignKey(f"{prefix}plans.id"), nullable=False),
            Column("customer_ref", String, nullable=True),
            Column("status", String, nullable=False),
            Column("current_start", DateTime(timezone=True), nullable=True),
            Column("current_end", DateTime(timezone=True), nullable=True),
            Column("total_count", Integer, nullable=True),
            Column("paid_count", Integer, nullable=False, default=0),
            Column("short_url", String, nullable=True),
            Column("metadata_json", JSON, nullable=False, default=dict),
            UniqueConstraint(
                "provider_subscription_id",
                name=f"uq_{prefix}subscriptions_provider_subscription_id",
            ),
        )

        self.processed_events = Table(
            f"{prefix}processed_events",
            self.metadata,
            Column("event_id", String, primary_key=True),
            Column("processed_at", DateTime(timezone=True), nullable=False),
        )

        self.outbound_deliveries = Table(
            f"{prefix}outbound_deliveries",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("event_id", String, nullable=False),
            Column("url", String, nullable=False),
            Column("payload", String, nullable=False),
            Column("attempts", Integer, nullable=False, default=0),
            Column("status", String, nullable=False),
            Column("next_attempt_at", DateTime(timezone=True), nullable=False),
            Column("last_error", String, nullable=True),
        )
