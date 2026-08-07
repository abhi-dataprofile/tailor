# Cloud storage + background auto-apply — setup

By default Tailor runs **fully local**: your profile lives in the browser, and applying
happens on demand from your laptop. Turn on the cloud to get: your data stored server-side,
multiple users, a career-page crawler that refreshes every few hours, and a background
worker that auto-applies for people who opted in — even when the tab is closed.

Everything below is **your** setup — I can't create the account or hold your keys.

## 1. Create a Supabase project (free)
1. Go to https://supabase.com → New project.
2. Open **SQL Editor**, paste the contents of [`db/schema.sql`](db/schema.sql), and run it.
   That creates `companies`, `jobs`, `profiles`, `user_jobs`, `tailorings`,
   `applications`, `crawl_runs`, and `emails`.
3. **Project Settings → API** — copy three values:
   - Project URL
   - `anon` `public` key
   - `service_role` key  ← **secret, server only, never shipped to the browser**

## 2. Add your keys
```bash
cp .env.example .env
```
Open `.env` and fill in:
```
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=eyJ...            # anon public
SUPABASE_SERVICE_KEY=eyJ...         # service_role (secret)
```
Optionally add an LLM key (`CLAUDE_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY`) so the
backend pipeline can tailor; without one it falls back to heuristics.

## 3. Run it
```bash
./run.sh --workers
```
- `run.sh` (no flag) = web server only.
- `--workers` also starts the **crawler loop** (refreshes career pages every
  `JOB_REFRESH_HOURS`) and the **background auto-applier** (every ~10 min, only for users
  who turned on Auto-submit).

The server prints `Cloud (Supabase): yes` when it picked up your keys, and `/api/config`
returns `"supabase": true`. Passwordless email sign-in then works from the app header.

## What each piece does
| File | Role |
|---|---|
| `supabase_client.py` | PostgREST client (`select/insert/upsert/update`), email-OTP auth |
| `worker.py` | crawls Greenhouse/Lever/Ashby career pages → `jobs` table |
| `pipeline.py` | per-user: match jobs to the résumé, tailor, queue |
| `apply.py` | background applier — submits for opted-in users, rate-limited, idempotent |
| `serve.py` | web + on-demand apply endpoints; reads `.env` via `envload` |

## Safety (unchanged in cloud mode)
- Legal / work-authorization / EEO answers come **only** from what the user entered — never guessed.
- CAPTCHA-protected boards (modern Greenhouse, Workday, iCIMS) are **detected and left for
  manual apply** — no bypassing.
- The `service_role` key stays server-side; the browser only ever gets the `anon` key.
- Auto-submit is **off by default** and per-user; the applier only touches users who opted in.
