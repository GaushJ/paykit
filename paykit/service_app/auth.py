"""API-key auth for the standalone service's client backends (§10).

`Authorization: Bearer <key>`, keys from `PAYKIT_SERVICE_API_KEYS`
(comma-separated), compared in constant time.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Header, HTTPException

from paykit.config import PayKitSettings
from paykit.core.errors import ConfigurationError


def make_api_key_auth(settings: PayKitSettings) -> Callable[..., Coroutine[Any, Any, str]]:
    valid_keys = settings.service_api_keys_list
    if not valid_keys:
        raise ConfigurationError(
            "PAYKIT_SERVICE_API_KEYS must be set (comma-separated) to run the standalone service"
        )

    async def verify_api_key(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401, detail="missing or malformed Authorization header"
            )
        key = authorization.removeprefix("Bearer ").strip()
        for valid_key in valid_keys:
            if hmac.compare_digest(key, valid_key):
                return key
        raise HTTPException(status_code=401, detail="invalid API key")

    return verify_api_key
