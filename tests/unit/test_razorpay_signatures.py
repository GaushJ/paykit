import hashlib
import hmac

from paykit.providers.razorpay.signatures import (
    extract_event_id,
    verify_order_checkout_signature,
    verify_subscription_checkout_signature,
    verify_webhook_signature,
)

KEY_SECRET = "test_key_secret"
WEBHOOK_SECRET = "test_webhook_secret"


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class TestOrderCheckoutSignature:
    def test_valid_signature(self) -> None:
        order_id, payment_id = "order_ABC123", "pay_XYZ789"
        signature = _sign(KEY_SECRET, f"{order_id}|{payment_id}")

        assert (
            verify_order_checkout_signature(
                KEY_SECRET,
                razorpay_order_id=order_id,
                razorpay_payment_id=payment_id,
                razorpay_signature=signature,
            )
            is True
        )

    def test_tampered_payment_id_fails(self) -> None:
        order_id = "order_ABC123"
        signature = _sign(KEY_SECRET, f"{order_id}|pay_XYZ789")

        assert (
            verify_order_checkout_signature(
                KEY_SECRET,
                razorpay_order_id=order_id,
                razorpay_payment_id="pay_ATTACKER",
                razorpay_signature=signature,
            )
            is False
        )

    def test_wrong_secret_fails(self) -> None:
        order_id, payment_id = "order_ABC123", "pay_XYZ789"
        signature = _sign("wrong_secret", f"{order_id}|{payment_id}")

        assert (
            verify_order_checkout_signature(
                KEY_SECRET,
                razorpay_order_id=order_id,
                razorpay_payment_id=payment_id,
                razorpay_signature=signature,
            )
            is False
        )


class TestSubscriptionCheckoutSignature:
    def test_valid_signature(self) -> None:
        payment_id, subscription_id = "pay_XYZ789", "sub_ABC123"
        signature = _sign(KEY_SECRET, f"{payment_id}|{subscription_id}")

        assert (
            verify_subscription_checkout_signature(
                KEY_SECRET,
                razorpay_payment_id=payment_id,
                razorpay_subscription_id=subscription_id,
                razorpay_signature=signature,
            )
            is True
        )

    def test_tampered_subscription_id_fails(self) -> None:
        payment_id = "pay_XYZ789"
        signature = _sign(KEY_SECRET, f"{payment_id}|sub_ABC123")

        assert (
            verify_subscription_checkout_signature(
                KEY_SECRET,
                razorpay_payment_id=payment_id,
                razorpay_subscription_id="sub_ATTACKER",
                razorpay_signature=signature,
            )
            is False
        )


class TestWebhookSignature:
    def test_valid_signature(self) -> None:
        body = b'{"event":"payment.captured","payload":{}}'
        signature = _sign(WEBHOOK_SECRET, body.decode())

        assert verify_webhook_signature(WEBHOOK_SECRET, body, signature) is True

    def test_tampered_body_fails(self) -> None:
        original = b'{"event":"payment.captured","amount":100}'
        signature = _sign(WEBHOOK_SECRET, original.decode())
        tampered = b'{"event":"payment.captured","amount":999999}'

        assert verify_webhook_signature(WEBHOOK_SECRET, tampered, signature) is False

    def test_wrong_secret_fails(self) -> None:
        body = b'{"event":"payment.captured"}'
        signature = _sign("some_other_secret", body.decode())

        assert verify_webhook_signature(WEBHOOK_SECRET, body, signature) is False


class TestExtractEventId:
    def test_uses_header_when_present(self) -> None:
        body = b"{}"
        headers = {"X-Razorpay-Event-Id": "evt_123", "Content-Type": "application/json"}

        assert extract_event_id(body, headers) == "evt_123"

    def test_header_lookup_is_case_insensitive(self) -> None:
        body = b"{}"
        headers = {"x-razorpay-event-id": "evt_456"}

        assert extract_event_id(body, headers) == "evt_456"

    def test_falls_back_to_body_hash_when_header_missing(self) -> None:
        body = b'{"event":"payment.captured"}'

        event_id = extract_event_id(body, {})

        assert event_id == hashlib.sha256(body).hexdigest()

    def test_body_hash_fallback_is_deterministic(self) -> None:
        body = b'{"a":1}'

        assert extract_event_id(body, {}) == extract_event_id(body, {})
