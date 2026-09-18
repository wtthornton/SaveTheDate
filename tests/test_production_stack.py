"""The production stack's settings are asserted here, not trusted to a `.env` file.

`docker-compose.prod.yml` (TAP-7733) carries every non-secret production value in the
repository on purpose. The handoff for this work called these "the environment, where
the easy-to-miss items are", and a value that can be missed eventually is: the whole
list would otherwise live in a gitignored `.env.prod` that nobody can review, no test
can read, and a rebuild from memory can silently get wrong.

Putting them in a committed file makes them checkable, so this checks them. Each
assertion below names a specific way the site breaks if the value drifts, because a
test that only says "the value is X" gets deleted by whoever wants it to be Y.

Nothing here starts Docker. These are assertions about committed text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = ROOT / "docker-compose.prod.yml"
DOCKERFILE_PATH = ROOT / "Dockerfile"
GITIGNORE_PATH = ROOT / ".gitignore"


def _compose() -> dict[str, Any]:
    with COMPOSE_PATH.open() as handle:
        return cast(dict[str, Any], yaml.safe_load(handle))


def _service(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], _compose()["services"][name])


def _app_env() -> dict[str, str]:
    return {str(k): str(v) for k, v in _service("app")["environment"].items()}


# -- The settings a guest would notice -------------------------------------


def test_save_the_date_hosts_names_the_card_hostname_and_only_that() -> None:
    """The card's hostname, exactly, and never the wedding's.

    Both production hostnames reach the same process; `SAVE_THE_DATE_HOSTS` is the
    only thing that decides which page each one gets. An explicit list rather than a
    substring test, because this project is itself called savethedate — a match on
    the name would serve the save-the-date card on `wedding.tapphouse.co`.
    """
    hosts = [host.strip() for host in _app_env()["SAVE_THE_DATE_HOSTS"].split(",")]
    assert hosts == ["savethedate.tapphouse.co"]
    assert "wedding.tapphouse.co" not in hosts


def test_production_does_not_wear_the_draft_banner() -> None:
    """REVIEW_INSTANCE true in production would tell every guest their invitation is a draft."""
    assert _app_env()["REVIEW_INSTANCE"] == "false"


def test_public_base_url_is_https_so_the_session_cookie_is_secure() -> None:
    """`Settings.cookie_secure` follows this when SESSION_COOKIE_SECURE is unset."""
    assert _app_env()["PUBLIC_BASE_URL"] == "https://wedding.tapphouse.co"


def test_the_card_sends_production_readers_to_the_production_wedding_site() -> None:
    """`Settings.wedding_site_url` follows PUBLIC_BASE_URL, so this is that link too.

    The value was hard-coded in `app/routers/public.py` once, which made the card on
    the review instance link to production. Deriving it from the one variable each
    deployment already sets to its own site is what keeps dev pointing at dev after a
    promotion — but it also means this line now decides where a real guest lands, not
    just where a session cookie's Secure flag comes from.

    The destination must be the wedding hostname and must NOT be a card hostname, or
    the only button on the card is a link back to the card.
    """
    destination = _app_env()["PUBLIC_BASE_URL"]
    hosts = [host.strip() for host in _app_env()["SAVE_THE_DATE_HOSTS"].split(",")]

    assert urlparse(destination).hostname == "wedding.tapphouse.co"
    assert urlparse(destination).hostname not in hosts


def test_email_stays_on_the_console_transport() -> None:
    """An unconfigured deployment that prints is obvious; one that silently sends is a lie.

    Flipping this to `resend` without a key produces no transport at all, which is
    the safe direction, but the value that belongs in the repository is `console`.
    """
    assert _app_env()["EMAIL_PROVIDER"] == "console"


def test_cors_allows_nothing() -> None:
    """The default is http://localhost:3000, which is a nonsense origin on a guest-facing host."""
    assert _app_env()["CORS_ORIGINS"] == ""


def test_host_registration_is_closed_by_default() -> None:
    """Interpolated with `:-` and no fallback value, so unset is the steady state.

    A `:?` here would make the stack refuse to start without a token, which is
    backwards: the token is set for one registration and then removed forever.
    """
    assert _app_env()["HOST_REGISTRATION_TOKEN"] == "${HOST_REGISTRATION_TOKEN:-}"


