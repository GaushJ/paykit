import pytest

from paykit.config import PayKitSettings
from paykit.core.errors import ConfigurationError
from paykit.providers import get_provider
from paykit.providers.dummy import DummyProvider
from paykit.providers.razorpay.adapter import RazorpayProvider


def make_settings(**overrides: object) -> PayKitSettings:
    defaults: dict[str, object] = dict(
        razorpay_key_id="rzp_test_x",
        razorpay_key_secret="secret",
        razorpay_webhook_secret="whsecret",
    )
    defaults.update(overrides)
    return PayKitSettings(_env_file=None, **defaults)  # type: ignore[arg-type,call-arg]


def test_razorpay_provider() -> None:
    provider = get_provider("razorpay", make_settings())
    assert isinstance(provider, RazorpayProvider)
    assert provider.name == "razorpay"


def test_dummy_provider() -> None:
    provider = get_provider("dummy", make_settings())
    assert isinstance(provider, DummyProvider)
    assert provider.name == "dummy"


def test_unknown_provider_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        get_provider("some_unknown_gateway", make_settings())
