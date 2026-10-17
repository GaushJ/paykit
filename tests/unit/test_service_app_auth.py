import pytest
from fastapi import HTTPException

from paykit.config import PayKitSettings
from paykit.core.errors import ConfigurationError
from paykit.service_app.auth import make_api_key_auth


def make_settings(**overrides: object) -> PayKitSettings:
    defaults: dict[str, object] = dict(
        razorpay_key_id="rzp_test_x",
        razorpay_key_secret="secret",
        razorpay_webhook_secret="whsecret",
        service_api_keys="key1,key2",
    )
    defaults.update(overrides)
    return PayKitSettings(_env_file=None, **defaults)  # type: ignore[arg-type,call-arg]


async def test_valid_key_returns_key() -> None:
    verify = make_api_key_auth(make_settings())
    result = await verify(authorization="Bearer key1")
    assert result == "key1"


async def test_invalid_key_raises_401() -> None:
    verify = make_api_key_auth(make_settings())
    with pytest.raises(HTTPException) as exc_info:
        await verify(authorization="Bearer not-a-real-key")
    assert exc_info.value.status_code == 401


async def test_missing_header_raises_401() -> None:
    verify = make_api_key_auth(make_settings())
    with pytest.raises(HTTPException) as exc_info:
        await verify(authorization=None)
    assert exc_info.value.status_code == 401


async def test_malformed_header_raises_401() -> None:
    verify = make_api_key_auth(make_settings())
    with pytest.raises(HTTPException) as exc_info:
        await verify(authorization="key1")  # missing "Bearer " prefix
    assert exc_info.value.status_code == 401


def test_no_configured_keys_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        make_api_key_auth(make_settings(service_api_keys=None))
