# Setup runbook

Get the full system live: Supabase (storage + auth) + the operator pipelines
(crawl / match / tailor) + the app. ~15 minutes, one time.

> You run the pipelines; Supabase stores everything; users sign in and see only
> their own data. Nothing here needs a cloud host — it all runs from your laptop.

---

## 0. Prerequisites

- **Python 3** (`python3 --version` → 3.8+). No other install needed — the
  backend is pure standard library.
- Optional but recommended: an **LLM API key** (OpenAI / Anthropic / Google /
  OpenRouter) for good tailoring, **or** [Ollama](https://ollama.com) running
  locally (`ollama pull qwen2.5:3b`).

---

## 1. Create a Supabase project

1. Go to **https://supabase.com** → **Sign in** (GitHub login is quickest).
2. **New project** → pick your org.
3. Fill in:
   - **Name:** `tailor` (anything).
   - **Database Password:** click **Generate a password**, then **copy it**
     somewhere safe (you rarely need it, but don't lose it).
   - **Region:** the one closest to you.
4. **Create new project** and wait ~2 minutes while it provisions.

---

## 2. Create the tables

1. Left sidebar → **SQL Editor** → **+ New snippet**.
2. Open [`db/schema.sql`](db/schema.sql) from this folder, copy the **whole file**,
   paste it into the editor.
3. Click **Run** (or ⌘/Ctrl-Enter). You should see *Success. No rows returned*.
4. (Optional check) Left sidebar → **Table Editor** — you should now see
   `companies`, `jobs`, `profiles`, `user_jobs`, `tailorings`, `applications`,
   `crawl_runs`.

---

## 3. Grab your keys

1. Left sidebar → **Project Settings** (gear) → **API**.
2. Copy three values:
   - **Project URL** — e.g. `https://abcd1234.supabase.co` → this is `SUPABASE_URL`
   - **`anon` `public`** key → this is `SUPABASE_ANON_KEY` (safe for the browser)
   - **`service_role` `secret`** key → this is `SUPABASE_SERVICE_KEY`
     (⚠️ **secret** — server-side only, never commit it or put it in the browser)

---

## 4. Turn on passwordless email sign-in

1. Left sidebar → **Authentication** → **Providers** → **Email** → make sure it's
   **enabled**. Turn **Confirm email** on (that's what sends the code).
2. **Authentication** → **Emails** (email templates) → **Magic Link** template →
   make sure the body contains the code token:
   ```
   Your login code is: {{ .Token }}
   ```
   (Supabase's default magic-link email uses a link; adding `{{ .Token }}` makes
   the 6-digit code appear so the in-app "enter code" step works.)
3. While testing, Supabase's built-in email works but is rate-limited. For real
   volume, add an SMTP provider under **Authentication → Settings → SMTP**.

---

## 5. Point the app at Supabase (env vars)

In the terminal where you'll run things (macOS/Linux):
```bash
export SUPABASE_URL="https://<your-ref>.supabase.co"
export SUPABASE_ANON_KEY="<anon public key>"
export SUPABASE_SERVICE_KEY="<service_role secret key>"
```
Pick an LLM for the pipeline (any one — or rely on local Ollama):
```bash
export LLM_PROVIDER="auto"                 # auto = hosted key first, else Ollama
export OPENAI_API_KEY="sk-..."             # and/or ANTHROPIC_API_KEY / GEMINI_API_KEY
# custom/cheap: export CUSTOM_API_KEY=... CUSTOM_BASE_URL=https://openrouter.ai/api/v1 CUSTOM_MODEL=...
```
> Tip: put these in a file `env.sh` and run `source env.sh` each session
> (add `env.sh` to `.gitignore` — never commit keys).

---

## 6. Fill the job catalog (crawler)

```bash
python3 worker.py --once     # one pass: seeds companies + ingests new postings
```
- First run seeds ~15.9k companies and starts crawling the most-due ones.
- Re-run `--once` a few times, or run `python3 worker.py` (continuous) to keep it
  fresh every 6h. Check **Table Editor → jobs** — rows should appear.

---

## 7. Match + tailor per user (pipeline)

Users appear in `profiles` after they sign in (step 9). To process everyone:
```bash
python3 pipeline.py          # scores new jobs per user, tailors their top matches
```
- Test the LLM half anytime without the DB: `python3 pipeline.py --dry`.
- Run it on a schedule (cron / `while true; do python3 pipeline.py; sleep 3600; done`).

## 7b. Automated background applications (optional)

For users who opt in, `apply.py` submits to their top matches hands-off:
```bash
python3 apply.py          # all opted-in users   (python3 apply.py --dry to preview safely)
```
A user opts in by setting, on their `profiles.data`:
```jsonc
"auto_apply": { "enabled": true, "min_score": 45, "max_per_run": 10 },
"standing":   { "work_authorized": "Yes", "needs_sponsorship": "No", "salary_expectation": "$150,000" }
```
Safety rails (always on): legal/comp/demographic answers come ONLY from `standing`
(never model-guessed — missing ones skip that job as `awaiting_review`); idempotent
(never double-applies); CAPTCHA/unsupported boards are recorded `manual`, not dropped;
rate-limited. Every attempt is logged in full to `applications.log` and the
`applications` table.

**Submission backends** (auto-picked per job, best first):
1. **Official employer API** — if the company is in `connectors.json`
   (`{ "acme": {"ats":"greenhouse_harvest","key":"<employer key>"} }`). Note: these
   APIs are *employer*-scoped — they only work for companies that partnered with you.
2. **Headless browser (Playwright)** — the general path. Enable with:
   ```bash
   pip install playwright && playwright install chromium
   export APPLY_BROWSER=1
   export APPLY_LIVE=1        # required to actually click submit; without it, it only prepares + screenshots
   ```
   It fills the real form, uploads a PDF résumé, and submits — but **aborts to `manual` on any CAPTCHA/bot-check** (it does not solve them). Test one form safely (never submits):
   `python3 apply_browser.py --url "<a real apply URL>" --headed`
3. **Legacy form engine** — works only on old boards; modern ones return `unsupported_form`.
   Anything unsubmittable is queued `manual`, never dropped.

> Reality check: there is **no public "apply to any company" API** — Harvest/Ashby/
> Workday are employer-side. So without partnerships, the browser backend is the path,
> and it carries fragility + ToS weight + CAPTCHA limits. Budget for selector
> maintenance and expect a meaningful `manual` rate.

---

## 8. Start the app

```bash
python3 serve.py
```
Open **http://localhost:8765**. Because `SUPABASE_ANON_KEY` is set, the **sign-in
gate** appears.

---

## 9. Sign in and verify

1. Enter your email → **Send code** → check your inbox → enter the 6-digit code.
2. You're in; your account chip shows in the top bar. Your profile syncs to
   Supabase (see **Table Editor → profiles**).
3. Fill your profile (or **Import**), then run `python3 pipeline.py` again so it
   tailors for you.
4. Top bar → **Jobs** → your ranked, matched postings with the "Tailored for you"
   panel and **Apply (review first)**.

---

## Going multi-user / production

- **Isolation:** open `db/schema.sql`, uncomment the **RLS** block at the bottom,
  and run it — each user is then locked to their own rows at the database level.
- **Hosting:** to let others reach it, host `serve.py` (Render/Railway/Fly) or move
  the pipelines to Inngest/Trigger.dev + Vercel later — same code (see
  [`ARCHITECTURE.md`](ARCHITECTURE.md)).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Gate never appears | `SUPABASE_ANON_KEY` not set — check `curl localhost:8765/api/config` shows `"auth": true`. |
| "Couldn't send code" | Email provider not enabled (step 4), or Supabase email rate limit — wait or add SMTP. |
| Code rejected | Template missing `{{ .Token }}` (step 4.2), or code expired — resend. |
| Jobs tab says "Connect your database" | `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` not set, or `serve.py` not restarted after setting them. |
| No jobs after crawl | Run `python3 worker.py --once` a few times; check **Table Editor → jobs**. |
| No matches for a user | Run `python3 pipeline.py`; make sure the user's profile has skills. |
| Tailoring empty | No LLM available — set an API key or start Ollama; test with `python3 pipeline.py --dry`. |
