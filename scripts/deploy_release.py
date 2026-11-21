"""Deploy the newest release tag Bill has signed. TAP-12474.

    python3 ~/.local/libexec/savethedate/deploy_release.py

`savethedate-deploy.service` runs this on it13 every five minutes. Before it,
production changed only when someone logged in and ran `scripts/prod.sh deploy` by
hand. The approval is now a tag. Bill signs `v<N>` with his own SSH key and pushes it.
That signature is the production approval, and nothing else moves production.

One tick:

1. Fetch `main` and the tags from `origin` into the deploy checkout.
2. Take the newest `v<N>` tag. Stop quietly if it is already live, older than what is
   live, or has failed before. A failed tag is never retried; the fix is a new tag.
3. Refuse it unless its signature verifies against `allowed_signers`, and unless the
   commit it names is on `origin/main`. Only merged code ships. A refusal changes
   nothing.
4. Keep the running image as `savethedate-prod:previous`, move the checkout to the tag
   and run the tag's own `scripts/prod.sh deploy`. That is build, migrate to completion,
   start the app, then wait for `/health`.
5. If that fails, record the tag as failed, move the checkout back, restore the
   previous image and start the previous app without re-running migrations. The unit
   then fails, and `scripts/prod.sh status` lists it under FAILED UNITS. A tick that
   was killed mid-deploy leaves an `attempt` file, and the next tick rolls it back the
   same way.

This file is stdlib-only and is installed OUTSIDE the checkout it moves
(`deploy/install-timers.sh` copies it). The code that decides what ships is not
replaced by the thing it is about to check out. A change to this file reaches it13
only when the installer runs again.

It never removes a volume and never runs `down`. The guest list is in that volume.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_ROLLBACK_FAILED = 3

IMAGE = "savethedate-prod:latest"
PREVIOUS_IMAGE = "savethedate-prod:previous"
TAG_PATTERN = re.compile(r"^v(\d+)$")
GOOD_SIGNATURE = re.compile(r'Good "git" signature for (\S+) with')

Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]
HealthCheck = Callable[[], bool]


def run_command(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, check=False)


class Refusal(Exception):
    """A check before anything moved did not hold. Nothing was changed."""


@dataclass(frozen=True)
class Config:
    checkout: Path
    allowed_signers: Path
    state_dir: Path

    @classmethod
    def from_environment(cls) -> Config:
        home = Path.home()
        return cls(
            checkout=Path(os.environ.get("SAVETHEDATE_DEPLOY_DIR", home / "code" / "SaveTheDate")),
            allowed_signers=home / ".config" / "savethedate" / "allowed_signers",
            state_dir=home / ".local" / "state" / "savethedate" / "deploy",
        )

    @property
    def failed_tags_file(self) -> Path:
        return self.state_dir / "failed-tags"

    @property
    def attempt_file(self) -> Path:
        """Written before the checkout moves, removed once the deploy is settled."""
        return self.state_dir / "attempt"


def tag_number(tag: str) -> int | None:
    match = TAG_PATTERN.match(tag)
    return int(match.group(1)) if match else None


class Deployer:
    def __init__(
        self,
        config: Config,
        run: Runner = run_command,
        healthy: HealthCheck | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self._run = run
        self._healthy = healthy
        self.log = log

    # -- plumbing --------------------------------------------------------

    def _cmd(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return self._run(argv, self.config.checkout)

    def _git(self, *argv: str) -> str:
        result = self._cmd("git", *argv)
        if result.returncode != 0:
            raise Refusal(f"git {' '.join(argv)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _failed_tags(self) -> set[str]:
        try:
            return set(self.config.failed_tags_file.read_text().split())
        except FileNotFoundError:
            return set()

    def _record_failed(self, tag: str) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        with self.config.failed_tags_file.open("a") as handle:
            handle.write(f"{tag}\n")

    # -- the tick --------------------------------------------------------

    def run(self) -> int:
        if self.config.attempt_file.exists():
            return self.recover()
        try:
            tag = self.choose_tag()
            if tag is None:
                return EXIT_DONE
            self.verify(tag)
        except Refusal as refusal:
            self.log(f"REFUSED: {refusal}. Nothing was changed.")
            return EXIT_REFUSED
        return self.deploy(tag)

    def choose_tag(self) -> str | None:
        if not self.config.allowed_signers.is_file():
            raise Refusal(f"{self.config.allowed_signers} is missing, so no tag can be verified")
        if self._git("status", "--porcelain", "--untracked-files=no"):
            raise Refusal(f"{self.config.checkout} has uncommitted changes")

        self._git("fetch", "--quiet", "--tags", "--force", "origin", "main")

        tags = [t for t in self._git("tag", "--list", "v*").split() if tag_number(t) is not None]
        if not tags:
            self.log("No release tag yet.")
            return None
        newest = max(tags, key=lambda t: tag_number(t) or 0)

        live = self._cmd("git", "describe", "--exact-match", "--tags", "--match", "v*", "HEAD")
        live_tag = live.stdout.strip() if live.returncode == 0 else None
        live_number = tag_number(live_tag) if live_tag else None
        if live_number is not None and (tag_number(newest) or 0) <= live_number:
            self.log(f"{live_tag} is live and is the newest release tag.")
            return None
        if newest in self._failed_tags():
            self.log(f"{newest} failed to deploy earlier and is not retried. Tag a fix as new.")
            return None
        return newest

    def verify(self, tag: str) -> None:
        verified = self._cmd(
            "git",
            "-c",
            "gpg.format=ssh",
            "-c",
            f"gpg.ssh.allowedSignersFile={self.config.allowed_signers}",
            "tag",
            "-v",
            tag,
        )
        match = GOOD_SIGNATURE.search(f"{verified.stderr}\n{verified.stdout}")
        if verified.returncode != 0 or match is None:
            raise Refusal(f"{tag} is not signed by a key in {self.config.allowed_signers}")
        self.log(f"{tag} is signed by {match.group(1)}.")

        on_main = self._cmd(
            "git", "merge-base", "--is-ancestor", f"{tag}^{{commit}}", "origin/main"
        )
        if on_main.returncode != 0:
            raise Refusal(f"{tag} names a commit that is not on origin/main")

    def deploy(self, tag: str) -> int:
        previous_commit = self._git("rev-parse", "HEAD")
        had_image = self._cmd("docker", "image", "inspect", IMAGE).returncode == 0
        if had_image and self._cmd("docker", "image", "tag", IMAGE, PREVIOUS_IMAGE).returncode:
            self.log(f"REFUSED: could not keep the running image as {PREVIOUS_IMAGE}.")
            return EXIT_REFUSED

        self.log(f"Deploying {tag} over {previous_commit[:12]}.")
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.attempt_file.write_text(f"{tag} {previous_commit} {int(had_image)}\n")
        self._git("checkout", "--quiet", "--detach", tag)
        deployed = self._cmd("scripts/prod.sh", "deploy")
        self.log(deployed.stdout + deployed.stderr)
        if deployed.returncode == 0:
            self.config.attempt_file.unlink()
            self.log(f"DEPLOYED {tag}.")
            return EXIT_DONE

        self.log(f"FAILED: {tag} did not deploy. Rolling back to {previous_commit[:12]}.")
        return self.fail(tag, previous_commit, had_image)

    def recover(self) -> int:
        """A tick that was killed mid-deploy left its attempt behind.

        The checkout already sits at that tag, so without this the next tick would
        read it as live and the half-finished deploy would stand. It is a failure, and
        it is rolled back like one.
        """
        tag, previous_commit, had_image = self.config.attempt_file.read_text().split()
        self.log(
            f"FAILED: the deploy of {tag} was interrupted. Rolling back to {previous_commit[:12]}."
        )
        return self.fail(tag, previous_commit, had_image == "1")

    def fail(self, tag: str, previous_commit: str, had_image: bool) -> int:
        self._record_failed(tag)
        result = self.rollback(previous_commit, had_image)
        if result != EXIT_ROLLBACK_FAILED:
            # Kept after a failed rollback, so the next tick tries the rollback again.
            self.config.attempt_file.unlink()
        return result

    def rollback(self, previous_commit: str, had_image: bool) -> int:
        steps: list[Sequence[str]] = [("git", "checkout", "--quiet", "--detach", previous_commit)]
        if had_image:
            steps.append(("docker", "image", "tag", PREVIOUS_IMAGE, IMAGE))
        # --no-deps: the schema may already be at the new revision, which the previous
        # code's `alembic upgrade head` does not know, so migrate must not run again.
        steps.append(("scripts/prod.sh", "compose", "up", "-d", "--no-deps", "db", "app"))
        for step in steps:
            result = self._cmd(*step)
            if result.returncode != 0:
                self.log(f"ROLLBACK FAILED at `{' '.join(step)}`: {result.stderr.strip()}")
                self.log("PRODUCTION MAY BE DOWN. Run scripts/prod.sh status on it13.")
                return EXIT_ROLLBACK_FAILED
        if not self.app_healthy():
            self.log("ROLLBACK FAILED: the previous app does not answer /health.")
            self.log("PRODUCTION MAY BE DOWN. Run scripts/prod.sh status on it13.")
            return EXIT_ROLLBACK_FAILED
        self.log("Rolled back. The previous app is serving.")
        return EXIT_FAILED

    def app_healthy(self) -> bool:
        if self._healthy is not None:
            return self._healthy()
        # `compose port` prints `127.0.0.1:8100`. The fallback is prod.sh's own default.
        published = self._cmd("scripts/prod.sh", "compose", "port", "app", "8000").stdout.strip()
        port = published.rsplit(":", 1)[-1] if published else "8100"
        url = f"http://127.0.0.1:{port}/health"
        last_error = "no answer"
        for _ in range(60):
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if response.status == 200:
                        return True
                    last_error = f"HTTP {response.status}"
            except (urllib.error.URLError, OSError) as exc:
                # Expected while the container starts; kept so the final failure says why.
                last_error = str(exc)
            time.sleep(1)
        self.log(f"{url}: {last_error}")
        return False


def main() -> int:
    config = Config.from_environment()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with (config.state_dir / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another deploy is running.")
            return EXIT_DONE
        return Deployer(config).run()


if __name__ == "__main__":
    sys.exit(main())
