# PayKit — Pluggable Payments SDK (Python) — Implementation Spec

> **For Claude Code:** This is the full spec for a reusable payments package. Build it phase by phase (see §12). After each phase, run the tests and make sure they pass before moving on. If something here conflicts with Razorpay's current official docs (https://razorpay.com/docs/api/), follow the docs and leave a `# NOTE:` comment explaining the difference. Don't invent API fields. If you're unsure, check the docs or ask.

---

## 1. Goal

Build **`paykit`**, a provider-agnostic payments package that can be dropped into any project with minimal wiring.

- **v1 provider:** Razorpay (India: UPI, cards, netbanking, wallets; one-time payments, refunds, subscriptions).
- **Later providers:** Dodo Payments / Paddle / Stripe for international. The core must make adding one a new adapter only, with **zero changes to core or app code**.
- **Two ways to use it:**
  1. **Embedded library:** `pip install paykit[fastapi]`, then mount a router in any FastAPI app.
  2. **Standalone service:** a small deployable FastAPI app (`paykit-service`) exposing REST + outbound webhooks, so backends in **any language** (Node, Go, etc.) can use it over HTTP.

### Non-goals (v1)
- No card data ever touches our servers. Razorpay Checkout handles it, which keeps us out of PCI scope.
- No UI beyond a minimal example checkout page.
- No multi-tenant billing dashboard.

---

## 2. Design principles

1. **Provider-agnostic core.** App code talks only to `PaymentService`, never to Razorpay directly.
2. **Money is `int` in minor units** (paise, cents) plus an ISO-4217 `currency` string. Never use floats.
3. **The webhook is the source of truth.** The client-side success callback is only a hint. Verify its signature, then confirm status by fetching from the provider API. Final state changes come from verified webhooks or verified API fetches.
4. **Idempotent everything.** Order creation takes an idempotency key. Webhooks are deduplicated by event ID. State transitions are monotonic, so a late webhook can't move `captured` back to `authorized`.
5. **Async-first.** FastAPI is async. Call Razorpay's REST API directly with `httpx.AsyncClient` rather than the sync `razorpay` SDK. Keep signature verification in our own code using `hmac` and `hmac.compare_digest`.
6. **Pluggable storage.** Persistence goes through a `PaymentRepository` protocol. Ship an in-memory implementation (for tests) and a SQLAlchemy 2.0 async implementation (Postgres/SQLite).
7. **Hooks, not coupling.** The app reacts to payment events by registering handlers (e.g. `@paykit.on("payment.captured")` to grant access). PayKit never imports app code.
8. **Secrets from env only.** Never log secrets, full signatures, or raw webhook bodies at INFO level.

---

## 3. Tech stack

- Python 3.11+
- `httpx` (async HTTP), `pydantic` v2, `pydantic-settings`
- `sqlalchemy[asyncio]` 2.x + `alembic` (optional extra `[sql]`)
- `fastapi` (optional extra `[fastapi]`)
- Tests: `pytest`, `pytest-asyncio`, `respx` (mock httpx), `httpx` test client
- Tooling: `ruff`, `mypy --strict` on `paykit/core`, `uv` or `pip` + `pyproject.toml`

Extras in `pyproject.toml`: `paykit[fastapi]`, `paykit[sql]`, `paykit[service]` (= fastapi + sql + uvicorn), `paykit[dev]`.

---

## 4. Repository layout

