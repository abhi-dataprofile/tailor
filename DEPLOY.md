# Deploying — the "GitHub kind of thing"

Short answer: **yes, and it's easy — but not GitHub *Pages***. GitHub Pages only serves
static files and can't run Python, Playwright, or the crawler. What you want is:

> **push to GitHub → a host builds the container → it's live.**

GitHub holds the code; a container host (Render / Railway / Fly) runs it. Supabase is the DB.

## Architecture (one container does most of it)
```
                    ┌────────────────────────── Render / Railway / Fly ──────────────────────────┐
  browser  ─────►   │  tailor-web   (serve.py)   — serves the UI + /api/* apply endpoints         │
                    │  tailor-worker             — worker.py (hourly crawl) + apply.py (auto-apply)│
                    └───────────────┬────────────────────────────────────────────────────────────┘
                                    │
                              Supabase  (Postgres: jobs, profiles, applications, …)
```
The **same Python server serves the frontend and the API**, so there's no CORS/split-host
hassle. `serve.py` already reads `$PORT` and binds `$HOST` (0.0.0.0 in the container).

## Fastest path — Render (GitHub-connected, push-to-deploy)
1. Push this repo to GitHub (see below).
2. On https://render.com → **New → Blueprint**, pick your repo. It reads `render.yaml`
   and creates two services: `tailor-web` and `tailor-worker`.
3. In each service's **Environment**, paste the secrets (never commit them):
   `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`, and an LLM key.
4. First deploy runs `db/schema.sql`? **No** — run that once yourself in the Supabase SQL editor.
5. Done. Every `git push` redeploys.

Railway and Fly.io work the same way (both detect the `Dockerfile`). Fly: `fly launch`.

## Put it on GitHub
This folder is already a git repo. From here:
```bash
git add -A
git commit -m "Tailor: app + crawler + apply engine"
gh repo create tailor --private --source=. --push      # needs the GitHub CLI, or:
# git remote add origin git@github.com:<you>/tailor.git && git push -u origin main
```
`.gitignore` already excludes `.env`, `.venv`, and `*.log`, so **no secrets or local state
get committed**. Double-check with `git status` before the first push.

## Cost / sizing
- Playwright + Chromium needs real RAM — use a **starter/paid** instance (~512MB–1GB), not free.
- The worker is CPU-light but network-steady; a small instance is fine.

## Security before you expose it publicly
The apply endpoints submit real applications, so when `HOST=0.0.0.0`:
- **Turn on Supabase auth** — `_req_user()` then ties every apply to a signed-in user, and
  `_apply_gate()` enforces per-user limits. Without auth, anyone hitting `/api/apply-browser`
  could submit as the default user. Don't run it public without auth.
- Keep `SUPABASE_SERVICE_KEY` in the server env only; the browser only ever gets the anon key.
- Auto-submit stays per-user and off by default.

## Static-only alternative (frontend on GitHub Pages)
If you only want the **résumé-tailoring UI** public (no backend crawl/apply), you *can* put
`index.html` + `dashboard.html` on GitHub Pages — but Find/Apply/cloud features that call
`/api/*` won't work without the Python server. For the full product, use the container path above.
