from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://savethedate:savethedate@localhost:5434/savethedate"
    public_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000"

    # True only on the throwaway review deployment (TAP-7738), which says so on every
    # page so a reviewer never mistakes a draft for the invitation that was really
    # sent. Defaults to False, so production has to do nothing to stay quiet.
    review_instance: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
