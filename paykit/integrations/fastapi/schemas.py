"""Request/response DTOs for the FastAPI router.

Kept separate from paykit's domain models (core/models.py) so the wire
contract can evolve independently of internals.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from paykit.core.models import Money, Order, Payment, Refund, Subscription


class MoneyOut(BaseModel):
    amount: int
    currency: str

    @classmethod
    def from_domain(cls, money: Money) -> MoneyOut:
        return cls(amount=money.amount, currency=money.currency)

    def to_money(self) -> Money:
        return Money(amount=self.amount, currency=self.currency)


class OrderOut(BaseModel):
    id: str
    provider: str
    amount: MoneyOut
    status: str
    receipt: str | None
    metadata: dict[str, str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, order: Order) -> OrderOut:
        return cls(
            id=order.id,
            provider=order.provider,
            amount=MoneyOut.from_domain(order.amount),
            status=order.status.value,
            receipt=order.receipt,
            metadata=order.metadata,
            created_at=order.created_at,
            updated_at=order.updated_at,
        )


class PaymentOut(BaseModel):
    id: str
    order_id: str | None
    subscription_id: str | None
    amount: MoneyOut
    amount_refunded: int
    status: str
    method: str | None
    error_code: str | None
    error_description: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, payment: Payment) -> PaymentOut:
        return cls(
            id=payment.id,
            order_id=payment.order_id,
            subscription_id=payment.subscription_id,
            amount=MoneyOut.from_domain(payment.amount),
            amount_refunded=payment.amount_refunded,
            status=payment.status.value,
            method=payment.method,
            error_code=payment.error_code,
            error_description=payment.error_description,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
        )


class RefundOut(BaseModel):
    id: str
    payment_id: str
    amount: MoneyOut
    status: str
    reason: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, refund: Refund) -> RefundOut:
        return cls(
            id=refund.id,
            payment_id=refund.payment_id,
            amount=MoneyOut.from_domain(refund.amount),
            status=refund.status.value,
            reason=refund.reason,
            created_at=refund.created_at,
        )


class SubscriptionOut(BaseModel):
    id: str
    plan_id: str
    status: str
    current_start: datetime | None
    current_end: datetime | None
    total_count: int | None
    paid_count: int
    short_url: str | None

    @classmethod
    def from_domain(cls, subscription: Subscription) -> SubscriptionOut:
        return cls(
            id=subscription.id,
            plan_id=subscription.plan_id,
            status=subscription.status.value,
            current_start=subscription.current_start,
            current_end=subscription.current_end,
            total_count=subscription.total_count,
            paid_count=subscription.paid_count,
            short_url=subscription.short_url,
        )


class CreateOrderRequest(BaseModel):
    """Body for POST /orders. Any extra fields (e.g. {"plan": "pro"}) are
    passed through to the app's `amount_resolver` - order amounts are
    always decided server-side, never trusted from the client."""

    model_config = ConfigDict(extra="allow")

    receipt: str | None = Field(default=None, description="App's own reference, e.g. an invoice id")
    metadata: dict[str, str] = Field(
        default_factory=dict, description="Forwarded to the provider as order notes"
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Reusing this key returns the same order instead of creating a new one",
    )
    prefill: dict[str, str] | None = Field(
        default=None, description="e.g. {\"email\": ..., \"contact\": ...} for Razorpay Checkout"
    )


class CreateOrderResponse(BaseModel):
    order: OrderOut
    checkout: dict[str, object] = Field(
        description="Everything the frontend needs to open the provider's checkout"
    )


class OrderDetailResponse(BaseModel):
    order: OrderOut
    payments: list[PaymentOut]


class RefundRequest(BaseModel):
    amount: MoneyOut | None = Field(
        default=None, description="Full refund if omitted"
    )
    reason: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class CreateSubscriptionRequest(BaseModel):
    plan_id: str = Field(description="paykit's own plan id, from create_plan()")
    total_count: int = Field(description="Total number of billing cycles")
    customer_notify: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)
    start_at: datetime | None = None
    customer_ref: str | None = Field(
        default=None,
        description=(
            "Only read by a customer_ref_getter that needs it from the body "
            "(the standalone service, §10); the embedded integration (§9) "
            "derives customer_ref from the authenticated user instead."
        ),
    )


class CreateSubscriptionResponse(BaseModel):
    subscription: SubscriptionOut
    short_url: str | None = Field(
        description="Razorpay's hosted checkout page for this subscription"
    )


class CancelSubscriptionRequest(BaseModel):
    at_cycle_end: bool = Field(
        default=True, description="If true, stays active until the current billing period ends"
    )
