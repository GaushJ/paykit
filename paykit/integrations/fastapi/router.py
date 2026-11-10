"""`create_payments_router(service, ...) -> APIRouter` (§9)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from paykit.core.errors import (
    AmountMismatchError,
    ConfigurationError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    ProviderError,
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

logger = logging.getLogger("paykit.integrations.fastapi")

DEFAULT_MAX_WEBHOOK_BODY_BYTES = 256 * 1024


def _provider_unavailable(exc: ProviderError) -> HTTPException:
    # Log the provider's detail server-side; don't echo gateway internals
    # (error bodies, request ids) back to end users.
    logger.warning("payment provider error: %s (code=%s)", exc, exc.code)
    return HTTPException(status_code=502, detail="payment provider error")


async def _read_capped_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise HTTPException(status_code=413, detail="request body too large")
    # Enforce the cap while streaming as well: Content-Length can be absent
    # (chunked transfer encoding) or wrong.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


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
    max_webhook_body_bytes: int = DEFAULT_MAX_WEBHOOK_BODY_BYTES,
) -> APIRouter:
    """Build the payments router.

    `enforce_ownership=False` skips the "is this the caller's own
    order/subscription" checks - for the standalone service (§10), where the
    caller is a trusted backend passing `customer_ref` explicitly per call,
    not a single logged-in end user.

    The refund route is only mounted when `admin_dependency` is given, or
    when `enforce_ownership=False` (trusted-backend mode). Otherwise any
    logged-in end user could refund any payment.
    """
    router = APIRouter(prefix=prefix, tags=tags or ["payments"])

    def _caller_ref(user: Any) -> str | None:
        return customer_ref_getter(user, {}) if enforce_ownership else None

    @router.post("/orders", response_model=CreateOrderResponse)
    async def create_order_route(
        body: CreateOrderRequest, user: Any = Depends(auth_dependency)
    ) -> CreateOrderResponse:
        request_body = body.model_dump()
        try:
            amount = amount_resolver(request_body, user)
            customer_ref = customer_ref_getter(user, request_body)
        except ConfigurationError:
            raise
        except ValueError as exc:  # includes pydantic ValidationError (e.g. bad Money)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            order = await service.create_order(
                amount,
                customer_ref=customer_ref,
                receipt=body.receipt,
                metadata=body.metadata,
                idempotency_key=body.idempotency_key,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AmountMismatchError as exc:
            logger.error("provider order amount mismatch: %s", exc)
            raise HTTPException(status_code=502, detail="payment provider error") from exc
        except ProviderError as exc:
            raise _provider_unavailable(exc) from exc

        checkout = service.checkout_config(order, prefill=body.prefill)
        return CreateOrderResponse(order=OrderOut.from_domain(order), checkout=checkout)

    @router.post("/confirm", response_model=PaymentOut)
    async def confirm_checkout_route(
        body: CheckoutResult, user: Any = Depends(auth_dependency)
    ) -> PaymentOut:
        try:
            payment = await service.confirm_checkout(body, customer_ref=_caller_ref(user))
        except SignatureVerificationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="order not found") from exc
        except AmountMismatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProviderError as exc:
            raise _provider_unavailable(exc) from exc
        return PaymentOut.from_domain(payment)

    @router.get("/orders/{order_id}", response_model=OrderDetailResponse)
    async def get_order_route(
        order_id: str, user: Any = Depends(auth_dependency)
    ) -> OrderDetailResponse:
        order = await service.get_order(order_id)
        owns_it = not enforce_ownership or (
            order is not None and order.customer_ref == _caller_ref(user)
        )
        if order is None or not owns_it:
            raise HTTPException(status_code=404, detail="order not found")
        payments = await service.list_payments_for_order(order_id)
        return OrderDetailResponse(
            order=OrderOut.from_domain(order),
            payments=[PaymentOut.from_domain(p) for p in payments],
        )

    if admin_dependency is not None or not enforce_ownership:
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
            except ProviderError as exc:
                raise _provider_unavailable(exc) from exc
            return RefundOut.from_domain(refund)

    @router.post("/subscriptions", response_model=CreateSubscriptionResponse)
    async def create_subscription_route(
        body: CreateSubscriptionRequest, user: Any = Depends(auth_dependency)
    ) -> CreateSubscriptionResponse:
        try:
            customer_ref = customer_ref_getter(user, body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            subscription = await service.create_subscription(
                body.plan_id,
                customer_ref=customer_ref,
                total_count=body.total_count,
                customer_notify=body.customer_notify,
                metadata=body.metadata,
                start_at=body.start_at,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProviderError as exc:
            raise _provider_unavailable(exc) from exc
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
            subscription is not None and subscription.customer_ref == _caller_ref(user)
        )
        if subscription is None or not owns_it:
            raise HTTPException(status_code=404, detail="subscription not found")
        try:
            cancelled = await service.cancel_subscription(
                subscription_id, at_cycle_end=body.at_cycle_end
            )
        except ProviderError as exc:
            raise _provider_unavailable(exc) from exc
        return SubscriptionOut.from_domain(cancelled)

    @router.post(f"/webhooks/{service.provider_name}", status_code=200)
    async def webhook_route(request: Request) -> Response:
        raw_body = await _read_capped_body(request, max_webhook_body_bytes)
        try:
            await service.handle_webhook(raw_body, dict(request.headers))
        except SignatureVerificationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(status_code=200)

    return router
