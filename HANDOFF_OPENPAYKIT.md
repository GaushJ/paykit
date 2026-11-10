# Handoff: paykit → OpenPayKit (open-source release prep)

Context for a Claude session working in `C:\work\paykit`. Written from a session working in
`C:\work\fit_app`, the first app that uses this library. Delete this file before committing, or don't commit it.

## 1. What happened and why

The owner wants to release this library as an open-source project named **OpenPayKit**.
Release-prep changes were made in this repo. **They are uncommitted.** The owner reviews and
commits themselves; don't commit or push unless asked.

Decisions already made:
- **The PyPI distribution name is `openpaykit`.** `paykit` is already taken on PyPI by an
  unrelated project; `openpaykit` was free as of 2026-11-08.
- **The import name stays `paykit`.** The `paykit` CLI command and the `PAYKIT_*` env-var prefix
  are also unchanged, so consumers like fit_app need no code changes, only a different requirements line.
- **Licence:** MIT, which pyproject already declared.
- **Releases:** published to PyPI from GitHub Actions with Trusted Publishing, triggered by `v*` tags.

## 2. Changes made (uncommitted)

| File | Change | Why |
|---|---|---|
| `migrations/` → `paykit/migrations/` | `git mv` | The wheel only packages `paykit/`, so `paykit db upgrade` was broken for anyone who installed from pip: no migrations shipped. |
| `alembic.ini` → `paykit/alembic.ini` | `git mv`; `script_location = %(here)s/migrations` | Same reason. |
| `paykit/cli.py` | `_alembic_config()` now resolves files relative to the package dir (`Path(__file__).parent`), not the repo root (`.parent.parent`) | Works from an installed wheel and from a source checkout. |
| `pyproject.toml` | `name = "openpaykit"`; added authors, keywords, classifiers, `[project.urls]`, `[tool.hatch.build.targets.sdist]` include list | Release metadata. |
| `pyproject.toml` | `service` extra changed from `paykit[fastapi]`/`paykit[sql]` to `openpaykit[...]` | **Real bug:** after the rename, the self-reference would have installed the *unrelated* PyPI `paykit` package. |
| `Dockerfile` | Removed `COPY migrations` and `COPY alembic.ini` | Both now live inside `paykit/`, which is already copied. |
| `README.md` | Title is "OpenPayKit"; `pip install openpaykit[...]`; added a note that the import/CLI name is still `paykit` | |
| `quickstart_demo.py` | `ruff --fix` import ordering (blank line) | Already failing before these changes; would have failed the new CI. |
| `LICENSE` | New, MIT, © 2026 gauransh | There was no licence file. |
| `.github/workflows/ci.yml` | New: on push to main and on PRs, runs `pip install -e ".[service,dev]"`, ruff, `mypy paykit`, and pytest on Python 3.11, 3.12 and 3.13 | |
| `.github/workflows/release.yml` | New: on a `v*` tag, checks the tag equals `v` + pyproject version, runs `python -m build`, and publishes with `pypa/gh-action-pypi-publish` (OIDC, environment `pypi`) | |

### Verification done
- `pytest`: 229 passed. `ruff check .`: clean. `mypy paykit`: clean.
- `python -m build` produced `openpaykit-0.1.0` wheel and sdist. The wheel contains `paykit/alembic.ini` and
  `paykit/migrations/**`, and no `__pycache__`.
- The wheel was installed on its own (`pip install --no-deps --target <dir>`). The test ran from outside the
  repo with `PYTHONPATH=<dir>`, and `paykit.__file__` confirmed the installed copy was the one imported.
  `paykit db upgrade` against `sqlite+aiosqlite` created `alembic_version` plus 7 `paykit_*` tables.
- Not verified: the GitHub Actions workflows (never run) and migrations against Postgres.

**Environment gotcha:** on this Windows machine, an Application Control policy blocks native DLLs
(e.g. `_pydantic_core`) in a *newly created* venv. Use the repo's existing `.venv` and install into a
`--target` directory instead.

## 3. Architecture

Provider-agnostic payments SDK. v1 has a Razorpay provider and a Dummy provider. The full design spec is in
`PAYKIT_SPEC.md`.

