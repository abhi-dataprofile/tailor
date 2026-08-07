# Architecture — job data platform

From a local scratchpad to a persistent, continuously-refreshed job index.

## Components (separation of concerns / CQRS)

```
                 ┌──────────────────────────────────────────┐
                 │  ATS public feeds (Greenhouse/Lever/Ashby)│
                 └───────────────┬──────────────────────────┘
                                 │  fetch (bounded concurrency, retries)
        WRITE PATH        ┌──────▼───────┐
   worker.py ───────────► │   ats.py     │  extract skills / visa / date
   (6h incremental) ────► │ (shared lib) │
                          └──────┬───────┘
                                 │ idempotent UPSERT (source_uid)
                          ┌──────▼───────────────────────────┐
                          │   Supabase / Postgres             │  ← single source of truth
                          │   companies · jobs · profiles     │
                          │   user_jobs · crawl_runs          │
                          └──────┬───────────────────────────┘
        READ PATH                │ query + rank (skill overlap)
   serve.py /api/jobs ◄──────────┘
   serve.py /api/profile
                                 │ JSON
                          ┌──────▼───────┐
                          │  Browser UI  │  LinkedIn-style Jobs section
                          └──────────────┘
```

- **`ats.py`** — the only place that knows how to talk to ATS feeds and turn raw
  postings into normalized rows (skills, visa signal, date). Used by both the
  crawler and the live-discovery endpoint (DRY).
- **`worker.py`** — the crawler. Write path only.
- **`serve.py`** — the API + static host. Read path (`/api/jobs`, `/api/profile`)
  plus the existing on-demand `/api/discover`. Never holds long-lived state.
- **`supabase_client.py`** — thin PostgREST client; the service key lives only
  here (server-side), never in the browser.

## Distributed-systems principles applied

| Principle | How |
|---|---|
| **Idempotency / at-least-once** | `jobs.source_uid = vendor:slug:external_id` is unique; ingestion is an UPSERT, so re-crawls insert only *new* postings and are safe to retry. |
| **Incremental work queue** | Companies are crawled by *due time* (`last_crawled_at < now-6h`), most-due first, in bounded batches — not one giant sweep. Load is spread so the whole ~15.9k dataset cycles within the freshness window. |
| **Fault isolation + backoff** | One bad feed never fails the batch; `fail_count` backs off and auto-disables a feed after N straight errors. |
| **Statelessness** | Worker and API are stateless; all state is in Postgres. Scale by running more workers (partition by company). |
| **Least privilege** | Service key server-side only; the browser talks to `serve.py`, never to Supabase directly. |
| **Observability** | `companies.last_status/last_crawled_at`, `crawl_runs` audit log. |
| **Single source of truth** | Postgres. The browser's localStorage becomes a cache/UX layer, not the system of record. |

## Data model (`db/schema.sql`)

- `companies` — crawl targets + schedule state.
- `jobs` — canonical postings, deduped by `source_uid`; `first_seen_at` is the
  watermark that lets matching process **only new** rows.
- `profiles` — user profile (single `local` user by default; `user_id` column
  makes multi-user + Supabase Auth/RLS a drop-in later).
- `user_jobs` — per-user pipeline state (new/saved/applied/dismissed) + cached match score.
- `crawl_runs` — audit.

## Setup (one time)

1. Create a Supabase project → **SQL Editor** → paste & run `db/schema.sql`.
2. Set env vars (server-side only — never commit them):
   ```bash
   export SUPABASE_URL="https://<ref>.supabase.co"
   export SUPABASE_SERVICE_KEY="<service_role key from Project Settings → API>"
   ```
3. Start the crawler:  `python3 worker.py`  (or `--once` from a cron/systemd timer).
4. Start the app:      `python3 serve.py`  → the `/api/jobs` endpoint now serves the index.

Without the env vars everything still runs — the crawler falls back to `--dry`
and the DB endpoints report `no_db`, so the existing on-device flow is unaffected.

## Operating model (current)

**Operator-run backend + Supabase shared store + multi-tenant read.** You (the
power user) run the heavy pipelines on your laptop — crawl, match, tailor, apply
— using the service key, and push results to Supabase. End users sign up and read
/ interact via the app; **all their data is stored per-user** in Supabase. This
avoids standing cloud infra now, and lifts to hosted workers later unchanged
(same code behind Inngest/SQS).

- **LLM:** `llm.py` — a backend, multi-provider layer (Claude · Gemini · OpenAI ·
  OpenRouter/custom · Ollama). `LLM_PROVIDER=auto` uses hosted keys first, else
  local Ollama, with fallback. Not Claude-only; each pipeline run picks what it wants.
- **Per-user data:** `profiles` (profile + memory/preferences), `user_jobs`
  (pipeline state), `tailorings` (per-user résumé rewrites), `applications`
  (per-user, human-in-the-loop). Shared catalog: `companies`, `jobs`.
- **Isolation:** RLS policies (in `schema.sql`, commented) enforce own-rows-only
  once Supabase Auth is on; the shared catalog is read-only to authed users and
  written only by the operator's service key.
- **Auth:** passwordless **email OTP** (Supabase GoTrue). The browser gets a
  one-time code, exchanges it for a JWT, and sends it as a Bearer token; the
  server resolves the token → `user_id` and scopes every read/write to that user
  (the client-supplied `user_id` is never trusted). Enable by setting
  `SUPABASE_ANON_KEY` (public) alongside the URL — the login gate then appears
  automatically. Sign-in also syncs the user's profile up so pipelines can tailor for them.

## Decisions taken (smart defaults — easy to change)

- **Crawler / pipelines host:** run locally by the operator now; lift to
  Inngest/Trigger.dev (managed) or AWS SQS+Fargate later — same code.
- **Users:** single `local` user today; schema + RLS are multi-user-ready.
- **Freshness:** 6h (`JOB_REFRESH_HOURS`).

## Next increment

LinkedIn-style **Jobs** section in the UI: left list of ranked cards (reading
`/api/jobs`), right detail pane, filters (role/location/remote/visa/date), and
save/apply/dismiss writing to `user_jobs`. Profile edits persist via `/api/profile`.
Best built and tested against a live Supabase instance.
