"""The production stack's settings are asserted here, not trusted to a `.env` file.

`docker-compose.prod.yml` (TAP-7733) carries every non-secret production value in the
repository on purpose. The handoff for this work called these "the environment, where
the easy-to-miss items are", and a value that can be missed eventually is: the whole
list would otherwise live in a gitignored `.env.prod` that nobody can review, no test
can read, and a rebuild from memory can silently get wrong.

Putting them in a committed file makes them checkable, so this checks them. Each
assertion below names a specific way the site breaks if the value drifts, because a
test that only says "the value is X" gets deleted by whoever wants it to be Y.

Nothing here starts Docker. Most of these are assertions about committed text; the
ones that run a script give it a `docker` that only writes down what it was asked.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import pytest
import yaml

from scripts.deploy_release import Config

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


# -- The settings that decide what reaches the error collector --------------


def test_the_error_collector_dsn_comes_only_from_the_secret_file() -> None:
    """The DSN is a credential, so this file passes it through and never holds one.

    `:-` with no fallback, so an `.env.prod` without it deploys with the collector off,
    which `app.error_collector` treats as never importing the SDK at all. A literal
    here would put a write credential for the collector in a public repository.
    """
    assert _app_env()["SENTRY_DSN"] == "${SENTRY_DSN:-}"


def test_production_errors_are_labelled_production() -> None:
    """Without it they would arrive as `development`, the code's default.

    The review instance runs the same code against invented guests; an event from it
    read as production is how somebody goes looking for a guest who does not exist.
    """
    assert _app_env()["SENTRY_ENVIRONMENT"] == "production"


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


# -- The settings that decide what it can do to its neighbours -------------

_MIB = {"K": 1 / 1024, "M": 1, "G": 1024}

# Peaks measured on 2026-11-21 and recorded beside each limit in the compose file.
_MEASURED_PEAK_MIB = {"app": 134, "db": 92}


def _memory_limit_mib(service: str) -> float:
    raw = str(_service(service)["deploy"]["resources"]["limits"]["memory"])
    return float(raw[:-1]) * _MIB[raw[-1].upper()]


def test_the_long_lived_services_have_a_memory_ceiling() -> None:
    """The box runs dozens of containers; an unbounded leak here gets someone else killed.

    Under host memory pressure the kernel or systemd-oomd picks a victim, and it need
    not be the container that is leaking. A limit in this file survives a recreate,
    where one applied with `docker update` does not (TAP-10296).
    """
    for service in ("app", "db"):
        assert _memory_limit_mib(service) > 0, service


def test_the_memory_ceilings_leave_room_above_what_was_measured() -> None:
    """A ceiling at the observed peak turns a busy evening into an OOM-killed site.

    The `db` figure covers the nightly `pg_dump` and the weekly drill's `pg_restore`,
    both of which run inside that container, so a limit that cut it close would show
    up first as a failed backup rather than as a slow page.
    """
    for service, peak in _MEASURED_PEAK_MIB.items():
        assert _memory_limit_mib(service) >= 2 * peak, service


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
            if key in {"POSTGRES_PASSWORD", "HOST_REGISTRATION_TOKEN", "SENTRY_DSN"}:
                assert value == "", f"{name} has a value for {key}"
            if "SECRET" in key or "ACCESS_KEY" in key:
                assert value == "", f"{name} has a value for {key}"


# -- The settings that decide whether production needs a source tree at all ---
#
# TAP-10322. it13 is retiring `~/code`. Production used to build its image from the
# development checkout there and run its backups out of it, so renaming that directory
# away would have stopped the nightly backup and made the next deploy impossible.

DEPLOY_DIR = "%h/srv/savethedate"
PIN = "registry.test:5000/savethedate:v7@sha256:" + "ab" * 32
DEPLOY_FILES = [
    *sorted((ROOT / "deploy").iterdir()),
    COMPOSE_PATH,
    ROOT / "scripts" / "prod.sh",
    ROOT / "scripts" / "backup.sh",
    ROOT / "scripts" / "restore-drill.sh",
    ROOT / "scripts" / "build-image.sh",
    ROOT / "scripts" / "deploy_release.py",
]


def test_nothing_is_built_on_the_production_box() -> None:
    """No service has a `build:`, so there is no source tree for production to need.

    A `build:` left on either service is also a quiet fallback: `compose up` builds
    an image it cannot find, and production would be running whatever the directory
    held that day instead of failing.
    """
    for name, service in _compose()["services"].items():
        assert "build" not in service, name
    deploy = (ROOT / "scripts" / "prod.sh").read_text()
    assert "dc build" not in deploy


def test_the_app_image_is_whatever_the_deploy_directory_pinned() -> None:
    """One variable, required, shared by the app and its migration.

    The digest does not exist until the image is built, so it cannot be a literal
    here. `:?` makes an unset pin an error. `:-` would let the stack start on a default,
    and an unpinned default is the thing being removed. The two services share the
    value, so a release cannot migrate with one image and serve with another.
    """
    raw = COMPOSE_PATH.read_text()
    assert raw.count("${SAVETHEDATE_IMAGE:?") == 1
    assert "${SAVETHEDATE_IMAGE:-" not in raw
    assert "savethedate-prod:latest" not in raw
    assert _service("app")["image"] == _service("migrate")["image"]
    assert str(_service("app")["image"]).startswith("${SAVETHEDATE_IMAGE:?")


def test_the_project_name_does_not_follow_the_directory() -> None:
    """Moving the compose file must not move the guest list to a new, empty volume.

    Without `name:`, Compose names the project after the directory. The volume is
    `<project>_pgdata`, so the same file run from `~/srv/savethedate` would create
    `savethedate_pgdata` beside the real `savethedate-prod_pgdata` and start on it.
    """
    assert _compose()["name"] == "savethedate-prod"
    assert _compose()["volumes"] == {"pgdata": None}


def test_no_production_file_points_into_the_development_checkout() -> None:
    """Acceptance: the units and scripts run with `~/code/SaveTheDate` renamed away.

    Any path under `~/code` or `/home/<user>/code` is a path, whatever follows it. A
    bare `~/code` in backticks is prose about the directory being retired.
    """
    path_into_code = re.compile(r"(~|%h|\$HOME|\$\{HOME\}|/home/[^/\s]+)/code/|code/SaveTheDate")
    for path in DEPLOY_FILES:
        found = path_into_code.search(path.read_text())
        assert found is None, f"{path.name}: {found.group(0) if found else ''}"


def test_the_backup_and_the_drill_run_from_the_deploy_directory() -> None:
    """`%h`, not a literal home, and the deploy directory, not a working copy."""
    for unit, script in (("backup", "backup.sh"), ("restore-drill", "restore-drill.sh")):
        lines = (ROOT / "deploy" / f"savethedate-{unit}.service").read_text().splitlines()
        assert f"WorkingDirectory={DEPLOY_DIR}" in lines
        assert f"ExecStart={DEPLOY_DIR}/scripts/{script}" in lines


def test_the_deploy_moves_the_deploy_directory_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing set, the deploy unit works on `~/srv/savethedate`."""
    monkeypatch.delenv("SAVETHEDATE_DEPLOY_DIR", raising=False)

    config = Config.from_environment()

    assert config.checkout == Path.home() / "srv" / "savethedate"
    assert config.image_file == config.checkout / "image.env"


