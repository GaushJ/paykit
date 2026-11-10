"""PayKit settings, read from the environment with prefix PAYKIT_."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("paykit")

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class PayKitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYKIT_", env_file=".env", extra="ignore")

    provider: str = "razorpay"

    # Required when provider == "razorpay" (enforced by the validator below),
    # optional otherwise so e.g. PAYKIT_PROVIDER=dummy needs no fake keys.
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None

    database_url: str | None = None
    table_prefix: str = Field(default="paykit_", pattern=r"^[a-z][a-z0-9_]{0,30}$")
    default_currency: str = "INR"
    http_timeout_seconds: float = Field(default=15, gt=0, le=120)

    # SQLAlchemy connection pool (ignored for SQLite). Size these against
    # your Postgres max_connections divided by the number of app processes.
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_recycle_seconds: int = Field(default=1800, ge=-1)

    # Max accepted webhook body. Razorpay payloads are a few KB.
    max_webhook_body_bytes: int = Field(default=256 * 1024, ge=1024)

    # Standalone service only
    service_api_keys: SecretStr | None = None
    outbound_webhook_url: str | None = None
    outbound_webhook_secret: SecretStr | None = None

    @property
    def service_api_keys_list(self) -> list[str]:
        if not self.service_api_keys:
            return []
        raw = self.service_api_keys.get_secret_value()
        return [k.strip() for k in raw.split(",") if k.strip()]

    def razorpay_credentials(self) -> tuple[str, str, str]:
        """(key_id, key_secret, webhook_secret) - raises if any is unset."""
        if not (self.razorpay_key_id and self.razorpay_key_secret and self.razorpay_webhook_secret):
            raise ValueError(
                "PAYKIT_RAZORPAY_KEY_ID, PAYKIT_RAZORPAY_KEY_SECRET and "
                "PAYKIT_RAZORPAY_WEBHOOK_SECRET must all be set when PAYKIT_PROVIDER=razorpay"
            )
        return (
            self.razorpay_key_id,
            self.razorpay_key_secret.get_secret_value(),
            self.razorpay_webhook_secret.get_secret_value(),
        )

    @model_validator(mode="after")
    def _validate(self) -> PayKitSettings:
        if self.provider == "razorpay":
            self.razorpay_credentials()  # raises -> pydantic ValidationError

        url = self.outbound_webhook_url
        if url:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("PAYKIT_OUTBOUND_WEBHOOK_URL must be an absolute http(s) URL")
            if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
                is_live_key = (self.razorpay_key_id or "").startswith("rzp_live_")
                if is_live_key:
                    # Payment events (amounts, customer refs) would travel in
                    # cleartext. Refuse rather than warn when real money is involved.
                    raise ValueError(
                        "PAYKIT_OUTBOUND_WEBHOOK_URL must use HTTPS with a live Razorpay key"
                    )
                logger.warning("PAYKIT_OUTBOUND_WEBHOOK_URL is not HTTPS: %s", url)
            if not self.outbound_webhook_secret:
                raise ValueError(
                    "PAYKIT_OUTBOUND_WEBHOOK_SECRET must be set when "
                    "PAYKIT_OUTBOUND_WEBHOOK_URL is set (outbound webhooks are always signed)"
                )
        return self
