# deploy/ — LAN testing deployment, public access, and backups

Runs the cumulative **Testing stack** of HolaFresca on the laptop at
`http://<laptop-ip>:8100`, always pinned to the head of the local
`integration/hola-fresca` branch that ticket-runner builds. One server
(`run.py`) serves both the API and the built SPA.

## Pieces

- `sync-integration.sh` — fetch + hard-reset the testing checkout to the
  integration head; reinstalls Python deps / rebuilds the frontend only when
  their inputs changed; `--restart` also restarts the service when the head moved.
- `holafresca-testing.service` — the app (`run.py` → uvicorn on :8100, serving
  the built `frontend/dist` + `/api`). `ExecStartPre` runs the sync so every
  start lands on head.
- `holafresca-testing-sync.{service,timer}` — run the sync with `--restart` every
  minute so new deploys go live automatically (belt-and-braces alongside the main
  repo's `post-commit` hook, which triggers an immediate sync).

## Topology (laptop)

- The main checkout (`~/Documents/Programming/AI/HolaFresca`) is where
  ticket-runner builds `integration/hola-fresca` locally (project publisher is
  `none` — nothing is pushed or deployed off-box). Its `.git/hooks/post-commit`
  triggers a sync when a commit lands on the integration branch.
- A **separate** checkout `~/Documents/Programming/AI/HolaFresca-testing` serves
  :8100. Its git `origin` is *that* local repo, so it fetches the integration
  branch directly. It has its own `.venv`, its own built `frontend/dist`, and a
  `data` symlink → the main checkout's `data/` (the gitignored recipe DB + raw
  cache) — never reset away.
- Not the same as the ticket-runner dashboard on :4600, or the local Vite dev
  server on :5173.

## Install / update

```sh
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now holafresca-testing.service holafresca-testing-sync.timer
```

`sync-integration.sh` runs from this `deploy/` dir inside the testing checkout;
because it is tracked, `git reset --hard` restores it instead of losing it.

## Deploying from another machine

The live site is `holafresca-dev.service`, which serves the **main checkout's
working tree** on :8100 — so a deploy is a `git pull` in that tree plus whatever
the new commits imply, not a build-and-ship.

```sh
hf-deploy            # from the laptop: deploy origin/main
hf-deploy --check    # dry run — print what it would do, change nothing
hf-deploy --force    # rebuild and restart even if already at head
```

- `update.sh` — runs **on the box**. Fast-forwards to `origin/main`, then does
  only the work the diff calls for: `pip install` if `requirements.txt` moved,
  `npm ci` if the lockfile did, a frontend rebuild if anything under `frontend/`
  did, and a database snapshot if `alembic/versions/` gained a file. Then
  `alembic upgrade head`, restart, and poll `/api/health` for 30 s.
- `deploy.sh` — runs **on the dev machine**; the above over ssh. The alias is
  `alias hf-deploy="$HOME/Documents/Programming/AI/HolaFresca/deploy/deploy.sh"`.

Push first. This deploys what GitHub has, not what is on your disk.

### The backstop

`holafresca-deploy.{service,timer}` poll `origin/main` every five minutes and
run the same `update.sh --poll`, so a push still goes live when you cannot reach
the box — sent from a phone, laptop shut, tailnet down. `hf-deploy` remains the
normal path; this is the safety net, which is why it is five minutes and not
one. Nothing needs to be running on the dev machine.

```sh
systemctl --user list-timers holafresca-deploy.timer   # when it next fires
journalctl --user -u holafresca-deploy.service -f      # what it has been doing
systemctl --user start holafresca-deploy.service       # do not wait for the tick
```

`--poll` differs from a manual run in what counts as a failure. A dirty tree, a
diverged tree, the wrong branch, an unreachable origin: only a human clears
those, so unattended they are skips. Failing the unit every five minutes for a
condition the timer cannot fix turns `systemctl --user status` into noise and
hides the deploys that broke for real — so a failed unit here always means a
deploy that genuinely broke. It is also silent when there is nothing to do,
which is the answer roughly 288 times a day.

Both entry points take an `flock` on `.git/holafresca-deploy.lock`, since the
timer and a manual `hf-deploy` can now fire at the same moment and two deploys
interleaving over one working tree would be a bad afternoon. The lock is in
`.git` rather than `$TMPDIR` because a systemd unit and a login shell need not
agree on what `$TMPDIR` is, and two private lock files are the same as none.
`--check` takes no lock, so a dry run never queues behind a running deploy.