```
paykit/
├── pyproject.toml
├── README.md
├── .env.example
├── paykit/
│   ├── __init__.py              # public API exports
│   ├── config.py                # PayKitSettings (pydantic-settings, env prefix PAYKIT_)
│   ├── core/
│   │   ├── models.py            # domain models + enums (Money, Order, Payment, Refund, Subscription, Customer, PaymentEvent)
│   │   ├── provider.py          # PaymentProvider Protocol (the adapter contract)
│   │   ├── service.py           # PaymentService — the facade apps use
│   │   ├── events.py            # EventBus: register/dispatch handlers
│   │   ├── state.py             # allowed status transitions
│   │   ├── errors.py            # exception hierarchy
│   │   └── repository.py        # PaymentRepository Protocol
│   ├── providers/
│   │   ├── __init__.py          # registry: get_provider(name, settings)
│   │   └── razorpay/
│   │       ├── client.py        # thin async REST client (httpx)
│   │       ├── adapter.py       # RazorpayProvider implements PaymentProvider
│   │       ├── signatures.py    # HMAC verification helpers
│   │       ├── mapping.py       # Razorpay payloads/statuses <-> domain models
│   │       └── webhooks.py      # event parsing + normalization
│   ├── storage/
│   │   ├── memory.py            # InMemoryRepository
│   │   └── sqlalchemy/
│   │       ├── tables.py
│   │       └── repository.py    # SQLAlchemyRepository
│   ├── integrations/
│   │   └── fastapi/
│   │       ├── router.py        # create_payments_router(service, ...) -> APIRouter
│   │       ├── deps.py
│   │       └── schemas.py       # request/response DTOs
│   └── service_app/             # standalone deployable service
│       ├── main.py              # create_app()
│       ├── auth.py              # API-key auth for client backends
│       └── outbound.py          # signed outbound webhooks to client backends
├── migrations/                  # alembic
├── examples/
│   ├── fastapi_embedded/        # minimal app mounting the router + checkout.html
│   └── node_client/             # tiny example calling the standalone service
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/                # sample Razorpay webhook payloads
```

---

## 5. Configuration

`paykit/config.py`, using `pydantic-settings` with `env_prefix="PAYKIT_"`:

| Env var | Required | Notes |
|---|---|---|
| `PAYKIT_PROVIDER` | yes | `razorpay` for now |
| `PAYKIT_RAZORPAY_KEY_ID` | yes | `rzp_test_...` / `rzp_live_...` |
| `PAYKIT_RAZORPAY_KEY_SECRET` | yes | `SecretStr` |
| `PAYKIT_RAZORPAY_WEBHOOK_SECRET` | yes | set in the Razorpay dashboard. **Different from the key secret** |
| `PAYKIT_DATABASE_URL` | no | e.g. `postgresql+asyncpg://...`; if absent, use in-memory |
| `PAYKIT_DEFAULT_CURRENCY` | no | default `INR` |
| `PAYKIT_HTTP_TIMEOUT_SECONDS` | no | default `15` |
| `PAYKIT_SERVICE_API_KEYS` | service only | comma-separated keys for client backends |
| `PAYKIT_OUTBOUND_WEBHOOK_URL` | service only | where to notify the client backend |
| `PAYKIT_OUTBOUND_WEBHOOK_SECRET` | service only | used to sign outbound events |

Log a warning at startup when live keys are used with a non-HTTPS webhook URL.

---

## 6. Domain model (`core/models.py`)

Use Pydantic v2 models. All timestamps are timezone-aware UTC.

```python
class Money(BaseModel):
    amount: int          # minor units, > 0
    currency: str        # ISO-4217 upper-case, e.g. "INR"

class OrderStatus(StrEnum):   CREATED, ATTEMPTED, PAID, EXPIRED
class PaymentStatus(StrEnum): CREATED, AUTHORIZED, CAPTURED, FAILED, REFUNDED, PARTIALLY_REFUNDED
class RefundStatus(StrEnum):  PENDING, PROCESSED, FAILED
class SubscriptionStatus(StrEnum):
    CREATED, AUTHENTICATED, ACTIVE, PENDING, HALTED, PAUSED, CANCELLED, COMPLETED, EXPIRED

class Order(BaseModel):
    id: str                       # our UUID
    provider: str                 # "razorpay"
    provider_order_id: str        # "order_..."
    amount: Money
    status: OrderStatus
    receipt: str | None           # app's reference, e.g. invoice id
    customer_ref: str | None      # app's user id (opaque to paykit)
    metadata: dict[str, str]      # forwarded as provider "notes"
    idempotency_key: str | None
    created_at: datetime; updated_at: datetime

class Payment(BaseModel):
    id: str; provider: str; provider_payment_id: str
    order_id: str | None; subscription_id: str | None
    amount: Money; amount_refunded: int
    status: PaymentStatus
    method: str | None            # upi / card / netbanking / wallet
    error_code: str | None; error_description: str | None
    created_at: datetime; updated_at: datetime

class Refund(BaseModel): id, provider_refund_id, payment_id, amount: Money, status, reason, created_at

class Plan(BaseModel): id, provider_plan_id, name, amount: Money, period ("daily"|"weekly"|"monthly"|"yearly"), interval: int

class Subscription(BaseModel):
    id, provider_subscription_id, plan_id, customer_ref, status,
    current_start, current_end, total_count, paid_count, short_url, metadata

class PaymentEvent(BaseModel):
    """Normalized event emitted to app handlers and outbound webhooks."""
    id: str                       # provider event id (used for dedupe)
    type: str                     # normalized, see §8.5
    provider: str
    occurred_at: datetime
    order: Order | None; payment: Payment | None
    refund: Refund | None; subscription: Subscription | None
    raw: dict                     # original payload (never exposed publicly)
```