def test_the_pin_is_not_tracked_so_writing_it_never_dirties_the_deploy_directory() -> None:
    """The deploy refuses to move a checkout with changes to tracked files."""
    assert "image.env" in GITIGNORE_PATH.read_text().splitlines()
    tracked = subprocess.run(
        ["git", "ls-files", "image.env"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert tracked.stdout == ""


def _deploy_dir(tmp_path: Path, image_env: str | None) -> tuple[Path, Path]:
    """A deploy directory with the real scripts and a `docker` that only takes notes."""
    (tmp_path / "scripts").mkdir()
    for name in ("prod.sh", "backup.sh", "restore-drill.sh"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    shutil.copy(COMPOSE_PATH, tmp_path / COMPOSE_PATH.name)
    (tmp_path / ".env.prod").write_text("POSTGRES_PASSWORD=not-a-real-one\n")
    if image_env is not None:
        (tmp_path / "image.env").write_text(image_env)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls"
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$*" >> {calls}\nexit 0\n')
    docker.chmod(0o755)
    return bin_dir, calls


def _run(cwd: Path, bin_dir: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", *argv],
        cwd=cwd,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "image_env",
    [
        None,
        "",
        "SAVETHEDATE_IMAGE=\n",
        "SAVETHEDATE_IMAGE=registry.test:5000/savethedate:v7\n",
        "SAVETHEDATE_IMAGE=registry.test:5000/savethedate:latest\n",
        "SAVETHEDATE_IMAGE=registry.test:5000/savethedate@sha256:" + "ab" * 32 + "\n",
        "SAVETHEDATE_IMAGE=registry.test:5000/savethedate:v7@sha256:abc\n",
    ],
    ids=["missing", "empty-file", "empty-value", "tag-only", "latest", "digest-only", "short"],
)
def test_prod_sh_refuses_an_image_that_is_not_pinned_by_tag_and_digest(
    tmp_path: Path, image_env: str | None
) -> None:
    """Compose's `:?` only catches a missing value. A bare tag would sail through it.

    `savethedate:v7` is a name somebody can push over; the digest is what makes the
    image the one that was built. Docker is never reached.
    """
    bin_dir, calls = _deploy_dir(tmp_path, image_env)

    result = _run(tmp_path, bin_dir, "scripts/prod.sh", "up")

    assert result.returncode != 0
    assert "image.env" in result.stderr
    assert not calls.exists()


def test_prod_sh_gives_compose_the_secrets_and_the_pin_from_beside_itself(
    tmp_path: Path,
) -> None:
    """Both env files and the compose file, all from the directory the script is in."""
    bin_dir, calls = _deploy_dir(tmp_path, f"SAVETHEDATE_IMAGE={PIN}\n")

    result = _run(tmp_path, bin_dir, "scripts/prod.sh", "compose", "ps")

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        f"compose --env-file {tmp_path}/.env.prod --env-file {tmp_path}/image.env "
        f"-f {tmp_path}/docker-compose.prod.yml ps"
    ]


def test_the_backup_reaches_compose_only_through_prod_sh(tmp_path: Path) -> None:
    """One place knows which files Compose is given, so the backup cannot disagree.

    The compose file now needs `image.env` to be read at all. A script that named the
    compose file and `.env.prod` itself would fail every night from the release that
    added the pin, and the backup is the one thing here that must not quietly stop.
    """
    for name in ("backup.sh", "restore-drill.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert 'dc() { "${ROOT}/scripts/prod.sh" compose "$@"; }' in text, name
        assert "docker compose" not in text, name
        assert "docker-compose.prod.yml" not in text, name

    bin_dir, calls = _deploy_dir(tmp_path, f"SAVETHEDATE_IMAGE={PIN}\n")
    # The fake database answers nothing, so the run stops at the empty dump. What is
    # checked is how Compose was reached on the way there.
    result = _run(tmp_path, bin_dir, "scripts/backup.sh", "--local")

    assert "the dump is empty" in result.stderr
    # One call per `compose` line; the SQL it was handed spills onto the lines after.
    asked = [line for line in calls.read_text().splitlines() if line.startswith("compose ")]
    assert len(asked) == 2, "the row count and the dump"
    prefix = (
        f"compose --env-file {tmp_path}/.env.prod --env-file {tmp_path}/image.env "
        f"-f {tmp_path}/docker-compose.prod.yml exec -T db "
    )
    for line in asked:
        assert line.startswith(prefix), line


# -- The image that is pulled is the commit that was tagged ---------------------


def _build_image(
    tmp_path: Path, *argv: str, digests: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the real build-image.sh in a small git repository, with Docker faked.

    The fake keeps the build context it was piped, so the test can see exactly what
    would have gone into the image.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "build-image.sh", repo / "scripts" / "build-image.sh")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run(
        [*git, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "release"],
        cwd=repo,
        check=True,
    )
    subprocess.run([*git, "tag", "--no-sign", "v7"], cwd=repo, check=True)
    (repo / "not-committed.txt").write_text("an edit that never reached main\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls"
    context = tmp_path / "context.tar"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {calls}\n'
        'case "$1" in\n'
        f"  build) cat > {context} ;;\n"
        f"  image) printf '%s' '{digests}' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    docker.chmod(0o755)
    result = _run(repo, bin_dir, "scripts/build-image.sh", *argv)
    return result, calls, context


def test_the_release_image_is_built_from_the_tagged_commit_and_says_so(tmp_path: Path) -> None:
    """Built from the commit, labelled with it, pushed, and printed as a pin.

    `git archive` is the build context: a file that is only in the working tree is
    not in it. The label is what `deploy_release.py` checks against the signed tag.
    """
    repository = "registry.test:5000/savethedate"
    digest = "sha256:" + "cd" * 32
    result, calls, context = _build_image(
        tmp_path,
        repository,
        "v7",
        digests=f"elsewhere.test/copy@sha256:{'00' * 32}\n{repository}@{digest}\n",
    )

    assert result.returncode == 0, result.stderr
    commit = subprocess.run(
        ["git", "rev-parse", "v7^{commit}"],
        cwd=tmp_path / "repo",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    build, push, _inspect = calls.read_text().splitlines()
    assert build == (
        f"build --label org.opencontainers.image.revision={commit} "
        f"--label org.opencontainers.image.version=v7 --tag {repository}:v7 -"
    )
    assert push == f"push {repository}:v7"
    with tarfile.open(context) as archive:
        names = archive.getnames()
    assert "Dockerfile" in names
    assert "not-committed.txt" not in names
    assert result.stdout.splitlines()[-1] == f"SAVETHEDATE_IMAGE={repository}:v7@{digest}"


@pytest.mark.parametrize(
    "argv",
    [
        ("registry.test:5000/savethedate", "latest"),
        ("registry.test:5000/savethedate", "v7.1"),
        ("registry.test:5000/savethedate", "v8"),
        ("registry.test:5000/savethedate@sha256:" + "cd" * 32, "v7"),
    ],
    ids=["latest", "dotted", "a-tag-that-does-not-exist", "a-digest-as-the-repository"],
)
def test_the_release_image_is_not_built_for_something_that_is_not_a_release(
    tmp_path: Path, argv: tuple[str, str]
) -> None:
    result, calls, _context = _build_image(tmp_path, *argv)

    assert result.returncode != 0
    assert "REFUSED" in result.stderr
    assert not calls.exists()


def test_a_push_that_yields_no_digest_does_not_print_a_pin(tmp_path: Path) -> None:
    """Without a digest there is nothing to pin, and a tag alone is not a pin."""
    result, _calls, _context = _build_image(tmp_path, "registry.test:5000/savethedate", "v7")

    assert result.returncode != 0
    assert "SAVETHEDATE_IMAGE=" not in result.stdout