```
paykit/
  config.py            PayKitSettings (pydantic-settings, env prefix PAYKIT_, reads .env)
  cli.py               `paykit db upgrade [rev]` → Alembic, using paykit/alembic.ini + paykit/migrations
  core/
    models.py          Money (int minor units + ISO currency), Order, Payment, Refund, Plan, Subscription, PaymentEvent, status enums
    state.py           Allowed status transitions per entity; can_transition() is the single source of truth
    provider.py        PaymentProvider interface that every provider implements
    repository.py      PaymentRepository interface (storage abstraction, transactions, event dedup)
    events.py          EventBus: @events.on("payment.captured"); dispatch logs and swallows handler errors
    service.py         PaymentService (the public API) + build_service_from_env()
    errors.py
  providers/
    __init__.py        get_provider(name, settings) registry: "razorpay" | "dummy"
    razorpay/          adapter.py (provider), client.py (httpx, Basic auth key_id:key_secret),
                       signatures.py (HMAC-SHA256, constant-time), webhooks.py (event parsing/normalising),
                       mapping.py (Razorpay JSON → domain models)
    dummy/             fully working in-process provider for tests and demos; no real money moves
  storage/
    memory.py          InMemoryRepository (default when PAYKIT_DATABASE_URL is unset; lost on restart)
    sqlalchemy/        SQLAlchemyRepository + tables.py (PayKitTables(prefix)); async engine
  integrations/fastapi/
    router.py          create_payments_router(...) (embedded mode)
    __init__.py        paykit_lifespan(service): closes HTTP client and DB engine on shutdown
  service_app/         standalone deployable FastAPI app (main.py, auth.py API keys, outbound.py signed outbound webhooks)
  migrations/          Alembic env + versions/0001_initial.py (env.py reads PAYKIT_DATABASE_URL directly, so Razorpay keys aren't needed to migrate)
```

### Settings (`PAYKIT_*`)

- **Required:**
  - `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`. These are required even when
    `PROVIDER=dummy`, which is a usability wart; see open items.
- **Optional:**
  - `PROVIDER` (default `razorpay`)
  - `DATABASE_URL` (must be an async driver URL, e.g. `postgresql+asyncpg://`)
  - `TABLE_PREFIX` (`paykit_`)
  - `DEFAULT_CURRENCY` (`INR`)
  - `HTTP_TIMEOUT_SECONDS` (15)
- **Standalone mode only:** `SERVICE_API_KEYS`, `OUTBOUND_WEBHOOK_URL`, `OUTBOUND_WEBHOOK_SECRET`.
- A validator warns if a `rzp_live_` key is combined with a non-HTTPS outbound webhook URL.

### Core rules (per the spec)

- **Money** is always an int in minor units plus a currency, never a float.
- **The webhook is the source of truth.** `confirm_checkout` (the client callback) verifies the signature and
  updates state, but apps should grant entitlements from webhook events.
- **Idempotency:**
  - Order creation takes an idempotency key.
  - Webhooks are deduplicated by event ID (`record_event_if_new`) inside a repository transaction.
  - State transitions are monotonic: illegal or backward transitions are logged and ignored, not raised.
- **Event dispatch** happens *after* the transaction commits. A failing handler never causes a non-2xx
  response to the provider.

### Webhook flow (`PaymentService.handle_webhook`)

1. `provider.verify_webhook(raw_body, headers)` checks the HMAC of the raw body with the webhook secret.
   On failure it raises `SignatureVerificationError`.
2. `provider.parse_webhook` normalises the payload to a `PaymentEvent`.
3. In a transaction: dedupe by event ID, then reconcile subscription, order, payment and refund (upsert,
   respecting `can_transition`). A captured payment promotes its order to PAID even if the payload
   lacked an order object.
4. `events.dispatch(final_event)`.

Razorpay events handled: `payment.captured`, `payment.failed`, `order.paid`, `refund.created`,
`refund.processed`, `refund.failed`, plus subscription events.

### Embedded FastAPI routes (`create_payments_router`)