### State transitions (`core/state.py`)
Put the allowed transitions in one table and write a function `can_transition(old, new) -> bool`. Illegal or backward transitions are **ignored and logged**, not raised. Webhooks arrive out of order, and that is normal.

Payment: `CREATED → AUTHORIZED → CAPTURED → (PARTIALLY_REFUNDED →) REFUNDED`; `CREATED|AUTHORIZED → FAILED`. A `FAILED` payment may still be followed by a different successful payment on the same order, so order status is tracked separately.

---

## 7. Core contracts

### 7.1 Provider protocol (`core/provider.py`)

```python
class PaymentProvider(Protocol):
    name: str

    async def create_order(self, amount: Money, *, receipt: str | None,
                           metadata: dict[str, str]) -> ProviderOrder: ...
    async def fetch_order(self, provider_order_id: str) -> ProviderOrder: ...
    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment: ...
    async def capture_payment(self, provider_payment_id: str, amount: Money) -> ProviderPayment: ...
    async def refund_payment(self, provider_payment_id: str, amount: Money | None,
                             *, reason: str | None, metadata: dict[str, str]) -> ProviderRefund: ...

    async def create_plan(self, name: str, amount: Money, period: str, interval: int) -> ProviderPlan: ...
    async def create_subscription(self, provider_plan_id: str, *, total_count: int,
                                  customer_notify: bool, metadata: dict[str, str],
                                  start_at: datetime | None = None) -> ProviderSubscription: ...
    async def cancel_subscription(self, provider_subscription_id: str, *, at_cycle_end: bool) -> ProviderSubscription: ...
    async def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription: ...

    def verify_checkout_signature(self, payload: CheckoutResult) -> bool: ...
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool: ...
    def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> PaymentEvent: ...

    def checkout_config(self, order: Order, *, prefill: dict | None = None) -> dict:
        """Everything the frontend needs to open checkout (public key, order id, amount...)."""
```

`Provider*` models are the adapter's normalized return types. They live in `core/models.py`, so no provider-specific types leak out.

### 7.2 Repository protocol (`core/repository.py`)
Async CRUD for orders, payments, refunds, plans, and subscriptions, plus:
- `get_order_by_idempotency_key(key)`
- `get_*_by_provider_id(provider_id)`
- `record_event_if_new(event_id) -> bool`: atomic insert into a `processed_events` table with a unique constraint. Returns `False` if the event was already seen.
- a transaction/unit-of-work context manager: `async with repo.transaction(): ...`

### 7.3 `PaymentService` (`core/service.py`): the only thing apps use

```python
service = PaymentService(provider=..., repository=..., events=EventBus())

order = await service.create_order(amount=Money(amount=49900, currency="INR"),
                                   customer_ref="user_42", receipt="inv_1001",
                                   metadata={"plan": "pro"}, idempotency_key="inv_1001")
cfg   = service.checkout_config(order, prefill={"email": "...", "contact": "..."})
pay   = await service.confirm_checkout(CheckoutResult(...))   # verify sig + fetch + persist
await service.handle_webhook(raw_body, headers)               # verify + dedupe + persist + dispatch
ref   = await service.refund(payment_id, amount=None, reason="requested_by_customer")
sub   = await service.create_subscription(plan_id, customer_ref="user_42", total_count=12)
await service.cancel_subscription(sub.id, at_cycle_end=True)

@service.events.on("payment.captured")
async def grant_access(event: PaymentEvent): ...
```

