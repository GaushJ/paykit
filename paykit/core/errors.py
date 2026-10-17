"""PayKit exception hierarchy."""

from __future__ import annotations


class PayKitError(Exception):
    """Base class for all PayKit errors."""


class ConfigurationError(PayKitError):
    """Raised when PayKit is misconfigured (missing/invalid settings, wiring)."""


class ProviderError(PayKitError):
    """Raised when a payment provider's API returns an error."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        description: str | None = None,
        http_status: int | None = None,
        provider_response: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.description = description
        self.http_status = http_status
        self.provider_response = provider_response


class SignatureVerificationError(PayKitError):
    """Raised when a checkout or webhook signature fails verification."""


class NotFoundError(PayKitError):
    """Raised when a requested order/payment/refund/subscription does not exist."""


class InvalidStateError(PayKitError):
    """Raised when an operation is not valid for the current state of an object."""


class AmountMismatchError(PayKitError):
    """Raised when a fetched payment's amount/currency doesn't match the stored order."""


class IdempotencyConflictError(PayKitError):
    """Raised when an idempotency key is reused with a different amount."""
