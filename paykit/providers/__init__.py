"""Provider registry.

Adding a new provider (Dodo/Paddle/Stripe, see PAYKIT_SPEC.md §16) means
creating `providers/<name>/` implementing `PaymentProvider` and registering
it here - core/service.py never imports a concrete provider directly.
"""

from __future__ import annotations

import logging

from paykit.config import PayKitSettings
from paykit.core.errors import ConfigurationError
from paykit.core.provider import PaymentProvider

logger = logging.getLogger("paykit.providers")


def get_provider(name: str, settings: PayKitSettings) -> PaymentProvider:
    if name == "razorpay":
        from paykit.providers.razorpay.adapter import RazorpayProvider

        key_id, key_secret, webhook_secret = settings.razorpay_credentials()
        return RazorpayProvider(
            key_id,
            key_secret,
            webhook_secret,
            http_timeout_seconds=settings.http_timeout_seconds,
        )
    if name == "dummy":
        # No real money moves - see paykit/providers/dummy/adapter.py.
        # DummyProvider() picks a random per-process signing secret, so a
        # deployment accidentally left on PAYKIT_PROVIDER=dummy can't have
        # its webhooks/checkout results forged with a well-known secret.
        from paykit.providers.dummy import DummyProvider

        logger.warning(
            "PAYKIT_PROVIDER=dummy: no real payments are processed. "
            "Never use this in production."
        )
        return DummyProvider()
    raise ConfigurationError(f"unknown PAYKIT_PROVIDER {name!r}")