**`confirm_checkout` flow**
1. Verify the signature. If it's invalid, raise `SignatureVerificationError`.
2. `fetch_payment` from the provider. **Check that the amount and currency match the stored order**, and that `order_id` matches.
3. Upsert the payment and update the order through state rules.
4. Return the payment. Don't dispatch the "captured" event from here if the webhook already did. Dedupe by a per-payment `captured_event_dispatched` flag, or dispatch only from webhooks. **Pick one, document it, and test it.** Preferred: dispatch business events **only from webhooks or reconciliation**, and treat `confirm_checkout` as giving the UI a fast answer.

**`handle_webhook` flow**
1. Verify the signature against the **raw bytes**. Never re-serialize JSON before verifying.
2. Parse into a `PaymentEvent`.
3. `record_event_if_new(event.id)`. If it's a duplicate, return 200 and do nothing.
4. Apply state changes in a transaction.
5. After commit, dispatch to handlers. A handler exception must be logged and **must not** cause a non-2xx response. Otherwise Razorpay retries and we'd double-apply.

**`reconcile(since: datetime)`**: re-fetches non-final orders and payments from the provider and fixes their state. Useful as a cron job in case webhooks were missed.

### 7.4 Errors (`core/errors.py`)
`PayKitError` → `ConfigurationError`, `ProviderError` (fields: `code`, `description`, `http_status`, `provider_response`), `SignatureVerificationError`, `NotFoundError`, `InvalidStateError`, `AmountMismatchError`, `IdempotencyConflictError` (same key with a different amount).

---

## 8. Razorpay adapter

### 8.1 REST client (`providers/razorpay/client.py`)
- Base URL: `https://api.razorpay.com/v1`
- Auth: HTTP Basic with `key_id:key_secret`
- One shared `httpx.AsyncClient`, closed on shutdown (`aclose()`).
- Retries with exponential backoff on network errors, 429, and 5xx for **idempotent GETs only**. Don't auto-retry POSTs that create money-moving objects. Order creation is protected by our own idempotency key.
- Map Razorpay error bodies (`{"error": {"code", "description", "field", "source", "step", "reason"}}`) to `ProviderError`.

### 8.2 Endpoints used (verify each against the current docs)
| Purpose | Method + path | Key fields |
|---|---|---|
| Create order | `POST /orders` | `amount` (paise), `currency`, `receipt` (≤ 40 chars), `notes` (≤ 15 keys) |
| Fetch order | `GET /orders/{id}` | |
| Order's payments | `GET /orders/{id}/payments` | used in reconcile |
| Fetch payment | `GET /payments/{id}` | `status`: created/authorized/captured/refunded/failed |
| Capture | `POST /payments/{id}/capture` | `amount`, `currency`. Only needed if auto-capture is off in the dashboard |
| Refund | `POST /payments/{id}/refund` | `amount` optional (full if omitted), `notes`, `speed` |
| Create plan | `POST /plans` | `period`, `interval`, `item: {name, amount, currency}` |
| Create subscription | `POST /subscriptions` | `plan_id`, `total_count`, `customer_notify`, `notes`, optional `start_at` |
| Fetch / cancel subscription | `GET /subscriptions/{id}`, `POST /subscriptions/{id}/cancel` | `cancel_at_cycle_end` |

### 8.3 Signatures (`providers/razorpay/signatures.py`)
All are HMAC-SHA256 hex digests, compared with `hmac.compare_digest`:
- **Checkout (orders):** `HMAC(key_secret, f"{razorpay_order_id}|{razorpay_payment_id}") == razorpay_signature`
- **Checkout (subscriptions):** `HMAC(key_secret, f"{razorpay_payment_id}|{razorpay_subscription_id}") == razorpay_signature`
- **Webhook:** `HMAC(webhook_secret, raw_body_bytes) == headers["X-Razorpay-Signature"]`
- Use `headers["X-Razorpay-Event-Id"]` as the event ID for dedupe when present. Otherwise fall back to a hash of the body.

Write unit tests with test vectors computed in the test itself, plus a tampered-body case and a wrong-secret case.

### 8.4 Checkout config (for the frontend)
`checkout_config(order)` returns:
```json
{ "provider": "razorpay", "key": "<key_id>", "order_id": "order_...", "amount": 49900,
  "currency": "INR", "name": "<app name>", "description": "...", "prefill": {...}, "notes": {...} }
```
The frontend loads `https://checkout.razorpay.com/v1/checkout.js`, opens `new Razorpay({...config, handler})`, and posts `razorpay_payment_id`, `razorpay_order_id`, and `razorpay_signature` back to `/payments/confirm`. Include a working `examples/fastapi_embedded/checkout.html`.

