"""HMAC-SHA256 signature verification for Razorpay checkout results and webhooks.

All comparisons use `hmac.compare_digest` (constant-time). Never compare
signatures with `==`.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

WEBHOOK_SIGNATURE_HEADER = "X-Razorpay-Signature"
WEBHOOK_EVENT_ID_HEADER = "X-Razorpay-Event-Id"


def _hmac_hex(secret: str, message: str | bytes) -> str:
    msg = message.encode("utf-8") if isinstance(message, str) else message
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_order_checkout_signature(
    key_secret: str,
    *,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> bool:
    """Verify the signature Razorpay Checkout returns for a one-time order payment."""
    expected = _hmac_hex(key_secret, f"{razorpay_order_id}|{razorpay_payment_id}")
    return hmac.compare_digest(expected, razorpay_signature)


def verify_subscription_checkout_signature(
    key_secret: str,
    *,
    razorpay_payment_id: str,
    razorpay_subscription_id: str,
    razorpay_signature: str,
) -> bool:
    """Verify the signature Razorpay Checkout returns for a subscription payment."""
    expected = _hmac_hex(key_secret, f"{razorpay_payment_id}|{razorpay_subscription_id}")
    return hmac.compare_digest(expected, razorpay_signature)


def verify_webhook_signature(webhook_secret: str, raw_body: bytes, signature: str) -> bool:
    """Verify a webhook signature against the *raw* request body bytes.

    Never re-serialize/re-encode JSON before calling this - any change to
    byte layout (key order, whitespace) breaks the HMAC.
    """
    expected = _hmac_hex(webhook_secret, raw_body)
    return hmac.compare_digest(expected, signature)


def extract_event_id(raw_body: bytes, headers: Mapping[str, str]) -> str:
    """Return the dedupe key for a webhook delivery.

    Prefers Razorpay's `X-Razorpay-Event-Id` header. Razorpay has always
    sent this in practice, but if it's ever absent we still need a stable
    id, so fall back to a hash of the raw body.
    """
    for key, value in headers.items():
        if key.lower() == WEBHOOK_EVENT_ID_HEADER.lower():
            return value
    return hashlib.sha256(raw_body).hexdigest()
