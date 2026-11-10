"""Route-level regression tests for the hardening pass."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from paykit.core.errors import ProviderError
from paykit.core.models import Money
from paykit.core.service import PaymentService
from paykit.integrations.fastapi import create_payments_router
from paykit.providers.dummy import DummyProvider
from paykit.storage.memory import InMemoryRepository

INR_499 = Money(amount=49900, currency="INR")


def build(*, admin_dependency=None, max_webhook_body_bytes: int = 256 * 1024):
    service = PaymentService(provider=DummyProvider(), repository=InMemoryRepository())
    router = create_payments_router(
        service,
        auth_dependency=lambda: {"id": "user_1"},
        customer_ref_getter=lambda user, body: user["id"],
        amount_resolver=lambda body, user: INR_499,
        admin_dependency=admin_dependency,
        max_webhook_body_bytes=max_webhook_body_bytes,
    )
    app = FastAPI()
    app.include_router(router)
    return service, TestClient(app, raise_server_exceptions=False)


def test_refund_route_not_mounted_without_admin_dependency() -> None:
    """Any logged-in end user used to be able to refund any payment."""
    service, client = build()
    order = asyncio.run(service.create_order(INR_499, customer_ref="user_1"))
    provider: DummyProvider = service.provider  # type: ignore[assignment]
    pp = provider.simulate_payment(order.provider_order_id)
    payment = asyncio.run(
        service.confirm_checkout(
            provider.make_checkout_result(pp, provider_order_id=order.provider_order_id)
        )
    )

    response = client.post(f"/payments/{payment.id}/refunds", json={})

    assert response.status_code in (404, 405)
    assert asyncio.run(service.get_payment(payment.id)).amount_refunded == 0


def test_confirm_rejects_other_customers_order() -> None:
    service, client = build()
    provider: DummyProvider = service.provider  # type: ignore[assignment]
    victim_order = asyncio.run(service.create_order(INR_499, customer_ref="victim"))
    pp = provider.simulate_payment(victim_order.provider_order_id)
    result = provider.make_checkout_result(pp, provider_order_id=victim_order.provider_order_id)

    response = client.post("/payments/confirm", json=result.model_dump())

    assert response.status_code == 404


def test_idempotency_key_from_another_customer_is_conflict() -> None:
    service, client = build()
    asyncio.run(service.create_order(INR_499, customer_ref="victim", idempotency_key="k1"))

    response = client.post("/payments/orders", json={"idempotency_key": "k1"})

    assert response.status_code == 409


def test_oversized_webhook_body_rejected() -> None:
    _, client = build(max_webhook_body_bytes=1024)

    response = client.post("/payments/webhooks/dummy", content=b"x" * 4096)

    assert response.status_code == 413


def test_oversized_metadata_rejected() -> None:
    _, client = build()

    too_many = {f"k{i}": "v" for i in range(16)}
    assert client.post("/payments/orders", json={"metadata": too_many}).status_code == 422
    too_long = {"k": "v" * 257}
    assert client.post("/payments/orders", json={"metadata": too_long}).status_code == 422
    assert client.post("/payments/orders", json={"receipt": "r" * 41}).status_code == 422
    assert (
        client.post("/payments/orders", json={"idempotency_key": "has spaces"}).status_code
        == 422
    )


def test_oversized_checkout_result_rejected() -> None:
    _, client = build()

    response = client.post(
        "/payments/confirm",
        json={"provider_order_id": "o", "provider_payment_id": "p", "signature": "s" * 10_000},
    )

    assert response.status_code == 422


def test_provider_error_is_502_without_leaking_details() -> None:
    service, client = build()

    async def failing_create_order(*args, **kwargs):
        raise ProviderError("internal gateway detail: req_id=abc123", code="SERVER_ERROR")

    service.provider.create_order = failing_create_order  # type: ignore[method-assign]

    response = client.post("/payments/orders", json={})

    assert response.status_code == 502
    assert "abc123" not in response.text
