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

    # -- Host authentication (TAP-7725) ------------------------------------

    session_cookie_name: str = "savethedate_host"
    session_lifetime_hours: int = 24 * 14

    # Registration is CLOSED unless this is set, and a caller must then present it.
    # This is a single-wedding install whose whole point is to be reachable from any
    # inbox; an open `/auth/register` on that URL invites strangers to create events.
    # Closed-by-default means forgetting to configure it fails safe.
    host_registration_token: str | None = None

    # Left unset, this follows the public base URL: https in production and on the
    # review tunnel, plain http for local development, where a Secure cookie would
    # simply never be sent back. Set it explicitly to override.
    session_cookie_secure: bool | None = None

    @property
    def cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.public_base_url.startswith("https://")

    @property
    def registration_is_open(self) -> bool:
        return bool(self.host_registration_token)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
