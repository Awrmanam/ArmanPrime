from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bot_token: SecretStr = SecretStr("")
    admin_telegram_user_id: int = 0
    order_notification_chat_id: int = 0
    database_url: str = "sqlite+aiosqlite:///./shop.sqlite3"
    redis_url: str = "redis://localhost:6379/0"
    encryption_key: SecretStr = SecretStr("")
    hmac_key: SecretStr = SecretStr("")
    callback_key: SecretStr = SecretStr("")
    price_quote_ttl_minutes: int = Field(default=30, ge=30, le=30)
    run_mode: str = "polling"
    webhook_url: str = ""
    webhook_secret: SecretStr = SecretStr("")
    brand_name: str = ""
    feature_wallet: bool = False
    feature_referrals: bool = False
    feature_cooperation: bool = False
    feature_membership_check: bool = False
    secure_file_path: str = "/var/lib/shopbot/secure"
    fx_provider: str = "manual"
    navasan_api_key: SecretStr = SecretStr("")
    navasan_base_url: str = "https://api.navasan.tech/latest/"
    fx_refresh_minutes: int = Field(default=360, ge=5)
    fx_max_age_minutes: int = Field(default=720, ge=5)
    fx_http_connect_timeout: float = Field(default=5.0, gt=0)
    fx_http_read_timeout: float = Field(default=10.0, gt=0)
    fx_retry_limit: int = Field(default=3, ge=1, le=5)

    @field_validator("price_quote_ttl_minutes")
    @classmethod
    def protected_quote_ttl(cls, value: int) -> int:
        return value


settings = Settings()
