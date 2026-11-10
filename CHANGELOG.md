# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First public release as **OpenPayKit** (`pip install openpaykit`). The import name, the `paykit`
CLI and the `PAYKIT_*` settings are unchanged.

### Security
- The refund route is only mounted with an `admin_dependency` (or in trusted-backend mode with
  `enforce_ownership=False`). Previously any authenticated user could refund any payment.
- Idempotency keys are scoped per customer. Reusing another customer's key used to return that
  customer's order.
- `/confirm` enforces order and subscription ownership.
- `DummyProvider` signs with a random per-process secret instead of the well-known
  `"dummy_secret"`, so a deployment left on `PAYKIT_PROVIDER=dummy` can't have its webhooks
  forged.
- A live Razorpay key with a non-HTTPS outbound webhook URL is a startup error (it was a warning).
  The outbound secret is required whenever the URL is set.
- Bounds on all client input: metadata, receipt, idempotency key, checkout result, webhook body
  size (413).
- Provider IDs are validated before they are interpolated into Razorpay REST paths.
- Provider errors return a generic 502 instead of leaking gateway details. Resolver errors return
  422 instead of 500.
- Refunds are validated against the refundable balance and currency.
- Docker image runs as a non-root user without a compiler toolchain. docker-compose binds ports
  to localhost.
- CI uses least-privilege tokens, and adds `pip-audit` and a gitleaks secret scan.

### Fixed
- A payment-only `payment.captured` webhook (common with Razorpay) arriving before `/confirm`
  stored the payment without its order, and never marked the order PAID. The customer was charged
  but not granted.
- A webhook no longer marks an order PAID when the payment amount differs from the order amount.
- Concurrent `/confirm` and webhook, or concurrent orders with the same idempotency key, failed
  with a 500 on a unique constraint. Inserts now run in savepoints and the loser re-reads the
  winning row.
- Refunds were double-counted when the refund webhook landed before `refund()` recorded it.
- SQLite: `SAVEPOINT` silently committed the enclosing transaction (a pysqlite quirk), so
  rollbacks didn't roll back.
- The outbound webhook worker died permanently on any unexpected exception.
- Migrations ship inside the wheel, so `paykit db upgrade` works from a pip install.

### Changed / Added
- Outbound webhooks are queued and delivered by a background worker, never inline in the provider
  webhook request. Lease-based claiming is safe with multiple replicas. There are concurrency and
  batch limits.
- The standalone service shares one DB pool between payments and outbound deliveries, and adds
  `GET /readyz`.
- Indexes for provider-id lookups, reconciliation, the outbound queue and event pruning (Alembic
  revision `0002`).
- Pool settings: `PAYKIT_DB_POOL_SIZE`, `PAYKIT_DB_MAX_OVERFLOW`, `PAYKIT_DB_POOL_RECYCLE_SECONDS`,
  plus `pool_pre_ping`.
- `PaymentService.reconcile(since, limit=500)` is batched, isolates per-object failures and
  returns the update count.
- `PaymentService.prune_processed_events()` caps growth of the dedupe table.
- `PaymentService.provider`, `.repository`, `.aclose()`, `.get_payment()` and
  `.get_order_by_provider_id()` are new public APIs.
- Razorpay settings are optional unless `PAYKIT_PROVIDER=razorpay`.
- In-memory repository: O(1) indexed lookups, reentrant transactions, and the same uniqueness
  rules as SQL.
- CI: a Postgres job (contract suite plus migration up/down), and a build job that checks the
  wheel ships its migrations.
