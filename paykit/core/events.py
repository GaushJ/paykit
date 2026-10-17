"""EventBus: apps subscribe to normalized PaymentEvents via handlers, not imports."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from paykit.core.models import PaymentEvent

logger = logging.getLogger("paykit.events")

Handler = Callable[[PaymentEvent], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event_type: str) -> Callable[[Handler], Handler]:
        """Decorator: `@bus.on("payment.captured")`. `event_type="*"` runs
        for every event regardless of type (e.g. the standalone service's
        outbound-webhook forwarder, §10)."""

        def register(handler: Handler) -> Handler:
            self._handlers[event_type].append(handler)
            return handler

        return register

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def dispatch(self, event: PaymentEvent) -> None:
        """Run all handlers for `event.type`, plus any "*" handlers. A
        handler exception is logged, never raised.

        Callers (webhook routes) must return 2xx even if a handler fails,
        otherwise the provider will retry and events get double-applied.
        """
        handlers = self._handlers.get(event.type, []) + self._handlers.get("*", [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "handler %r failed for event %s (%s)", handler, event.id, event.type
                )
