"""The adapter contract every payment provider implements."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

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


class PaymentProvider(Protocol):
    name: str

    async def create_order(
        self, amount: Money, *, receipt: str | None, metadata: dict[str, str]
    ) -> ProviderOrder: ...

    async def fetch_order(self, provider_order_id: str) -> ProviderOrder: ...

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment: ...

    async def capture_payment(self, provider_payment_id: str, amount: Money) -> ProviderPayment: ...

    async def refund_payment(
        self,
        provider_payment_id: str,
        amount: Money | None,
        *,
        reason: str | None,
        metadata: dict[str, str],
    ) -> ProviderRefund: ...

    async def create_plan(
        self, name: str, amount: Money, period: str, interval: int
    ) -> ProviderPlan: ...

    async def create_subscription(
        self,
        provider_plan_id: str,
        *,
        total_count: int,
        customer_notify: bool,
        metadata: dict[str, str],
        start_at: datetime | None = None,
    ) -> ProviderSubscription: ...

    async def cancel_subscription(
        self, provider_subscription_id: str, *, at_cycle_end: bool
    ) -> ProviderSubscription: ...

    async def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription: ...

    def verify_checkout_signature(self, payload: CheckoutResult) -> bool: ...

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool: ...

    def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> PaymentEvent: ...

    def checkout_config(
        self, order: Order, *, prefill: dict[str, str] | None = None
    ) -> dict[str, object]:
        """Everything the frontend needs to open checkout (public key, order id, amount...)."""
        ...
