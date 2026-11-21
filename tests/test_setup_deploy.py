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
