"""Thin async REST client for the Razorpay API.

- Auth: HTTP Basic with `key_id:key_secret`.
- One shared `httpx.AsyncClient`; call `aclose()` on shutdown.
- Retries with exponential backoff on network errors, 429, and 5xx - but
  only for GET (idempotent). POSTs that create money-moving objects are
  never auto-retried; order creation is protected by our own idempotency
  key instead (see core/service.py).
- Razorpay error bodies (`{"error": {...}}`) are mapped to `ProviderError`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

import httpx

from paykit.core.errors import ProviderError

logger = logging.getLogger("paykit.providers.razorpay")

DEFAULT_BASE_URL = "https://api.razorpay.com/v1"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

JSONDict = dict[str, object]
QueryParams = Mapping[str, str | int | float | bool | None]


class RazorpayClient:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 15,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=(key_id, key_secret),
            timeout=timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, *, params: QueryParams | None = None) -> JSONDict:
        return await self._request("GET", path, params=params, retryable=True)

    async def post(self, path: str, *, json: JSONDict | None = None) -> JSONDict:
        return await self._request("POST", path, json=json, retryable=False)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json: JSONDict | None = None,
        retryable: bool,
    ) -> JSONDict:
        attempts = self._max_retries + 1 if retryable else 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                last_exc = exc
                if not retryable or attempt == attempts - 1:
                    raise ProviderError(
                        f"network error calling Razorpay {method} {path}: {exc}"
                    ) from exc
                await self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                if (
                    retryable
                    and response.status_code in RETRYABLE_STATUS_CODES
                    and attempt < attempts - 1
                ):
                    await self._sleep_backoff(attempt)
                    continue
                raise self._map_error(response)

            if not response.content:
                return {}
            result: JSONDict = response.json()
            return result

        # Unreachable in practice (loop always returns or raises), but keeps mypy happy.
        assert last_exc is not None
        raise ProviderError(f"exhausted retries calling Razorpay {method} {path}") from last_exc

    async def _sleep_backoff(self, attempt: int) -> None:
        await asyncio.sleep(self._backoff_base_seconds * (2**attempt))

    def _map_error(self, response: httpx.Response) -> ProviderError:
        body: JSONDict = {}
        try:
            body = response.json()
        except ValueError:
            pass

        error = body.get("error", {}) if isinstance(body, dict) else {}
        error = error if isinstance(error, dict) else {}
        code = error.get("code")
        description = error.get("description") or response.text[:200]
        return ProviderError(
            description or f"Razorpay API error (HTTP {response.status_code})",
            code=code,
            description=description,
            http_status=response.status_code,
            provider_response=body,
        )
