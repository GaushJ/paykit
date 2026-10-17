import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from paykit.core.models import CheckoutResult, Money, Order, OrderStatus
from paykit.providers.razorpay.adapter import RazorpayProvider

BASE_URL = "https://api.razorpay.com/v1"
KEY_ID = "rzp_test_abc123"
KEY_SECRET = "key_secret"
WEBHOOK_SECRET = "webhook_secret"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "razorpay"


def make_provider() -> RazorpayProvider:
    return RazorpayProvider(KEY_ID, KEY_SECRET, WEBHOOK_SECRET, display_name="Acme")


@respx.mock
async def test_create_order() -> None:
    respx.post(f"{BASE_URL}/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "order_1",
                "amount": 49900,
                "currency": "INR",
                "receipt": "inv_1001",
                "status": "created",
                "notes": {"plan": "pro"},
                "created_at": 1567674000,
            },
        )
    )
    provider = make_provider()
    try:
        order = await provider.create_order(
            Money(amount=49900, currency="INR"), receipt="inv_1001", metadata={"plan": "pro"}
        )
    finally:
        await provider.aclose()

    assert order.provider_order_id == "order_1"
    assert order.status == OrderStatus.CREATED


@respx.mock
async def test_fetch_order() -> None:
    respx.get(f"{BASE_URL}/orders/order_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "order_1",
                "amount": 49900,
                "currency": "INR",
                "receipt": None,
                "status": "paid",
                "notes": {},
                "created_at": 1567674000,
            },
        )
    )
    provider = make_provider()
    try:
        order = await provider.fetch_order("order_1")
    finally:
        await provider.aclose()

    assert order.status == OrderStatus.PAID


@respx.mock
async def test_fetch_payment() -> None:
    respx.get(f"{BASE_URL}/payments/pay_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "pay_1",
                "amount": 49900,
                "currency": "INR",
                "status": "captured",
                "order_id": "order_1",
                "amount_refunded": 0,
                "method": "upi",
                "created_at": 1567674599,
            },
        )
    )
    provider = make_provider()
    try:
        payment = await provider.fetch_payment("pay_1")
    finally:
        await provider.aclose()

    assert payment.provider_payment_id == "pay_1"


@respx.mock
async def test_capture_payment() -> None:
    respx.post(f"{BASE_URL}/payments/pay_1/capture").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "pay_1",
                "amount": 49900,
                "currency": "INR",
                "status": "captured",
                "order_id": "order_1",
                "amount_refunded": 0,
                "method": "upi",
                "created_at": 1567674599,
            },
        )
    )
    provider = make_provider()
    try:
        payment = await provider.capture_payment("pay_1", Money(amount=49900, currency="INR"))
    finally:
        await provider.aclose()

    assert payment.status.value == "captured"


@respx.mock
async def test_refund_payment_sends_reason_in_notes() -> None:
    route = respx.post(f"{BASE_URL}/payments/pay_1/refund").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "rfnd_1",
                "payment_id": "pay_1",
                "amount": 49900,
                "currency": "INR",
                "status": "processed",
                "notes": {},
                "created_at": 1567675000,
            },
        )
    )
    provider = make_provider()
    try:
        refund = await provider.refund_payment(
            "pay_1", None, reason="requested_by_customer", metadata={}
        )
    finally:
        await provider.aclose()

    assert refund.provider_refund_id == "rfnd_1"
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["notes"]["reason"] == "requested_by_customer"
    assert "amount" not in sent_body


@respx.mock
async def test_create_plan() -> None:
    respx.post(f"{BASE_URL}/plans").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "plan_1",
                "period": "monthly",
                "interval": 1,
                "item": {"name": "Pro", "amount": 49900, "currency": "INR"},
            },
        )
    )
    provider = make_provider()
    try:
        plan = await provider.create_plan(
            "Pro", Money(amount=49900, currency="INR"), "monthly", 1
        )
    finally:
        await provider.aclose()

    assert plan.provider_plan_id == "plan_1"


@respx.mock
async def test_create_subscription() -> None:
    respx.post(f"{BASE_URL}/subscriptions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "sub_1",
                "plan_id": "plan_1",
                "status": "created",
                "total_count": 12,
                "paid_count": 0,
                "short_url": "https://rzp.io/i/xyz",
                "notes": {"user_id": "user_42"},
            },
        )
    )
    provider = make_provider()
    try:
        sub = await provider.create_subscription(
            "plan_1", total_count=12, customer_notify=True, metadata={"user_id": "user_42"}
        )
    finally:
        await provider.aclose()

    assert sub.provider_subscription_id == "sub_1"
    assert sub.total_count == 12


