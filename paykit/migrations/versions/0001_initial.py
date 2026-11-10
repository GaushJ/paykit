"""initial paykit tables

Revision ID: 0001
Revises:
Create Date: 2026-09-23

"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

from paykit.storage.sqlalchemy.tables import PayKitTables

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _tables() -> PayKitTables:
    return PayKitTables(os.environ.get("PAYKIT_TABLE_PREFIX", "paykit_"))


def upgrade() -> None:
    # Reuses the same Table definitions the app queries against (single
    # source of truth) rather than hand-duplicating them as op.create_table
    # calls. Pre-1.0 and pre-release: this migration tracks the *current*
    # full table set rather than a frozen historical snapshot. Once paykit
    # has real deployments, schema changes must become incremental
    # revisions on top of this one instead of editing it in place.
    _tables().metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    _tables().metadata.drop_all(op.get_bind())