Note that "already deployed" means origin is an *ancestor* of `HEAD`, not equal
to it: testing equality treats an unpushed local commit as a deploy, with
nothing to merge but a service restart — which under a five-minute timer bounces
the live site forever.

Two things it does deliberately differently from `sync-integration.sh`:

- **No `git reset --hard`.** That script owns its checkout; this one does not.
  The live tree is also the tree that gets edited on the box, so uncommitted
  tracked changes abort the deploy rather than being flattened. Untracked files
  are ignored — they are usually scratch, and blocking on them means a stray
  `.log` stops a deploy.
- **It runs migrations.** Nothing runs them at boot, so a deploy is the only
  place they happen; a model that ships without one passes tests and breaks
  production. The snapshot goes first because the nightly backup can be a day
  old and `alembic downgrade` is not a restore.

If the health check fails it prints the last good revision and the `git reset`
to get back to it — but deliberately does not run it, because once a migration
has applied, rewinding the code alone lands on a schema the old code has never
seen.

`deploy.sh` tries the box at several addresses in order and uses the first that
answers: Tailscale MagicDNS, the full `.ts.net` name, `.local` over mDNS, then
the LAN address. Tailscale is the normal route but not a hard dependency — if
the tailnet is down and you are in the house, the last two still work.
`HOLAFRESCA_HOST=<addr>` overrides the list. Nothing here is exposed publicly:
this is ssh on the private network, not through the tunnel.

## Getting to it from outside the house — Cloudflare Tunnel + Access

`cloudflared` runs on the laptop and dials *out* to Cloudflare, so there is no
port forwarded, no inbound firewall rule and no public IP anywhere. Cloudflare
Access sits in front of the hostname doing Google sign-in against an email
allowlist, which is why the app has no login page of its own.

- `cloudflared.service` — the tunnel (user unit, like the others here).
- `setup-tunnel.sh` — creates the tunnel, writes `~/.cloudflared/config.yml`
  pointing at `localhost:8100`, adds the DNS record, installs the unit.
- `~/.cloudflared/` holds the config and the tunnel credentials. Outside the
  repo deliberately: that JSON file is a bearer token for the tunnel.

The binary is user-installed at `~/.local/bin/cloudflared` (same arrangement as
`rclone` — there is no passwordless sudo on this box).

### One-time setup

```sh
cloudflared tunnel login          # browser: pick the zone to authorise
deploy/setup-tunnel.sh hola.example.com
```

Then in the Cloudflare dashboard, **Zero Trust → Access → Applications**: add a
self-hosted app for that hostname, with a policy of *Allow* / *Emails* listing
who gets in, and Google as the login method. Free for up to 50 users.

Finally, tell the app which Access instance to trust — in the gitignored `.env`
at the repo root, then `systemctl --user restart holafresca-dev.service`:

```
HOLAFRESCA_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
HOLAFRESCA_ACCESS_AUD=<the application's Audience tag>
HOLAFRESCA_ACCESS_HOSTNAME=hola.example.com
HOLAFRESCA_ACCESS_OWNER_EMAIL=you@example.com
```

Set the Access session duration to something long (a month) — it is short by
default, and every lapse costs a round trip through Google.

Lapses themselves are handled: `frontend/src/api/session.js` tags API calls with
`X-Requested-With`, which is what makes the edge answer an expired request with
a same-origin 401 instead of a cross-origin 302 that `fetch` follows and then
cannot read (a redirect surfaces as a bare `TypeError`, indistinguishable from
being offline, and the tab just fills with errors). On a 401 it reloads, because
only a document load can follow the chain out to Google and back. Nothing else
in this API returns 401, which is what makes the signal safe to act on.

### What actually authenticates a request

`app/api/access.py`, and it is worth being precise about, because the laptop
still answers on `0.0.0.0:8100` for the LAN and Tailscale.

- The `Cf-Access-Authenticated-User-Email` header is **never read**. Cloudflare
  sets it, but so can anyone else on the LAN — it is a claim, not a proof. Only
  the signed assertion is trusted, checked against the team's published keys for
  signature, audience, issuer and expiry.
- A request addressed to the public hostname *without* a valid assertion is
  refused, not fallen back on. That is the case that matters if the Access
  policy is ever off or misconfigured: otherwise a stranger arrives as the owner.
