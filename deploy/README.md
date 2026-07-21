# deploy/ — LAN testing deployment

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
