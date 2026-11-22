# Runbook — the Lisa & Bill wedding site

**Who this is for:** whoever is fixing this while Bill cannot be reached. It assumes
you are comfortable with Linux, Docker and SSH, and assumes you have never seen this
project before. You do not need to understand the application to follow it.

**The wedding is Sunday 20 February 2028, in Port Aransas, Texas.** The date cannot
move.

---

## The one thing that matters

**The guest list is irreplaceable.** Guests receive a link containing a token, and
that token is the only credential — there is no login. If the `guests` table is lost,
every link in every inbox stops working, and there is no way to reissue them without
knowing who was invited.

So, before you do anything that changes the database:

```bash
cd ~/srv/savethedate
./scripts/backup.sh          # takes about a second; uploads off this machine
```

And two commands that must **never** be run against production:

| Never run | Why |
| --- | --- |
| `alembic downgrade base` | Drops every table. It defaults to the **dev** database, which is its own trap, but pointed at production it is total loss. |
| `docker compose ... down -v` | The `-v` removes the volume, which is the database. `scripts/prod.sh down` deliberately offers no way to pass it. |

---

## What is running, and where

Everything is on this one machine (`it13`), reached from the internet through a
**Cloudflare Tunnel** that dials outward. No ports are forwarded; there is no public
IP to find.

| Hostname | What serves it |
| --- | --- |
| `wedding.tapphouse.co` | **Production.** The wedding site and every guest invite link. |
| `savethedate.tapphouse.co` | **Production.** The public save-the-date card. Same process. |
| `dev-wedding.tapphouse.co` | A throwaway review instance with invented guests. Not production. |
| `dev-savethedate.tapphouse.co` | The card on that same review instance. |
| `home.tapphouse.co` | **Not ours.** Home Assistant, a CNAME straight to Nabu Casa. It does not touch this tunnel. Leave it alone. |

Both production hostnames reach **one process on one port** (`127.0.0.1:8100`). The
application decides which page to serve from the `Host` header. If the wrong page
appears on a hostname, the cause is `SAVE_THE_DATE_HOSTS` in
`docker-compose.prod.yml`, not DNS.

**Production lives in `~/srv/savethedate`, the deploy directory, and nowhere else**
(TAP-10322). It is a clone of the repository that nobody edits: the deploy unit moves
it from one signed tag to the next. Every command in this runbook is run from there.
It does not need `~/code` to exist, and nothing in it is built on this machine.

| In `~/srv/savethedate` | What it is |
| --- | --- |
| `docker-compose.prod.yml`, `scripts/`, `deploy/` | From the live tag. Never edited by hand. |
| `.env.prod` | The secrets: the database password. Not in git. |
| `.env.backup` | The backup bucket's credentials. Not in git. |
| `image.env` | The image production runs, pinned by tag and digest. One line, not in git. The deploy unit writes it. |
| `.backups/` | A week of local dump copies. The real backups are off this machine. |

The moving parts:

| Thing | What it is | Check it |
| --- | --- | --- |
| Production stack | Docker Compose project `savethedate-prod` | `./scripts/prod.sh status` |
| Deploys | user timer `savethedate-deploy.timer`, every five minutes | `systemctl --user list-timers 'savethedate-*'` |
| Tunnel | systemd **user** service `cloudflared-tapphouse` | `systemctl --user status cloudflared-tapphouse` |
| Nightly backup | user timer `savethedate-backup.timer`, 03:30 | `systemctl --user list-timers 'savethedate-*'` |
| Weekly restore drill | user timer `savethedate-restore-drill.timer`, Sun 04:30 | same |

These are **user** services, which only survive logout because lingering is enabled
(`loginctl show-user wtthornton -p Linger` says `yes`). If that is ever turned off,
the tunnel and the backups stop when the last session ends.

---

## "The site is down"

Work down this list. Each step tells you what a pass means.

