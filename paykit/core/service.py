"""PaymentService: the only thing app code talks to (design principle #1).

It never imports a concrete provider - `build_service_from_env` wires one
up via `paykit.providers.get_provider`, and the rest of this module only
sees the `PaymentProvider` protocol.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from paykit.config import PayKitSettings
from paykit.core.errors import (
    AmountMismatchError,
    DuplicateError,
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
# Statuses meaning money was captured for the order at some point.
_PAID_PAYMENT_STATUSES = {
    PaymentStatus.CAPTURED,
    PaymentStatus.PARTIALLY_REFUNDED,
    PaymentStatus.REFUNDED,
}
# Razorpay redelivers failed webhooks for ~24h; keep dedupe records well past that.
DEFAULT_EVENT_RETENTION = timedelta(days=30)


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

    @property
    def provider(self) -> PaymentProvider:
        return self._provider

    @property
    def repository(self) -> PaymentRepository:
        return self._repository

    async def aclose(self) -> None:
        """Release the provider's HTTP client and the repository's DB pool."""
        provider_aclose = getattr(self._provider, "aclose", None)
        if provider_aclose is not None:
            await provider_aclose()
        repository_dispose = getattr(self._repository, "dispose", None)
        if repository_dispose is not None:
            await repository_dispose()

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
                return self._idempotent_replay(existing, amount, customer_ref, idempotency_key)

        provider_order = await self._provider.create_order(
            amount, receipt=receipt, metadata=metadata
        )
        if provider_order.amount != amount:
            raise AmountMismatchError(
                f"provider created order for {provider_order.amount!r}, expected {amount!r}"
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
        try:
            return await self._repository.create_order(order)
        except DuplicateError:
            # A concurrent request with the same idempotency key won the
            # race. Its provider order is the one we keep; ours is an unpaid
            # orphan at the provider, which simply expires.
            if idempotency_key is None:
                raise
            winner = await self._repository.get_order_by_idempotency_key(idempotency_key)
            if winner is None:
                raise
            return self._idempotent_replay(winner, amount, customer_ref, idempotency_key)

    @staticmethod
    def _idempotent_replay(
        existing: Order, amount: Money, customer_ref: str | None, idempotency_key: str
    ) -> Order:
        # Keys are scoped to the customer: replaying someone else's key must
        # not hand back their order (that would leak it, plus its checkout
        # config, to another user). Same error either way, so the response
        # doesn't reveal whether the key exists for another customer.
        if existing.customer_ref != customer_ref or existing.amount != amount:
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} was already used for a different request"
            )
        return existing

    def checkout_config(
        self, order: Order, *, prefill: dict[str, str] | None = None
    ) -> dict[str, object]:
        return self._provider.checkout_config(order, prefill=prefill)

    async def confirm_checkout(
        self, payload: CheckoutResult, *, customer_ref: str | None = None
    ) -> Payment:
        """Verify the client's checkout result and persist what the provider
        confirms. Gives the UI a fast answer - the *authoritative* state
        change and event dispatch happen via `handle_webhook` (see §7.3).

        If `customer_ref` is given, the order/subscription must belong to that
        customer, otherwise NotFoundError (the same error as a missing one, so
        callers can't probe for other customers' orders).
        """
        if not self._provider.verify_checkout_signature(payload):
            raise SignatureVerificationError("checkout signature verification failed")

        order: Order | None = None
        if payload.provider_order_id is not None:
            order = await self._repository.get_order_by_provider_id(payload.provider_order_id)
            if order is None or (customer_ref is not None and order.customer_ref != customer_ref):
                raise NotFoundError(f"no order for provider_order_id {payload.provider_order_id!r}")

        subscription: Subscription | None = None
        if payload.provider_subscription_id is not None:
            subscription = await self._repository.get_subscription_by_provider_id(
                payload.provider_subscription_id
            )
            if customer_ref is not None and (
                subscription is None or subscription.customer_ref != customer_ref
            ):
                raise NotFoundError(
                    f"no subscription for {payload.provider_subscription_id!r}"
                )

        provider_payment = await self._provider.fetch_payment(payload.provider_payment_id)

        if order is not None:
            # The signature binds order_id|payment_id, but check the provider's
            # own view too (defence in depth against a provider/adapter bug).
            if (
                provider_payment.provider_order_id is not None
                and provider_payment.provider_order_id != order.provider_order_id
            ):
                raise AmountMismatchError("payment does not belong to this order")
            if provider_payment.amount != order.amount:
                raise AmountMismatchError(
                    f"payment amount {provider_payment.amount!r} does not match "
                    f"order amount {order.amount!r}"
                )

        async with self._repository.transaction():
            payment = await self._upsert_payment_from_provider(
                provider_payment,
                order_id=order.id if order else None,
                subscription_id=subscription.id if subscription else None,
            )
            if order is not None and provider_payment.status in _PAID_PAYMENT_STATUSES:
                locked = await self._repository.get_order_by_provider_id(
                    order.provider_order_id, for_update=True
                )
                if locked is not None:
                    await self._maybe_update_order_status(locked, new_status=OrderStatus.PAID)

        return payment

    async def _upsert_payment_from_provider(
        self,
        provider_payment: _ProviderPaymentLike,
        *,
        order_id: str | None,
        subscription_id: str | None,
    ) -> Payment:
        """Insert or monotonically update a payment. Call inside
        `transaction()` so the row lock (`for_update`) is held until commit."""
        existing = await self._repository.get_payment_by_provider_id(
            provider_payment.provider_payment_id, for_update=True
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
            try:
                return await self._repository.create_payment(new_payment)
            except DuplicateError:
                # confirm_checkout and the webhook raced to insert it; the
                # other one won. Fall through and merge into its row.
                existing = await self._repository.get_payment_by_provider_id(
                    provider_payment.provider_payment_id, for_update=True
                )
                if existing is None:
                    raise

        # Back-fill links the first writer didn't know (e.g. a payment-only
        # webhook arrived before the order was resolvable).
        links: dict[str, object] = {}
        if existing.order_id is None and order_id is not None:
            links["order_id"] = order_id
        if existing.subscription_id is None and subscription_id is not None:
            links["subscription_id"] = subscription_id

        if not can_transition(existing.status, provider_payment.status):
            logger.info(
                "ignoring payment status transition %s -> %s for %s",
                existing.status,
                provider_payment.status,
                existing.provider_payment_id,
            )
            if links:
                return await self._repository.update_payment(
                    existing.model_copy(update={**links, "updated_at": datetime.now(UTC)})
                )
            return existing

        merged = existing.model_copy(
            update={
                **links,
                "status": provider_payment.status,
                "amount_refunded": max(existing.amount_refunded, provider_payment.amount_refunded),
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

    async def get_order_by_provider_id(self, provider_order_id: str) -> Order | None:
        return await self._repository.get_order_by_provider_id(provider_order_id)

    async def get_payment(self, payment_id: str) -> Payment | None:
        return await self._repository.get_payment(payment_id)

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
        remaining = payment.amount.amount - payment.amount_refunded
        if amount is not None:
            if amount.currency != payment.amount.currency:
                raise InvalidStateError(
                    f"refund currency {amount.currency} != payment currency "
                    f"{payment.amount.currency}"
                )
            if amount.amount > remaining:
                raise InvalidStateError(
                    f"refund of {amount.amount} exceeds refundable balance {remaining}"
                )

        # The provider call stays outside any DB transaction: holding a row
        # lock across a network round-trip would serialize unrelated work.
        # The provider itself rejects over-refunds if two requests race here.
        provider_refund = await self._provider.refund_payment(
            payment.provider_payment_id, amount, reason=reason, metadata=metadata or {}
        )

        async with self._repository.transaction():
            # Re-read under lock: a webhook or another refund may have
            # updated amount_refunded since the read above.
            locked = await self._repository.get_payment(payment_id, for_update=True)
            if locked is None:  # pragma: no cover - deleted mid-flight
                raise NotFoundError(f"no payment with id {payment_id!r}")

            refund = await self._repository.get_refund_by_provider_id(
                provider_refund.provider_refund_id
            )
            already_recorded = refund is not None
            if refund is None:
                try:
                    refund = await self._repository.create_refund(
                        Refund(
                            id=str(uuid4()),
                            provider_refund_id=provider_refund.provider_refund_id,
                            payment_id=locked.id,
                            amount=provider_refund.amount,
                            status=provider_refund.status,
                            reason=reason,
                            created_at=provider_refund.created_at,
                        )
                    )
                except DuplicateError:
                    refund = await self._repository.get_refund_by_provider_id(
                        provider_refund.provider_refund_id
                    )
                    if refund is None:
                        raise
                    already_recorded = True

            # If the refund webhook got here first, its payment entity already
            # carried the provider's authoritative amount_refunded - adding
            # ours on top would double-count.
            if not already_recorded:
                new_amount_refunded = min(
                    locked.amount_refunded + provider_refund.amount.amount,
                    locked.amount.amount,
                )
                new_status = (
                    PaymentStatus.REFUNDED
                    if new_amount_refunded >= locked.amount.amount
                    else PaymentStatus.PARTIALLY_REFUNDED
                )
                if can_transition(locked.status, new_status):
                    await self._repository.update_payment(
                        locked.model_copy(
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
        async with self._repository.transaction():
            return await self._upsert_payment_from_provider(
                provider_payment,
                order_id=payment.order_id,
                subscription_id=payment.subscription_id,
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

        final_event: PaymentEvent | None = None
        for attempt in range(2):
            try:
                async with self._repository.transaction():
                    if not await self._repository.record_event_if_new(event.id):
                        return
                    final_event = await self._reconcile_event(event)
                break
            except DuplicateError:
                # Two *different* events about a brand-new object (e.g.
                # payment.authorized + payment.captured delivered together)
                # raced to insert it. Our transaction - including the dedupe
                # record - rolled back; one retry sees the winner's row.
                if attempt == 1:
                    raise
        assert final_event is not None

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
        if (
            payment is not None
            and payment.order_id is not None
            and payment.status in _PAID_PAYMENT_STATUSES
        ):
            if order is None or order.id != payment.order_id:
                linked = await self._repository.get_order(payment.order_id)
                if linked is not None:
                    order = await self._repository.get_order_by_provider_id(
                        linked.provider_order_id, for_update=True
                    )
            if order is not None:
                if payment.amount != order.amount:
                    # Never mark an order PAID for a different amount than
                    # was priced server-side (e.g. a partial-payment config).
                    logger.error(
                        "payment %s amount %r != order %s amount %r; not marking order paid",
                        payment.id,
                        payment.amount,
                        order.id,
                        order.amount,
                    )
                else:
                    order = await self._maybe_update_order_status(
                        order, new_status=OrderStatus.PAID
                    )

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
        # Webhook placeholders carry *provider* ids in order_id /
        # subscription_id. Payment-only payloads (`contains: ["payment"]`)
        # are common, so resolve the links ourselves instead of relying on
        # an order/subscription entity being present in the same payload.
        if order is None and placeholder.order_id is not None:
            order = await self._repository.get_order_by_provider_id(
                placeholder.order_id, for_update=True
            )
        if subscription is None and placeholder.subscription_id is not None:
            subscription = await self._repository.get_subscription_by_provider_id(
                placeholder.subscription_id
            )
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

    async def reconcile(self, since: datetime, *, limit: int = 500) -> int:
        """Re-fetch non-final orders/payments from the provider and fix their
        state - a safety net for missed webhooks. Run it on a schedule.

        Processes at most `limit` orders and `limit` payments per call (oldest
        first) so one run's memory and provider-API usage stay bounded; call
        again until it returns 0 to drain a backlog. One object failing (e.g.
        a provider 5xx) is logged and skipped rather than aborting the run.
        Only dispatches events for payments whose status actually changed.
        Returns the number of objects updated.
        """
        updated_count = 0
        for order in await self._repository.list_non_final_orders(since, limit=limit):
            try:
                provider_order = await self._provider.fetch_order(order.provider_order_id)
            except Exception:
                logger.exception("reconcile: fetching order %s failed", order.id)
                continue
            async with self._repository.transaction():
                current = await self._repository.get_order_by_provider_id(
                    order.provider_order_id, for_update=True
                )
                if (
                    current is None
                    or provider_order.status == current.status
                    or not can_transition(current.status, provider_order.status)
                ):
                    continue
                await self._repository.update_order(
                    current.model_copy(
                        update={"status": provider_order.status, "updated_at": datetime.now(UTC)}
                    )
                )
                updated_count += 1

        for payment in await self._repository.list_non_final_payments(since, limit=limit):
            try:
                provider_payment = await self._provider.fetch_payment(payment.provider_payment_id)
            except Exception:
                logger.exception("reconcile: fetching payment %s failed", payment.id)
                continue
            async with self._repository.transaction():
                current_payment = await self._repository.get_payment(payment.id, for_update=True)
                if (
                    current_payment is None
                    or provider_payment.status == current_payment.status
                    or not can_transition(current_payment.status, provider_payment.status)
                ):
                    continue
                updated = await self._upsert_payment_from_provider(
                    provider_payment,
                    order_id=current_payment.order_id,
                    subscription_id=current_payment.subscription_id,
                )
                if updated.order_id is not None and updated.status in _PAID_PAYMENT_STATUSES:
                    linked = await self._repository.get_order(updated.order_id)
                    if linked is not None and linked.amount == updated.amount:
                        await self._maybe_update_order_status(
                            linked, new_status=OrderStatus.PAID
                        )
            updated_count += 1
            event = PaymentEvent(
                id=f"reconcile:{updated.id}:{updated.status.value}",
                type=f"payment.{updated.status.value}",
                provider=self._provider.name,
                occurred_at=datetime.now(UTC),
                payment=updated,
                raw={},
            )
            await self.events.dispatch(event)
        return updated_count

    async def prune_processed_events(
        self, *, older_than: timedelta = DEFAULT_EVENT_RETENTION
    ) -> int:
        """Delete webhook-dedupe records older than `older_than` so the table
        doesn't grow forever. Schedule it (e.g. daily) alongside reconcile.
        Returns the number of records deleted."""
        if older_than < timedelta(days=2):
            raise ValueError("older_than must exceed the provider's redelivery window (2 days+)")
        return await self._repository.prune_processed_events(datetime.now(UTC) - older_than)


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


def engine_options(settings: PayKitSettings) -> dict[str, object]:
    """`create_async_engine` kwargs from settings. Pool sizing only applies
    to server databases; SQLite (tests/dev) keeps SQLAlchemy's defaults."""
    if settings.database_url is None or settings.database_url.startswith("sqlite"):
        return {}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle_seconds,
        "pool_pre_ping": True,  # survive DB failovers / idle-connection reaping
    }


def build_service_from_env(*, settings: PayKitSettings | None = None) -> PaymentService:
    """Wire a PaymentService from PAYKIT_* environment variables."""
    settings = settings or PayKitSettings()

    from paykit.providers import get_provider

    provider = get_provider(settings.provider, settings)

    repository: PaymentRepository
    if settings.database_url:
        from sqlalchemy.ext.asyncio import create_async_engine

        from paykit.storage.sqlalchemy.repository import SQLAlchemyRepository

        engine = create_async_engine(settings.database_url, **engine_options(settings))
        repository = SQLAlchemyRepository(engine, table_prefix=settings.table_prefix)
    else:
        from paykit.storage.memory import InMemoryRepository

        if (settings.razorpay_key_id or "").startswith("rzp_live_"):
            logger.error(
                "LIVE Razorpay key with in-memory storage (PAYKIT_DATABASE_URL unset): a "
                "restart between checkout and webhook loses the order - customers can be "
                "charged and never granted. Set PAYKIT_DATABASE_URL and run `paykit db upgrade`."
            )
        repository = InMemoryRepository()

    return PaymentService(provider=provider, repository=repository, events=EventBus())