- A request to the laptop's own address without an assertion is still the
  bootstrap account, exactly as before. The home network is trusted; that is a
  decision, not an oversight, and it is the thing to revisit by binding `run.py`
  to `127.0.0.1` if it ever stops being true.
- Leave `HOLAFRESCA_ACCESS_TEAM_DOMAIN` or `_AUD` unset and none of it is
  enforced — which is what local dev and the test suite run as.

A verified address that has no account gets one created, because Access already
vetted it at the edge: adding a household member is a change to the Access
policy, not a database chore. New accounts are never admin. The one exception is
`HOLAFRESCA_ACCESS_OWNER_EMAIL`, whose first sign-in *claims* the bootstrap row
rather than starting a second account beside years of plan history.

```sh
systemctl --user status cloudflared.service
journalctl --user -u cloudflared.service -f      # connections, reconnects
cloudflared tunnel list
```

## Backups

`holafresca-backup.{service,timer}` run daily: the mapping export into
`exports/*.csv`, a gzipped whole-database snapshot into `~/backups/holafresca/`
keeping the newest 7, then an `rclone sync` of that directory to Google Drive.
Roughly 32 MB per snapshot from a 260 MB database, ~16 s to make and ~5 s to
upload. Both local steps open the database read-only.

The sync line is prefixed `-` so a network failure cannot mark the unit failed
and mask a local backup that did succeed — the on-disk copy is the one that must
never silently stop. `--max-age 7d` mirrors the local retention rather than
letting `sync` delete anything the pruner has not already dropped.

`OnCalendar=03:00` with `Persistent=true`: the laptop is usually off then, so in
practice most runs happen shortly after the next boot, which is the intent — a
backup that only runs on machines left on overnight is not a backup.

Note the testing checkout's `data/` is a symlink to the main checkout's, so there
is one database and backing up the main one covers both.

```sh
systemctl --user start holafresca-backup.service   # run now
python -m app.backup status                        # what exists locally
journalctl --user -u holafresca-backup.service     # what happened
rclone lsl "gdrive:HolaFresca Backups/snapshots"   # what made it offsite
```

Restoring is `gunzip -c <snapshot> > data/holafresca.db` with the app stopped.
Verify with `PRAGMA integrity_check` before trusting it.

Drive holds `snapshots/` (rolling 7 days) and `raw-cache/`, a one-off zip of
`data/raw/` — 25,636 payloads, ~100 MB. The raw cache is re-scrapeable in
principle but that means 16k requests against HelloFresh, so it is worth the
one-time upload; it changes rarely enough not to belong in the daily job.

The `gdrive` remote uses OAuth user credentials against a personal Google Cloud
project. A service account cannot work here: files it uploads are owned by the
service account, which has zero storage quota, and the escapes Google documents
(Shared Drives, domain-wide delegation) both need Workspace rather than a
consumer account. rclone lives at `~/.local/bin/rclone`, outside the repo.

`commit-exports.sh` commits and pushes the refreshed CSVs, as the step straight
after the export. This used to be manual, on the grounds that writing to
whatever branch happens to be checked out is a surprising thing for a background
job to do. That was right while the box was the machine you worked on. It is not
that machine any more — it is only ever deployed to — so manual had quietly
become never, and the history stopped accumulating. The original worry is
answered by refusing to run on anything but `main` level with origin, rather
than by not running.

The snapshot is still the disaster copy; the git history of `exports/` is for
reviewing how a mapping decision changed over time.

It never leaves an unpushed commit behind, which is the whole design. HEAD ahead
of origin is harmless on its own — the deploy reads it as already deployed — but
the next push from the laptop makes the two diverge, and a diverged tree is one
the deploy timer skips *silently*. So the commit and the push are one operation
and a failed push rolls the commit back; the CSVs stay dirty and tomorrow tries
again. For the same reason it will not commit when origin has moved ahead:
fast-forwarding is the deploy timer's job, and doing it here would land prod on
new code with a stale `dist/` and an unmigrated database.

It stages with `git add --update -- exports/` — tracked, already-modified files
only, never `-A` — refuses if anything outside `exports/` is staged, and takes
the deploy lock, since a 03:00 backup and a five-minute deploy timer will
collide eventually.

`update.sh` correspondingly exempts `exports/` from its dirty-tree guard. That
guard is there to protect hand-edits to `app/` and `frontend/`, which it still
catches; blocking on a CSV that a background job rewrote means one failed
overnight push takes the deploy path down with it until somebody notices.
