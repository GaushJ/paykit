import httpx
import pytest
import respx

from paykit.core.errors import ProviderError
from paykit.providers.razorpay.client import RazorpayClient

BASE_URL = "https://api.razorpay.com/v1"


def make_client(**kwargs: object) -> RazorpayClient:
    return RazorpayClient(
        "rzp_test_key",
        "secret",
        base_url=BASE_URL,
        backoff_base_seconds=0.001,
        **kwargs,  # type: ignore[arg-type]
    )


@respx.mock
async def test_get_returns_parsed_json() -> None:
    respx.get(f"{BASE_URL}/orders/order_1").mock(
        return_value=httpx.Response(200, json={"id": "order_1", "status": "created"})
    )
    client = make_client()
    try:
        result = await client.get("/orders/order_1")
    finally:
        await client.aclose()

    assert result == {"id": "order_1", "status": "created"}


@respx.mock
async def test_post_returns_parsed_json() -> None:
    respx.post(f"{BASE_URL}/orders").mock(
        return_value=httpx.Response(200, json={"id": "order_1"})
    )
    client = make_client()
    try:
        result = await client.post("/orders", json={"amount": 100, "currency": "INR"})
    finally:
        await client.aclose()

    assert result == {"id": "order_1"}


@respx.mock
async def test_request_uses_basic_auth() -> None:
    route = respx.get(f"{BASE_URL}/orders/order_1").mock(
        return_value=httpx.Response(200, json={})
    )
    client = make_client()
    try:
        await client.get("/orders/order_1")
    finally:
        await client.aclose()

    assert route.calls.last.request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_error_body_maps_to_provider_error() -> None:
    respx.get(f"{BASE_URL}/orders/bad").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "BAD_REQUEST_ERROR",
                    "description": "The id provided does not exist",
                    "field": None,
                }
            },
        )
    )
    client = make_client()
    try:
        with pytest.raises(ProviderError) as exc_info:
            await client.get("/orders/bad")
    finally:
        await client.aclose()

    err = exc_info.value
    assert err.code == "BAD_REQUEST_ERROR"
    assert err.description == "The id provided does not exist"
    assert err.http_status == 400
    assert err.provider_response is not None


@respx.mock
async def test_get_retries_on_5xx_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/orders/order_1")
    route.side_effect = [
        httpx.Response(500, json={"error": {"description": "boom"}}),
        httpx.Response(200, json={"id": "order_1"}),
    ]
    client = make_client()
    try:
        result = await client.get("/orders/order_1")
    finally:
        await client.aclose()

    assert result == {"id": "order_1"}
    assert route.call_count == 2


@respx.mock
async def test_get_retries_on_network_error_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/orders/order_1")
    route.side_effect = [httpx.ConnectError("boom"), httpx.Response(200, json={"id": "order_1"})]
    client = make_client()
    try:
        result = await client.get("/orders/order_1")
    finally:
        await client.aclose()

    assert result == {"id": "order_1"}


@respx.mock
async def test_get_exhausts_retries_and_raises() -> None:
    respx.get(f"{BASE_URL}/orders/order_1").mock(
        return_value=httpx.Response(503, json={"error": {"description": "down"}})
    )
    client = make_client(max_retries=2)
    try:
        with pytest.raises(ProviderError):
            await client.get("/orders/order_1")
    finally:
        await client.aclose()


@respx.mock
async def test_post_is_never_retried_on_5xx() -> None:
    route = respx.post(f"{BASE_URL}/orders").mock(
        return_value=httpx.Response(500, json={"error": {"description": "boom"}})
    )
    client = make_client()
    try:
        with pytest.raises(ProviderError):
            await client.post("/orders", json={"amount": 100})
    finally:
        await client.aclose()

    assert route.call_count == 1
