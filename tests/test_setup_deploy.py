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

BILL = 'bill@example.com namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBill'
BOT = "release-bot@websitedup ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBot"
LOOKALIKE = 'bill@example.com.evil namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEvil'


class Box:
    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "home"
        self.home.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.systemctl_calls = tmp / "systemctl-calls"
        self._fake("loginctl", "echo yes")
        self._fake("systemctl", f'echo "$*" >> {self.systemctl_calls}')
        self.source = tmp / "websitedup_allowed_signers"
        self.source.write_text(f"{LOOKALIKE}\n{BOT}\n{BILL}\n")
        self.target = self.home / ".config" / "savethedate" / "allowed_signers"

    def _fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\nexit 0\n")
        path.chmod(0o755)

    def setup(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
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
    assert box.target.read_text() == f"{BILL}\n"
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


def test_a_line_without_a_public_key_is_refused(box: Box) -> None:
    box.source.write_text('bill@example.com namespaces="git"\n')

    result = box.setup(str(box.source), "bill@example.com")

    assert result.returncode != 0
    assert not box.target.exists()


def test_a_missing_signers_file_is_refused(box: Box) -> None:
    result = box.setup(str(box.home / "nope"), "bill@example.com")

    assert result.returncode != 0
    assert not box.target.exists()
