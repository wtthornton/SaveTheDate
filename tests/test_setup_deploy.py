"""Bill's one-time it13 setup is one command, and it copies only his key. TAP-12474.

`deploy/setup-deploy.sh` runs here for real, against a temporary HOME, with
`systemctl` and `loginctl` faked on PATH. A key in the SaveTheDate allowed_signers
file is a production approval, so the tests check which lines land there and where
the script refuses.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "setup-deploy.sh"
INSTALLER = ROOT / "deploy" / "install-timers.sh"


def _public_key(directory: Path, name: str) -> str:
    key = directory / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
        check=True,
    )
    return key.with_suffix(".pub").read_text().strip()


class Box:
    def __init__(self, tmp: Path) -> None:
        self.bill = f'bill@example.com namespaces="git" {_public_key(tmp, "bill")}'
        bot = f"release-bot@websitedup {_public_key(tmp, 'bot')}"
        lookalike = f'bill@example.com.evil namespaces="git" {_public_key(tmp, "evil")}'
        self.home = tmp / "home"
        self.home.mkdir()
        # The deploy directory the units run from. Production has been moved.
        self.deploy_dir = self.home / "srv" / "savethedate"
        self.deploy_dir.mkdir(parents=True)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.systemctl_calls = tmp / "systemctl-calls"
        self._fake("loginctl", "echo yes")
        self._fake("systemctl", f'echo "$*" >> {self.systemctl_calls}')
        self.source = tmp / "websitedup_allowed_signers"
        self.source.write_text(f"{lookalike}\n{bot}\n{self.bill}\n")
        self.target = self.home / ".config" / "savethedate" / "allowed_signers"

    def _fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\nexit 0\n")
        path.chmod(0o755)

    def setup(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._bash(SCRIPT, *args)

    def install(self) -> subprocess.CompletedProcess[str]:
        """`install-timers.sh` on its own, as the deploy runs it after each move."""
        return self._bash(INSTALLER)

    def _bash(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *args],
            env={
                **os.environ,
                "HOME": str(self.home),
                "USER": "wtthornton",
                "PATH": f"{self.bin}:{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def enabled_deploy_timer(self) -> bool:
        if not self.systemctl_calls.exists():
            return False
        return any(
            "enable --now savethedate-deploy.timer" in line
            for line in self.systemctl_calls.read_text().splitlines()
        )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_it_copies_only_the_named_principal_at_mode_600_and_installs(box: Box) -> None:
    """Not the release bot's key, and not a principal that merely starts the same."""
    result = box.setup(str(box.source), "bill@example.com")

    assert result.returncode == 0, result.stderr
    assert box.target.read_text() == f"{box.bill}\n"
    assert stat.S_IMODE(box.target.stat().st_mode) == 0o600
    assert stat.S_IMODE(box.target.parent.stat().st_mode) == 0o700
    assert (box.home / ".local/libexec/savethedate/deploy_release.py").is_file()
    assert box.enabled_deploy_timer()
    assert "wrote" in result.stdout


def test_running_it_again_changes_nothing(box: Box) -> None:
    box.setup(str(box.source), "bill@example.com")
    before = box.target.stat().st_mtime_ns

    again = box.setup(str(box.source), "bill@example.com")

    assert again.returncode == 0, again.stderr
    assert "unchanged" in again.stdout
    assert box.target.stat().st_mtime_ns == before


def test_a_principal_with_no_line_is_refused_and_nothing_is_written(box: Box) -> None:
    result = box.setup(str(box.source), "someone@example.com")

    assert result.returncode != 0
    assert "REFUSED" in result.stderr
    assert "bill@example.com" in result.stderr  # it lists what the file does hold
    assert not box.target.exists()
    assert not box.enabled_deploy_timer()


@pytest.mark.parametrize(
    "bad_line",
    [
        'bill@example.com namespaces="git"',
        'bill@example.com namespaces="git" # ssh-',
        'bill@example.com namespaces="git" ssh-ed25519 AAAAtruncated',
    ],
)
def test_a_line_without_a_valid_public_key_is_refused(box: Box, bad_line: str) -> None:
    """Checked per line with ssh-keygen, so a junk line beside a good one still fails."""
    box.source.write_text(f"{box.bill}\n{bad_line}\n")

    result = box.setup(str(box.source), "bill@example.com")

    assert result.returncode != 0
    assert not box.target.exists()


def test_a_missing_signers_file_is_refused(box: Box) -> None:
    result = box.setup(str(box.home / "nope"), "bill@example.com")

    assert result.returncode != 0
    assert not box.target.exists()


def test_another_approver_already_in_the_file_is_not_dropped_silently(box: Box) -> None:
    box.target.parent.mkdir(parents=True)
    box.target.write_text('ops@example.com namespaces="git" ssh-ed25519 AAAA\n')

    result = box.setup(str(box.source), "bill@example.com")

    assert result.returncode != 0
    assert "ops@example.com" in result.stderr
    assert box.target.read_text().startswith("ops@example.com")


def test_an_empty_principal_is_refused(box: Box) -> None:
    result = box.setup(str(box.source), "")

    assert result.returncode != 0
    assert not box.target.exists()


# -- before production is moved (TAP-10322) ------------------------------------------