### 1. Is it actually down, or is it one browser?

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://wedding.tapphouse.co/
```

- **200** — the site is up. If somebody still sees a broken page, skip to
  [The design looks broken](#the-design-looks-broken).
- **502 / 503** — Cloudflare is up but cannot reach the app. Go to step 2.
- **A timeout or DNS failure** — the tunnel is down, or the house has no internet.
  Go to step 3.

### 2. 502 or 503: the app is not answering

```bash
cd ~/srv/savethedate
./scripts/prod.sh status
```

If the containers are missing or stopped:

```bash
./scripts/prod.sh up
```

If they are running but `/health` does not answer, read the logs — the reason is
almost always in the last twenty lines:

```bash
./scripts/prod.sh logs
```

`/health` checks the database as well as the process (TAP-7734). A 503 with
`"database": "unreachable"` means the app is up and Postgres is not: look at the `db`
container, not the app. `/health/live` answers ok whenever the process does, so if it
answers and `/health` does not, restarting the app will not help.

Every log line is one JSON object, and each request writes one line with its
`request_id`, `path`, `status` and `duration_ms`. The same id comes back to the
browser as the `X-Request-ID` response header, so a host who can find it in their
browser's network tab points you straight at the request. Invite tokens show up as
`[token]`, on purpose. To pull out the errors, or one request:

```bash
./scripts/prod.sh compose logs --no-log-prefix app | jq -cR 'fromjson? | select(.level == "ERROR")'
./scripts/prod.sh compose logs --no-log-prefix app | jq -cR 'fromjson? | select(.request_id == "…")'
```

An error carries its traceback in the `exception` field. If `SENTRY_DSN` is set in
`.env.prod`, each error is also sent to that collector, tagged with the same
`request_id`, and with invite tokens shown as `[token]` there too (TAP-12484). With it
empty, nothing leaves this box and the logs are the only record.

If the app is crash-looping right after a deploy, the migration is the first
suspect:

```bash
./scripts/prod.sh compose logs migrate
```

A migration that fails **stops the release on purpose**, so the app will not start
against a half-migrated schema. That is the system working. Fix the migration, or
redeploy the previous commit.

If the app or database restarted and the logs simply stop with no error, check
whether it hit its memory ceiling (384M for the app, 512M for the database, set in
`docker-compose.prod.yml`, TAP-10296):

```bash
docker inspect -f '{{.Name}} OOMKilled={{.State.OOMKilled}}' savethedate-prod-app-1 savethedate-prod-db-1
```

`OOMKilled=true` means the limit did its job — something grew without bound. Find
what grew before raising the number; the limits sit at roughly three and five times
the measured peaks.

### 3. Timeout or DNS failure: the tunnel

```bash
systemctl --user status cloudflared-tapphouse
journalctl --user -u cloudflared-tapphouse -n 50 --no-pager
systemctl --user restart cloudflared-tapphouse
```

Restarting the tunnel costs a few seconds of the dev hostname and nothing else. It
**cannot** affect Home Assistant, which does not go through it.

If you edited `~/.cloudflared/config.yml`, validate before restarting — an invalid
file leaves the tunnel down:

```bash
~/.local/bin/cloudflared --config ~/.cloudflared/config.yml tunnel ingress validate
~/.local/bin/cloudflared --config ~/.cloudflared/config.yml tunnel ingress rule https://wedding.tapphouse.co
```

Timestamped backups of that file sit beside it as `config.yml.bak-*`.

If the tunnel is healthy and the house internet is out, there is nothing to do here.
Cloudflare will keep serving 502 until the line returns; the tunnel reconnects by
itself.

---

## The design looks broken

Almost certainly a **cached stylesheet**, not a broken deploy. Cloudflare serves
`/static/*` with a four-hour browser TTL.

Every template references CSS through `static_url()`, which appends a content hash,
so a rebuilt file lands at a new URL and the cache cannot answer for it. If somebody
still sees a stale page, have them hard-reload once.

If you changed a template or `app/static/src/app.css`, you must rebuild and commit
the stylesheet, or the class you added does nothing:

```bash
~/.local/bin/tailwindcss -i app/static/src/app.css -o app/static/app.css
```

`tests/test_stylesheet.py` fails if you forget.

---

## Restoring the database

This is the procedure the weekly drill rehearses. **Read all of it before starting.**

### First: is there anything to restore from?

```bash
cd ~/srv/savethedate
./scripts/restore-drill.sh
```

This downloads the newest backup, restores it into a scratch database, counts the
guests and drops the scratch database again. It never writes to production. If it
prints `GUESTS RESTORED: <n>` and exits 0, the backup is good.

If it fails, read why — the failures are specific: an empty remote means backups were
never running; a dump more than two days old means the timer has stopped; a shortfall
against the manifest means the restore is incomplete.

### Then: restore into production

Only do this if production data is actually lost or corrupt. It **replaces** the
current contents.

```bash
cd ~/srv/savethedate

# 1. Take a dump of the current state first, whatever state that is. If this is a
#    corruption rather than a loss, you may want it back.
./scripts/backup.sh || true

# 2. Fetch the backup you want. List what is available:
set -a; . .env.backup; set +a
~/.local/bin/rclone lsf "$BACKUP_REMOTE/daily/" --include '*.dump' | sort

# 3. Download the one you chose.
~/.local/bin/rclone copy "$BACKUP_REMOTE/daily/savethedate-YYYYMMDDTHHMMSSZ.dump" /tmp/

# 4. Stop the app so nothing writes during the restore. Leave the database running.
./scripts/prod.sh compose stop app

# 5. Restore. --clean drops and recreates each object; --exit-on-error means a
#    partial restore fails loudly instead of looking like a success.
./scripts/prod.sh compose exec -T db pg_restore -U savethedate -d savethedate \
  --clean --if-exists --no-owner --no-acl --exit-on-error \
  < /tmp/savethedate-YYYYMMDDTHHMMSSZ.dump

# 6. Count the guests before you trust it.
./scripts/prod.sh compose exec -T db psql -U savethedate -d savethedate \
  -c 'select count(*) from guests;'

# 7. Start the app again.
./scripts/prod.sh compose start app
./scripts/prod.sh status
```

### Then: check a real invite link still works

Restoring the database restores the tokens, so links that were already sent keep
working. Prove it:

```bash
TOKEN=$(./scripts/prod.sh compose exec -T db psql -U savethedate -d savethedate -At \
        -c 'select invite_token from guests limit 1;')
curl -sS -o /dev/null -w '%{http_code}\n' "https://wedding.tapphouse.co/invites/$TOKEN"
```

`200` means a guest's link works.

---

## Deploying a change

Production deploys itself from a release tag that Bill has signed (TAP-12474).
Nobody logs in to it13 to run `scripts/prod.sh deploy`.

**To release**, Bill signs a tag on a commit that is already on `main`, pushes it,
and pushes the image built from it. Both happen on nlt-dev. The tags are `v1`, `v2`,
and so on, with a plain number and no dots:

```bash
git fetch origin
git -c gpg.format=ssh -c user.signingkey="$HOME/.ssh/id_ed25519.pub" \
    tag -s v3 -m "v3: <what this release carries>" origin/main
git push origin v3
scripts/build-image.sh <repository> v3
```

`<repository>` is the image repository production pulls from. It is the part of the
line in `~/srv/savethedate/image.env` before `:v<N>@sha256:`.

The signature is the production approval. The image is what ships: it13 builds
nothing (TAP-10322). `scripts/build-image.sh` builds from the commit the tag names
(`git archive`, so nothing uncommitted can get in), labels the image with that
commit, and pushes it as `<repository>:v3`. The two steps can run in either order.
A tag whose image is not there yet is refused, nothing changes, and the next tick
looks again.

Within about five minutes of both, `savethedate-deploy.timer` on it13 runs
`savethedate-deploy.service`, which:

1. fetches `main` and the tags into `~/srv/savethedate`;
2. looks at the `v<N>` tags numbered above the live one, newest first. A tag
   **counts** only if `git tag -v` verifies it against
   `~/.config/savethedate/allowed_signers` and the commit it names is on
   `origin/main`. Tags that don't count are skipped and logged, so an unsigned tag
   cannot block a signed one. If none count, the tick is refused and nothing changes;
3. takes the newest tag that counts, and does nothing if that tag has failed before.
   It never falls back to an older approval;
4. refuses if the commit that is live does not run the pinned image: its
   `docker-compose.prod.yml` must read `SAVETHEDATE_IMAGE` and have no `build:`. A
   rollback puts the previous pin back, and that brings nothing back if the previous
   commit builds its own image. A commit from before TAP-10322 is refused this way,
   and the fix is the one-time move below, not a deploy. Then it
   pulls `<repository>:v<N>` and reads its digest. The image must carry the tag's
   commit in its `org.opencontainers.image.revision` label. If the image is missing,
   or was built from a different commit, the tick is **refused**: nothing has moved,
   the tag is not marked failed, and the next tick tries again;
5. checks out the tag (detached), writes `image.env` with the image pinned as
   `<repository>:v<N>@sha256:<digest>`, and runs that tag's `scripts/prod.sh deploy`:
   pull the pinned image, run `migrate` to completion on its own, run `up -d`, and
   wait for `/health`. If any of that fails, it records the tag in
   `~/.local/state/savethedate/deploy/failed-tags`, checks out the previous commit,
   puts the previous line back in `image.env`, starts the previous app with
   `--no-deps` so the previous code's migrate never runs, and **fails the unit**.
   The previous image is still on the box, so going back needs no registry.
   A failed tag is never retried. Tag the fix as the next number;
6. once the app is settled, at the new tag or back at the previous commit, runs
   that checkout's own `deploy/install-timers.sh` (TAP-12499). That replaces the
   installed copy of `scripts/deploy_release.py` and the units with the ones from
   the commit now serving. Once the installed copy has this step (see below), a
   release that changes the deploy script or a unit installs itself; nobody logs
   in to rerun the installer. The new copy verifies the *next* tag. The copy that
   verified this one was already installed, so a tag's own code never decides
   whether that tag ships. A `deploy_release.py` that does not compile is refused
   and the working copy is kept (exit 4, below), so a bad tag cannot replace the
   verifier with one that fails every tick.

**Once, if the installed copy predates TAP-12499.** Only the installed copy runs the
reinstall step. An installed copy older than TAP-12499 does not have that step, so
the first tag that carries it would ship without installing it. If
`~/.local/libexec/savethedate/deploy_release.py` has no `def reinstall`, rerun the
one-time setup below once, after a tag that carries TAP-12499 is live. It is
idempotent. Once deploys have run, leave out the `git pull`, as that section says.

```bash
grep -q 'def reinstall' ~/.local/libexec/savethedate/deploy_release.py || echo "predates TAP-12499: rerun setup once"
```

**Every release turns the backup and drill timers back on.** The reinstall runs
`deploy/install-timers.sh`, which runs `enable --now` on
`savethedate-backup.timer` and `savethedate-restore-drill.timer`. A timer you
turned off by hand starts again at the next deploy or rollback. To keep one off for
longer, stop releasing until it can come back, or change `install-timers.sh` in a
release.

**A rollback to a commit older than TAP-12499 breaks the reinstall chain.** The
reinstall after a rollback installs that older commit's `deploy_release.py`, and
that copy has no reinstall step. From then on no deploy installs itself, including
the deploy of a later tag that carries TAP-12499 again, until the one-time setup
line is run once more. The `grep` above tells you whether you are in that state.

**If that reinstall fails after a good deploy**, the release stays live. Nothing is
rolled back, because the app passed `/health`. The unit exits **4** and fails, so
`./scripts/prod.sh status` lists it under FAILED UNITS until the next tick, and
the journal says **REINSTALL FAILED** and that the installed deploy copy may be
stale. The next tick finds the tag live and changes nothing, running whatever copy
is installed. The following release reinstalls as usual. To catch up sooner, fix
the cause (the log shows the installer's own output) and run
`deploy/install-timers.sh` from `~/srv/savethedate`. If the reinstall fails after
a rollback, the unit still exits 1 for the failed deploy, and the log says
REINSTALL FAILED too.

The unit's exit codes: 0 deployed or nothing to do; 1 the deploy failed and the
previous app is back; 2 refused, nothing changed; 3 **ROLLBACK FAILED / PRODUCTION
MAY BE DOWN**; 4 deployed, but the reinstall failed.

**A rollback after a migration that succeeded runs the previous app on the NEW
schema.** Nothing reverses a migration. So the previous app comes back only if the
migration was backward compatible. Write migrations expand-then-contract: add
columns and tables in one release, and drop what the old code reads only in a later
release, after the one that stopped reading it is live. If a release breaks that
rule and then fails its health check, the previous app fails `/health` too. The
deploy then logs **ROLLBACK FAILED / PRODUCTION MAY BE DOWN** rather than claiming
it recovered.

**A mistyped tag number is permanent once it ships.** If `v30` goes live where `v3`
was meant, every later tag has to be numbered above 30. Numbers are cheap; carry on
from `v31`. An unsigned or unmerged tag never ships, so it needs no cleanup. Deleting
it (`git push origin :refs/tags/vN`) only quiets the log.

What a deploy did:

```bash
journalctl --user -u savethedate-deploy.service -n 80 --no-pager
./scripts/prod.sh status        # lists a failed deploy under FAILED UNITS
git -C ~/srv/savethedate describe --tags   # the tag that is live
cat ~/srv/savethedate/image.env            # the image that is live, by digest
```

**The image and the tag are tied by the commit, and by nothing stronger.** The
signature approves a commit; the image says, in a label, which commit it was built
from; the deploy refuses when they differ. That catches the wrong image under the
right tag. It does not defend against someone who can push to the registry, since
they can write any label. Whoever can push to `<repository>` can ship to production.

A deploy killed halfway (a reboot, the 20-minute timeout) leaves
`~/.local/state/savethedate/deploy/attempt` behind. The next tick rolls that deploy
back as a failure. If the log says **ROLLBACK FAILED / PRODUCTION MAY BE DOWN**, the
attempt file is kept and every tick retries the rollback. Look at
`./scripts/prod.sh status` and the app's logs straight away. If the log says
**STUCK**, the attempt file is unreadable. No deploy runs until someone checks
production and removes that file by hand.

**Migrations run before the app is touched, on purpose (TAP-12474).** `up` on its own looks
like it gates on `migrate`, but Compose recreates every container before it starts
any of them. With a failing migration, the old app was already gone and the new one
never started. `run --rm migrate` touches nothing else, and Alembic runs each Postgres
migration in a transaction, so a failed migration leaves the previous app serving on
an unchanged schema.

### One-time setup on it13 (Bill)

The deploy unit runs an **installed copy** of `scripts/deploy_release.py` from
`~/.local/libexec/savethedate/`, not the copy in the checkout. The checkout is the
thing it moves, and the code that decides what ships must not move with it. This
setup installs the first copy and the units. From then on every deploy and every
rollback reinstalls them from the commit that is live (step 6 above).

Bill pastes this one line on it13. Nothing is edited by hand:

```bash
cd ~/srv/savethedate && git pull --ff-only && deploy/setup-deploy.sh ~/.config/websitedup/allowed_signers tapp.thornton@gmail.com
```

`deploy/setup-deploy.sh SIGNERS_FILE PRINCIPAL` copies the allowed_signers line for
that principal, and only that one, into `~/.config/savethedate/allowed_signers` at
mode 600. It then runs `deploy/install-timers.sh`. It refuses, and writes nothing,
in four cases: the principal has no line; a line holds no valid key (each one is
checked with `ssh-keygen -l`); the target already lists a different principal; or
the first field lists several principals at once. The WebsiteDup file also
lists WebsiteDup's release bot. That key is left out on purpose, because a signature
from any key in the SaveTheDate file is a production approval.

The `git pull` is for this first setup only. Once deploys run, the checkout sits
detached at the live tag, and `git pull` fails there. To run it again (say, after
rotating the key), leave the pull out:

```bash
cd ~/srv/savethedate && deploy/setup-deploy.sh ~/.config/websitedup/allowed_signers tapp.thornton@gmail.com
```

An unchanged signers file is left alone.

`install-timers.sh` on its own will not enable `savethedate-deploy.timer` until
`allowed_signers` exists and is non-empty.

The checkout must stay clean. The deploy refuses to move a checkout with uncommitted
changes to tracked files. `.env.prod`, `.env.backup` and `image.env` are gitignored
and are not affected.

### Moving production out of `~/code` (Bill, once)

Until TAP-10322, production ran from the development checkout `~/code/SaveTheDate`
and built its image there. This moves it to `~/srv/savethedate`. The database is not
touched: the Compose project is named `savethedate-prod` in the compose file, not
after the directory, so the stack started from the new directory uses the same
`savethedate-prod_pgdata` volume. Take a backup first anyway.

`vN` below is the first release tag that carries TAP-10322, and `<repository>` is
the image repository you chose. it13 must be able to `docker pull` from it and
nlt-dev must be able to `docker push` to it.

Nothing breaks if this waits. Until it is done, it13 keeps running the deploy script
and units that were installed before TAP-10322, from `~/code/SaveTheDate`, and they
keep working on releases that carry TAP-10322:

- `scripts/prod.sh` finds no `image.env`, sees that the app container was created from
  the local build `savethedate-prod:latest`, and builds in the checkout as it always
  did. It prints `NOT PINNED` each time. Once the app runs a pinned image this stops:
  a missing `image.env` is then refused.
- `deploy/install-timers.sh` finds no `~/srv/savethedate`, installs nothing, and says
  `NOT INSTALLED`. The old units and the old deploy script stay as they are, so the
  nightly backup keeps running from the old checkout.

On **it13**, stop the deploy timer for the length of the move, so a tick cannot deploy
from the old checkout while you are starting production from the new one.

```bash
systemctl --user stop savethedate-deploy.timer
cd ~/code/SaveTheDate && ./scripts/backup.sh
```

On **nlt-dev**, sign and push `vN` as in "Deploying a change", then build and push
its image. The last line it prints is the pin:

```bash
scripts/build-image.sh <repository> vN
# SAVETHEDATE_IMAGE=<repository>:vN@sha256:...
```

On **it13**, make the deploy directory, at the tag, with the secrets and the pin:

```bash
mkdir -p ~/srv
git clone https://github.com/wtthornton/SaveTheDate.git ~/srv/savethedate
cd ~/srv/savethedate
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$HOME/.config/savethedate/allowed_signers" tag -v vN
git checkout --detach vN
cp -p ~/code/SaveTheDate/.env.prod ~/code/SaveTheDate/.env.backup .
echo 'SAVETHEDATE_IMAGE=<repository>:vN@sha256:...' > image.env    # the line build-image.sh printed
./scripts/prod.sh deploy
deploy/setup-deploy.sh ~/.config/websitedup/allowed_signers tapp.thornton@gmail.com
```

`prod.sh deploy` pulls the image and replaces the running app with it. The last line
reinstalls the three units, which now point at `~/srv/savethedate`, and the deploy
script, and starts the deploy timer again.

Then prove production no longer needs the old checkout:

```bash
mv ~/code/SaveTheDate ~/code/SaveTheDate.retired
cd ~/srv/savethedate
./scripts/prod.sh down && ./scripts/prod.sh up     # a few seconds of 502
./scripts/prod.sh status                           # health: ok, no FAILED UNITS
systemctl --user start savethedate-backup.service
journalctl --user -u savethedate-backup.service -n 20 --no-pager          # "done. Remote now holds ..."
systemctl --user start savethedate-restore-drill.service
journalctl --user -u savethedate-restore-drill.service -n 30 --no-pager   # "GUESTS RESTORED: <n>"
```

Once that passes, the secrets in the retired checkout are spare copies of live
credentials. Delete `~/code/SaveTheDate.retired/.env.prod` and `.env.backup`.

The review instance (`dev-wedding.tapphouse.co`) is separate and is not moved by
this. If it was running from `~/code/SaveTheDate` on this box, the rename stops it.

### Break glass: only if the deploy unit itself is broken

This is not a way to deploy. It skips every check the unit makes: the signature,
`origin/main`, the rollback. Use it only to restore service with a tag Bill has
already signed. Check it first:

```bash
cd ~/srv/savethedate
git fetch --tags origin
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$HOME/.config/savethedate/allowed_signers" tag -v v3
git checkout --detach v3
cat image.env        # keep this line: it is the way back
docker pull <repository>:v3
docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' <repository>:v3
echo 'SAVETHEDATE_IMAGE=<repository>:v3@sha256:...' > image.env    # the digest printed above
./scripts/prod.sh deploy
```

`prod.sh` refuses an `image.env` that is not exactly one line pinned by tag and
digest. Backups are not affected by it: `scripts/backup.sh` and the restore drill
talk to the running database container directly and never read `image.env`. To go back, put
the line you kept back in `image.env`, check out the tag it names, and run
`./scripts/prod.sh compose up -d --no-deps db app`.

Three things that are easy to get wrong:

- **Building re-resolves dependencies, and it happens once per release.** There is
  no lockfile, so an image built months from now may pull newer versions of FastAPI
  and friends than the ones this was tested against. If a deploy breaks for no reason
  you can see in the diff, that is the first suspect. Production never rebuilds: it
  runs the digest in `image.env`, and a failed deploy goes back to the previous digest
  on its own. Building the same tag twice gives two different images, so do not
  re-push a tag that is live.
- **The review instance is separate.** `scripts/review-instance.sh reload` has
  nothing to do with production. Its root also behaves differently on purpose:
  `dev-wedding.tapphouse.co/` redirects into a guest's invitation so the token-gated
  pages can be reviewed, which `wedding.tapphouse.co/` must never do. That difference is
  gated on `REVIEW_INSTANCE` and asserted in both directions.
- **Each deployment links to its own wedding site.** The save-the-date card's "Visit
  the wedding site" button follows `PUBLIC_BASE_URL` — `dev-wedding.tapphouse.co` on
  the review instance (set in `scripts/review-instance.sh`), `wedding.tapphouse.co` in
  production (set in `docker-compose.prod.yml`). It was hard-coded to production once,
  so the card on `dev-savethedate` walked reviewers out of the review instance; while
  production was down that read as the button being broken. Both sides are asserted —
  `tests/test_review_instance.py` and `tests/test_production_stack.py` — so a promotion
  cannot carry one environment's link into the other.

---

## Adding the one real host account

Only needed once, and it may already be done. Check first:

```bash
./scripts/prod.sh compose exec -T db psql -U savethedate -d savethedate \
  -c 'select email from hosts;'
```

If there is no real account:

1. Generate a token and put it in `.env.prod` as `HOST_REGISTRATION_TOKEN=...`:
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
2. `./scripts/prod.sh deploy`
3. Register exactly one account:
   ```bash
   curl -sS -X POST https://wedding.tapphouse.co/auth/register \
     -H 'Content-Type: application/json' \
     -d '{"email":"...","password":"...","registration_token":"..."}'
   ```
4. **Delete the `HOST_REGISTRATION_TOKEN` line from `.env.prod`** and
   `./scripts/prod.sh deploy` again.

Leaving it set means anyone who finds `/auth/register` can create events on the
wedding's own database.

---

## Things that look like emergencies and are not

- **`wedding.tapphouse.co` returns 502 while you are deploying.** Expected, for a few
  seconds. It returns 502 rather than quietly serving the development site, which is
  deliberate: a guest-facing hostname showing invented guests is worse than a brief
  outage.
- **The app restarted by itself overnight.** `restart: unless-stopped` did its job.
  `docker inspect -f '{{.RestartCount}}' savethedate-prod-app-1` shows how often.
- **A guest reports "too many requests".** The limiter allows 60 requests per minute
  per address. A household behind one address reloading a lot can reach it; it clears
  within a minute. It is per-address, so it cannot lock out anyone else.

---

## Who to contact

- **Bill Thornton** — built this. tapp.thornton@gmail.com
- **Cloudflare** holds the domain `tapphouse.co`, its DNS, the tunnel, and the backup
  bucket. Losing access to that account is the single worst administrative failure
  available; everything else here is recoverable.
- `tapphouse.co` expires **2027-08-01**, inside the RSVP window. Auto-renew is on. If
  the card on file has lapsed, the domain — and therefore every invite link — goes
  with it.
