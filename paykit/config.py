"""PayKit settings, read from the environment with prefix PAYKIT_."""

from __future__ import annotations

import logging

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("paykit")


class PayKitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYKIT_", env_file=".env", extra="ignore")

    provider: str = "razorpay"

    razorpay_key_id: str
    razorpay_key_secret: SecretStr
    razorpay_webhook_secret: SecretStr

    database_url: str | None = None
    table_prefix: str = "paykit_"
    default_currency: str = "INR"
    http_timeout_seconds: float = 15

    # Standalone service only
    service_api_keys: str | None = None
    outbound_webhook_url: str | None = None
    outbound_webhook_secret: SecretStr | None = None

    @property
    def service_api_keys_list(self) -> list[str]:
        if not self.service_api_keys:
            return []
        return [k.strip() for k in self.service_api_keys.split(",") if k.strip()]

    @model_validator(mode="after")
    def _warn_insecure_webhook_url(self) -> PayKitSettings:
        is_live_key = self.razorpay_key_id.startswith("rzp_live_")
        url = self.outbound_webhook_url
        if is_live_key and url and not url.startswith("https://"):
            logger.warning(
                "PAYKIT_RAZORPAY_KEY_ID looks like a live key but "
                "PAYKIT_OUTBOUND_WEBHOOK_URL is not HTTPS: %s",
                url,
            )
        return self