def test_before_the_move_the_installer_leaves_the_old_units_and_deployer_alone(box: Box) -> None:
    """The old deployer runs this release's installer after it deploys this release.

    The units here run from `~/srv/savethedate`. Until Bill has made that directory,
    installing them would point the nightly backup at a script that is not there, and
    replace the deployer with one that refuses every tick. So nothing is installed,
    the old units keep running from the old checkout, and it exits 0: the old
    deployer would otherwise report the release as a failed reinstall.
    """
    box.deploy_dir.rmdir()
    libexec = box.home / ".local" / "libexec" / "savethedate"
    libexec.mkdir(parents=True)
    (libexec / "deploy_release.py").write_text("# the deployer from before the move\n")
    unit_dir = box.home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "savethedate-backup.service").write_text("# the old unit\n")

    result = box.install()

    assert result.returncode == 0, result.stderr
    assert (libexec / "deploy_release.py").read_text() == "# the deployer from before the move\n"
    assert sorted(p.name for p in unit_dir.iterdir()) == ["savethedate-backup.service"]
    assert (unit_dir / "savethedate-backup.service").read_text() == "# the old unit\n"
    assert not box.systemctl_calls.exists()
    assert "NOT INSTALLED" in result.stderr
    assert "Moving production out of" in result.stderr


def test_the_installer_reads_the_deploy_directory_from_the_units_it_installs() -> None:
    """One place says where production runs from. The installer's check follows it."""
    installer = INSTALLER.read_text()

    assert "srv/savethedate" not in installer
    assert "WorkingDirectory=" in installer


# -- install-timers.sh replaces what is running (TAP-12499) -------------------------


def test_the_installed_deploy_script_is_replaced_never_rewritten_in_place(box: Box) -> None:
    """The deploy now reinstalls itself, so the file being replaced is the running one.

    A hard link to the old copy still reads the old bytes afterwards, so the old file
    was not written into. That alone does not prove a rename: GNU `install` passes it
    too, because it unlinks the destination before writing a new one, leaving a moment
    with no file there. The static test below is what pins the rename. This one checks
    the result: the right bytes and mode, and no temporary file left behind.
    """
    libexec = box.home / ".local" / "libexec" / "savethedate"
    libexec.mkdir(parents=True)
    installed = libexec / "deploy_release.py"
    installed.write_text("# the copy that is running\n")
    held = box.home / "held"
    os.link(installed, held)

    result = box.install()

    assert result.returncode == 0, result.stderr
    assert held.read_text() == "# the copy that is running\n"
    assert installed.read_bytes() == (ROOT / "scripts" / "deploy_release.py").read_bytes()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert sorted(p.name for p in libexec.iterdir()) == ["deploy_release.py"]
    unit_dir = box.home / ".config" / "systemd" / "user"
    assert all(not p.name.startswith(".") for p in unit_dir.iterdir())


def test_every_installed_file_arrives_by_rename_from_the_same_directory() -> None:
    """Never a moment where the destination is missing or half written.

    `install` and `cp` onto the destination both open it for writing, or unlink it
    and then write a new one; a deploy started in that window would run a truncated
    script, and a daemon-reload would read a truncated unit. A temporary file in the
    destination's own directory is renamed over it, which is atomic on one filesystem.
    """
    text = INSTALLER.read_text()
    body = text.split("install_atomically() {", 1)[1].split("\n}", 1)[0]
    commands = [line.strip() for line in body.splitlines() if line.strip()]
    assert 'tmp="$(mktemp "$(dirname "$dest")/.$(basename "$dest").XXXXXX")"' in commands
    # Renamed into place, and only then forgotten by the EXIT trap that removes it.
    assert commands[-2:] == ['mv -f -- "$tmp" "$dest"', 'tmp=""']
    writes_dest = [c for c in commands if '"$dest"' in c and not c.startswith("tmp=")]
    assert writes_dest == ['mv -f -- "$tmp" "$dest"']

    outside = text.replace(body, "")
    assert 'trap \'if [ -n "$tmp" ]; then rm -f -- "$tmp"; fi\' EXIT' in outside
    assert "install -m" not in outside
    assert 'install_atomically 644 "${ROOT}/deploy/${unit}" "${UNIT_DIR}/${unit}"' in outside
    assert (
        'install_atomically 755 "${ROOT}/scripts/deploy_release.py" '
        '"${LIBEXEC_DIR}/deploy_release.py"' in outside
    )


def test_a_failed_rename_leaves_no_temporary_file_and_fails(box: Box) -> None:
    """Under `set -e` a failing `mv` would otherwise strand the dot-file it was moving."""
    libexec = box.home / ".local" / "libexec" / "savethedate"
    libexec.mkdir(parents=True)
    installed = libexec / "deploy_release.py"
    installed.write_text("# the copy that is running\n")
    (box.bin / "mv").write_text("#!/bin/sh\nexit 1\n")
    (box.bin / "mv").chmod(0o755)

    result = box.install()

    assert result.returncode != 0
    assert installed.read_text() == "# the copy that is running\n"
    unit_dir = box.home / ".config" / "systemd" / "user"
    left = [p.name for d in (libexec, unit_dir) if d.exists() for p in d.iterdir()]
    assert sorted(left) == ["deploy_release.py"]
