"""PayKit: a provider-agnostic payments SDK. Public API surface.

Note: PaymentService / build_service_from_env are added once core/service.py
lands (build phase 5) - imported here lazily via __getattr__ so this module
stays importable during earlier phases and so importing `paykit` doesn't
force in the (optional) fastapi/sqlalchemy extras.
"""

from __future__ import annotations

from typing import Any

from paykit.config import PayKitSettings
from paykit.core.errors import (
    AmountMismatchError,
    ConfigurationError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    PayKitError,
    ProviderError,
    SignatureVerificationError,
)
from paykit.core.events import EventBus
from paykit.core.models import (
    CheckoutResult,
    Money,
    Order,
    OrderStatus,
    Payment,
    PaymentEvent,
    PaymentStatus,
    Plan,
    Refund,
    RefundStatus,
    Subscription,
    SubscriptionStatus,
)

__all__ = [
    "AmountMismatchError",
    "CheckoutResult",
    "ConfigurationError",
    "EventBus",
    "IdempotencyConflictError",
    "InvalidStateError",
    "Money",
    "NotFoundError",
    "Order",
    "OrderStatus",
    "PayKitError",
    "PayKitSettings",
    "Payment",
    "PaymentEvent",
    "PaymentService",
    "PaymentStatus",
    "Plan",
    "ProviderError",
    "Refund",
    "RefundStatus",
    "SignatureVerificationError",
    "Subscription",
    "SubscriptionStatus",
    "build_service_from_env",
]


def __getattr__(name: str) -> Any:
    if name == "PaymentService":
        from paykit.core.service import PaymentService

        return PaymentService
    if name == "build_service_from_env":
        from paykit.core.service import build_service_from_env

        return build_service_from_env
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
