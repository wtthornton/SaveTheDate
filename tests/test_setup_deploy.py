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


# -- install-timers.sh replaces what is running (TAP-12499) -------------------------


def test_the_installed_deploy_script_is_replaced_never_rewritten_in_place(box: Box) -> None:
    """The deploy now reinstalls itself, so the file being replaced is the running one.

    A hard link to the old copy still reads the old bytes afterwards: the installer
    put a new file at that name rather than writing into the old one. And it leaves
    no temporary file behind in the directory.
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
    assert commands[-1] == 'mv -f -- "$tmp" "$dest"'
    writes_dest = [c for c in commands if '"$dest"' in c and not c.startswith("tmp=")]
    assert writes_dest == ['mv -f -- "$tmp" "$dest"']

    outside = text.replace(body, "")
    assert "install -m" not in outside
    assert 'install_atomically 644 "${ROOT}/deploy/${unit}" "${UNIT_DIR}/${unit}"' in outside
    assert (
        'install_atomically 755 "${ROOT}/scripts/deploy_release.py" '
        '"${LIBEXEC_DIR}/deploy_release.py"' in outside
    )