Arguments:
- `auth_dependency`
- `customer_ref_getter(user, body)`
- `amount_resolver(body, user) -> Money`: the server sets the price; it is never trusted from the client.
- `admin_dependency` (gates refunds)
- `enforce_ownership`

Routes:
- `POST /orders`: returns the order plus `checkout_config`, which includes the public `key_id` for the frontend's Razorpay Checkout.
- `POST /confirm`
- `GET /orders/{order_id}`
- `POST /{payment_id}/refunds`
- `POST /subscriptions`
- `POST /subscriptions/{id}/cancel`
- `POST /webhooks/{provider_name}`, i.e. `/webhooks/razorpay`

### Two ways to deploy it

1. **Embedded library** (what fit_app does): `build_service_from_env()`, mount `create_payments_router`,
   wrap with `paykit_lifespan`.
2. **Standalone service**: `uvicorn paykit.service_app.main:app`, or `docker compose up`, which runs Postgres,
   then `paykit db upgrade`, then the service. Other backends call it over REST using API keys and receive
   signed outbound webhooks.

## 4. How fit_app uses it (the reference consumer)

`C:\work\fit_app\backend\routers\payments.py`:
- `payment_service = build_service_from_env()` at import time.
- `PRICES = {"pro_unlock": Money(49900, "INR")}`. The amount resolver reads `body["metadata"]["item"]`.
  The metadata is persisted on the Order.
- `create_payments_router(payment_service, prefix="/api/payments", auth_dependency=get_current_user,
  customer_ref_getter=lambda user, body: str(user.id), amount_resolver=...)`.
- `@payment_service.events.on("payment.captured")` loads the order and checks `metadata.item == "pro_unlock"`,
  then sets `user.is_pro = True`.
- `main.py` wraps the app lifespan with `paykit_lifespan(payments.payment_service)`.
- Tests (`backend/tests/conftest.py`) set `PAYKIT_PROVIDER=dummy` plus placeholder Razorpay values.
- The frontend loads `checkout.js` and calls `new Razorpay(checkout_config)`, then POSTs the result to `/confirm`.
- Currently installed with `paykit[fastapi,sql] @ git+https://github.com/GaushJ/paykit.git@main`, which is unpinned.
  Once `openpaykit` is on PyPI, this becomes `openpaykit[fastapi,sql]==0.1.0`. **Do not change it before the
  package is published, or fit_app's Render build breaks.**
- fit_app production does not set `PAYKIT_DATABASE_URL` yet, so it uses in-memory storage. It needs Postgres
  plus `paykit db upgrade` before taking live payments; otherwise a restart between checkout and the webhook
  means the customer is charged and never unlocked.

## 5. Release steps remaining (owner)

1. Review, commit and push these changes.
2. On PyPI: Account → Publishing → add a pending publisher. Project `openpaykit`, owner `GaushJ`, repo `paykit`,
   workflow `release.yml`, environment `pypi`. On GitHub: create environment `pypi` (Settings → Environments).
3. Run `git tag v0.1.0 && git push origin v0.1.0`; the release workflow publishes.
4. Optional: rename the GitHub repo to `openpaykit`. GitHub redirects the old URL; update `[project.urls]`.
5. Switch fit_app to `openpaykit[fastapi,sql]==0.1.0`. Locally, run `pip uninstall paykit` first.

## 6. Open items / suggestions (not done)

- `PayKitSettings` requires the Razorpay keys even when `PROVIDER=dummy`. Consider making them optional and
  validating them only when `provider == "razorpay"`.
- Consider renaming the import package to `openpaykit` for the long term. Two installed packages both
  called `paykit` would clash. This is a breaking change, so do it before 1.0 if at all.
- Add `CHANGELOG.md`, `CONTRIBUTING.md` and `SECURITY.md` (how to report vulnerabilities; important for a payments lib).
- Add a CI job that runs migrations against a Postgres service container. Today only SQLite has been verified.
- The README could show the "embedded in FastAPI" pattern from fit_app as a worked example.
- Before publishing, scan the git history for secrets (e.g. `gitleaks`). `.env` is gitignored, but check anyway.
