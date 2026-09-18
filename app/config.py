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

    # -- The public front door (TAP-7781) ----------------------------------

    # Hostnames whose ROOT serves the save-the-date card instead of the welcome page.
    # Comma-separated, e.g. "savethedate.tapphouse.co,dev-savethedate.tapphouse.co".
    #
    # An explicit list rather than a substring test, because this project is itself
    # called savethedate: matching on the name would put the card on `dev-wedding`
    # the moment someone renamed something, and a guest-facing hostname silently
    # serving the wrong page is the failure this list exists to make impossible.
    # Unset means every hostname serves the welcome, which is the safe direction.
    save_the_date_hosts: str = ""

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

    # -- Throttling the public guest routes (TAP-7727) ---------------------

    # Per address, per window, across `/invites/*` and `/api/invites/*`. A guest
    # reading all four pages spends four; the default leaves room for reloads and for
    # a household behind one address, while still bounding a scraper.
    invite_rate_limit: int = 60
    invite_rate_window_seconds: int = 60

    # Name of a header holding the real client address, e.g. "CF-Connecting-IP".
    # Leave unset unless a proxy you control OVERWRITES it on every request: a header
    # a caller can set is an unlimited supply of fresh buckets, which turns the
    # limiter off rather than on.
    trusted_client_ip_header: str | None = None

    # -- Email delivery (TAP-7731) -----------------------------------------

    # "console" prints instead of sending. The default on purpose: an unconfigured
    # deployment that prints is obvious and harmless, whereas one that silently
    # succeeds is a lie and one that mails real guests by accident is worse.
    email_provider: str = "console"
    resend_api_key: str | None = None
    email_from: str = "SaveTheDate <invites@example.com>"

    # Shared secret for the provider's bounce/complaint webhook. Unset means the
    # webhook refuses everything, which is the right default for an endpoint that
    # changes delivery state.
    email_webhook_secret: str | None = None

    @property
    def cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.public_base_url.startswith("https://")

    @property
    def wedding_site_url(self) -> str:
        """Where the save-the-date card sends somebody who wants more than a date.

        Derived from `public_base_url` rather than configured separately, because they
        are the same thing: the root of the wedding site on this deployment. One
        variable per environment is what keeps a dev card linking to dev and a
        production card linking to production. Hard-coding it meant
        `dev-savethedate.tapphouse.co` sent reviewers to the real
        `wedding.tapphouse.co` — reviewing the card silently left the review instance,
        and while production was down it looked like the card was broken.

        Absolute, and with a trailing slash: the card is served on its OWN hostname, so
        a relative link would keep the reader on the save-the-date host.
        """
        return self.public_base_url.rstrip("/") + "/"

    @property
    def registration_is_open(self) -> bool:
        return bool(self.host_registration_token)

    @property
    def save_the_date_host_list(self) -> list[str]:
        """Lowercased, because a Host header's case is not the author's to assume."""
        return [
            host.strip().casefold() for host in self.save_the_date_hosts.split(",") if host.strip()
        ]

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
