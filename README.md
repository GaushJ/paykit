# PayKit

A provider-agnostic payments SDK for Python. v1 ships a **Razorpay** adapter (India: UPI, cards,
netbanking, wallets; one-time payments, refunds, subscriptions). App code talks only to
`PaymentService` — never to Razorpay directly — so a second provider (Dodo/Paddle/Stripe) is a
new adapter, not a rewrite.

Two ways to use it:
1. **Embedded library** — `pip install paykit[fastapi]`, mount a router in your own FastAPI app.
2. **Standalone service** — a small deployable FastAPI app exposing REST + outbound webhooks, so
   backends in any language (Node, Go, ...) can use it over HTTP.

Core design rules, in short: money is always an `int` in minor units + an ISO-4217 currency,
never a float. The **webhook is the source of truth** — a client-side "payment succeeded" callback
is only a hint; state changes are driven by verified webhooks (or reconciliation). Everything is
idempotent: order creation takes an idempotency key, webhooks are deduplicated by event ID, and
state transitions are monotonic so a late/out-of-order webhook can't move a payment backwards.

## Install

```bash
pip install paykit[fastapi]   # embedded library, mounted in your own app
pip install paykit[sql]       # + SQLAlchemy/Alembic persistence (Postgres/SQLite)
pip install paykit[service]   # fastapi + sql + uvicorn, for the standalone service
pip install paykit[dev]       # + pytest/ruff/mypy, for working on paykit itself
```

## 5-minute quickstart (no Razorpay account needed)

`DummyProvider` is a real, fully-working `PaymentProvider` implementation backed by an in-process
dict — no network calls, no API keys. It's how the test suite proves core/service.py has no
Razorpay-specific assumptions, and it's the fastest way to see the whole flow:

```python
import asyncio
from paykit import Money, PaymentService
from paykit.providers.dummy import DummyProvider
from paykit.storage.memory import InMemoryRepository

async def main():
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())

    @service.events.on("payment.captured")
    async def grant_access(event):
        print(f"grant access for order {event.payment.order_id}")

    order = await service.create_order(
        Money(amount=49900, currency="INR"), customer_ref="user_42", idempotency_key="inv_1001"
    )
    print(service.checkout_config(order))  # -> hand this to the frontend

    # --- simulate what a real checkout + webhook would do ---
    provider = service._provider
    payment = provider.simulate_payment(order.provider_order_id)
    result = provider.make_checkout_result(payment, provider_order_id=order.provider_order_id)
    await service.confirm_checkout(result)  # fast UI answer; does NOT fire "payment.captured"

    from paykit.providers.dummy import placeholder_payment
    raw_body, headers = provider.make_webhook(
        event_id="evt_1", event_type="payment.captured", payment=placeholder_payment(payment)
    )
    await service.handle_webhook(raw_body, headers)  # <- this is what fires the handler above

asyncio.run(main())
```

Swap `DummyProvider()` / `InMemoryRepository()` for the real Razorpay adapter and a persistent
repository (below) when you're ready — nothing else about your app code changes.

## Using Razorpay for real

```bash
export PAYKIT_RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
export PAYKIT_RAZORPAY_KEY_SECRET=...
export PAYKIT_RAZORPAY_WEBHOOK_SECRET=...       # set when you add the webhook URL in the dashboard
export PAYKIT_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/mydb   # omit for in-memory
```

```python
from paykit import build_service_from_env
from paykit.integrations.fastapi import create_payments_router, paykit_lifespan
from fastapi import FastAPI

service = build_service_from_env()  # wires RazorpayProvider + SQLAlchemyRepository from env

app = FastAPI(lifespan=paykit_lifespan(service))
app.include_router(create_payments_router(
    service,
    auth_dependency=get_current_user,               # your app's own auth
    customer_ref_getter=lambda user, body: user.id,  # how to tag orders with this user
    amount_resolver=lambda body, user: price_the_cart(body, user),  # NEVER trust a client amount
))
```

This mounts `POST /payments/orders`, `POST /payments/confirm`, `GET /payments/orders/{id}`,
`POST /payments/{id}/refunds`, `POST /payments/subscriptions(+/{id}/cancel)`, and
`POST /payments/webhooks/razorpay`. See `examples/fastapi_embedded/` for a complete runnable app
+ `checkout.html` that opens Razorpay Checkout and posts the result back — run it with
`pip install paykit[fastapi] uvicorn` (an ASGI server isn't part of the `fastapi` extra, since a
real app usually already has one) then
`uvicorn examples.fastapi_embedded.main:app --reload`.

Before going live: run `paykit db upgrade` (Alembic; needs `PAYKIT_DATABASE_URL`), and add the
webhook URL + secret in the Razorpay dashboard — see **Go-live checklist** below.

## Standalone service

For backends that aren't Python. `examples/node_client/` has a ~40-line example of both sides
(creating an order, verifying an outbound webhook signature).

```bash
docker compose up   # Postgres + `paykit db upgrade` + the service, from this repo's Dockerfile
```

or directly:

```bash
export PAYKIT_RAZORPAY_KEY_ID=... PAYKIT_RAZORPAY_KEY_SECRET=... PAYKIT_RAZORPAY_WEBHOOK_SECRET=...
export PAYKIT_SERVICE_API_KEYS=key1,key2                     # your backends' API keys
export PAYKIT_OUTBOUND_WEBHOOK_URL=https://your-backend.example.com/paykit/webhook
export PAYKIT_OUTBOUND_WEBHOOK_SECRET=...
uvicorn paykit.service_app.main:app
```