# -- The settings that decide whether one guest can throttle another --------


def test_the_rate_limiter_is_told_where_the_real_client_address_is() -> None:
    """Behind the tunnel every request arrives from the tunnel's local end.

    Without this the whole world shares one rate-limit bucket and the first few
    guests throttle everyone else. It is only safe because the app's port is bound
    to loopback (below): a header a caller can set is otherwise an unlimited supply
    of fresh buckets, which turns the limiter off rather than on.
    """
    assert _app_env()["TRUSTED_CLIENT_IP_HEADER"] == "CF-Connecting-IP"


def test_the_app_is_published_on_loopback_only() -> None:
    """This is what makes trusting CF-Connecting-IP and X-Forwarded-* defensible.

    Bound to 0.0.0.0 instead, anything on the LAN could forge either header — one to
    mint unlimited rate-limit buckets, the other to change the scheme of the invite
    links a host copies out of the dashboard.
    """
    for published in _service("app")["ports"]:
        assert str(published).startswith("127.0.0.1:"), published


def test_the_database_publishes_no_port_at_all() -> None:
    """The app reaches Postgres over the Compose network; backups run inside the container.

    The plan asked for a port different from development's 5434. No port is the
    stronger form of the same intent: it cannot be confused with 5434, and it cannot
    be reached from the LAN.
    """
    assert "ports" not in _service("db")


# -- The settings that decide whether it comes back ------------------------


def test_the_stack_returns_after_a_power_cut() -> None:
    """`unless-stopped` on the long-lived services, so nobody has to log in.

    `migrate` is deliberately excluded: it is a one-shot that exits 0, and a restart
    policy on it would re-run the migration in a loop.
    """
    assert _service("app")["restart"] == "unless-stopped"
    assert _service("db")["restart"] == "unless-stopped"
    assert _service("migrate")["restart"] == "no"


def test_migrations_are_a_release_step_the_app_waits_for() -> None:
    """Not on app boot. Two instances racing `alembic upgrade` is a bad way to learn about locking.

    `service_completed_successfully` is what makes a failed migration stop the
    release, instead of producing an app talking to a half-migrated schema.
    """
    depends = _service("app")["depends_on"]
    assert depends["migrate"]["condition"] == "service_completed_successfully"
    assert depends["db"]["condition"] == "service_healthy"
    assert _service("migrate")["command"] == ["alembic", "upgrade", "head"]


def test_the_image_does_not_migrate_on_start() -> None:
    """The container's own command starts uvicorn and nothing else.

    If `alembic upgrade` ever appears in the Dockerfile's CMD or an entrypoint, the
    release step above becomes decorative and the race it prevents comes back.
    """
    dockerfile = DOCKERFILE_PATH.read_text()
    directives = [
        line
        for line in dockerfile.splitlines()
        if line.startswith(("CMD", "ENTRYPOINT", "RUN alembic"))
    ]
    assert directives, "the Dockerfile has no CMD at all"
    for line in directives:
        assert "alembic" not in line, line


# -- The settings that decide whether a credential reaches the internet -----


def test_the_production_secrets_are_gitignored() -> None:
    """This repository is PUBLIC.

    `.env.prod` holds the database password and, briefly, the host-registration
    token. `.env.backup` holds the R2 access key. Neither may ever be committed, and
    neither is covered by the bare `.env` rule above them.
    """
    ignored = GITIGNORE_PATH.read_text().splitlines()
    assert ".env.prod" in ignored
    assert ".env.backup" in ignored


def test_the_example_env_files_carry_no_values() -> None:
    """The committed examples exist to show the shape, and must never gain a real secret."""
    for name in (".env.prod.example", ".env.backup.example"):
        for line in (ROOT / name).read_text().splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key in {"POSTGRES_PASSWORD", "HOST_REGISTRATION_TOKEN"}:
                assert value == "", f"{name} has a value for {key}"
            if "SECRET" in key or "ACCESS_KEY" in key:
                assert value == "", f"{name} has a value for {key}"