### 8.5 Webhook event mapping (`providers/razorpay/webhooks.py`)
Normalize Razorpay events to PayKit event types:

| Razorpay event | PayKit event |
|---|---|
| `payment.authorized` | `payment.authorized` |
| `payment.captured` | `payment.captured` |
| `payment.failed` | `payment.failed` |
| `order.paid` | `order.paid` |
| `refund.created` / `refund.processed` / `refund.failed` | `refund.created` / `refund.processed` / `refund.failed` |
| `subscription.authenticated` / `.activated` / `.charged` / `.pending` / `.halted` / `.paused` / `.resumed` / `.cancelled` / `.completed` | `subscription.<same>` |

Unknown events are stored, acknowledged with 200, and dispatched as `provider.<original_name>` so apps can opt in. Put real-shaped sample payloads for each in `tests/fixtures/razorpay/`.

---

## 9. FastAPI integration (`integrations/fastapi`)

```python
from paykit import PaymentService, build_service_from_env
from paykit.integrations.fastapi import create_payments_router

service = build_service_from_env()          # reads PAYKIT_* env
app.include_router(create_payments_router(service, prefix="/payments",
                   auth_dependency=get_current_user))  # app's own auth
```

- `build_service_from_env()` wires provider, repository, and EventBus from settings.
- Lifespan helper: `paykit_lifespan(service)` opens and closes the HTTP client and DB engine.
- `auth_dependency` protects user-facing routes. The webhook route is **never** behind app auth. It's protected by signature verification only.

### Routes
| Route | Auth | Purpose |
|---|---|---|
| `POST /payments/orders` | app auth | create order → returns `{order, checkout}` |
| `POST /payments/confirm` | app auth | verify checkout result → returns payment status |
| `GET /payments/orders/{id}` | app auth | order + its payments |
| `POST /payments/{payment_id}/refunds` | app auth + admin check hook | refund |
| `POST /payments/subscriptions` | app auth | create subscription → returns `short_url` / checkout config |
| `POST /payments/subscriptions/{id}/cancel` | app auth | cancel |
| `POST /payments/webhooks/razorpay` | **signature only** | reads `await request.body()` raw, always returns 200 after a valid signature (even for duplicates), 400 on a bad signature |

Rules:
- **Amounts for orders come from the server, never trusted from the client.** Let the app pass an `amount_resolver(request_body, user) -> Money` callable to the router factory. The default resolver is disabled and raises `ConfigurationError` if the app hasn't provided one.
- `customer_ref` is taken from the authenticated user via a `customer_ref_getter(user)` callable.
- An ownership check makes sure users can only read their own orders and subscriptions.

---

## 10. Standalone service (`service_app`), for non-Python backends

- `create_app()` = FastAPI + the same router + API-key auth (`Authorization: Bearer <key>`, keys from `PAYKIT_SERVICE_API_KEYS`, compared in constant time).
- Here the calling backend passes `amount` and `customer_ref` explicitly, since it's the trusted server.
- **Outbound webhooks:** after each normalized event, POST it as JSON to `PAYKIT_OUTBOUND_WEBHOOK_URL` with these headers:
  - `X-PayKit-Event-Id`, `X-PayKit-Timestamp`
  - `X-PayKit-Signature: HMAC_SHA256(outbound_secret, f"{timestamp}.{raw_body}")`
  - Retry with backoff (e.g. 1m, 5m, 30m, 2h, 6h) using an `outbound_deliveries` table and a background worker loop. Start with a simple asyncio task and leave a note that it can be swapped for a real queue later.
  - Document how receivers verify the signature and reject timestamps older than 5 minutes.
- Ship `Dockerfile`, `docker-compose.yml` (service + Postgres), and an OpenAPI spec (FastAPI auto-generates it; make sure DTOs have good descriptions). Other languages can generate clients from it.
- `examples/node_client/`: a ~40-line example that creates an order and verifies an outbound webhook.

---

## 11. Persistence (SQLAlchemy)

