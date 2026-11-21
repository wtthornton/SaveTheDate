"""The release-tag deploy on it13 (TAP-12474) ships only what Bill signed, and fails safe.

`scripts/deploy_release.py` is the only thing that moves production now. These tests
run it against real git repositories with real SSH-signed tags: an `origin`, and a
clone standing in for it13's checkout. Signature verification is git's own. Only
Docker and `scripts/prod.sh` are faked, and the fake records what was asked of them
and where the checkout was at that moment.

The checkout's own `deploy/install-timers.sh` (TAP-12499) runs for real, under a
temporary HOME with `systemctl` and `loginctl` faked on PATH, so "the installed copy"
in these tests is a real file the real installer wrote.

Nothing here starts Docker or touches the network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.deploy_release import (
    EXIT_DONE,
    EXIT_FAILED,
    EXIT_REFUSED,
    EXIT_REINSTALL_FAILED,
    EXIT_ROLLBACK_FAILED,
    IMAGE,
    PREVIOUS_IMAGE,
    Config,
    Deployer,
    run_command,
)

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ("bash", "deploy/install-timers.sh")


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _keypair(directory: Path, name: str) -> Path:
    key = directory / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
        check=True,
    )
    return key


@dataclass
class FakeHost:
    """Docker and prod.sh. Records each call with the checkout's commit at the time.

    The installer is recorded too, and then really run, under `installer_env`.
    """

    checkout: Path
    installer_env: dict[str, str]
    failing: set[tuple[str, ...]] = field(default_factory=set)
    calls: list[tuple[tuple[str, ...], str]] = field(default_factory=list)

    def run(self, argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            return run_command(argv, cwd)
        call = tuple(argv)
        self.calls.append((call, _git(self.checkout, "rev-parse", "HEAD")))
        failed = any(call[: len(prefix)] == prefix for prefix in self.failing)
        if call == INSTALLER and not failed:
            return subprocess.run(
                call, cwd=cwd, env=self.installer_env, capture_output=True, text=True, check=False
            )
        return subprocess.CompletedProcess(call, 1 if failed else 0, "", "boom" if failed else "")

    def asked(self, *prefix: str) -> list[str]:
        """The checkout commit at each call that starts with `prefix`."""
        return [head for call, head in self.calls if call[: len(prefix)] == prefix]


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.dev = tmp / "dev"
        self.checkout = tmp / "it13-checkout"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.dev)], check=True)
        # The real installer and units, so the checkout can reinstall from itself.
        shutil.copytree(ROOT / "deploy", self.dev / "deploy")
        (self.dev / "scripts").mkdir()
        _git(self.dev, "add", "deploy")
        self.commit("first", deploy_script="# deploy_release.py as of the first commit\n")
        _git(self.dev, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.checkout)], check=True)

        self.bill = _keypair(tmp, "bill")
        self.stranger = _keypair(tmp, "stranger")
        self.allowed_signers = tmp / "allowed_signers"
        pub = self.bill.with_suffix(".pub").read_text().strip()
        self.allowed_signers.write_text(f'bill@example.com namespaces="git" {pub}\n')

        self.config = Config(
            checkout=self.checkout,
            allowed_signers=self.allowed_signers,
            state_dir=tmp / "state",
        )
        self.home = tmp / "home"
        self.home.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.systemctl_calls = tmp / "systemctl-calls"
        self.linger("yes")
        self._fake("systemctl", f'echo "$*" >> {self.systemctl_calls}')
        installer_env = {
            **os.environ,
            "HOME": str(self.home),
            "USER": "wtthornton",
            "PATH": f"{self.bin}:{os.environ['PATH']}",
        }
        self.host = FakeHost(self.checkout, installer_env)
        self.healthy = True
        self.log: list[str] = []

    def _fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\nexit 0\n")
        path.chmod(0o755)

    def linger(self, answer: str) -> None:
        """What `loginctl show-user -p Linger` says. The installer refuses on "no"."""
        self._fake("loginctl", f"echo {answer}")

    def commit(self, message: str, deploy_script: str | None = None) -> str:
        (self.dev / "file.txt").write_text(message)
        _git(self.dev, "add", "file.txt")
        if deploy_script is not None:
            (self.dev / "scripts" / "deploy_release.py").write_text(deploy_script)
            _git(self.dev, "add", "scripts/deploy_release.py")
        _git(self.dev, "commit", "-q", "-m", message)
        return _git(self.dev, "rev-parse", "HEAD")

    @property
    def installed_script(self) -> Path:
        return self.home / ".local" / "libexec" / "savethedate" / "deploy_release.py"

    def deploy_script_at(self, revision: str) -> bytes:
        return subprocess.run(
            ["git", "show", f"{revision}:scripts/deploy_release.py"],
            cwd=self.dev,
            capture_output=True,
            check=True,
        ).stdout

    def installer_ran_after(self, *prefix: str) -> bool:
        """The installer ran, and only after the last call starting with `prefix`."""
        names = [call for call, _ in self.host.calls]
        matching = [i for i, call in enumerate(names) if call[: len(prefix)] == prefix]
        return bool(matching) and INSTALLER in names[matching[-1] :]

    def push_main(self) -> None:
        _git(self.dev, "push", "-q", "origin", "main")

    def tag(self, name: str, key: Path | None = None, commit: str = "HEAD") -> None:
        if key is None:
            _git(self.dev, "tag", "-a", name, "-m", name, commit)
        else:
            _git(
                self.dev,
                "-c",
                "gpg.format=ssh",
                "-c",
                f"user.signingkey={key}",
                "tag",
                "-s",
                name,
                "-m",
                name,
                commit,
            )
        _git(self.dev, "push", "-q", "origin", name)

    def deploy(self) -> int:
        deployer = Deployer(
            self.config, run=self.host.run, healthy=lambda: self.healthy, log=self.log.append
        )
        return deployer.run()

    def head(self) -> str:
        return _git(self.checkout, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    # Keep the developer's own git config (signing defaults, hooks) out of it.
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return World(tmp_path)


# -- What ships ---------------------------------------------------------------


def test_a_tag_bill_signed_on_main_is_deployed_from_the_tag(world: World) -> None:
    """The happy path, in the order that keeps a way back.

    The running image is kept as `previous` BEFORE the checkout moves, and the deploy
    runs with the checkout at the tag, so it is the tag's own prod.sh that runs.
    """
    before = world.head()
    tagged = world.commit("release one")
    world.push_main()
    world.tag("v1", world.bill)

    assert world.deploy() == EXIT_DONE

    assert world.head() == tagged
    assert world.host.asked("docker", "image", "tag", IMAGE, PREVIOUS_IMAGE) == [before]
    assert world.host.asked("scripts/prod.sh", "deploy") == [tagged]
    assert not world.config.attempt_file.exists()


def test_a_tick_with_nothing_new_changes_nothing(world: World) -> None:
    world.commit("release one")
    world.push_main()
    world.tag("v1", world.bill)
    assert world.deploy() == EXIT_DONE
    world.host.calls.clear()

    assert world.deploy() == EXIT_DONE
    assert world.host.calls == []


def test_no_tag_at_all_changes_nothing(world: World) -> None:
    before = world.head()
    assert world.deploy() == EXIT_DONE
    assert world.head() == before
    assert world.host.calls == []


def test_an_older_tag_never_replaces_a_newer_live_one(world: World) -> None:
    """A tag numbered below what is live is not a release, it is a downgrade."""
    first = world.commit("one")
    world.commit("two")
    world.push_main()
    world.tag("v2", world.bill)
    assert world.deploy() == EXIT_DONE
    live = world.head()
    world.host.calls.clear()

    world.tag("v1", world.bill, commit=first)
    assert world.deploy() == EXIT_DONE
    assert world.head() == live
    assert world.host.calls == []


# -- What is refused ------------------------------------------------------------


@pytest.mark.parametrize("signer", ["unsigned", "stranger"])
def test_a_tag_bill_did_not_sign_is_refused_and_nothing_moves(world: World, signer: str) -> None:
    """The signature IS the production approval.

    An unsigned annotated tag, and a tag signed by a key that is not in
    `allowed_signers`, both leave the checkout where it was and never reach Docker.
    """
    before = world.head()
    world.commit("unapproved")
    world.push_main()
    world.tag("v1", None if signer == "unsigned" else world.stranger)

    assert world.deploy() == EXIT_REFUSED
    assert world.head() == before
    assert world.host.calls == []


def test_an_unsigned_tag_cannot_stand_in_front_of_a_signed_one(world: World) -> None:
    """Anyone who can push can push `v99`. It is skipped, not obeyed and not blocking."""
    approved = world.commit("approved")
    world.push_main()
    world.tag("v2", world.bill)
    world.commit("pushed by someone else")
    world.push_main()
    world.tag("v99")

    assert world.deploy() == EXIT_DONE
    assert world.head() == approved
    assert any(line.startswith("Ignoring v99") for line in world.log)


def test_a_signed_tag_on_a_commit_not_on_main_is_refused(world: World) -> None:
    """Only merged code ships, even when Bill signed it."""
    before = world.head()
    world.commit("never merged")
    world.tag("v1", world.bill)  # the tag is pushed; main is not

    assert world.deploy() == EXIT_REFUSED
    assert world.head() == before
    assert world.host.calls == []


def test_a_dirty_checkout_is_refused(world: World) -> None:
    """A hand edit on it13 would otherwise be carried into, or lost by, the move."""
    world.commit("release one")
    world.push_main()
    world.tag("v1", world.bill)
    (world.checkout / "file.txt").write_text("edited by hand on it13")

    assert world.deploy() == EXIT_REFUSED
    assert world.host.calls == []


def test_without_allowed_signers_nothing_is_deployed(world: World) -> None:
    world.commit("release one")
    world.push_main()
    world.tag("v1", world.bill)
    world.allowed_signers.unlink()

    assert world.deploy() == EXIT_REFUSED
    assert world.host.calls == []


# -- What happens when it fails ---------------------------------------------------


def test_a_failed_deploy_rolls_back_and_the_tag_is_never_retried(world: World) -> None:
    """A failed migration or health check puts the previous app back and fails loudly.

    The checkout returns to the previous commit, the previous image becomes `latest`
    again, and the previous app is started WITHOUT its dependencies, so migrate does
    not run against a schema the previous code may not know. The exit is a failure,
    so the systemd unit fails and `prod.sh status` shows it.
    """
    before = world.head()
    world.commit("broken release")
    world.push_main()
    world.tag("v1", world.bill)
    world.host.failing.add(("scripts/prod.sh", "deploy"))

    assert world.deploy() == EXIT_FAILED

    assert world.head() == before
    assert world.host.asked("docker", "image", "tag", PREVIOUS_IMAGE, IMAGE) == [before]
    rollback = ("scripts/prod.sh", "compose", "up", "-d", "--no-deps", "db", "app")
    assert world.host.asked(*rollback) == [before]
    assert any("FAILED" in line for line in world.log)

    world.host.calls.clear()
    assert world.deploy() == EXIT_DONE
    assert world.host.calls == []
    assert world.head() == before


def test_a_fixed_release_after_a_failed_one_deploys(world: World) -> None:
    world.commit("broken release")
    world.push_main()
    world.tag("v1", world.bill)
    world.host.failing.add(("scripts/prod.sh", "deploy"))
    assert world.deploy() == EXIT_FAILED

    world.host.failing.clear()
    fixed = world.commit("fixed release")
    world.push_main()
    world.tag("v2", world.bill)
    assert world.deploy() == EXIT_DONE
    assert world.head() == fixed


def test_a_rollback_that_does_not_come_back_healthy_says_production_may_be_down(
    world: World,
) -> None:
    world.commit("broken release")
    world.push_main()
    world.tag("v1", world.bill)
    world.host.failing.add(("scripts/prod.sh", "deploy"))
    world.healthy = False

    assert world.deploy() == EXIT_ROLLBACK_FAILED
    assert any("PRODUCTION MAY BE DOWN" in line for line in world.log)
    # Kept, so the next tick tries the rollback again rather than reading v1 as live.
    assert world.config.attempt_file.exists()


def test_a_tag_that_cannot_be_checked_out_is_a_failure_not_a_crash(world: World) -> None:
    """Git refuses to overwrite an untracked file. That must roll back, not traceback.

    A traceback would leave the attempt file behind and blame the next tick.
    """
    before = world.head()
    (world.dev / "new.txt").write_text("tracked in v1")
    _git(world.dev, "add", "new.txt")
    world.commit("adds new.txt")
    world.push_main()
    world.tag("v1", world.bill)
    (world.checkout / "new.txt").write_text("untracked on it13")

    assert world.deploy() == EXIT_FAILED
    assert world.head() == before
    assert world.host.asked("scripts/prod.sh", "deploy") == []
    assert not world.config.attempt_file.exists()


def test_an_unreadable_attempt_file_stops_everything_loudly(world: World) -> None:
    world.commit("release one")
    world.push_main()
    world.tag("v1", world.bill)
    world.config.state_dir.mkdir()
    world.config.attempt_file.write_text("")

    assert world.deploy() == EXIT_ROLLBACK_FAILED
    assert any(line.startswith("STUCK") for line in world.log)
    assert world.host.calls == []


def test_a_deploy_killed_halfway_is_rolled_back_by_the_next_tick(world: World) -> None:
    """The checkout already sits at the tag, so it would otherwise read as live."""
    before = world.head()
    world.commit("release one")
    world.push_main()
    world.tag("v1", world.bill)
    world.config.state_dir.mkdir()
    world.config.attempt_file.write_text(f"v1 {before} 1\n")
    _git(world.checkout, "fetch", "-q", "--tags", "origin")
    _git(world.checkout, "checkout", "-q", "--detach", "v1")

    assert world.deploy() == EXIT_FAILED

    assert world.head() == before
    assert world.host.asked("scripts/prod.sh", "deploy") == []
    assert world.host.asked(*INSTALLER) == [before]
    assert world.installed_script.read_bytes() == world.deploy_script_at(before)
    assert not world.config.attempt_file.exists()
    assert world.deploy() == EXIT_DONE  # v1 is recorded as failed
    assert world.head() == before


# -- The installed copy follows the live tag (TAP-12499) ----------------------------


def test_a_successful_deploy_installs_the_deploy_script_and_units_from_the_live_tag(
    world: World,
) -> None:
    """Nobody reruns install-timers.sh by hand. The tag that just went live does it.

    It runs only after the tag's prod.sh deploy succeeded, with the checkout still at
    the tag. The tag's code never runs before the installed copy has taken the tag.
    """
    before = world.head()
    tagged = world.commit("release one", deploy_script="# deploy_release.py as of v1\n")
    world.push_main()
    world.tag("v1", world.bill)
    assert world.deploy_script_at("v1") != world.deploy_script_at(before)

    assert world.deploy() == EXIT_DONE

    assert world.installed_script.read_bytes() == world.deploy_script_at("v1")
    assert world.host.asked(*INSTALLER) == [tagged]
    assert world.installer_ran_after("scripts/prod.sh", "deploy")
    unit = "savethedate-deploy.service"
    installed_unit = world.home / ".config" / "systemd" / "user" / unit
    assert installed_unit.read_bytes() == (world.checkout / "deploy" / unit).read_bytes()
    assert "--user daemon-reload" in world.systemctl_calls.read_text().splitlines()


def test_a_rollback_reinstalls_from_the_commit_it_rolled_back_to(world: World) -> None:
    """The installed copy never runs ahead of, or lags behind, what is live.

    The copy starts out stale here, so only a reinstall at the rolled-back commit can
    make it match. The failed tag's own installer never runs.
    """
    before = world.head()
    world.installed_script.parent.mkdir(parents=True)
    world.installed_script.write_text("# a copy from some older commit\n")
    world.commit("broken release", deploy_script="# deploy_release.py as of v1\n")
    world.push_main()
    world.tag("v1", world.bill)
    world.host.failing.add(("scripts/prod.sh", "deploy"))

    assert world.deploy() == EXIT_FAILED

    assert world.head() == before
    assert world.installed_script.read_bytes() == world.deploy_script_at(before)
    assert world.host.asked(*INSTALLER) == [before]
    assert world.installer_ran_after("scripts/prod.sh", "compose", "up")


def test_a_reinstall_that_fails_after_a_deploy_keeps_the_new_app_and_fails_loudly(
    world: World,
) -> None:
    """The release shipped. Only the installed copy is behind, and the log says so.

    No rollback: the app that just passed /health stays. The attempt file is cleared,
    because the deploy itself is settled; kept, the next tick would roll back a good
    release. The next tick finds the tag live and changes nothing, using whatever copy
    is installed.
    """
    world.installed_script.parent.mkdir(parents=True)
    world.installed_script.write_text("# the copy installed before v1\n")
    tagged = world.commit("release one", deploy_script="# deploy_release.py as of v1\n")
    world.push_main()
    world.tag("v1", world.bill)
    world.linger("no")  # the real installer refuses

    assert world.deploy() == EXIT_REINSTALL_FAILED

    assert world.head() == tagged
    assert world.host.asked("docker", "image", "tag", PREVIOUS_IMAGE, IMAGE) == []
    assert world.host.asked("scripts/prod.sh", "compose", "up", "-d", "--no-deps") == []
    assert not world.config.attempt_file.exists()
    assert not world.config.failed_tags_file.exists()
    assert world.installed_script.read_text() == "# the copy installed before v1\n"
    assert any(line.startswith("REINSTALL FAILED") for line in world.log)
    assert any("may be stale" in line for line in world.log)

    world.host.calls.clear()
    assert world.deploy() == EXIT_DONE
    assert world.host.calls == []
    assert world.head() == tagged


def test_a_reinstall_that_fails_after_a_rollback_still_reports_the_rollback(
    world: World,
) -> None:
    """The deploy failed and the previous app is back; that is the headline, exit 1.

    The attempt is still cleared. Kept, every tick would restart the previous app.
    """
    before = world.head()
    world.commit("broken release")
    world.push_main()
    world.tag("v1", world.bill)
    world.host.failing.update({("scripts/prod.sh", "deploy"), INSTALLER})

    assert world.deploy() == EXIT_FAILED

    assert world.head() == before
    assert not world.config.attempt_file.exists()
    assert any(line.startswith("REINSTALL FAILED") for line in world.log)


# -- The committed wiring ----------------------------------------------------------


def test_prod_sh_migrates_on_its_own_before_up() -> None:
    """`up` recreates every container before starting any, so it cannot be the gate.

    Reproduced with Compose v5.8.0: with `migrate` failing, `up -d` had already removed
    the running app and then never started the new one. Production went down on a
    failed migration. `run --rm migrate` touches no other container.
    """
    text = (ROOT / "scripts" / "prod.sh").read_text()
    body = text.split("deploy() {", 1)[1].split("\n}", 1)[0]
    steps = [line.strip() for line in body.splitlines() if line.strip().startswith(("dc ", "_"))]
    assert steps == ["dc build", "dc run --rm migrate", "dc up -d", "_wait_healthy"]


def test_prod_sh_stops_before_up_when_the_migration_fails(tmp_path: Path) -> None:
    """The real prod.sh, with a fake `docker` that fails only the migration."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "prod.sh", tmp_path / "scripts" / "prod.sh")
    (tmp_path / ".env.prod").write_text("POSTGRES_PASSWORD=not-a-real-one\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {calls}\n'
        'case "$*" in *" run --rm migrate"*) exit 1 ;; esac\n'
        "exit 0\n"
    )
    docker.chmod(0o755)

    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "prod.sh"), "deploy"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    asked = [line.split(" -f ", 1)[1].split(" ", 1)[1] for line in calls.read_text().splitlines()]
    assert asked == ["build", "run --rm migrate"]


def test_the_deploy_unit_runs_the_installed_copy_not_the_checkout() -> None:
    """The checkout is what the deploy moves. The code that decides must not move with it."""
    unit = (ROOT / "deploy" / "savethedate-deploy.service").read_text()
    assert "ExecStart=/usr/bin/python3 %h/.local/libexec/savethedate/deploy_release.py" in unit
    assert "code/SaveTheDate/scripts" not in unit

    installer = (ROOT / "deploy" / "install-timers.sh").read_text()
    assert (
        'install_atomically 755 "${ROOT}/scripts/deploy_release.py" '
        '"${LIBEXEC_DIR}/deploy_release.py"' in installer
    )
    for name in ("savethedate-deploy.service", "savethedate-deploy.timer"):
        assert f"  {name}\n" in installer


def test_the_deploy_script_needs_nothing_but_the_standard_library() -> None:
    """it13 runs it with /usr/bin/python3, outside the project's virtualenv."""
    source = (ROOT / "scripts" / "deploy_release.py").read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imports <= {
        "__future__",
        "collections",
        "dataclasses",
        "fcntl",
        "os",
        "pathlib",
        "re",
        "subprocess",
        "sys",
        "time",
        "urllib",
    }
