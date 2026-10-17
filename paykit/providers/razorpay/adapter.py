"""RazorpayProvider: implements core.provider.PaymentProvider for Razorpay."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from paykit.core.models import (
    CheckoutResult,
    Money,
    Order,
    PaymentEvent,
    ProviderOrder,
    ProviderPayment,
    ProviderPlan,
    ProviderRefund,
    ProviderSubscription,
)
from paykit.providers.razorpay.client import RazorpayClient
from paykit.providers.razorpay.mapping import (
    order_from_razorpay,
    payment_from_razorpay,
    plan_from_razorpay,
    refund_from_razorpay,
    subscription_from_razorpay,
)
from paykit.providers.razorpay.signatures import (
    WEBHOOK_SIGNATURE_HEADER,
    extract_event_id,
    verify_order_checkout_signature,
    verify_subscription_checkout_signature,
    verify_webhook_signature,
)
from paykit.providers.razorpay.webhooks import build_payment_event


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return None


class RazorpayProvider:
    name = "razorpay"

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        webhook_secret: str,
        *,
        client: RazorpayClient | None = None,
        display_name: str = "Payment",
        http_timeout_seconds: float = 15,
    ) -> None:
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._display_name = display_name
        self._client = client or RazorpayClient(
            key_id, key_secret, timeout_seconds=http_timeout_seconds
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- Orders / payments / refunds ----------------------------------

    async def create_order(
        self, amount: Money, *, receipt: str | None, metadata: dict[str, str]
    ) -> ProviderOrder:
        body: dict[str, object] = {"amount": amount.amount, "currency": amount.currency}
        if receipt is not None:
            body["receipt"] = receipt
        if metadata:
            body["notes"] = metadata
        response = await self._client.post("/orders", json=body)
        return order_from_razorpay(response)

    async def fetch_order(self, provider_order_id: str) -> ProviderOrder:
        response = await self._client.get(f"/orders/{provider_order_id}")
        return order_from_razorpay(response)

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        response = await self._client.get(f"/payments/{provider_payment_id}")
        return payment_from_razorpay(response)

    async def capture_payment(self, provider_payment_id: str, amount: Money) -> ProviderPayment:
        response = await self._client.post(
            f"/payments/{provider_payment_id}/capture",
            json={"amount": amount.amount, "currency": amount.currency},
        )
        return payment_from_razorpay(response)

    async def refund_payment(
        self,
        provider_payment_id: str,
        amount: Money | None,
        *,
        reason: str | None,
        metadata: dict[str, str],
    ) -> ProviderRefund:
        body: dict[str, object] = {}
        if amount is not None:
            body["amount"] = amount.amount
        notes = dict(metadata)
        if reason is not None:
            notes["reason"] = reason
        if notes:
            body["notes"] = notes
        response = await self._client.post(
            f"/payments/{provider_payment_id}/refund", json=body
        )
        return refund_from_razorpay(response)

    # -- Plans / subscriptions ------------------------------------------

    async def create_plan(
        self, name: str, amount: Money, period: str, interval: int
    ) -> ProviderPlan:
        response = await self._client.post(
            "/plans",
            json={
                "period": period,
                "interval": interval,
                "item": {"name": name, "amount": amount.amount, "currency": amount.currency},
            },
        )
        return plan_from_razorpay(response)

    async def create_subscription(
        self,
        provider_plan_id: str,
        *,
        total_count: int,
        customer_notify: bool,
        metadata: dict[str, str],
        start_at: datetime | None = None,
    ) -> ProviderSubscription:
        body: dict[str, object] = {
            "plan_id": provider_plan_id,
            "total_count": total_count,
            "customer_notify": customer_notify,
        }
        if metadata:
            body["notes"] = metadata
        if start_at is not None:
            body["start_at"] = int(start_at.timestamp())
        response = await self._client.post("/subscriptions", json=body)
        return subscription_from_razorpay(response)

    async def cancel_subscription(
        self, provider_subscription_id: str, *, at_cycle_end: bool
    ) -> ProviderSubscription:
        response = await self._client.post(
            f"/subscriptions/{provider_subscription_id}/cancel",
            json={"cancel_at_cycle_end": at_cycle_end},
        )
        return subscription_from_razorpay(response)

    async def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        response = await self._client.get(f"/subscriptions/{provider_subscription_id}")
        return subscription_from_razorpay(response)

    # -- Signatures / webhooks -------------------------------------------

    def verify_checkout_signature(self, payload: CheckoutResult) -> bool:
        if payload.provider_order_id is not None:
            return verify_order_checkout_signature(
                self._key_secret,
                razorpay_order_id=payload.provider_order_id,
                razorpay_payment_id=payload.provider_payment_id,
                razorpay_signature=payload.signature,
            )
        if payload.provider_subscription_id is not None:
            return verify_subscription_checkout_signature(
                self._key_secret,
                razorpay_payment_id=payload.provider_payment_id,
                razorpay_subscription_id=payload.provider_subscription_id,
                razorpay_signature=payload.signature,
            )
        raise ValueError("CheckoutResult must carry either an order id or a subscription id")

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        signature = _get_header(headers, WEBHOOK_SIGNATURE_HEADER)
        if signature is None:
            return False
        return verify_webhook_signature(self._webhook_secret, raw_body, signature)

    def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> PaymentEvent:
        """Parse a webhook body into a normalized PaymentEvent.

        Does not itself verify the signature - callers must call
        `verify_webhook` first (see core/service.py's handle_webhook flow).
        """
        body = json.loads(raw_body)
        event_id = extract_event_id(raw_body, headers)
        return build_payment_event(body, event_id=event_id)

    def checkout_config(
        self, order: Order, *, prefill: dict[str, str] | None = None
    ) -> dict[str, object]:
        return {
            "provider": self.name,
            "key": self._key_id,
            "order_id": order.provider_order_id,
            "amount": order.amount.amount,
            "currency": order.amount.currency,
            "name": self._display_name,
            "description": order.receipt or order.id,
            "prefill": prefill or {},
            "notes": order.metadata,
        }
