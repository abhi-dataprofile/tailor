# Test the agent with your own profile

A sandbox for watching the auto-apply agent fill a **real** application form with **your real
details** — without submitting anything until you decide to.

Nothing here touches the database, your dashboard, or your live application history.

---

## 1. Add your profile

```bash
cp mytest/profile.example.json mytest/profile.json
```

Open `mytest/profile.json` and replace the placeholders with your real details.

Two things worth understanding:

- **`data.standing`** — the legal/demographic answers (sponsorship, work authorization,
  salary, veteran/disability status). The AI **never** answers these. It uses exactly what you
  wrote, or leaves the field blank and tells you. That's deliberate: those answers are legally
  meaningful and are yours to give.
- **`data.standing._custom`** — free-text answers you've given before ("Why this role?"). The
  agent reuses them on reworded versions of the same question across different boards.

Anything you leave blank stays blank. The agent does not invent facts about you.

## 2. Run it against a real job

```bash
.venv/bin/python mytest/run.py --url https://job-boards.greenhouse.io/COMPANY/jobs/123456
```

Watch it happen in a real Chrome window:

```bash
.venv/bin/python mytest/run.py --url <JOB_URL> --watch
```

Don't have a URL handy? Let it pick a real open job for you:

```bash
.venv/bin/python mytest/run.py --find greenhouse
```

Supported boards: `greenhouse` · `lever` · `ashby` · `smartrecruiters` · `recruitee`

## 3. Read the result

You get:

- **your tailored résumé** → `mytest/out_resume.html` (open it in a browser)
- **a screenshot** of the completely filled form
- **the list of anything it couldn't answer**, and why

If something is listed as unanswered, add it under `data.standing._custom` in your profile and
re-run — it will use your answer from then on, including on other companies' forms.

---

## Actually submitting

Everything above is a dry run: the form gets filled, then the browser closes without submitting.

When you want to really apply:

```bash
.venv/bin/python mytest/run.py --url <JOB_URL> --watch --live
```

`--live` always opens a visible browser, prints who the application will be sent as, and waits
for you to type `SUBMIT` before anything is sent. If any required question is unanswered, the
agent refuses to submit rather than sending a half-filled form.

That confirmation step is the point: a real application goes out under **your** name, so the
decision to send it is yours, not the agent's.

---

## Also useful

```bash
.venv/bin/python test_engine.py          # 54 offline checks of the engine (no DB, no network)
.venv/bin/python smoke_apply.py --vendor ashby   # end-to-end check on a given board
```
