"""DummyProvider: a minimal in-memory PaymentProvider.

This is the worked example for "how to add a new provider" (README) and is
also what proves core/service.py has no Razorpay-specific assumptions
(PAYKIT_SPEC.md §13). It implements the exact same `PaymentProvider`
protocol `RazorpayProvider` does, just against an in-process dict instead
of a real payment gateway - so it's also handy for local development and
demos that don't need a real Razorpay account.

Not registered as a provider apps would use in production: no real money
moves, and `verify_checkout_signature`/`verify_webhook` use a shared
secret you provide rather than a gateway-issued one.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from itertools import count

from paykit.core.models import (
    CheckoutResult,
    Money,
    Order,
    OrderStatus,
    Payment,
    PaymentEvent,
    PaymentStatus,
    ProviderOrder,
    ProviderPayment,
    ProviderPlan,
    ProviderRefund,
    ProviderSubscription,
    Refund,
    RefundStatus,
    Subscription,
    SubscriptionStatus,
)


class DummyProvider:
    name = "dummy"

    def __init__(self, *, secret: str = "dummy_secret") -> None:
        self._secret = secret
        self._orders: dict[str, ProviderOrder] = {}
        self._payments: dict[str, ProviderPayment] = {}
        self._refunds: dict[str, ProviderRefund] = {}
        self._plans: dict[str, ProviderPlan] = {}
        self._subscriptions: dict[str, ProviderSubscription] = {}
        self._counter = count(1)

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._counter)}"

    def _sign(self, value: str) -> str:
        return hmac.new(self._secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    # -- PaymentProvider protocol -----------------------------------------

    async def create_order(
        self, amount: Money, *, receipt: str | None, metadata: dict[str, str]
    ) -> ProviderOrder:
        order = ProviderOrder(
            provider_order_id=self._next_id("order"),
            amount=amount,
            status=OrderStatus.CREATED,
            receipt=receipt,
            notes=metadata,
            created_at=datetime.now(UTC),
        )
        self._orders[order.provider_order_id] = order
        return order

    async def fetch_order(self, provider_order_id: str) -> ProviderOrder:
        return self._orders[provider_order_id]

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        return self._payments[provider_payment_id]

    async def capture_payment(self, provider_payment_id: str, amount: Money) -> ProviderPayment:
        payment = self._payments[provider_payment_id]
        updated = payment.model_copy(update={"status": PaymentStatus.CAPTURED})
        self._payments[provider_payment_id] = updated
        return updated

    async def refund_payment(
        self,
        provider_payment_id: str,
        amount: Money | None,
        *,
        reason: str | None,
        metadata: dict[str, str],
    ) -> ProviderRefund:
        payment = self._payments[provider_payment_id]
        refund_amount = amount or payment.amount
        refund = ProviderRefund(
            provider_refund_id=self._next_id("rfnd"),
            provider_payment_id=provider_payment_id,
            amount=refund_amount,
            status=RefundStatus.PROCESSED,
            notes=metadata,
            created_at=datetime.now(UTC),
        )
        self._refunds[refund.provider_refund_id] = refund

        new_amount_refunded = payment.amount_refunded + refund_amount.amount
        new_status = (
            PaymentStatus.REFUNDED
            if new_amount_refunded >= payment.amount.amount
            else PaymentStatus.PARTIALLY_REFUNDED
        )
        self._payments[provider_payment_id] = payment.model_copy(
            update={"amount_refunded": new_amount_refunded, "status": new_status}
        )
        return refund

    async def create_plan(
        self, name: str, amount: Money, period: str, interval: int
    ) -> ProviderPlan:
        plan = ProviderPlan(
            provider_plan_id=self._next_id("plan"),
            name=name,
            amount=amount,
            period=period,
            interval=interval,
        )
        self._plans[plan.provider_plan_id] = plan
        return plan

    async def create_subscription(
        self,
        provider_plan_id: str,
        *,
        total_count: int,
        customer_notify: bool,
        metadata: dict[str, str],
        start_at: datetime | None = None,
    ) -> ProviderSubscription:
        sub = ProviderSubscription(
            provider_subscription_id=self._next_id("sub"),
            provider_plan_id=provider_plan_id,
            status=SubscriptionStatus.CREATED,
            total_count=total_count,
            paid_count=0,
            notes=metadata,
        )
        self._subscriptions[sub.provider_subscription_id] = sub
        return sub

    async def cancel_subscription(
        self, provider_subscription_id: str, *, at_cycle_end: bool
    ) -> ProviderSubscription:
        sub = self._subscriptions[provider_subscription_id]
        updated = sub.model_copy(update={"status": SubscriptionStatus.CANCELLED})
        self._subscriptions[provider_subscription_id] = updated
        return updated

    async def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        return self._subscriptions[provider_subscription_id]

    def verify_checkout_signature(self, payload: CheckoutResult) -> bool:
        expected = self._sign(payload.provider_payment_id)
        return hmac.compare_digest(expected, payload.signature)

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        # Real ASGI servers (Starlette/FastAPI) always hand handlers
        # lowercased header names, so - like RazorpayProvider - look this up
        # case-insensitively rather than assuming "X-Dummy-Signature" casing.
        signature = next(
            (v for k, v in headers.items() if k.lower() == "x-dummy-signature"), None
        )
        if signature is None:
            return False
        expected = hmac.new(self._secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> PaymentEvent:
        return PaymentEvent.model_validate_json(raw_body)

    def checkout_config(
        self, order: Order, *, prefill: dict[str, str] | None = None
    ) -> dict[str, object]:
        return {
            "provider": self.name,
            "order_id": order.provider_order_id,
            "amount": order.amount.amount,
            "currency": order.amount.currency,
            "prefill": prefill or {},
        }

    # -- Test/demo helpers (not part of PaymentProvider) --------------------
    # A dummy gateway needs some way to *simulate* what a real gateway would
    # do (checkout completing, a webhook arriving) - these give tests and
    # demos exactly that, without a browser or a real Razorpay account.

    def simulate_payment(
        self,
        provider_order_id: str,
        *,
        status: PaymentStatus = PaymentStatus.CAPTURED,
        method: str = "card",
    ) -> ProviderPayment:
        order = self._orders[provider_order_id]
        payment = ProviderPayment(
            provider_payment_id=self._next_id("pay"),
            provider_order_id=provider_order_id,
            amount=order.amount,
            amount_refunded=0,
            status=status,
            method=method,
            created_at=datetime.now(UTC),
        )
        self._payments[payment.provider_payment_id] = payment
        return payment

    def make_checkout_result(
        self,
        payment: ProviderPayment,
        *,
        provider_order_id: str | None = None,
        provider_subscription_id: str | None = None,
    ) -> CheckoutResult:
        return CheckoutResult(
            provider_order_id=provider_order_id,
            provider_subscription_id=provider_subscription_id,
            provider_payment_id=payment.provider_payment_id,
            signature=self._sign(payment.provider_payment_id),
        )

    def make_webhook(
        self,
        *,
        event_id: str,
        event_type: str,
        order: Order | None = None,
        payment: Payment | None = None,
        refund: Refund | None = None,
        subscription: Subscription | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Build a signed (raw_body, headers) pair for `handle_webhook`.

        Order/Payment/Refund/Subscription here are *placeholder* domain
        objects (id == provider id) - same convention RazorpayProvider's
        webhooks.py uses, since the adapter layer has no repository access
        to know paykit's real internal ids. See `placeholder_*` below.
        """
        event = PaymentEvent(
            id=event_id,
            type=event_type,
            provider=self.name,
            occurred_at=datetime.now(UTC),
            order=order,
            payment=payment,
            refund=refund,
            subscription=subscription,
            raw={},
        )
        raw_body = event.model_dump_json().encode()
        signature = hmac.new(self._secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return raw_body, {"X-Dummy-Signature": signature, "Content-Type": "application/json"}


def placeholder_order(po: ProviderOrder) -> Order:
    return Order(
        id=po.provider_order_id,
        provider="dummy",
        provider_order_id=po.provider_order_id,
        amount=po.amount,
        status=po.status,
        receipt=po.receipt,
        customer_ref=None,
        metadata=po.notes,
        idempotency_key=None,
        created_at=po.created_at,
        updated_at=po.created_at,
    )


def placeholder_payment(
    pp: ProviderPayment, *, provider_subscription_id: str | None = None
) -> Payment:
    return Payment(
        id=pp.provider_payment_id,
        provider="dummy",
        provider_payment_id=pp.provider_payment_id,
        order_id=pp.provider_order_id,
        subscription_id=provider_subscription_id,
        amount=pp.amount,
        amount_refunded=pp.amount_refunded,
        status=pp.status,
        method=pp.method,
        error_code=pp.error_code,
        error_description=pp.error_description,
        created_at=pp.created_at,
        updated_at=pp.created_at,
    )


def placeholder_refund(pr: ProviderRefund) -> Refund:
    return Refund(
        id=pr.provider_refund_id,
        provider_refund_id=pr.provider_refund_id,
        payment_id=pr.provider_payment_id,
        amount=pr.amount,
        status=pr.status,
        reason=pr.notes.get("reason"),
        created_at=pr.created_at,
    )


def placeholder_subscription(ps: ProviderSubscription) -> Subscription:
    return Subscription(
        id=ps.provider_subscription_id,
        provider_subscription_id=ps.provider_subscription_id,
        plan_id=ps.provider_plan_id,
        customer_ref=None,
        status=ps.status,
        current_start=ps.current_start,
        current_end=ps.current_end,
        total_count=ps.total_count,
        paid_count=ps.paid_count,
        short_url=ps.short_url,
        metadata=ps.notes,
    )
