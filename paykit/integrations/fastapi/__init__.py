"""FastAPI integration: `create_payments_router` + `paykit_lifespan`."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from paykit.core.service import PaymentService
from paykit.integrations.fastapi.router import create_payments_router

__all__ = ["create_payments_router", "paykit_lifespan"]


def paykit_lifespan(
    service: PaymentService,
) -> Callable[[object], AbstractAsyncContextManager[None]]:
    """`FastAPI(lifespan=paykit_lifespan(service))` - closes the provider's
    HTTP client and (if using SQLAlchemy) the DB engine on shutdown."""

    @asynccontextmanager
    async def _lifespan(app: object) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await service.aclose()

    return _lifespan
