"""Deploy the newest release tag Bill has signed. TAP-12474.

    python3 ~/.local/libexec/savethedate/deploy_release.py

`savethedate-deploy.service` runs this on it13 every five minutes. Before it,
production changed only when someone logged in and ran `scripts/prod.sh deploy` by
hand. The approval is now a tag. Bill signs `v<N>` with his own SSH key and pushes it.
That signature is the production approval, and nothing else moves production.

One tick:

1. Fetch `main` and the tags from `origin` into the deploy directory,
   `~/srv/savethedate`: a clone of the repository that is nobody's working copy, with
   `.env.prod` and `image.env` beside the compose file (TAP-10322).
2. Look at the `v<N>` tags numbered above the live one, newest first. A tag counts
   only if its signature verifies against `allowed_signers` and the commit it names is
   on `origin/main`: only merged code that Bill approved ships. Tags that do not count
   are skipped and logged, so an unsigned tag cannot block a real one. If none count,
   the tick is refused and nothing changes.
3. Take the newest tag that counts. Stop quietly if it has failed before. A failed tag
   is never retried, and an older approval is never shipped in its place; the fix is
   a new tag.
4. Refuse if the live commit's compose file does not run the image `image.env`
   pins. A rollback restores the previous pin, which brings nothing back when the
   previous commit builds its own image and ignores the pin (any commit from before
   TAP-10322). Then pull the image that was built for the tag, `<repository>:v<N>`, and read its
   digest. Nothing is built on it13. The repository is the one `image.env` already
   names. The image must carry the tag's commit in its
   `org.opencontainers.image.revision` label, which `scripts/build-image.sh` sets. No
   image yet, or one built from another commit, is a refusal: nothing has moved, and
   the next tick looks again.
5. Move the checkout to the tag, write `image.env` with the image pinned by tag and
   digest, and run the tag's own `scripts/prod.sh deploy`. That is pull, migrate to
   completion, start the app, then wait for `/health`. If that fails, record the tag
   as failed, move the checkout back, put the previous pin back in `image.env` and
   start the previous app without re-running migrations. The unit
   then fails, and `scripts/prod.sh status` lists it under FAILED UNITS. The previous
   app is then running on whatever schema the new migration left; that is safe only
   for backward-compatible migrations, and a /health failure there is reported as
   PRODUCTION MAY BE DOWN.
6. A tick that was killed mid-deploy leaves an `attempt` file, and the next tick rolls
   it back the same way.
7. Once the app is settled, at the new tag or back at the previous commit, run that
   checkout's own `deploy/install-timers.sh` (TAP-12499). It replaces the installed
   copy of this file and the units with the ones from the commit now serving, so the
   installed copy never runs ahead of, or behind, what is live. The attempt file is
   cleared first, so an installer that hangs or crashes cannot send later ticks into
   recovery. If it fails after a good deploy, nothing is rolled back: the unit fails
   with EXIT_REINSTALL_FAILED and later ticks run whatever copy is installed.

This file is stdlib-only and is installed OUTSIDE the checkout it moves
(`deploy/install-timers.sh` copies it). The code that decides what ships is not
replaced by the thing it is about to check out: a tag is verified by the copy already
installed, and its own copy is installed only after it has shipped, to verify the
next one. That is WebsiteDup's ADR-0044 split, verify with the live tag's code.

It never removes a volume and never runs `down`. The guest list is in that volume.
"""

from __future__ import annotations

import fcntl
import json
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

# Every exit but EXIT_DONE fails the unit, so `scripts/prod.sh status` lists it.
EXIT_DONE = 0  # deployed, or nothing to do
EXIT_FAILED = 1  # the deploy failed; the previous app is back and serving
EXIT_REFUSED = 2  # a check before anything moved did not hold; nothing changed
EXIT_ROLLBACK_FAILED = 3  # PRODUCTION MAY BE DOWN; the attempt file is kept for a retry
# The tag deployed and is serving, but install-timers.sh failed afterwards, so the
# installed deploy copy and units may be stale. Nothing is rolled back. TAP-12499.
EXIT_REINSTALL_FAILED = 4

