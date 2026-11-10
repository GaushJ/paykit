"""lookup indexes for provider ids, reconciliation, event pruning and outbound queue

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08

Fresh databases already get these from 0001 (which builds the current table
metadata); `if_not_exists` makes this a no-op there and adds them to
databases created by an earlier 0001.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _indexes() -> list[tuple[str, str, list[str]]]:
    p = os.environ.get("PAYKIT_TABLE_PREFIX", "paykit_")
    return [
        (f"ix_{p}orders_provider_order_id", f"{p}orders", ["provider_order_id"]),
        (f"ix_{p}orders_customer_ref", f"{p}orders", ["customer_ref"]),
        (f"ix_{p}orders_status_created_at", f"{p}orders", ["status", "created_at"]),
        (f"ix_{p}payments_provider_payment_id", f"{p}payments", ["provider_payment_id"]),
        (f"ix_{p}payments_order_id", f"{p}payments", ["order_id"]),
        (f"ix_{p}payments_subscription_id", f"{p}payments", ["subscription_id"]),
        (f"ix_{p}payments_status_created_at", f"{p}payments", ["status", "created_at"]),
        (f"ix_{p}refunds_payment_id", f"{p}refunds", ["payment_id"]),
        (f"ix_{p}subscriptions_customer_ref", f"{p}subscriptions", ["customer_ref"]),
        (f"ix_{p}processed_events_processed_at", f"{p}processed_events", ["processed_at"]),
        (
            f"ix_{p}outbound_deliveries_status_next_attempt_at",
            f"{p}outbound_deliveries",
            ["status", "next_attempt_at"],
        ),
    ]


def upgrade() -> None:
    for name, table, columns in _indexes():
        op.create_index(name, table, columns, if_not_exists=True)


def downgrade() -> None:
    for name, table, _ in reversed(_indexes()):
        op.drop_index(name, table_name=table, if_exists=True)
