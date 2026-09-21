# Free-tier deployment

Supabase (PostgreSQL) + Render (the API, same-origin with the built SPA) + GitHub Actions
(scheduled checks, in place of a persistent worker). No component here is paid at the
volumes this project runs at; see each provider's own limits before relying on it.

No code above `src/` cares which host runs it. This doc is account setup and configuration,
not an alternative to `docker/docker-compose.yml` for local development.

## 1. Why not the persistent worker

`product-tracker worker` (§10 of the main README) needs a process that keeps running.
Render's free plan stops a web service between requests and does not offer a free
background-worker service at all, so there is nothing to keep it running on. Instead,
`POST /internal/scheduler/check-all` (added for this deployment) runs one sweep on demand,
guarded by `INTERNAL_SCHEDULER_TOKEN`, and `.github/workflows/scheduled-check.yml` calls it
every three hours using GitHub Actions' own cron — no external cron account needed, and the
request incidentally wakes a sleeping instance back up.

If you later move to a host that can run a persistent process, switch back to
`product-tracker worker` and drop the scheduled workflow; nothing else changes, since both
paths call the same `run_all_checks` service function under a shared claim/lease guard —
running both at once is safe, not just switchable (see `services/check_runner.py`).

## 2. Supabase: the database

1. Create a project at supabase.com (free tier). Note the database password you set.
2. Project Settings → Database → Connection string → **URI**, "Session" mode (not
   "Transaction" pooling mode — this app opens normal long-lived connections, not one per
   statement). Copy it.
3. It arrives as `postgresql://postgres:<password>@<host>:5432/postgres`. This project
   requires the psycopg driver in the URL; insert `+psycopg` after `postgresql`:
   ```
   postgresql+psycopg://postgres:<password>@<host>:5432/postgres
   ```
   (Settings normalises a bare `postgresql://` automatically at startup, so this step is
   optional in practice — but the effective value is always the `+psycopg` form, so setting
   it correctly up front avoids a moment of confusion reading `product-tracker config`.)
4. This is your `DATABASE_URL`. Keep it — Render needs it in step 3.

## 3. Render: the API

This repo's `render.yaml` is a Blueprint: Render reads it and configures the service for
you, rather than you clicking through the dashboard by hand.

1. render.com → New → Blueprint → connect this GitHub repository. Render finds
   `render.yaml` at the root and proposes one service, `product-tracker`, on the free plan.
2. It will prompt for the three `sync: false` variables — set them here, not in the file:
   - `DATABASE_URL` — from step 2.
   - `API_KEY` — a strong random value (e.g. `openssl rand -hex 32`). Required before
     sharing the URL with anyone; see §15 of the main README, "Letting other people in".
   - `INTERNAL_SCHEDULER_TOKEN` — a second, different random value. This is not `API_KEY`
     rotated for another purpose: it guards a route that triggers outbound checks against
     every tracked listing, and the GitHub Actions workflow needs it separately from
     whatever client key you hand out.
3. Deploy. Render's free plan rejects a Blueprint's `preDeployCommand` outright (the
   review step errors on it before anything deploys), so migrations are not run for you.
   Run them yourself, once, from your own machine with `DATABASE_URL` pointed at Supabase:
   ```bash
   alembic upgrade head
   product-tracker stores sync
   product-tracker status
   ```
   Do this before or after the Render deploy — the app never migrates on startup either
   way, so order doesn't matter, only that it happens once before you rely on the API.
4. Verify:
   ```bash
   curl https://<your-service>.onrender.com/health
   curl https://<your-service>.onrender.com/health/ready
   ```
   `/health/ready` should report `database: healthy`. `scheduler: healthy` will say no
   worker has ever reported in — expected here, see §1.

The frontend needs no separate deployment or `VITE_API_URL`: the Dockerfile already builds
the SPA and the API serves it at `/ui` on the same origin, exactly as it does locally. This
is a deliberate choice, not the default for every free-tier plan — splitting the frontend
onto Vercel would additionally require CORS configuration and a frontend code change to
point `frontend/src/api.ts` at a separate origin, for no benefit when one Render service
can already serve both.

## 4. GitHub Actions: the schedule

1. Repo → Settings → Secrets and variables → Actions → New repository secret, twice:
   - `RENDER_APP_URL` — `https://<your-service>.onrender.com`
   - `INTERNAL_SCHEDULER_TOKEN` — the same value set on Render in step 3.
2. `.github/workflows/scheduled-check.yml` is already in the repo and runs on its own —
   nothing to enable. To run it once without waiting three hours: Actions tab →
   "Scheduled check" → Run workflow.
3. Watch a run's log: it wakes the instance, then reports the JSON `check-all` returned
   (`checked`, `skipped`, `failures`, `notifications_sent`).

## 5. Known limitations of this path specifically

- **No browser rendering.** `PLAYWRIGHT_ENABLED=false` in `render.yaml` — a free instance's
  memory does not fit Chromium, and the image built by `docker/Dockerfile`'s default
  `BASE_IMAGE` has no browser installed regardless. Every store this project verifies
  against (main README, "Verified against live retailers") works without it except Amazon
  India's delivery-area localisation.
- **Cold starts.** A free Render instance that has had no traffic sleeps; the next request
  waits out a cold start (tens of seconds). The scheduled workflow accounts for this
  (`Wake the instance` step); a person opening `/ui` right after a quiet period will see the
  same delay once.
- **Sweep timing, not per-product timing.** `CHECK_INTERVAL_SECONDS` is no longer what
  decides when a product is checked — the worker that read it is not running. Every active
  product is checked once per scheduled-check run (every three hours, by the workflow's
  cron), regardless of its individual `check_interval_seconds`. A per-product interval
  shorter than three hours has no effect on this path.
