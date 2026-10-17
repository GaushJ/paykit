"""Domain models and enums. Provider-agnostic; no Razorpay-specific fields here."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class Money(BaseModel):
    """An amount in minor units (paise, cents, ...) plus its ISO-4217 currency."""

    amount: int
    currency: str

    @field_validator("amount")
    @classmethod
    def _amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount must be > 0")
        return v

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, v: str) -> str:
        v = v.upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be an ISO-4217 code, e.g. 'INR'")
        return v


class OrderStatus(StrEnum):
    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"
    EXPIRED = "expired"


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class RefundStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class SubscriptionStatus(StrEnum):
    CREATED = "created"
    AUTHENTICATED = "authenticated"
    ACTIVE = "active"
    PENDING = "pending"
    HALTED = "halted"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXPIRED = "expired"


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Order(_Base):
    id: str
    provider: str
    provider_order_id: str
    amount: Money
    status: OrderStatus
    receipt: str | None = None
    customer_ref: str | None = None
    metadata: dict[str, str] = {}
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime


class Payment(_Base):
    id: str
    provider: str
    provider_payment_id: str
    order_id: str | None = None
    subscription_id: str | None = None
    amount: Money
    amount_refunded: int = 0
    status: PaymentStatus
    method: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    created_at: datetime
    updated_at: datetime


class Refund(_Base):
    id: str
    provider_refund_id: str
    payment_id: str
    amount: Money
    status: RefundStatus
    reason: str | None = None
    created_at: datetime


class Plan(_Base):
    id: str
    provider_plan_id: str
    name: str
    amount: Money
    period: str  # "daily" | "weekly" | "monthly" | "yearly"
    interval: int


class Subscription(_Base):
    id: str
    provider_subscription_id: str
    plan_id: str
    customer_ref: str | None = None
    status: SubscriptionStatus
    current_start: datetime | None = None
    current_end: datetime | None = None
    total_count: int | None = None
    paid_count: int = 0
    short_url: str | None = None
    metadata: dict[str, str] = {}


class PaymentEvent(_Base):
    """Normalized event emitted to app handlers and outbound webhooks."""

    id: str
    type: str
    provider: str
    occurred_at: datetime
    order: Order | None = None
    payment: Payment | None = None
    refund: Refund | None = None
    subscription: Subscription | None = None
    raw: dict[str, object] = {}


class CheckoutResult(BaseModel):
    """What the frontend posts back after Razorpay Checkout completes."""

    provider_order_id: str | None = None
    provider_subscription_id: str | None = None
    provider_payment_id: str
    signature: str


# --- Provider-normalized return types (adapter contract, §7.1) -------------
# These are what a PaymentProvider implementation returns. They carry
# whatever the provider gave us; PaymentService maps them onto the domain
# models above (assigning our own `id`, looking up `order_id`, etc).


class ProviderOrder(BaseModel):
    provider_order_id: str
    amount: Money
    status: OrderStatus
    receipt: str | None = None
    notes: dict[str, str] = {}
    created_at: datetime


class ProviderPayment(BaseModel):
    provider_payment_id: str
    provider_order_id: str | None = None
    provider_subscription_id: str | None = None
    amount: Money
    amount_refunded: int = 0
    status: PaymentStatus
    method: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    created_at: datetime


class ProviderRefund(BaseModel):
    provider_refund_id: str
    provider_payment_id: str
    amount: Money
    status: RefundStatus
    notes: dict[str, str] = {}
    created_at: datetime


class ProviderPlan(BaseModel):
    provider_plan_id: str
    name: str
    amount: Money
    period: str
    interval: int


class ProviderSubscription(BaseModel):
    provider_subscription_id: str
    provider_plan_id: str
    status: SubscriptionStatus
    current_start: datetime | None = None
    current_end: datetime | None = None
    total_count: int | None = None
    paid_count: int = 0
    short_url: str | None = None
    notes: dict[str, str] = {}
