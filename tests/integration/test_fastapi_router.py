"""Router tests use plain (sync) test functions and `asyncio.run()` for any
direct async setup - mixing pytest-asyncio's event loop with starlette's
TestClient (which runs its own) inside one `async def` test is unreliable.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from paykit.core.models import Money
from paykit.core.service import PaymentService
from paykit.integrations.fastapi import create_payments_router
from paykit.providers.dummy import DummyProvider
from paykit.storage.memory import InMemoryRepository

INR_499 = Money(amount=49900, currency="INR")


def make_user(user_id: str) -> dict:
    return {"id": user_id}


def build_app(
    service: PaymentService, *, admin_dependency=None
) -> tuple[FastAPI, dict]:
    current_user = make_user("user_1")

    def get_current_user() -> dict:
        return current_user

    router = create_payments_router(
        service,
        auth_dependency=get_current_user,
        customer_ref_getter=lambda user, body: user["id"],
        amount_resolver=lambda body, user: INR_499,
        admin_dependency=admin_dependency,
    )
    app = FastAPI()
    app.include_router(router)
    return app, current_user


def test_create_order_route() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    app, _ = build_app(service)
    client = TestClient(app)

    response = client.post("/payments/orders", json={"receipt": "inv_1"})

    assert response.status_code == 200
    body = response.json()
    assert body["order"]["status"] == "created"
    assert body["order"]["amount"] == {"amount": 49900, "currency": "INR"}
    assert body["checkout"]["provider"] == "dummy"


def test_create_order_without_amount_resolver_returns_500() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    router = create_payments_router(
        service,
        auth_dependency=lambda: make_user("user_1"),
        customer_ref_getter=lambda user, body: user["id"],
        # amount_resolver intentionally omitted -> disabled_amount_resolver
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/payments/orders", json={})

    assert response.status_code == 500


def test_confirm_checkout_route_happy_path() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    provider: DummyProvider = service._provider  # type: ignore[assignment]
    app, _ = build_app(service)
    client = TestClient(app)

    order_response = client.post("/payments/orders", json={"receipt": "inv_1"})
    provider_order_id = order_response.json()["order"]["id"]
    # `order.id` == our internal id, not the provider id - fetch it back to sign correctly.
    stored_order = asyncio.run(service.get_order(provider_order_id))
    assert stored_order is not None

    pp = provider.simulate_payment(stored_order.provider_order_id)
    checkout_result = provider.make_checkout_result(
        pp, provider_order_id=stored_order.provider_order_id
    )

    response = client.post(
        "/payments/confirm",
        json={
            "provider_order_id": checkout_result.provider_order_id,
            "provider_subscription_id": checkout_result.provider_subscription_id,
            "provider_payment_id": checkout_result.provider_payment_id,
            "signature": checkout_result.signature,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "captured"


def test_confirm_checkout_bad_signature_returns_400() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    provider: DummyProvider = service._provider  # type: ignore[assignment]
    app, _ = build_app(service)
    client = TestClient(app)

    order = asyncio.run(service.create_order(INR_499))
    pp = provider.simulate_payment(order.provider_order_id)

    response = client.post(
        "/payments/confirm",
        json={
            "provider_order_id": order.provider_order_id,
            "provider_payment_id": pp.provider_payment_id,
            "signature": "not-the-real-signature",
        },
    )

    assert response.status_code == 400


def test_get_order_route_ownership_check() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    app, current_user = build_app(service)
    client = TestClient(app)

    own_order = asyncio.run(service.create_order(INR_499, customer_ref=current_user["id"]))
    other_order = asyncio.run(service.create_order(INR_499, customer_ref="someone_else"))

    own_response = client.get(f"/payments/orders/{own_order.id}")
    assert own_response.status_code == 200
    assert own_response.json()["order"]["id"] == own_order.id

    other_response = client.get(f"/payments/orders/{other_order.id}")
    assert other_response.status_code == 404

    missing_response = client.get("/payments/orders/does-not-exist")
    assert missing_response.status_code == 404


def test_enforce_ownership_false_allows_reading_any_order() -> None:
    """§10: the standalone service's caller is a trusted backend, not a
    single logged-in end user, so ownership checks are skipped."""
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    router = create_payments_router(
        service,
        auth_dependency=lambda: make_user("service_account"),
        customer_ref_getter=lambda user, body: body.get("customer_ref", ""),
        amount_resolver=lambda body, user: INR_499,
        enforce_ownership=False,
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    someone_elses_order = asyncio.run(
        service.create_order(INR_499, customer_ref="end_user_42")
    )

    response = client.get(f"/payments/orders/{someone_elses_order.id}")

    assert response.status_code == 200
    assert response.json()["order"]["id"] == someone_elses_order.id


def test_refund_route_happy_path() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    provider: DummyProvider = service._provider  # type: ignore[assignment]
    app, _ = build_app(service)
    client = TestClient(app)

    order = asyncio.run(service.create_order(INR_499))
    pp = provider.simulate_payment(order.provider_order_id)
    checkout_result = provider.make_checkout_result(pp, provider_order_id=order.provider_order_id)
    payment = asyncio.run(service.confirm_checkout(checkout_result))

    response = client.post(
        f"/payments/{payment.id}/refunds", json={"reason": "requested_by_customer"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processed"


def test_refund_route_requires_admin_when_configured() -> None:
    def deny_admin() -> None:
        raise HTTPException(status_code=403, detail="admin only")

    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    provider: DummyProvider = service._provider  # type: ignore[assignment]
    app, _ = build_app(service, admin_dependency=deny_admin)
    client = TestClient(app)

    order = asyncio.run(service.create_order(INR_499))
    pp = provider.simulate_payment(order.provider_order_id)
    checkout_result = provider.make_checkout_result(pp, provider_order_id=order.provider_order_id)
    payment = asyncio.run(service.confirm_checkout(checkout_result))

    response = client.post(f"/payments/{payment.id}/refunds", json={})

    assert response.status_code == 403


def test_refund_route_not_found() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    app, _ = build_app(service)
    client = TestClient(app)

    response = client.post("/payments/does-not-exist/refunds", json={})

    assert response.status_code == 404


def test_subscription_routes() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    app, current_user = build_app(service)
    client = TestClient(app)

    plan = asyncio.run(service.create_plan("Pro", INR_499, "monthly", 1))

    create_response = client.post(
        "/payments/subscriptions", json={"plan_id": plan.id, "total_count": 12}
    )
    assert create_response.status_code == 200
    body = create_response.json()
    assert body["subscription"]["status"] == "created"
    subscription_id = body["subscription"]["id"]

    cancel_response = client.post(
        f"/payments/subscriptions/{subscription_id}/cancel", json={"at_cycle_end": False}
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"


def test_cancel_subscription_ownership_check() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    app, _ = build_app(service)
    client = TestClient(app)

    plan = asyncio.run(service.create_plan("Pro", INR_499, "monthly", 1))
    other_sub = asyncio.run(
        service.create_subscription(plan.id, customer_ref="someone_else", total_count=12)
    )

    response = client.post(
        f"/payments/subscriptions/{other_sub.id}/cancel", json={"at_cycle_end": False}
    )
    assert response.status_code == 404


def test_webhook_route_valid_signature_returns_200_and_bad_signature_returns_400() -> None:
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    provider: DummyProvider = service._provider  # type: ignore[assignment]
    app, _ = build_app(service)
    client = TestClient(app)

    order = asyncio.run(service.create_order(INR_499))
    pp = provider.simulate_payment(order.provider_order_id)

    from paykit.providers.dummy import placeholder_payment

    raw_body, headers = provider.make_webhook(
        event_id="evt_route_1",
        event_type="payment.captured",
        payment=placeholder_payment(pp),
    )
    ok_response = client.post(
        "/payments/webhooks/dummy", content=raw_body, headers=headers
    )
    assert ok_response.status_code == 200

    bad_response = client.post(
        "/payments/webhooks/dummy",
        content=raw_body,
        headers={**headers, "X-Dummy-Signature": "bad"},
    )
    assert bad_response.status_code == 400
