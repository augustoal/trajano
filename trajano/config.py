from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRAJANO_", extra="ignore")

    database_url: str = "sqlite:///./trajano.db"
    offer_ttl_hours: int = 48
    order_confirm_hours: int = 24
    rfq_rate_limit_per_hour: int = 60
    text_max_length: int = 2000
    api_key_pepper: str = "dev-pepper-change-me"
    mandate_signing_key: str = "dev-mandate-key-change-me"
    cookie_secure: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
