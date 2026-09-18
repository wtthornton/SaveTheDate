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
cd ~/code/SaveTheDate
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

The moving parts:

| Thing | What it is | Check it |
| --- | --- | --- |
| Production stack | Docker Compose project `savethedate-prod` | `./scripts/prod.sh status` |
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
cd ~/code/SaveTheDate
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

If the app is crash-looping right after a deploy, the migration is the first
suspect:

```bash
./scripts/prod.sh compose logs migrate
```

A migration that fails **stops the release on purpose**, so the app will not start
against a half-migrated schema. That is the system working. Fix the migration, or
redeploy the previous commit.

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
cd ~/code/SaveTheDate
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
cd ~/code/SaveTheDate

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

```bash
cd ~/code/SaveTheDate
git pull
./scripts/prod.sh deploy
```

`deploy` rebuilds the image, runs migrations to completion, and only then starts the
app. It waits for `/health` and fails loudly if the app does not come back.

Two things that are easy to get wrong:

- **Rebuilding re-resolves dependencies.** There is no lockfile, so an image built
  months from now may pull newer versions of FastAPI and friends than the ones this
  was tested against. If a deploy breaks for no reason you can see in the diff, that
  is the first suspect. The previous image is still on disk — `docker images
  savethedate-prod` — and starting it again is a fast way back.
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
