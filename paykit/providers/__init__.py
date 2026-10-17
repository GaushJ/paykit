"""Provider registry.

Adding a new provider (Dodo/Paddle/Stripe, see PAYKIT_SPEC.md §16) means
creating `providers/<name>/` implementing `PaymentProvider` and registering
it here - core/service.py never imports a concrete provider directly.
"""

from __future__ import annotations

from paykit.config import PayKitSettings
from paykit.core.errors import ConfigurationError
from paykit.core.provider import PaymentProvider


def get_provider(name: str, settings: PayKitSettings) -> PaymentProvider:
    if name == "razorpay":
        from paykit.providers.razorpay.adapter import RazorpayProvider

        return RazorpayProvider(
            settings.razorpay_key_id,
            settings.razorpay_key_secret.get_secret_value(),
            settings.razorpay_webhook_secret.get_secret_value(),
            http_timeout_seconds=settings.http_timeout_seconds,
        )
    if name == "dummy":
        # No real money moves - see paykit/providers/dummy/adapter.py.
        # Useful for demos/local dev without a Razorpay account; PayKitSettings
        # still requires PAYKIT_RAZORPAY_* to be set to *something* even when
        # unused (build_service_from_env() reads the whole settings object
        # up front) - the quickstart uses PaymentService(...) directly
        # instead to skip that entirely.
        from paykit.providers.dummy import DummyProvider

        return DummyProvider()
    raise ConfigurationError(f"unknown PAYKIT_PROVIDER {name!r}")
