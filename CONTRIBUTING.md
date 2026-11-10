# Contributing to OpenPayKit

Thanks for helping. A few ground rules keep a payments library trustworthy.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[service,dev]"
pytest && ruff check . && mypy paykit
```

To run the repository contract suite against real Postgres, set
`PAYKIT_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/paykit_test`.

## Rules for changes

- **Money is `int` minor units + currency.** Never a float.
- **State changes go through `can_transition`** (`paykit/core/state.py`). Never move a status
  backwards.
- **The webhook is the source of truth.** `confirm_checkout` is a fast UI hint only.
- **Every bug fix comes with a regression test** that fails without the fix.
- **Schema changes are new Alembic revisions** under `paykit/migrations/versions/`. Never edit a
  released one.
- **New repository methods** must be added to the `PaymentRepository` protocol and to both
  backends, and covered by `tests/integration/test_repository_contract.py`.
- **New providers** follow the "How to add a new provider" section of the README.
  `DummyProvider` is the reference.

## Pull requests

Keep PRs focused. CI must pass: lint, mypy, tests on Python 3.11–3.13, the Postgres job,
dependency audit, secret scan and the package build. Add an entry under "Unreleased" in
`CHANGELOG.md`.

Security issues: please don't open a PR or issue. See [SECURITY.md](SECURITY.md).
