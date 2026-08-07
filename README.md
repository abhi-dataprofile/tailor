# Tailor — AI job-application platform

Find roles across thousands of career pages, tailor your résumé to each one,
auto-fill and (optionally) submit the application, and track everything —
**Find → Prep → Apply → Track**.

Runs **local-first** (your data in the browser, tailoring on your machine via
[Ollama](https://ollama.com) or heuristics — no keys needed), and scales to a
**cloud deployment** (Supabase + hourly crawler + background auto-applier) when
you add keys.

## Quick start (local)
```bash
python3 serve.py          # → http://localhost:8765
```
First run opens onboarding: upload your résumé (it's parsed and prefilled), confirm
the extra things applications ask — work authorization, mailing address, links —
and you're ready. Tailoring uses a local Ollama model if present, else a heuristic
fallback. Any provider key (Claude / Gemini / OpenAI) can be added in **⚙ Model**.

## Go cloud (multi-user + background auto-apply)
```bash
cp .env.example .env      # add Supabase + LLM keys
./run.sh --workers        # web + hourly crawler + background applier
```

## Docs
| Doc | What |
|---|---|
| [SETUP.md](SETUP.md) | local install + Ollama |
| [CLOUD.md](CLOUD.md) | Supabase storage + background jobs |
| [DEPLOY.md](DEPLOY.md) | containerized deploy (Render / Railway / Fly, push-to-deploy) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | how the pieces fit |

## Safety
Legal / work-authorization / EEO answers are filled **only** from what you enter —
never guessed. CAPTCHA-protected boards (modern Greenhouse, Workday, iCIMS) are
detected and left for manual apply. Auto-submit is **off by default** and per-user.

---

## Local mode — how it works

**Everything can run locally.** The "understanding" and the writing are done by a
model running on *your* machine through [Ollama](https://ollama.com). Nothing is
uploaded, there are no API keys, and it works offline once the model is
downloaded. If Ollama isn't running, the app still works in a lighter
"heuristic" mode (keyword matching + rule-based rewrites) — you just don't get
the full generative quality.

---

## What's in this folder

```
resume-tailor/
├── index.html          ← the app (open this in a browser)
├── README.md           ← this guide
├── start-mac.command   ← double-click to run on macOS
├── start-windows.bat   ← double-click to run on Windows
└── start-linux.sh      ← run on Linux
```

The model itself is **not** in this folder — Ollama downloads and stores it once
(a few GB, in Ollama's own location). This folder stays tiny.

---

## Setup (about 5 minutes, one time)

### Step 1 — Install Ollama

- **macOS:** download from <https://ollama.com/download> and open the app
  (or `brew install ollama`).
- **Windows:** download the installer from <https://ollama.com/download> and run it.
- **Linux:** run `curl -fsSL https://ollama.com/install.sh | sh`.

After installing, Ollama runs in the background and listens on
`http://localhost:11434`.

### Step 2 — Download a model

Open a terminal (on Windows, use **PowerShell** or **Command Prompt**) and run:

```
ollama pull qwen2.5:3b
```

That's the default and a good balance of speed and quality. Alternatives:

| Model              | Size    | Notes                                   |
|--------------------|---------|-----------------------------------------|
| `qwen2.5:1.5b`     | ~1 GB   | Fastest, lightest. Good on any laptop.  |
| `qwen2.5:3b`       | ~1.9 GB | **Default.** Best balance.              |
| `qwen2.5:7b`       | ~4.7 GB | Best writing. Needs a stronger machine. |
| `llama3.2:3b`      | ~2 GB   | Alternative if you prefer Llama.        |

You can change the model any time in the app: **⚙ Model** (top-right).

### Optional — use the Claude API for the best writing

In **⚙ Model** you can paste an **Anthropic API key**. When a key is present, the
app uses Claude for the writing (higher quality than a small local model). If the
key is missing, invalid, expired, or out of credit, it automatically falls back
to your local Ollama model, and then to heuristics — so it never breaks.

Two things to know: your key is stored **only in this browser** and is sent only
to Anthropic, so only use it on a machine you trust; and the Claude API costs
money per use (a cheaper model like `claude-haiku-4-5-20251001` keeps costs low).
This is entirely optional — the app is fully functional with just local Ollama.

### Step 3 — Start the app

Pick whichever is easiest:

**Easiest (recommended) — use the start script.** It serves the folder on
`http://localhost:8765` and opens your browser. Serving from `localhost` means
Ollama accepts the requests automatically (no extra config).

- **macOS:** double-click `start-mac.command`.
  (First time, if macOS blocks it: right-click → Open, or run
  `chmod +x start-mac.command` in Terminal.)
- **Windows:** double-click `start-windows.bat`.
- **Linux:** run `bash start-linux.sh` (or `chmod +x start-linux.sh && ./start-linux.sh`).

These use Python's built-in web server. If you don't have Python, install it
from <https://python.org>, or use any static server you like
(e.g. `npx serve` if you have Node).

**Alternative — open the file directly.** You can just double-click
`index.html`. But because the page then runs from `file://`, Ollama may refuse
the connection for security. If so, allow it by setting one environment
variable and restarting Ollama:

- **macOS:** `launchctl setenv OLLAMA_ORIGINS "*"` then quit and reopen Ollama.
- **Windows:** set a user environment variable `OLLAMA_ORIGINS` = `*`, then
  restart Ollama (quit from the tray and reopen).
- **Linux:** `OLLAMA_ORIGINS="*" ollama serve` (or add it to the systemd service).

Using the start script avoids all of this, so it's the recommended route.

---

## Using it

1. In the app, click **⚙ Model** → **Test connection**. You want the green
   "connected" state. (If it says the model isn't pulled, run the
   `ollama pull …` command from Step 2.)
2. Fill in **Your profile** (step 2) once — name, summary, skills, experience,
   projects, values. It's saved in your browser, so you only do this once.
   Use **Save** (top-right) to export a backup file you can reload later or move
   to another computer.
   - **Import to fill it fast:** at the top of Step 2, **Import from a résumé**     (PDF, Word, or paste — your local model reads it) or **Import from
     LinkedIn**. LinkedIn has two options: upload your profile PDF
     (*More → Save to PDF*), or upload the CSV files from your LinkedIn data
     export (*Settings → Data privacy → Get a copy of your data*). Everything is
     parsed on your device. (Reading PDF/Word files loads a small reader library
     over the internet; pasting text and CSV import work fully offline.)
   - **Memory & preferences** (on the same page) are standing instructions the
     model honors every time it writes — tone, length, what to emphasize, what
     to avoid, target roles, plus any custom facts you add. You keep and refine
     these over time, and a live preview shows the exact structured block the
     model receives. Nothing hidden.
3. Paste a **job description** (step 1). Optionally add the hiring company and
   click **Look up** for context (from Wikipedia).
4. Click **⚡ Auto-tailor my resume**. The app will understand the role, check
   your fit, rewrite your summary and bullets, reprioritize your skills, and
   pick or suggest projects — then drop you on a finished, fully editable
   resume (step 4).
5. Edit anything by clicking it, then **Print / Save as PDF** or
   **Download HTML**.

Your **Step 2 profile is your master data and stays untouched** while you tailor.
Adding skills or bullets, and applying refinements, only affect the *current*
tailored resume. When you want to keep something permanently, use **Save to my
profile** on the resume (Step 4) to promote it into Step 2. Use **reset
tailoring** to clear a job's tailoring and start clean.

The **📝 Notes** button (top-right) opens sticky notes — jot reminders per
application (recruiter follow-ups, salary, questions to ask). Notes are saved on
your device and included in your Save/Load backup.

---

## Honest notes

- **Truthfulness:** the writer is instructed never to invent metrics, numbers,
  employers, or technologies you didn't list — it strengthens and tailors your
  *real* material. When a bullet would be stronger with a number but you didn't
  give one, it appends a placeholder like **`[add metric: e.g., %, $, time, users]`**
  for you to fill in with a real figure — it does **not** make the number up.
  If you have no projects (or none relevant to the role), it suggests
  high-relevance ones on **Step 3 (Tailor)** as guidance — it does **not** put
  un-built projects on your resume. Build one, add it in Step 2, and then it
  appears. Always read what it produces before using it.
- **Speed:** depends on your machine and model size. A 1.5B/3B model is a few
  seconds per section on most laptops; 7B is slower without a good GPU.
- **Privacy:** your profile stays in your browser (local storage) and the model
  runs locally. Nothing about you is sent anywhere. The only network calls are
  to `localhost` (Ollama) and, if you use it, Wikipedia for company blurbs.

---

## Troubleshooting

- **You see a "Directory listing" with files like `bash`, `cat`, `cp`, `ls`:**
  the server is serving the wrong folder. This happens if you *paste the start
  script's text* into Terminal instead of running it as a file. Fix: press
  **Ctrl+C**, then type `cd ` and drag the **resume-tailor** folder into the
  Terminal window, press Enter, and run `python3 -m http.server 8765`. Open
  http://localhost:8765. (Or just double-click the start script — don't copy its
  contents.)
- **"Ollama offline — heuristic mode":** Ollama isn't running or isn't
  reachable. Open the Ollama app (or run `ollama serve`), then click
  **⚙ Model → Test connection**.
- **"Ollama up — run: ollama pull …":** Ollama is running but the model isn't
  downloaded. Run the pull command from Step 2.
- **Connection blocked when opening `index.html` directly:** use a start script
  (serves from `localhost`), or set `OLLAMA_ORIGINS=*` as described above.
- **Too slow:** switch to `qwen2.5:1.5b` in **⚙ Model**.
- **Weird or truncated output:** try again, or use a larger model
  (`qwen2.5:7b`). Small models occasionally return malformed results — the app
  falls back to heuristic rewriting for that section when that happens.
