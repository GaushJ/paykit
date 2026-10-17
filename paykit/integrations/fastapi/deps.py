"""Shared types/defaults for `create_payments_router`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from paykit.core.errors import ConfigurationError
from paykit.core.models import Money

# `request_body` is the create-order request as a dict (including whatever
# app-specific fields it carried, e.g. {"plan": "pro"}); `user` is whatever
# `auth_dependency` returned.
AmountResolver = Callable[[dict[str, Any], Any], Money]

# Called as customer_ref_getter(user, request_body). The embedded (§9) case
# ignores request_body and derives customer_ref from the logged-in `user`;
# the standalone-service (§10) case ignores `user` (just an API key) and
# reads customer_ref from request_body, since the caller is a trusted
# backend passing it explicitly per call. For read/cancel routes, which
# have no create-time body, request_body is `{}`.
CustomerRefGetter = Callable[[Any, dict[str, Any]], str]


def disabled_amount_resolver(request_body: dict[str, Any], user: Any) -> Money:
    """Default `amount_resolver` - refuses to run.

    Order amounts must be decided server-side and never trusted from the
    client (§9). Pass `amount_resolver=lambda body, user: Money(...)` to
    `create_payments_router()`.
    """
    raise ConfigurationError(
        "create_payments_router() was not given an amount_resolver. Amounts for "
        "orders must be decided server-side - pass "
        "amount_resolver=lambda request_body, user: Money(...)."
    )
