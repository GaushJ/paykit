"""PaymentService: the only thing app code talks to (design principle #1).

It never imports a concrete provider - `build_service_from_env` wires one
up via `paykit.providers.get_provider`, and the rest of this module only
sees the `PaymentProvider` protocol.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from paykit.config import PayKitSettings
from paykit.core.errors import (
    AmountMismatchError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
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
    Subscription,
)
from paykit.core.provider import PaymentProvider
from paykit.core.repository import PaymentRepository
from paykit.core.state import can_transition

logger = logging.getLogger("paykit.service")

# A payment can only be refunded once it has (at least partially) captured funds.
_REFUNDABLE_PAYMENT_STATUSES = {PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED}
_CAPTURABLE_PAYMENT_STATUSES = {PaymentStatus.CREATED, PaymentStatus.AUTHORIZED}


class _ProviderPaymentLike(Protocol):
    """Structural shape `_upsert_payment_from_provider` needs - satisfied by
    both `ProviderPayment` (from confirm_checkout/capture_payment) and
    `_AsProviderPayment` (wrapping a webhook's placeholder domain Payment)."""

    provider_payment_id: str
    amount: Money
    amount_refunded: int
    status: PaymentStatus
    method: str | None
    error_code: str | None
    error_description: str | None
    created_at: datetime


class PaymentService:
    def __init__(
        self,
        *,
        provider: PaymentProvider,
        repository: PaymentRepository,
        events: EventBus | None = None,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self.events = events or EventBus()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    # -- Orders -----------------------------------------------------------

    async def create_order(
        self,
        amount: Money,
        *,
        customer_ref: str | None = None,
        receipt: str | None = None,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        metadata = metadata or {}

        if idempotency_key is not None:
            existing = await self._repository.get_order_by_idempotency_key(idempotency_key)
            if existing is not None:
                if existing.amount != amount:
                    raise IdempotencyConflictError(
                        f"idempotency key {idempotency_key!r} was already used with a "
                        f"different amount ({existing.amount!r} != {amount!r})"
                    )
                return existing

        provider_order = await self._provider.create_order(
            amount, receipt=receipt, metadata=metadata
        )
        order = Order(
            id=str(uuid4()),
            provider=self._provider.name,
            provider_order_id=provider_order.provider_order_id,
            amount=provider_order.amount,
            status=provider_order.status,
            receipt=provider_order.receipt,
            customer_ref=customer_ref,
            metadata=metadata,
            idempotency_key=idempotency_key,
            created_at=provider_order.created_at,
            updated_at=provider_order.created_at,
        )
        return await self._repository.create_order(order)

    def checkout_config(
        self, order: Order, *, prefill: dict[str, str] | None = None
    ) -> dict[str, object]:
        return self._provider.checkout_config(order, prefill=prefill)

    async def confirm_checkout(self, payload: CheckoutResult) -> Payment:
        """Verify the client's checkout result and persist what the provider
        confirms. Gives the UI a fast answer - the *authoritative* state
        change and event dispatch happen via `handle_webhook` (see §7.3).
        """
        if not self._provider.verify_checkout_signature(payload):
            raise SignatureVerificationError("checkout signature verification failed")

        provider_payment = await self._provider.fetch_payment(payload.provider_payment_id)

        order: Order | None = None
        if payload.provider_order_id is not None:
            order = await self._repository.get_order_by_provider_id(payload.provider_order_id)
            if order is None:
                raise NotFoundError(f"no order for provider_order_id {payload.provider_order_id!r}")
            if provider_payment.amount != order.amount:
                raise AmountMismatchError(
                    f"payment amount {provider_payment.amount!r} does not match "
                    f"order amount {order.amount!r}"
                )

        subscription: Subscription | None = None
        if payload.provider_subscription_id is not None:
            subscription = await self._repository.get_subscription_by_provider_id(
                payload.provider_subscription_id
            )

        payment = await self._upsert_payment_from_provider(
            provider_payment,
            order_id=order.id if order else None,
            subscription_id=subscription.id if subscription else None,
        )

        if order is not None and provider_payment.status in (
            PaymentStatus.CAPTURED,
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
        ):
            await self._maybe_update_order_status(order, new_status=OrderStatus.PAID)

        return payment

    async def _upsert_payment_from_provider(
        self,
        provider_payment: _ProviderPaymentLike,
        *,
        order_id: str | None,
        subscription_id: str | None,
    ) -> Payment:
        existing = await self._repository.get_payment_by_provider_id(
            provider_payment.provider_payment_id
        )
        if existing is None:
            new_payment = Payment(
                id=str(uuid4()),
                provider=self._provider.name,
                provider_payment_id=provider_payment.provider_payment_id,
                order_id=order_id,
                subscription_id=subscription_id,
                amount=provider_payment.amount,
                amount_refunded=provider_payment.amount_refunded,
                status=provider_payment.status,
                method=provider_payment.method,
                error_code=provider_payment.error_code,
                error_description=provider_payment.error_description,
                created_at=provider_payment.created_at,
                updated_at=provider_payment.created_at,
            )
            return await self._repository.create_payment(new_payment)

        if not can_transition(existing.status, provider_payment.status):
            logger.info(
                "ignoring payment status transition %s -> %s for %s",
                existing.status,
                provider_payment.status,
                existing.provider_payment_id,
            )
            return existing

        merged = existing.model_copy(
            update={
                "status": provider_payment.status,
                "amount_refunded": provider_payment.amount_refunded,
                "method": provider_payment.method or existing.method,
                "error_code": provider_payment.error_code,
                "error_description": provider_payment.error_description,
                "updated_at": datetime.now(UTC),
            }
        )
        return await self._repository.update_payment(merged)

    async def _maybe_update_order_status(self, order: Order, *, new_status: OrderStatus) -> Order:
        if not can_transition(order.status, new_status):
            return order
        merged = order.model_copy(update={"status": new_status, "updated_at": datetime.now(UTC)})
        return await self._repository.update_order(merged)

    # -- Reads (for integrations that list/display orders, e.g. the FastAPI
    # router's GET routes) --------------------------------------------------

    async def get_order(self, order_id: str) -> Order | None:
        return await self._repository.get_order(order_id)

    async def list_payments_for_order(self, order_id: str) -> list[Payment]:
        return await self._repository.list_payments_for_order(order_id)

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        return await self._repository.get_subscription(subscription_id)

    # -- Refunds / captures ------------------------------------------------

    async def refund(
        self,
        payment_id: str,
        amount: Money | None,
        *,
        reason: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Refund:
        payment = await self._repository.get_payment(payment_id)
        if payment is None:
            raise NotFoundError(f"no payment with id {payment_id!r}")
        if payment.status not in _REFUNDABLE_PAYMENT_STATUSES:
            raise InvalidStateError(
                f"payment {payment_id!r} is {payment.status.value}, not refundable"
            )

        provider_refund = await self._provider.refund_payment(
            payment.provider_payment_id, amount, reason=reason, metadata=metadata or {}
        )
        refund = Refund(
            id=str(uuid4()),
            provider_refund_id=provider_refund.provider_refund_id,
            payment_id=payment.id,
            amount=provider_refund.amount,
            status=provider_refund.status,
            reason=reason,
            created_at=provider_refund.created_at,
        )
        refund = await self._repository.create_refund(refund)

        new_amount_refunded = payment.amount_refunded + provider_refund.amount.amount
        new_status = (
            PaymentStatus.REFUNDED
            if new_amount_refunded >= payment.amount.amount
            else PaymentStatus.PARTIALLY_REFUNDED
        )
        if can_transition(payment.status, new_status):
            await self._repository.update_payment(
                payment.model_copy(
                    update={
                        "amount_refunded": new_amount_refunded,
                        "status": new_status,
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
        return refund

    async def capture_payment(self, payment_id: str, amount: Money) -> Payment:
        payment = await self._repository.get_payment(payment_id)
        if payment is None:
            raise NotFoundError(f"no payment with id {payment_id!r}")
        if payment.status not in _CAPTURABLE_PAYMENT_STATUSES:
            raise InvalidStateError(
                f"payment {payment_id!r} is {payment.status.value}, not capturable"
            )
        provider_payment = await self._provider.capture_payment(
            payment.provider_payment_id, amount
        )
        return await self._upsert_payment_from_provider(
            provider_payment, order_id=payment.order_id, subscription_id=payment.subscription_id
        )

    # -- Plans / subscriptions ------------------------------------------

    async def create_plan(self, name: str, amount: Money, period: str, interval: int) -> Plan:
        provider_plan = await self._provider.create_plan(name, amount, period, interval)
        plan = Plan(
            id=str(uuid4()),
            provider_plan_id=provider_plan.provider_plan_id,
            name=provider_plan.name,
            amount=provider_plan.amount,
            period=provider_plan.period,
            interval=provider_plan.interval,
        )
        return await self._repository.create_plan(plan)

    async def create_subscription(
        self,
        plan_id: str,
        *,
        customer_ref: str | None = None,
        total_count: int,
        customer_notify: bool = True,
        metadata: dict[str, str] | None = None,
        start_at: datetime | None = None,
    ) -> Subscription:
        plan = await self._repository.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(f"no plan with id {plan_id!r}")

        metadata = metadata or {}
        provider_sub = await self._provider.create_subscription(
            plan.provider_plan_id,
            total_count=total_count,
            customer_notify=customer_notify,
            metadata=metadata,
            start_at=start_at,
        )
        subscription = Subscription(
            id=str(uuid4()),
            provider_subscription_id=provider_sub.provider_subscription_id,
            plan_id=plan.id,
            customer_ref=customer_ref,
            status=provider_sub.status,
            current_start=provider_sub.current_start,
            current_end=provider_sub.current_end,
            total_count=provider_sub.total_count,
            paid_count=provider_sub.paid_count,
            short_url=provider_sub.short_url,
            metadata=provider_sub.notes or metadata,
        )
        return await self._repository.create_subscription(subscription)

    async def cancel_subscription(
        self, subscription_id: str, *, at_cycle_end: bool
    ) -> Subscription:
        subscription = await self._repository.get_subscription(subscription_id)
        if subscription is None:
            raise NotFoundError(f"no subscription with id {subscription_id!r}")

        provider_sub = await self._provider.cancel_subscription(
            subscription.provider_subscription_id, at_cycle_end=at_cycle_end
        )
        if not can_transition(subscription.status, provider_sub.status):
            return subscription
        merged = subscription.model_copy(
            update={
                "status": provider_sub.status,
                "current_start": provider_sub.current_start,
                "current_end": provider_sub.current_end,
            }
        )
        return await self._repository.update_subscription(merged)

    # -- Webhooks -----------------------------------------------------------

    async def handle_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        if not self._provider.verify_webhook(raw_body, headers):
            raise SignatureVerificationError("webhook signature verification failed")

        event = self._provider.parse_webhook(raw_body, headers)

        async with self._repository.transaction():
            is_new = await self._repository.record_event_if_new(event.id)
            if not is_new:
                return
            final_event = await self._reconcile_event(event)

        # Dispatch only after the transaction committed. EventBus.dispatch
        # logs and swallows handler exceptions itself, so a bad handler
        # never turns this into a non-2xx response for the provider.
        await self.events.dispatch(final_event)

    async def _reconcile_event(self, event: PaymentEvent) -> PaymentEvent:
        subscription = await self._reconcile_subscription(event.subscription)
        order = await self._reconcile_order(event.order)
        payment = await self._reconcile_payment(
            event.payment, order=order, subscription=subscription
        )
        refund = await self._reconcile_refund(event.refund, payment=payment)

        # A payment.captured payload doesn't always carry a synchronized
        # order sub-object (some webhook payloads only `contains: ["payment"]`)
        # - don't rely on one being present. Promote the order directly from
        # the payment's own status, same as confirm_checkout does.
        if payment is not None and payment.order_id is not None and payment.status in (
            PaymentStatus.CAPTURED,
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
        ):
            if order is None or order.id != payment.order_id:
                order = await self._repository.get_order(payment.order_id)
            if order is not None:
                order = await self._maybe_update_order_status(order, new_status=OrderStatus.PAID)

        return event.model_copy(
            update={
                "order": order,
                "payment": payment,
                "refund": refund,
                "subscription": subscription,
            }
        )

    async def _reconcile_order(self, placeholder: Order | None) -> Order | None:
        if placeholder is None:
            return None
        existing = await self._repository.get_order_by_provider_id(
            placeholder.provider_order_id, for_update=True
        )
        if existing is None:
            return await self._repository.create_order(
                placeholder.model_copy(update={"id": str(uuid4())})
            )
        if not can_transition(existing.status, placeholder.status):
            logger.info(
                "ignoring order status transition %s -> %s for %s",
                existing.status,
                placeholder.status,
                existing.provider_order_id,
            )
            return existing
        merged = existing.model_copy(
            update={"status": placeholder.status, "updated_at": placeholder.updated_at}
        )
        return await self._repository.update_order(merged)

    async def _reconcile_payment(
        self,
        placeholder: Payment | None,
        *,
        order: Order | None,
        subscription: Subscription | None,
    ) -> Payment | None:
        if placeholder is None:
            return None
        return await self._upsert_payment_from_provider(
            _AsProviderPayment(placeholder),
            order_id=order.id if order else None,
            subscription_id=subscription.id if subscription else None,
        )

    async def _reconcile_refund(
        self, placeholder: Refund | None, *, payment: Payment | None
    ) -> Refund | None:
        if placeholder is None:
            return None
        existing = await self._repository.get_refund_by_provider_id(
            placeholder.provider_refund_id
        )
        resolved_payment_id = payment.id if payment else placeholder.payment_id
        if existing is None:
            return await self._repository.create_refund(
                placeholder.model_copy(
                    update={"id": str(uuid4()), "payment_id": resolved_payment_id}
                )
            )
        if not can_transition(existing.status, placeholder.status):
            return existing
        return await self._repository.update_refund(
            existing.model_copy(update={"status": placeholder.status})
        )

    async def _reconcile_subscription(
        self, placeholder: Subscription | None
    ) -> Subscription | None:
        if placeholder is None:
            return None
        existing = await self._repository.get_subscription_by_provider_id(
            placeholder.provider_subscription_id, for_update=True
        )
        if existing is None:
            # `placeholder.plan_id` is the provider's plan id (adapter has no
            # internal id to give it) - resolve it to ours if we know this
            # plan, otherwise keep the provider id as a best-effort value
            # rather than failing the whole webhook over an unknown plan
            # (e.g. one created directly in the provider's dashboard).
            plan = await self._repository.get_plan_by_provider_id(placeholder.plan_id)
            return await self._repository.create_subscription(
                placeholder.model_copy(
                    update={
                        "id": str(uuid4()),
                        "plan_id": plan.id if plan else placeholder.plan_id,
                    }
                )
            )
        if not can_transition(existing.status, placeholder.status):
            logger.info(
                "ignoring subscription status transition %s -> %s for %s",
                existing.status,
                placeholder.status,
                existing.provider_subscription_id,
            )
            return existing
        merged = existing.model_copy(
            update={
                "status": placeholder.status,
                "current_start": placeholder.current_start,
                "current_end": placeholder.current_end,
                "paid_count": placeholder.paid_count,
                "total_count": placeholder.total_count,
                "short_url": placeholder.short_url or existing.short_url,
            }
        )
        return await self._repository.update_subscription(merged)

    # -- Reconciliation -----------------------------------------------------------

    async def reconcile(self, since: datetime) -> None:
        """Re-fetch non-final orders/payments from the provider and fix their
        state - a safety net for missed webhooks. Only dispatches events for
        objects whose status actually changed, so re-running this on a
        schedule doesn't re-notify handlers for objects that are unchanged."""
        for order in await self._repository.list_non_final_orders(since):
            provider_order = await self._provider.fetch_order(order.provider_order_id)
            if provider_order.status == order.status or not can_transition(
                order.status, provider_order.status
            ):
                continue
            await self._repository.update_order(
                order.model_copy(
                    update={"status": provider_order.status, "updated_at": datetime.now(UTC)}
                )
            )

        for payment in await self._repository.list_non_final_payments(since):
            provider_payment = await self._provider.fetch_payment(payment.provider_payment_id)
            if provider_payment.status == payment.status or not can_transition(
                payment.status, provider_payment.status
            ):
                continue
            updated = await self._repository.update_payment(
                payment.model_copy(
                    update={
                        "status": provider_payment.status,
                        "amount_refunded": provider_payment.amount_refunded,
                        "error_code": provider_payment.error_code,
                        "error_description": provider_payment.error_description,
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
            event = PaymentEvent(
                id=f"reconcile:{updated.id}:{updated.status.value}:{datetime.now(UTC).timestamp()}",
                type=f"payment.{updated.status.value}",
                provider=self._provider.name,
                occurred_at=datetime.now(UTC),
                payment=updated,
                raw={},
            )
            await self.events.dispatch(event)


class _AsProviderPayment:
    """Adapts a placeholder domain `Payment` (built by the adapter layer
    from a webhook payload) to look like a `ProviderPayment` so it can flow
    through the same `_upsert_payment_from_provider` merge logic used for
    `confirm_checkout` and `capture_payment`."""

    def __init__(self, payment: Payment) -> None:
        self.provider_payment_id = payment.provider_payment_id
        self.amount = payment.amount
        self.amount_refunded = payment.amount_refunded
        self.status = payment.status
        self.method = payment.method
        self.error_code = payment.error_code
        self.error_description = payment.error_description
        self.created_at = payment.created_at


def build_service_from_env(*, settings: PayKitSettings | None = None) -> PaymentService:
    """Wire a PaymentService from PAYKIT_* environment variables."""
    settings = settings or PayKitSettings()  # type: ignore[call-arg]

    from paykit.providers import get_provider

    provider = get_provider(settings.provider, settings)

    repository: PaymentRepository
    if settings.database_url:
        from sqlalchemy.ext.asyncio import create_async_engine

        from paykit.storage.sqlalchemy.repository import SQLAlchemyRepository

        engine = create_async_engine(settings.database_url)
        repository = SQLAlchemyRepository(engine, table_prefix=settings.table_prefix)
    else:
        from paykit.storage.memory import InMemoryRepository

        repository = InMemoryRepository()

    return PaymentService(provider=provider, repository=repository, events=EventBus())
