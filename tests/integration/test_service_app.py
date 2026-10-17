"""Full standalone-service wiring: API-key auth, body-driven amount/customer_ref,
no ownership gate. Razorpay's REST API is mocked with respx since
build_service_from_env() always wires a real RazorpayProvider."""

from __future__ import annotations

import asyncio
import os

# `paykit.service_app.main` builds a module-level `app = create_app()` (so
# `uvicorn paykit.service_app.main:app` works directly), which needs these
# env vars to exist at import time - set fakes before importing it.
os.environ.setdefault("PAYKIT_RAZORPAY_KEY_ID", "rzp_test_module_import")
os.environ.setdefault("PAYKIT_RAZORPAY_KEY_SECRET", "secret")
os.environ.setdefault("PAYKIT_RAZORPAY_WEBHOOK_SECRET", "whsecret")
os.environ.setdefault("PAYKIT_SERVICE_API_KEYS", "module_import_key")

import httpx
import respx
from fastapi.testclient import TestClient

from paykit.config import PayKitSettings
from paykit.service_app.main import create_app

RAZORPAY_BASE = "https://api.razorpay.com/v1"


def make_settings(**overrides: object) -> PayKitSettings:
    defaults: dict[str, object] = dict(
        razorpay_key_id="rzp_test_x",
        razorpay_key_secret="secret",
        razorpay_webhook_secret="whsecret",
        service_api_keys="devkey1,devkey2",
    )
    defaults.update(overrides)
    return PayKitSettings(_env_file=None, **defaults)  # type: ignore[arg-type,call-arg]


def test_healthz() -> None:
    app = create_app(settings=make_settings())
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_orders_route_requires_api_key() -> None:
    app = create_app(settings=make_settings())
    client = TestClient(app)

    response = client.post("/payments/orders", json={"amount": 49900, "customer_ref": "user_1"})

    assert response.status_code == 401


def test_orders_route_rejects_unknown_key() -> None:
    app = create_app(settings=make_settings())
    client = TestClient(app)

    response = client.post(
        "/payments/orders",
        json={"amount": 49900, "customer_ref": "user_1"},
        headers={"Authorization": "Bearer not-a-real-key"},
    )

    assert response.status_code == 401


@respx.mock
def test_create_order_uses_body_amount_and_customer_ref() -> None:
    respx.post(f"{RAZORPAY_BASE}/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "order_std_1",
                "amount": 49900,
                "currency": "INR",
                "receipt": None,
                "status": "created",
                "notes": {},
                "created_at": 1700000000,
            },
        )
    )
    app = create_app(settings=make_settings())
    client = TestClient(app)

    response = client.post(
        "/payments/orders",
        json={"amount": 49900, "currency": "INR", "customer_ref": "backend_user_42"},
        headers={"Authorization": "Bearer devkey1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["order"]["amount"] == {"amount": 49900, "currency": "INR"}

    service = app.state.paykit_service
    stored_order = asyncio.run(service.get_order(body["order"]["id"]))
    assert stored_order is not None
    assert stored_order.customer_ref == "backend_user_42"


def test_create_order_missing_amount_returns_error() -> None:
    app = create_app(settings=make_settings())
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/payments/orders",
        json={"customer_ref": "user_1"},
        headers={"Authorization": "Bearer devkey1"},
    )

    assert response.status_code == 500


@respx.mock
def test_get_order_has_no_ownership_gate() -> None:
    """§10: the caller is a trusted backend, not a single end user - any
    valid API key can read any order the backend created."""
    respx.post(f"{RAZORPAY_BASE}/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "order_std_2",
                "amount": 10000,
                "currency": "INR",
                "receipt": None,
                "status": "created",
                "notes": {},
                "created_at": 1700000000,
            },
        )
    )
    app = create_app(settings=make_settings())
    client = TestClient(app)

    create_response = client.post(
        "/payments/orders",
        json={"amount": 10000, "customer_ref": "some_end_user"},
        headers={"Authorization": "Bearer devkey1"},
    )
    order_id = create_response.json()["order"]["id"]

    # A *different* API key (still a valid one for this backend) can still read it.
    get_response = client.get(
        f"/payments/orders/{order_id}", headers={"Authorization": "Bearer devkey2"}
    )

    assert get_response.status_code == 200
    assert get_response.json()["order"]["id"] == order_id


def test_webhook_route_bad_signature_returns_400() -> None:
    app = create_app(settings=make_settings())
    client = TestClient(app)

    response = client.post(
        "/payments/webhooks/razorpay",
        content=b'{"event":"payment.captured"}',
        headers={"X-Razorpay-Signature": "not-a-real-signature"},
    )

    assert response.status_code == 400
