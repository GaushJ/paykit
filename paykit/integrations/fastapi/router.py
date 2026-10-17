"""`create_payments_router(service, ...) -> APIRouter` (§9)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from paykit.core.errors import (
    AmountMismatchError,
    ConfigurationError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    SignatureVerificationError,
)
from paykit.core.models import CheckoutResult
from paykit.core.service import PaymentService
from paykit.integrations.fastapi.deps import (
    AmountResolver,
    CustomerRefGetter,
    disabled_amount_resolver,
)
from paykit.integrations.fastapi.schemas import (
    CancelSubscriptionRequest,
    CreateOrderRequest,
    CreateOrderResponse,
    CreateSubscriptionRequest,
    CreateSubscriptionResponse,
    OrderDetailResponse,
    OrderOut,
    PaymentOut,
    RefundOut,
    RefundRequest,
    SubscriptionOut,
)


def create_payments_router(
    service: PaymentService,
    *,
    prefix: str = "/payments",
    auth_dependency: Callable[..., Any],
    customer_ref_getter: CustomerRefGetter,
    amount_resolver: AmountResolver = disabled_amount_resolver,
    admin_dependency: Callable[..., Any] | None = None,
    enforce_ownership: bool = True,
    tags: list[str | Any] | None = None,
) -> APIRouter:
    """`enforce_ownership=False` skips the "is this the caller's own
    order/subscription" check on the read/cancel routes - for the standalone
    service (§10), the caller is a trusted backend passing `customer_ref`
    explicitly per call, not a single logged-in end user."""
    router = APIRouter(prefix=prefix, tags=tags or ["payments"])

    @router.post("/orders", response_model=CreateOrderResponse)
    async def create_order_route(
        body: CreateOrderRequest, user: Any = Depends(auth_dependency)
    ) -> CreateOrderResponse:
        try:
            request_body = body.model_dump()
            amount = amount_resolver(request_body, user)
            order = await service.create_order(
                amount,
                customer_ref=customer_ref_getter(user, request_body),
                receipt=body.receipt,
                metadata=body.metadata,
                idempotency_key=body.idempotency_key,
            )
        except ConfigurationError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        checkout = service.checkout_config(order, prefill=body.prefill)
        return CreateOrderResponse(order=OrderOut.from_domain(order), checkout=checkout)

    @router.post("/confirm", response_model=PaymentOut)
    async def confirm_checkout_route(
        body: CheckoutResult, user: Any = Depends(auth_dependency)
    ) -> PaymentOut:
        try:
            payment = await service.confirm_checkout(body)
        except SignatureVerificationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AmountMismatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return PaymentOut.from_domain(payment)

    @router.get("/orders/{order_id}", response_model=OrderDetailResponse)
    async def get_order_route(
        order_id: str, user: Any = Depends(auth_dependency)
    ) -> OrderDetailResponse:
        order = await service.get_order(order_id)
        owns_it = not enforce_ownership or (
            order is not None and order.customer_ref == customer_ref_getter(user, {})
        )
        if order is None or not owns_it:
            raise HTTPException(status_code=404, detail="order not found")
        payments = await service.list_payments_for_order(order_id)
        return OrderDetailResponse(
            order=OrderOut.from_domain(order),
            payments=[PaymentOut.from_domain(p) for p in payments],
        )

    refund_dependencies = [Depends(admin_dependency)] if admin_dependency is not None else []

    @router.post(
        "/{payment_id}/refunds", response_model=RefundOut, dependencies=refund_dependencies
    )
    async def refund_route(
        payment_id: str, body: RefundRequest, user: Any = Depends(auth_dependency)
    ) -> RefundOut:
        amount = body.amount.to_money() if body.amount else None
        try:
            refund = await service.refund(
                payment_id, amount, reason=body.reason, metadata=body.metadata
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RefundOut.from_domain(refund)

    @router.post("/subscriptions", response_model=CreateSubscriptionResponse)
    async def create_subscription_route(
        body: CreateSubscriptionRequest, user: Any = Depends(auth_dependency)
    ) -> CreateSubscriptionResponse:
        try:
            subscription = await service.create_subscription(
                body.plan_id,
                customer_ref=customer_ref_getter(user, body.model_dump()),
                total_count=body.total_count,
                customer_notify=body.customer_notify,
                metadata=body.metadata,
                start_at=body.start_at,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return CreateSubscriptionResponse(
            subscription=SubscriptionOut.from_domain(subscription),
            short_url=subscription.short_url,
        )

    @router.post("/subscriptions/{subscription_id}/cancel", response_model=SubscriptionOut)
    async def cancel_subscription_route(
        subscription_id: str,
        body: CancelSubscriptionRequest,
        user: Any = Depends(auth_dependency),
    ) -> SubscriptionOut:
        subscription = await service.get_subscription(subscription_id)
        owns_it = not enforce_ownership or (
            subscription is not None
            and subscription.customer_ref == customer_ref_getter(user, {})
        )
        if subscription is None or not owns_it:
            raise HTTPException(status_code=404, detail="subscription not found")
        cancelled = await service.cancel_subscription(
            subscription_id, at_cycle_end=body.at_cycle_end
        )
        return SubscriptionOut.from_domain(cancelled)

    @router.post(f"/webhooks/{service.provider_name}", status_code=200)
    async def webhook_route(request: Request) -> Response:
        raw_body = await request.body()
        try:
            await service.handle_webhook(raw_body, dict(request.headers))
        except SignatureVerificationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(status_code=200)

    return router
