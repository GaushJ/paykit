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
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from paykit.config import PayKitSettings
from paykit.core.models import Money
from paykit.core.service import build_service_from_env
from paykit.integrations.fastapi import create_payments_router, paykit_lifespan
from paykit.service_app.auth import make_api_key_auth
from paykit.service_app.outbound import OutboundWebhookSender


def resolve_amount_from_body(request_body: dict[str, object], user: object) -> Money:
    """`amount_resolver` for the standalone service: unlike the embedded
    integration, the caller here is a trusted backend that passes
    amount/currency explicitly per call (§10)."""
    if "amount" not in request_body:
        raise ValueError("request body must include an integer 'amount' (minor units)")
    amount = request_body["amount"]
    currency = request_body.get("currency", "INR")
    if not isinstance(amount, int) or not isinstance(currency, str):
        raise ValueError("'amount' must be an int and 'currency' a string")
    return Money(amount=amount, currency=currency)


def customer_ref_from_body(user: object, request_body: dict[str, object]) -> str:
    """`customer_ref_getter` for the standalone service: read from the
    request body rather than the API key, since one API key belongs to a
    whole backend serving many end users."""
    customer_ref = request_body.get("customer_ref")
    if not customer_ref or not isinstance(customer_ref, str):
        raise ValueError("request body must include a 'customer_ref' string")
    return customer_ref


def create_app(*, settings: PayKitSettings | None = None) -> FastAPI:
    settings = settings or PayKitSettings()  # type: ignore[call-arg]
    service = build_service_from_env(settings=settings)
    verify_api_key = make_api_key_auth(settings)

    sender: OutboundWebhookSender | None = None
    if settings.outbound_webhook_url and settings.outbound_webhook_secret:
        repository = None
        if settings.database_url:
            from sqlalchemy.ext.asyncio import create_async_engine

            from paykit.service_app.storage import SQLAlchemyOutboundDeliveryRepository

            engine = create_async_engine(settings.database_url)
            repository = SQLAlchemyOutboundDeliveryRepository(
                engine, table_prefix=settings.table_prefix
            )
        sender = OutboundWebhookSender(
            url=settings.outbound_webhook_url,
            secret=settings.outbound_webhook_secret.get_secret_value(),
            repository=repository,
        )
        service.events.subscribe("*", sender.handle_event)

    base_lifespan = paykit_lifespan(service)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        worker_task = asyncio.create_task(sender.run_worker()) if sender is not None else None
        try:
            async with base_lifespan(app):
                yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
            if sender is not None:
                await sender.aclose()

    app = FastAPI(title="paykit-service", lifespan=lifespan)
    app.state.paykit_service = service  # for introspection (tests, admin tooling)
    app.include_router(
        create_payments_router(
            service,
            auth_dependency=verify_api_key,
            customer_ref_getter=customer_ref_from_body,
            amount_resolver=resolve_amount_from_body,
            enforce_ownership=False,
        )
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