Tables: `paykit_orders`, `paykit_payments`, `paykit_refunds`, `paykit_plans`, `paykit_subscriptions`, `paykit_processed_events`, `paykit_outbound_deliveries`.
- Unique constraints on every `provider_*_id`, on `orders.idempotency_key`, and on `processed_events.event_id`.
- Table prefix is configurable (default `paykit_`) so it can live in an existing app DB without clashes.
- Use Alembic migrations inside the package. Expose a `paykit db upgrade` CLI command (Typer or argparse).
- Row-level locking (`SELECT ... FOR UPDATE`) when applying webhook state changes on Postgres. SQLite falls back gracefully.

---

## 12. Build phases (do them in order)

1. **Skeleton:** pyproject with extras, config, errors, models, state machine, ruff/mypy/pytest setup. Tests for the state transitions.
2. **Razorpay signatures + client:** HMAC helpers with tests, async REST client, error mapping. Mock the HTTP with `respx`.
3. **Adapter:** `RazorpayProvider` implementing the protocol, plus mapping and webhook parsing with fixture payloads.
4. **Repository:** in-memory first, then SQLAlchemy + Alembic. Run the same test suite against both (parametrized fixture).
5. **`PaymentService`:** create_order (idempotency), confirm_checkout (amount check), handle_webhook (dedupe, transaction, dispatch after commit), refunds, subscriptions, reconcile.
6. **FastAPI router + example app + `checkout.html`.** Integration tests with the test client and signed fake webhooks.
7. **Standalone service:** API keys, outbound signed webhooks with retries, Dockerfile, compose, Node example.
8. **Docs:** README with a 5-minute quickstart, a "how to add a new provider" guide (use a stub `providers/dummy` adapter as the worked example), and a go-live checklist.

**Definition of done for each phase:** tests pass, `ruff check` is clean, and `mypy` is clean on `core/`, with no secrets in logs.

---

## 13. Testing requirements

- Unit: signatures (valid, tampered, wrong secret), state machine, mapping, idempotency conflicts, amount mismatch.
- Webhooks: duplicate delivery is processed once, out-of-order events (`captured` arriving before `authorized`), a handler exception still returns 200, and a bad signature returns 400.
- Integration: full flow of create order → simulated checkout signature → confirm → webhook `payment.captured` → handler called exactly once.
- A `DummyProvider` in tests proves the core has no Razorpay-specific assumptions.
- Optional `@pytest.mark.live` tests that hit the Razorpay **test mode** API when `PAYKIT_RAZORPAY_KEY_ID` starts with `rzp_test_`. Skipped by default.

---

## 14. Security checklist

- [ ] Webhook signature verified on **raw body bytes** using the **webhook secret**
- [ ] Constant-time comparisons everywhere (`hmac.compare_digest`)
- [ ] Order amount decided server-side; verified again against the fetched payment
- [ ] Event dedupe with a unique DB constraint (not just an in-memory set)
- [ ] Secrets as `SecretStr`, never logged; raw payloads logged only at DEBUG and redacted
- [ ] Webhook route excluded from CSRF/auth middleware but rate-limited
- [ ] HTTPS required for live mode webhook URLs
- [ ] Test and live keys never mixed (fail at startup if key prefix and `PAYKIT_ENV` disagree)

---

## 15. Go-live checklist (put this in the README)

1. Complete Razorpay KYC/activation and generate **live** keys.
2. Dashboard → Webhooks: add `https://<domain>/payments/webhooks/razorpay`, set a secret, and subscribe to the payment, order, refund, and subscription events listed in §8.5.
3. Decide on auto-capture (dashboard setting). If it's off, call `capture` within Razorpay's allowed window or authorized payments get auto-refunded.
4. Run `paykit db upgrade` on the production DB.
5. Schedule `reconcile` (e.g. every 15 min for the last 24h).
6. Do one small real payment and a refund end-to-end before announcing.

---

## 16. Future providers (design check, don't implement now)

Adding Dodo Payments / Paddle / Stripe should mean creating `providers/<name>/` implementing `PaymentProvider`, adding its webhook route via `create_payments_router(..., providers=[...])`, and adding a region-based `ProviderRouter` (e.g. `IN → razorpay`, everything else → an MoR). Keep the multi-provider hooks in the service constructor (`providers: dict[str, PaymentProvider]`, `select_provider(currency, country)`) even though v1 registers only Razorpay.