COMPOSE_FILE = "docker-compose.prod.yml"
# The one line of `image.env`, which Compose reads as `${SAVETHEDATE_IMAGE}`.
IMAGE_VARIABLE = "SAVETHEDATE_IMAGE"
# repository:tag@sha256:<64 hex>. `scripts/prod.sh` refuses anything else too.
PIN_PATTERN = re.compile(
    r"^(?P<repository>[^\s@]+):(?P<tag>[^\s@:/]+)@(?P<digest>sha256:[0-9a-f]{64})$"
)
# Set by `scripts/build-image.sh` to the commit the image was built from.
REVISION_LABEL = "org.opencontainers.image.revision"
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
            # Not under ~/code. Production must keep running with that directory gone.
            checkout=Path(os.environ.get("SAVETHEDATE_DEPLOY_DIR", home / "srv" / "savethedate")),
            allowed_signers=home / ".config" / "savethedate" / "allowed_signers",
            state_dir=home / ".local" / "state" / "savethedate" / "deploy",
        )

    @property
    def image_file(self) -> Path:
        """The image production runs, pinned by tag and digest. Gitignored."""
        return self.checkout / "image.env"

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
        except Refusal as refusal:
            self.log(f"REFUSED: {refusal}. Nothing was changed.")
            return EXIT_REFUSED
        if tag is None:
            return EXIT_DONE
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

        live = self._cmd("git", "describe", "--exact-match", "--tags", "--match", "v*", "HEAD")
        live_tag = live.stdout.strip() if live.returncode == 0 else None
        live_number = (tag_number(live_tag) if live_tag else None) or 0
        newer = sorted(
            (t for t in tags if (tag_number(t) or 0) > live_number),
            key=lambda t: tag_number(t) or 0,
            reverse=True,
        )
        if not newer:
            self.log(f"{live_tag} is live and is the newest release tag.")
            return None

        # Newest first, skipping any tag that is not an approval. An unsigned `v99`
        # that anyone could push must not stand in front of the next real release.
        refused: list[str] = []
        failed = self._failed_tags()
        for tag in newer:
            problem = self.problem_with(tag)
            if problem is not None:
                self.log(f"Ignoring {tag}: {problem}.")
                refused.append(f"{tag} ({problem})")
                continue
            if tag in failed:
                # Never fall back to an older approval: that would ship something
                # Bill has since superseded.
                self.log(f"{tag} failed to deploy earlier and is not retried. Tag a fix as new.")
                return None
            return tag
        raise Refusal(f"no tag newer than what is live is approved: {', '.join(refused)}")

    def problem_with(self, tag: str) -> str | None:
        """Why `tag` is not a production approval, or None when it is one."""
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
            return f"not signed by a key in {self.config.allowed_signers}"
        on_main = self._cmd(
            "git", "merge-base", "--is-ancestor", f"{tag}^{{commit}}", "origin/main"
        )
        if on_main.returncode != 0:
            return "signed, but names a commit that is not on origin/main"
        self.log(f"{tag} is signed by {match.group(1)} and is on origin/main.")
        return None

    def current_pin(self) -> tuple[str, str]:
        """The pinned image `image.env` names now, and the repository it is in.

        The pin is the way back. The repository is where the next release is pulled
        from, so it is written down in exactly one place.
        """
        try:
            lines = self.config.image_file.read_text().splitlines()
        except FileNotFoundError:
            raise Refusal(
                f"{self.config.image_file} is missing, so there is no image repository to "
                "pull from and no image to go back to"
            ) from None
        # The same rule as scripts/prod.sh: exactly one assignment in the file, and it
        # is the pin, written with no spaces and no `export`. Compose takes the last
        # value it reads, so a pin with anything after it is not a pin.
        assignments = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
        name, _, value = assignments[0].partition("=") if len(assignments) == 1 else ("", "", "")
        match = PIN_PATTERN.match(value) if name == IMAGE_VARIABLE else None
        if match is None:
            raise Refusal(
                f"{self.config.image_file} does not hold exactly one line, "
                f"{IMAGE_VARIABLE}=repository:tag@sha256:digest"
            )
        return value, match.group("repository")

    def require_a_way_back(self) -> None:
        """Refuse to deploy over a live commit that does not run the pinned image.

        A rollback checks the previous commit out again and puts the previous pin
        back in `image.env`. That brings the previous app back only if the previous
        commit's compose file runs what `image.env` names. A commit from before
        TAP-10322 does not: it has `build:` and `image: savethedate-prod:latest`, and
        never reads the pin. Rolling back to it would start whatever that local tag
        last pointed at, or rebuild it, and then report the previous app as serving.

        So it is checked here, before anything has moved. Production reaches a pinned
        commit by the one-time move in the runbook, not by a deploy.
        """
        shown = self._cmd("git", "show", f"HEAD:{COMPOSE_FILE}")
        settings = [
            line.strip()
            for line in (shown.stdout if shown.returncode == 0 else "").splitlines()
            if not line.lstrip().startswith("#")
        ]
        reads_the_pin = any("${" + IMAGE_VARIABLE + ":?" in line for line in settings)
        can_build = any(line.startswith("build:") for line in settings)
        if not reads_the_pin or can_build:
            raise Refusal(
                f"the live commit's {COMPOSE_FILE} does not run the image {IMAGE_VARIABLE} "
                "pins (it predates TAP-10322, or builds its own), so a failed deploy could "
                "not be rolled back to it. Move production onto a pinned release by hand: "
                "docs/RUNBOOK.md, 'Moving production out of `~/code`'"
            )

    def pin_for(self, tag: str, repository: str) -> str:
        """Pull the image built for `tag` and return it pinned by tag and digest.

        Runs before anything has moved, so every failure here is a Refusal. An image
        that has not been pushed yet is the ordinary case: the tag can arrive first.
        """
        reference = f"{repository}:{tag}"
        pulled = self._cmd("docker", "pull", "--quiet", reference)
        if pulled.returncode != 0:
            raise Refusal(
                f"{reference} could not be pulled ({pulled.stderr.strip() or 'no output'}). "
                "Build and push it with scripts/build-image.sh on nlt-dev"
            )
        inspected = self._cmd("docker", "image", "inspect", reference)
        if inspected.returncode != 0:
            raise Refusal(f"docker image inspect {reference} failed: {inspected.stderr.strip()}")
        try:
            image = json.loads(inspected.stdout)[0]
            labels = image["Config"].get("Labels") or {}
            digests = [
                name.partition("@")[2]
                for name in image.get("RepoDigests") or []
                if name.partition("@")[0] == repository
            ]
        except (ValueError, LookupError, TypeError, AttributeError):
            raise Refusal(f"docker image inspect {reference} gave nothing readable") from None

        # The signature approves a commit. This is what ties the image to it: built
        # from any other commit, it is not what Bill signed. It catches the wrong
        # image under the right tag. It is not a defence against whoever can push to
        # the registry, who can write any label; the registry is trusted to that extent.
        commit = self._git("rev-parse", f"{tag}^{{commit}}")
        built_from = labels.get(REVISION_LABEL)
        if built_from != commit:
            raise Refusal(
                f"{reference} was built from {built_from or 'an unlabelled commit'}, "
                f"but {tag} names {commit}"
            )
        pin = f"{reference}@{digests[0]}" if len(digests) == 1 else ""
        if PIN_PATTERN.match(pin) is None:
            raise Refusal(f"{reference} has no single registry digest for {repository}: {digests}")
        return pin

    def _write_pin(self, pin: str) -> None:
        # Whole or not at all, like the attempt file: Compose must never read half a line.
        partial = self.config.image_file.with_suffix(".partial")
        partial.write_text(f"{IMAGE_VARIABLE}={pin}\n")
        os.replace(partial, self.config.image_file)

    def deploy(self, tag: str) -> int:
        previous_commit = self._git("rev-parse", "HEAD")
        try:
            self.require_a_way_back()
            previous_pin, repository = self.current_pin()
            pin = self.pin_for(tag, repository)
        except Refusal as refusal:
            self.log(f"REFUSED: {refusal}. Nothing was changed.")
            return EXIT_REFUSED

        self.log(f"Deploying {tag} as {pin} over {previous_commit[:12]}.")
        self._write_attempt(f"{tag} {previous_commit} {previous_pin}\n")
        try:
            self._git("checkout", "--quiet", "--detach", tag)
            self._write_pin(pin)
        except (Refusal, OSError) as problem:
            self.log(f"FAILED: {problem}.")
            return self.fail(tag, previous_commit, previous_pin)
        deployed = self._cmd("scripts/prod.sh", "deploy")
        self.log(deployed.stdout + deployed.stderr)
        if deployed.returncode == 0:
            # Settled before the reinstall, on purpose. The release is live and
            # healthy; a reinstall that fails or is killed must not leave an attempt
            # behind, or the next tick would roll back a good release.
            self.config.attempt_file.unlink()
            self.log(f"DEPLOYED {tag}.")
            if not self.reinstall(tag):
                self.log(f"{tag} is serving and is NOT rolled back; only the reinstall failed.")
                return EXIT_REINSTALL_FAILED
            return EXIT_DONE

        self.log(f"FAILED: {tag} did not deploy. Rolling back to {previous_commit[:12]}.")
        return self.fail(tag, previous_commit, previous_pin)

    def reinstall(self, live: str) -> bool:
        """Install this script and the units from the checkout, which is now `live`.

        It runs the checkout's own `deploy/install-timers.sh`, so the copy the next
        tick runs is the one from the tag (or the rolled-back commit) that is serving.
        Only ever called once that is settled: the code being installed has already
        run as prod.sh, and it decides nothing about the tag it came from. It does
        decide about the next one, which is how a fix to this file ships with the
        release that carries it rather than by hand.

        The installer renames a finished copy over this file, so the process running
        now is untouched, and it only daemon-reloads and enables timers, which affect
        later runs of this unit, not this one.
        """
        try:
            installed = self._cmd("bash", "deploy/install-timers.sh")
        except OSError as exc:
            # bash itself could not be started. Reported like any other failure, so
            # the unit fails with its own exit instead of a traceback.
            outcome = f"could not be started ({exc})"
        else:
            if installed.returncode == 0:
                self.log(f"Installed the deploy script and units from {live}.")
                return True
            self.log(f"{installed.stdout}{installed.stderr}".rstrip())
            outcome = f"exited {installed.returncode}"
        self.log(
            f"REINSTALL FAILED: deploy/install-timers.sh at {live} {outcome}. The installed "
            f"deploy script and units may be stale: they may not be {live}'s. Later ticks "
            "keep running whatever copy is installed."
        )
        self.log(
            f"Once the cause is fixed, run deploy/install-timers.sh from {self.config.checkout} "
            "on it13, or let the next release reinstall it."
        )
        return False

    def _write_attempt(self, text: str) -> None:
        # Written whole or not at all, so a kill mid-write cannot leave a file that
        # every later tick fails to read.
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        partial = self.config.attempt_file.with_suffix(".partial")
        partial.write_text(text)
        os.replace(partial, self.config.attempt_file)

    def recover(self) -> int:
        """A tick that was killed mid-deploy left its attempt behind.

        The checkout already sits at that tag, so without this the next tick would
        read it as live and the half-finished deploy would stand. It is a failure, and
        it is rolled back like one.
        """
        fields = self.config.attempt_file.read_text().split()
        if (
            len(fields) != 3
            or tag_number(fields[0]) is None
            or PIN_PATTERN.match(fields[2]) is None
        ):
            self.log(f"STUCK: {self.config.attempt_file} is not an attempt record: {fields!r}.")
            self.log("A deploy may be half finished. Check scripts/prod.sh status on it13,")
            self.log("then remove the file by hand. No deploy runs until then.")
            return EXIT_ROLLBACK_FAILED
        tag, previous_commit, previous_pin = fields
        self.log(
            f"FAILED: the deploy of {tag} was interrupted. Rolling back to {previous_commit[:12]}."
        )
        return self.fail(tag, previous_commit, previous_pin)

    def fail(self, tag: str, previous_commit: str, previous_pin: str) -> int:
        self._record_failed(tag)
        result = self.rollback(previous_commit, previous_pin)
        if result == EXIT_ROLLBACK_FAILED:
            # Kept after a failed rollback, so the next tick tries the rollback again.
            return result
        # The previous app answers /health, so the attempt is settled, and it is
        # cleared BEFORE the reinstall, as after a good deploy. An installer that hangs
        # until the unit's timeout kills it must not leave an attempt behind: every
        # later tick would restart the previous app, reinstall, and never ship again.
        self.config.attempt_file.unlink()
        # A failure here is logged by reinstall(). The exit stays EXIT_FAILED, because
        # the failed deploy is the news.
        self.reinstall(previous_commit[:12])
        return result

    def rollback(self, previous_commit: str, previous_pin: str) -> int:
        moved_back = self._cmd("git", "checkout", "--quiet", "--detach", previous_commit)
        if moved_back.returncode != 0:
            self.log(f"ROLLBACK FAILED at the checkout: {moved_back.stderr.strip()}")
            self.log("PRODUCTION MAY BE DOWN. Run scripts/prod.sh status on it13.")
            return EXIT_ROLLBACK_FAILED
        # The previous image is still on this box, and the pin names it by digest, so
        # going back is one line in a file and needs no registry.
        try:
            self._write_pin(previous_pin)
        except OSError as exc:
            self.log(f"ROLLBACK FAILED writing {self.config.image_file}: {exc}")
            self.log("PRODUCTION MAY BE DOWN. Run scripts/prod.sh status on it13.")
            return EXIT_ROLLBACK_FAILED
        # --no-deps: the schema may already be at the new revision, which the previous
        # code's `alembic upgrade head` does not know, so migrate must not run again.
        # That also means the previous app now runs on the NEW schema. It comes back
        # only if the migration was backward compatible (expand, then contract in a
        # later release); if it dropped something the previous code reads, /health
        # fails below and this says so.
        step = ("scripts/prod.sh", "compose", "up", "-d", "--no-deps", "db", "app")
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
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
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