```bash
curl -X POST http://localhost:8000/payments/orders \
  -H "Authorization: Bearer key1" -H "Content-Type: application/json" \
  -d '{"amount": 49900, "currency": "INR", "customer_ref": "user_42"}'
```

Unlike the embedded router, the standalone service's caller is a trusted backend: it passes
`amount` and `customer_ref` explicitly in the request body (there's no logged-in browser user to
derive them from), and read/cancel routes skip the ownership check accordingly. Every normalized
event also gets POSTed to `PAYKIT_OUTBOUND_WEBHOOK_URL`, signed the same way Razorpay signs
webhooks to you: `HMAC_SHA256(secret, f"{timestamp}.{raw_body}")` in `X-PayKit-Signature`, with
`X-PayKit-Event-Id` and `X-PayKit-Timestamp` alongside it — reject anything older than 5 minutes.
Delivery retries with backoff (1m, 5m, 30m, 2h, 6h) and survives a restart (persisted in
`paykit_outbound_deliveries`). See `paykit/service_app/outbound.py`'s docstring for the exact
receiver-side verification code, and `examples/node_client/verify_webhook.js` for a working one.

## How to add a new provider

`paykit/providers/dummy/adapter.py` is the worked example — a complete, ~250-line
`PaymentProvider` implementation with no real gateway behind it. To add a real one (Dodo Payments,
Paddle, Stripe, ...):

1. Create `paykit/providers/<name>/` and implement every method of `PaymentProvider`
   (`paykit/core/provider.py`) — order/payment/refund/plan/subscription CRUD against the gateway's
   REST API, `verify_checkout_signature`/`verify_webhook` using `hmac.compare_digest`,
   `parse_webhook` returning a normalized `PaymentEvent`, and `checkout_config`.
2. Write `mapping.py` translating the gateway's JSON <-> paykit's `Provider*` models
   (`core/models.py`) — check field names against the gateway's *current* docs, don't guess, and
   leave a `# NOTE:` where you had to make a judgment call (see `razorpay/mapping.py` for
   examples, e.g. how it infers `PARTIALLY_REFUNDED` since Razorpay's payment `status` field
   doesn't have that value).
3. Register it in `paykit/providers/__init__.py`'s `get_provider()`.
4. For multi-provider apps: add a `select_provider(currency, country)` policy in front of
   `get_provider` (e.g. `IN -> razorpay`, everything else -> the new one) and pass
   `providers={"razorpay": ..., "<name>": ...}` where you construct `PaymentService` — the core
   was built with this in mind (`core/service.py` never imports a concrete provider), it's just
   not wired up automatically since v1 only registers Razorpay.
5. Add webhook fixture payloads under `tests/fixtures/<name>/` and run the same
   `tests/unit/test_service.py` / `tests/integration/test_full_payment_flow.py`-style flows against
   your adapter — swapping `DummyProvider()` for yours is the whole point of the provider
   abstraction.

## Testing

```bash
pip install -e ".[dev,fastapi,sql]"
pytest                       # 226+ tests: unit (signatures/mapping/state/service) + integration
                              #  (repository contract run against both in-memory & SQLAlchemy,
                              #  FastAPI router, standalone service, full payment flow)
ruff check .
mypy paykit/core             # --strict; the rest of the package is checked non-strict
```

`PAYKIT_TEST_DATABASE_URL` (a `postgresql+asyncpg://...` URL) runs the repository contract suite
against real Postgres instead of in-memory SQLite, if set.

## Security checklist

- [x] Webhook signature verified on **raw body bytes** using the **webhook secret**, never a
      re-serialized copy
- [x] Constant-time comparisons everywhere (`hmac.compare_digest`)
- [x] Order amount decided server-side (`amount_resolver`); verified again against the fetched
      payment in `confirm_checkout`
- [x] Event dedupe via a unique DB constraint (`paykit_processed_events`), not an in-memory set
- [x] Secrets are `SecretStr`, never logged
- [ ] HTTPS for your live-mode webhook URL — `PayKitSettings` warns at startup if a live key is
      paired with a non-HTTPS `PAYKIT_OUTBOUND_WEBHOOK_URL`, but can't enforce it for the inbound
      Razorpay webhook URL (that's configured in Razorpay's dashboard, outside paykit)
- [ ] Rate-limit the webhook route at your reverse proxy/gateway (it's intentionally excluded from
      app auth — signature verification is its only gate)

## Go-live checklist

1. Complete Razorpay KYC/activation and generate **live** keys.
2. Razorpay dashboard -> Webhooks: add `https://<domain>/payments/webhooks/razorpay`, set a
   secret (`PAYKIT_RAZORPAY_WEBHOOK_SECRET`), and subscribe to the payment/order/refund/
   subscription events in `providers/razorpay/webhooks.py`'s `KNOWN_EVENTS`.
3. Decide on auto-capture (Razorpay dashboard setting). If it's off, call
   `service.capture_payment(...)` within Razorpay's allowed capture window, or authorized
   payments get auto-refunded.
4. Run `paykit db upgrade` against the production DB.
5. Schedule `service.reconcile(since=...)` (e.g. every 15 min, looking back 24h) as a cron job —
   it only re-dispatches events for objects whose status actually changed, so it's safe to run
   this often.
6. Do one small real payment and a refund end-to-end before announcing.
