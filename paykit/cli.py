"""`paykit` CLI - currently just database migrations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config


def _alembic_config() -> Config:
    from alembic.config import Config

    package_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(package_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(package_root / "migrations"))
    return cfg


def db_upgrade(revision: str = "head") -> None:
    from alembic import command

    command.upgrade(_alembic_config(), revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paykit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    db_parser = subparsers.add_parser("db", help="Database migration commands")
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    upgrade_parser = db_subparsers.add_parser("upgrade", help="Apply migrations up to a revision")
    upgrade_parser.add_argument("revision", nargs="?", default="head")

    args = parser.parse_args(argv)

    if args.command == "db" and args.db_command == "upgrade":
        db_upgrade(args.revision)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
