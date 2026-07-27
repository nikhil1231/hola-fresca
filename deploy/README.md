# deploy/ — LAN testing deployment and backups

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

## Backups

`holafresca-backup.{service,timer}` run `python -m app.backup` daily: the mapping
export into `exports/*.csv`, then a gzipped whole-database snapshot into
`~/backups/holafresca/`, keeping the newest 7. Roughly 32 MB per snapshot from a
260 MB database, ~16 s. Both steps open the database read-only.

`OnCalendar=03:00` with `Persistent=true`: the laptop is usually off then, so in
practice most runs happen shortly after the next boot, which is the intent — a
backup that only runs on machines left on overnight is not a backup.

Note the testing checkout's `data/` is a symlink to the main checkout's, so there
is one database and backing up the main one covers both.

```sh
systemctl --user start holafresca-backup.service   # run now
python -m app.backup status                        # what exists, how big
journalctl --user -u holafresca-backup.service     # what happened
```

Restoring is `gunzip -c <snapshot> > data/holafresca.db` with the app stopped.
Verify with `PRAGMA integrity_check` before trusting it.

Committing `exports/` is deliberately left manual — the timer refreshes the CSVs
but does not commit, because writing to whatever branch happens to be checked out
is a surprising thing for a background job to do. The snapshot is the disaster
copy; the git history of `exports/` is for reviewing how a mapping decision
changed over time.
