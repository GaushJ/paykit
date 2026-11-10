"""Standalone deployable paykit service (§10) - lets backends in any
language (Node, Go, ...) use paykit over HTTP, authenticated by API key,
with outbound signed webhooks notifying them of events.

Run:

    PAYKIT_RAZORPAY_KEY_ID=... PAYKIT_RAZORPAY_KEY_SECRET=... \
    PAYKIT_RAZORPAY_WEBHOOK_SECRET=... PAYKIT_SERVICE_API_KEYS=key1,key2 \
    uvicorn paykit.service_app.main:app
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from paykit.config import PayKitSettings
from paykit.core.models import Money
from paykit.core.service import build_service_from_env
from paykit.integrations.fastapi import create_payments_router, paykit_lifespan
from paykit.service_app.auth import make_api_key_auth
from paykit.service_app.outbound import OutboundWebhookSender


def make_amount_resolver(default_currency: str) -> Callable[[dict[str, object], object], Money]:
    """`amount_resolver` for the standalone service: unlike the embedded
    integration, the caller here is a trusted backend that passes
    amount/currency explicitly per call (§10)."""

    def resolve_amount_from_body(request_body: dict[str, object], user: object) -> Money:
        if "amount" not in request_body:
            raise ValueError("request body must include an integer 'amount' (minor units)")
        amount = request_body["amount"]
        currency = request_body.get("currency", default_currency)
        # bool is an int subclass; reject it explicitly.
        if isinstance(amount, bool) or not isinstance(amount, int) or not isinstance(currency, str):
            raise ValueError("'amount' must be an int and 'currency' a string")
        return Money(amount=amount, currency=currency)

    return resolve_amount_from_body


resolve_amount_from_body = make_amount_resolver("INR")


def customer_ref_from_body(user: object, request_body: dict[str, object]) -> str:
    """`customer_ref_getter` for the standalone service: read from the
    request body rather than the API key, since one API key belongs to a
    whole backend serving many end users."""
    customer_ref = request_body.get("customer_ref")
    if not customer_ref or not isinstance(customer_ref, str) or len(customer_ref) > 128:
        raise ValueError("request body must include a 'customer_ref' string (max 128 chars)")
    return customer_ref


def create_app(*, settings: PayKitSettings | None = None) -> FastAPI:
    settings = settings or PayKitSettings()
    service = build_service_from_env(settings=settings)
    verify_api_key = make_api_key_auth(settings)

    sender: OutboundWebhookSender | None = None
    if settings.outbound_webhook_url and settings.outbound_webhook_secret:
        delivery_repository = None
        engine = getattr(service.repository, "engine", None)
        if engine is not None:
            from paykit.service_app.storage import SQLAlchemyOutboundDeliveryRepository

            # Share the payment repository's engine: one connection pool per
            # process, sized by PAYKIT_DB_POOL_SIZE, instead of two.
            delivery_repository = SQLAlchemyOutboundDeliveryRepository(
                engine, table_prefix=settings.table_prefix
            )
        sender = OutboundWebhookSender(
            url=settings.outbound_webhook_url,
            secret=settings.outbound_webhook_secret.get_secret_value(),
            repository=delivery_repository,
        )
        service.events.subscribe("*", sender.handle_event)

    base_lifespan = paykit_lifespan(service)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with base_lifespan(app):  # on exit: closes provider client + DB pool
            worker_task = (
                asyncio.create_task(sender.run_worker()) if sender is not None else None
            )
            try:
                yield
            finally:
                # Stop the worker *before* the shared DB pool is disposed.
                if worker_task is not None:
                    worker_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker_task
                if sender is not None:
                    await sender.aclose()

    app = FastAPI(title="paykit-service", lifespan=lifespan)
    app.state.paykit_service = service  # for introspection (tests, admin tooling)
    app.include_router(
        create_payments_router(
            service,
            auth_dependency=verify_api_key,
            customer_ref_getter=customer_ref_from_body,
            amount_resolver=make_amount_resolver(settings.default_currency),
            enforce_ownership=False,
        )
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Readiness: the database (if configured) is reachable."""
        engine = getattr(service.repository, "engine", None)
        if engine is not None:
            from sqlalchemy import text

            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except Exception as exc:
                raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ready"}

    return app


app = create_app()