@respx.mock
async def test_cancel_subscription() -> None:
    respx.post(f"{BASE_URL}/subscriptions/sub_1/cancel").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "sub_1",
                "plan_id": "plan_1",
                "status": "cancelled",
                "total_count": 12,
                "paid_count": 1,
                "notes": {},
            },
        )
    )
    provider = make_provider()
    try:
        sub = await provider.cancel_subscription("sub_1", at_cycle_end=True)
    finally:
        await provider.aclose()

    assert sub.status.value == "cancelled"


@respx.mock
async def test_fetch_subscription() -> None:
    respx.get(f"{BASE_URL}/subscriptions/sub_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "sub_1",
                "plan_id": "plan_1",
                "status": "active",
                "total_count": 12,
                "paid_count": 3,
                "notes": {},
            },
        )
    )
    provider = make_provider()
    try:
        sub = await provider.fetch_subscription("sub_1")
    finally:
        await provider.aclose()

    assert sub.paid_count == 3


class TestVerifyCheckoutSignature:
    def test_order_checkout(self) -> None:
        provider = make_provider()
        order_id, payment_id = "order_1", "pay_1"
        signature = hmac.new(
            KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()
        result = CheckoutResult(
            provider_order_id=order_id, provider_payment_id=payment_id, signature=signature
        )
        assert provider.verify_checkout_signature(result) is True

    def test_subscription_checkout(self) -> None:
        provider = make_provider()
        payment_id, subscription_id = "pay_1", "sub_1"
        signature = hmac.new(
            KEY_SECRET.encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256
        ).hexdigest()
        result = CheckoutResult(
            provider_subscription_id=subscription_id,
            provider_payment_id=payment_id,
            signature=signature,
        )
        assert provider.verify_checkout_signature(result) is True

    def test_neither_id_raises(self) -> None:
        provider = make_provider()
        result = CheckoutResult(provider_payment_id="pay_1", signature="whatever")
        with pytest.raises(ValueError):
            provider.verify_checkout_signature(result)


class TestVerifyAndParseWebhook:
    def test_verify_webhook_valid_signature(self) -> None:
        provider = make_provider()
        raw_body = (FIXTURES / "payment_captured.json").read_bytes()
        signature = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()

        assert provider.verify_webhook(raw_body, {"X-Razorpay-Signature": signature}) is True

    def test_verify_webhook_missing_header(self) -> None:
        provider = make_provider()
        raw_body = (FIXTURES / "payment_captured.json").read_bytes()

        assert provider.verify_webhook(raw_body, {}) is False

    def test_verify_webhook_bad_signature(self) -> None:
        provider = make_provider()
        raw_body = (FIXTURES / "payment_captured.json").read_bytes()

        assert provider.verify_webhook(raw_body, {"X-Razorpay-Signature": "deadbeef"}) is False

    def test_parse_webhook_uses_event_id_header(self) -> None:
        provider = make_provider()
        raw_body = (FIXTURES / "payment_captured.json").read_bytes()

        event = provider.parse_webhook(
            raw_body, {"X-Razorpay-Event-Id": "evt_from_header"}
        )

        assert event.id == "evt_from_header"
        assert event.type == "payment.captured"

    def test_parse_webhook_falls_back_to_body_hash(self) -> None:
        provider = make_provider()
        raw_body = (FIXTURES / "payment_captured.json").read_bytes()

        event = provider.parse_webhook(raw_body, {})

        assert event.id == hashlib.sha256(raw_body).hexdigest()


class TestCheckoutConfig:
    def test_shape(self) -> None:
        provider = make_provider()
        order = Order(
            id="internal_1",
            provider="razorpay",
            provider_order_id="order_1",
            amount=Money(amount=49900, currency="INR"),
            status=OrderStatus.CREATED,
            receipt="inv_1001",
            customer_ref="user_42",
            metadata={"plan": "pro"},
            idempotency_key="inv_1001",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        config = provider.checkout_config(order, prefill={"email": "a@b.com"})

        assert config == {
            "provider": "razorpay",
            "key": KEY_ID,
            "order_id": "order_1",
            "amount": 49900,
            "currency": "INR",
            "name": "Acme",
            "description": "inv_1001",
            "prefill": {"email": "a@b.com"},
            "notes": {"plan": "pro"},
        }
