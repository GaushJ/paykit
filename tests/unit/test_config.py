import pytest
from pydantic import ValidationError

from paykit.config import PayKitSettings


@pytest.fixture(autouse=True)
def _clear_paykit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("PAYKIT_"):
            monkeypatch.delenv(key, raising=False)


def test_reads_required_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setenv("PAYKIT_RAZORPAY_WEBHOOK_SECRET", "whsecret")

    settings = PayKitSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.provider == "razorpay"
    assert settings.razorpay_key_id == "rzp_test_abc"
    assert settings.razorpay_key_secret.get_secret_value() == "secret"
    assert settings.default_currency == "INR"


def test_missing_required_setting_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_ID", "rzp_test_abc")
    with pytest.raises(ValidationError):
        PayKitSettings(_env_file=None)  # type: ignore[call-arg]


def test_service_api_keys_list_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setenv("PAYKIT_RAZORPAY_WEBHOOK_SECRET", "whsecret")
    monkeypatch.setenv("PAYKIT_SERVICE_API_KEYS", "key1, key2,key3")

    settings = PayKitSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.service_api_keys_list == ["key1", "key2", "key3"]


def test_rejects_live_key_with_insecure_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_ID", "rzp_live_abc")
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setenv("PAYKIT_RAZORPAY_WEBHOOK_SECRET", "whsecret")
    monkeypatch.setenv("PAYKIT_OUTBOUND_WEBHOOK_URL", "http://insecure.example.com/hook")
    monkeypatch.setenv("PAYKIT_OUTBOUND_WEBHOOK_SECRET", "out")

    with pytest.raises(ValidationError, match="HTTPS"):
        PayKitSettings(_env_file=None)  # type: ignore[call-arg]


def test_warns_on_test_key_with_insecure_webhook_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setenv("PAYKIT_RAZORPAY_WEBHOOK_SECRET", "whsecret")
    monkeypatch.setenv("PAYKIT_OUTBOUND_WEBHOOK_URL", "http://insecure.example.com/hook")
    monkeypatch.setenv("PAYKIT_OUTBOUND_WEBHOOK_SECRET", "out")

    with caplog.at_level("WARNING", logger="paykit"):
        PayKitSettings(_env_file=None)  # type: ignore[call-arg]

    assert any("HTTPS" in record.message for record in caplog.records)


def test_loopback_http_webhook_url_allowed_with_live_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_ID", "rzp_live_abc")
    monkeypatch.setenv("PAYKIT_RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setenv("PAYKIT_RAZORPAY_WEBHOOK_SECRET", "whsecret")
    monkeypatch.setenv("PAYKIT_OUTBOUND_WEBHOOK_URL", "http://localhost:3000/hook")
    monkeypatch.setenv("PAYKIT_OUTBOUND_WEBHOOK_SECRET", "out")

    PayKitSettings(_env_file=None)  # type: ignore[call-arg]


def test_outbound_url_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYKIT_PROVIDER", "dummy")
    monkeypatch.setenv("PAYKIT_OUTBOUND_WEBHOOK_URL", "https://example.com/hook")

    with pytest.raises(ValidationError, match="SECRET"):
        PayKitSettings(_env_file=None)  # type: ignore[call-arg]


def test_dummy_provider_needs_no_razorpay_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYKIT_PROVIDER", "dummy")

    settings = PayKitSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.razorpay_key_id is None


def test_invalid_table_prefix_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYKIT_PROVIDER", "dummy")
    monkeypatch.setenv("PAYKIT_TABLE_PREFIX", "paykit; DROP TABLE x")

    with pytest.raises(ValidationError):
        PayKitSettings(_env_file=None)  # type: ignore[call-arg]
