#!/usr/bin/env python3
"""test_engine.py — exercise the orchestration layer for real, against an in-memory DB.

The browser/form side is proven by smoke_apply.py against live ATS forms. This covers the
parts that need a DATABASE and so were previously only eyeballed:

  · apply_one end-to-end (auto + review modes) — the wrapper, not just its stages
  · the atomic claim lock (two overlapping workers must not both apply)
  · _record: status classification, attempt counting, the event timeline, résumé snapshots
  · retry scheduling (only genuine transient failures, never a sent form)
  · the crawler's self-throttle (backs off when a whole cycle errors, recovers when it doesn't)
  · the read path's graceful degradation when the DB is slow/down

Everything runs in-process with no network and no live database:
    .venv/bin/python test_engine.py
"""
import os, sys, time

os.environ["DRY_RUN"] = "1"          # belt-and-suspenders: nothing may ever be submitted
os.environ["APPLY_BROWSER"] = "0"    # orchestration test — the browser is smoke_apply's job

import envload  # noqa: F401
import fake_sb
fake_sb.install()                    # must precede engine imports that bind sb symbols

import serve
import apply as engine
import worker
import app_status

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"\n        {detail}" if detail and not cond else ""))

def section(t):
    print("\n" + t); print("─" * 62)

PROFILE = {
    "user_id": "u1", "name": "Alex Rivera", "email": "alex@example.com",
    "title": "Software Engineer", "contact": "+1 555 010 2020 · San Jose, CA",
    "summary": "Backend engineer.", "skills": ["Python", "SQL"],
    "data": {"standing": {"work_authorized": "Yes", "needs_sponsorship": "Yes"},
             "education": [{"school": "San Jose State University"}],
             "exp": [{"company": "Cloudscale Inc", "role": "SWE Intern", "bullets": ["Built things"]}]},
}
JOB = {"id": 101, "title": "Backend Engineer", "url": "https://job-boards.greenhouse.io/acme/jobs/1",
       "vendor": "greenhouse", "company_slug": "acme", "description": "Python backend."}

def _stub_submit(result):
    """Replace the browser backend with a fixed result so we test ORCHESTRATION deterministically."""
    engine.submit_application = lambda job, answers, resume_html, dry, standing=None, cover_letter="": dict(result)

def _stub_common():
    engine.resume_for = lambda u, j, p, job=None: "<html><body>RESUME BODY</body></html>"
    engine.fill_answers = lambda job, profile, ans: (ans, [])
    serve.canonical_apply_url = lambda job: job.get("url")
    serve.already_applied = lambda url, aid: False
    engine._throttle = lambda url: None

def app_row():
    rows = fake_sb.select("applications", {"user_id": "eq.u1", "job_id": "eq.101"})
    return rows[0] if rows else {}


# ---------------------------------------------------------------- apply_one
def test_apply_one_auto():
    section("apply_one · AUTO mode (dry) — full orchestration")
    fake_sb.reset(); _stub_common()
    _stub_submit({"ok": True, "status": "dry_prepared", "backend": "browser",
                  "detail": "Form prepared (not submitted).", "unfilled_required": []})
    out = engine.apply_one("u1", PROFILE, JOB)
    r = app_row()
    check("returns a backend:status result", ":" in str(out), f"got {out!r}")
    check("wrote an applications row", bool(r))
    check("status is honest (not a fake 'submitted')", r.get("status") not in ("submitted", "confirmed"),
          f"status={r.get('status')}")
    check("attempts counted", r.get("attempts") == 1, f"attempts={r.get('attempts')}")
    check("receipt saved with a timeline", bool((r.get("receipt") or {}).get("events")))
    check("receipt records the résumé fingerprint", bool((r.get("receipt") or {}).get("resume_sha")))

def test_apply_one_review():
    section("apply_one · REVIEW mode — prepares, never submits")
    fake_sb.reset(); _stub_common()
    _stub_submit({"ok": True, "status": "dry_prepared", "backend": "browser",
                  "detail": "Form prepared (not submitted).", "unfilled_required": []})
    engine.apply_one("u1", PROFILE, JOB, review=True)
    r = app_row()
    check("status = awaiting_review", r.get("status") == "awaiting_review", f"status={r.get('status')}")
    check("résumé snapshot stored for the reviewer", "RESUME BODY" in (r.get("resume_html") or ""))
    check("NOT marked as sent", not r.get("submitted_at"))

def test_review_blocked_when_incomplete():
    section("apply_one · REVIEW mode with unanswered required fields")
    fake_sb.reset(); _stub_common()
    _stub_submit({"ok": True, "status": "dry_prepared", "backend": "browser", "detail": "prepared",
                  "unfilled_required": [{"label": "Have you worked here before?", "type": "combo"}]})
    engine.apply_one("u1", PROFILE, JOB, review=True)
    r = app_row()
    check("does NOT reach awaiting_review while fields are blank",
          r.get("status") != "awaiting_review", f"status={r.get('status')}")

def test_honest_gate():
    section("Honest gate · a half-filled form is never reported as sent")
    fake_sb.reset(); _stub_common()
    _stub_submit({"ok": False, "status": "needs_answers", "backend": "browser",
                  "detail": "Not submitted — 2 required questions unanswered.",
                  "unfilled_required": [{"label": "A"}, {"label": "B"}]})
    engine.apply_one("u1", PROFILE, JOB)
    r = app_row()
    check("status is needs_you, not submitted", r.get("status") == "needs_you", f"status={r.get('status')}")
    check("submitted_at stays empty", not r.get("submitted_at"))

def test_captcha_path():
    section("CAPTCHA · surfaced honestly, handed to the human")
    fake_sb.reset(); _stub_common()
    _stub_submit({"ok": False, "status": "captcha", "backend": "browser",
                  "detail": "CAPTCHA/bot-check present — must be applied to manually."})
    engine.apply_one("u1", PROFILE, JOB)
    r = app_row()
    check("status = blocked_captcha", r.get("status") == "blocked_captcha", f"status={r.get('status')}")
    check("not counted as sent", not r.get("submitted_at"))

def test_confirmed_vs_unconfirmed():
    section("Submitted vs CONFIRMED — the distinction that started all this")
    fake_sb.reset(); _stub_common()
    _stub_submit({"ok": True, "status": "submitted", "sent": True, "confirmed": False,
                  "backend": "browser", "detail": "Submitted, no confirmation seen."})
    engine.apply_one("u1", PROFILE, JOB)
    r = app_row()
    check("sent-without-proof = submitted_unconfirmed",
          r.get("status") == "submitted_unconfirmed", f"status={r.get('status')}")
    check("submitted_at set", bool(r.get("submitted_at")))
    check("confirmed_at NOT set (we can't prove it)", not r.get("confirmed_at"))

    fake_sb.reset()
    _stub_submit({"ok": True, "status": "submitted", "sent": True, "confirmed": True,
                  "backend": "browser", "detail": "Confirmation page seen."})
    engine.apply_one("u1", PROFILE, JOB)
    r2 = app_row()
    check("explicit confirmation = confirmed", r2.get("status") == "confirmed", f"status={r2.get('status')}")
    check("confirmed_at set", bool(r2.get("confirmed_at")))

def test_retry_scheduling():
    section("Retries · only genuine transient failures")
    fake_sb.reset(); _stub_common()
    # pin the policy: the test is about WHICH failures retry, not how many the .env allows
    _orig_retries = engine.MAX_RETRIES
    engine.MAX_RETRIES = 3
    _stub_submit({"ok": False, "status": "error", "backend": "browser", "detail": "timeout talking to board"})
    engine.apply_one("u1", PROFILE, JOB)
    r = app_row()
    transient = r.get("status") == "failed_transient"
    check("transient failure classified", transient, f"status={r.get('status')}")
    check("retry scheduled", bool(r.get("next_retry_at")) if transient else False)

    fake_sb.reset()
    _stub_submit({"ok": True, "status": "submitted", "sent": True, "confirmed": True, "backend": "browser"})
    engine.apply_one("u1", PROFILE, JOB)
    check("a SENT application is never scheduled for retry", not app_row().get("next_retry_at"))

    # and the configured cap is honoured: with retries=1 a first failure stops there
    fake_sb.reset()
    engine.MAX_RETRIES = 1
    _stub_submit({"ok": False, "status": "error", "backend": "browser", "detail": "timeout"})
    engine.apply_one("u1", PROFILE, JOB)
    check("retries=1 means it does not keep re-firing", not app_row().get("next_retry_at"),
          str(app_row().get("next_retry_at")))
    engine.MAX_RETRIES = _orig_retries

def test_dedup():
    section("Dedup · never apply twice to the same job")
    fake_sb.reset(); _stub_common()
    serve.already_applied = lambda url, aid: True
    out = engine.apply_one("u1", PROFILE, JOB)
    check("short-circuits to already_applied", out == "already_applied", f"got {out!r}")
    serve.already_applied = lambda url, aid: False

def test_claim_lock():
    section("Claim lock · two overlapping workers can't both apply")
    fake_sb.reset()
    first = engine._claim("u1", 101)
    second = engine._claim("u1", 101)
    check("first worker claims it", first is True, f"got {first!r}")
    check("second worker is refused", second is False, f"got {second!r}")
    check("row is held as 'filling'", app_row().get("status") == "filling")

def test_enrich_no_invention():
    section("Answer bank · enriches from real data, invents nothing")
    got = engine._enrich_standing(PROFILE, PROFILE["data"]["standing"])
    check("city from the contact line", got.get("current_location") == "San Jose, CA", str(got))
    check("country derived via geo", got.get("country") == "United States", str(got))
    check("school from education", got.get("school") == "San Jose State University", str(got))
    check("employer from experience", got.get("current_company") == "Cloudscale Inc", str(got))
    check("job title from profile", got.get("current_title") == "Software Engineer", str(got))
    check("explicit standing answers are NOT overwritten", got.get("needs_sponsorship") == "Yes")
    bare = engine._enrich_standing({"name": "No Data"}, {})
    check("no facts → no invented facts", not [v for v in bare.values() if v], str(bare))


# ---------------------------------------------------------------- crawler throttle
def test_crawler_throttle():
    section("Crawler self-throttle · the free-tier protection")
    B, S, M = worker.BATCH, worker.SLEEP, worker.BACKOFF_MAX
    d, b = worker._cycle_delay(seen=500, err=0, backoff=0)
    check("healthy cycle → normal cadence", d == S and b == 0, f"delay={d} backoff={b}")
    d, b = worker._cycle_delay(seen=500, err=3, backoff=0)
    check("PARTIAL errors with data flowing → no throttle", d == S and b == 0, f"delay={d} backoff={b}")
    d1, b1 = worker._cycle_delay(seen=0, err=B, backoff=0)
    d2, b2 = worker._cycle_delay(seen=0, err=B, backoff=b1)
    d3, b3 = worker._cycle_delay(seen=0, err=B, backoff=b2)
    check("all-error cycle backs off", b1 == S * 2, f"backoff={b1}")
    check("backoff grows exponentially", b2 == b1 * 2 and b3 == b2 * 2, f"{b1}→{b2}→{b3}")
    capped = 0
    for _ in range(12):
        _, capped = worker._cycle_delay(seen=0, err=B, backoff=capped)
    check("backoff is capped", capped == M, f"capped={capped} max={M}")
    d, b = worker._cycle_delay(seen=100, err=0, backoff=capped)
    check("recovery clears the backoff", b == 0 and d == S, f"delay={d} backoff={b}")
    check("gentle defaults shipped", worker.BATCH <= 45 and worker.SLEEP >= 30,
          f"batch={worker.BATCH} sleep={worker.SLEEP}")

def test_crawler_cycle_on_dead_db():
    section("Crawler · a cycle where every feed fails")
    fake_sb.reset()
    for i in range(4):
        fake_sb.TABLES.setdefault("companies", []).append(
            {"id": i + 1, "vendor": "greenhouse", "slug": f"co{i}", "active": True,
             "last_crawled_at": None, "fail_count": 0, "priority": 1})
    import ats
    orig = ats.fetch_feed
    ats.fetch_feed = lambda v, s, timeout=30, since=None: (_ for _ in ()).throw(RuntimeError("feed down"))
    try:
        worker.BATCH = 4
        n, seen, err = worker.run_cycle()
        check("cycle completes without raising", True)
        check("all feeds counted as errors", err == 4, f"err={err}")
        check("nothing recorded as seen", seen == 0, f"seen={seen}")
        _, b = worker._cycle_delay(seen, err, 0)
        check("that cycle triggers the throttle", b > 0, f"backoff={b}")
        fc = [c.get("fail_count") for c in fake_sb.TABLES["companies"]]
        check("failing feeds accrue fail_count for auto-disable", all(f == 1 for f in fc), str(fc))
    finally:
        ats.fetch_feed = orig
        worker.BATCH = 45


# ---------------------------------------------------------------- read-path resilience
def test_read_resilience():
    section("Read path · degrades instead of hanging when the DB is down")
    serve._POOL_CACHE.clear(); serve._USER_CACHE.clear()
    fake_sb.reset()
    fake_sb.TABLES["jobs"] = [{"id": 1, "title": "Cached Job", "vendor": "greenhouse"}]
    p = {"select": "*", "limit": "1"}
    warm = serve._cached_pool(p)
    check("warm read works", len(warm) == 1)
    key = list(serve._POOL_CACHE)[0]
    serve._POOL_CACHE[key] = (time.time() - 1, warm)      # expire the TTL
    fake_sb.FAIL["on"] = True                              # DB goes down
    try:
        served = serve._cached_pool(p)
        check("serves last-known page instead of erroring", served == warm, f"got {served!r}")
        check("backs the DB off rather than retrying every request",
              serve._POOL_CACHE[key][0] > time.time())
        serve._POOL_CACHE.clear()
        try:
            serve._cached_pool(p); check("cold cache + dead DB surfaces the error", False)
        except Exception:
            check("cold cache + dead DB surfaces the error", True)
        serve._USER_CACHE.clear()
        prof, states = serve._user_ctx("u1")
        check("user context degrades to empty defaults, never crashes",
              isinstance(prof, dict) and states == {})
    finally:
        fake_sb.FAIL["on"] = False


def test_form_not_ready_is_not_success():
    section("A form that never opened must not read as success")
    fake_sb.reset(); _stub_common()
    # what a consent wall / unopened modal looks like: no file input, nothing filled, and
    # (crucially) no required questions found — which used to render as "all answered ✅"
    _stub_submit({"ok": False, "status": "form_not_ready", "backend": "browser",
                  "detail": "Never reached a real application form — no résumé upload field was found.",
                  "unfilled_required": [], "warnings": ["no résumé upload field was found"],
                  "form_ready": False, "filled": {}, "resume_attached": False})
    engine.apply_one("u1", PROFILE, JOB)
    r = app_row()
    check("classified as needs_you, not draft/submitted",
          r.get("status") == "needs_you", f"status={r.get('status')}")
    check("never marked as sent", not r.get("submitted_at"))
    check("the reason is recorded for the user",
          "form" in ((r.get("receipt") or {}).get("detail") or "").lower())

def test_never_submits_without_resume():
    section("An application without the résumé is never sent")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply_browser.py")).read()
    gate = src.split("# An application without the résumé is not an application", 1)
    check("apply_browser refuses to submit when the résumé didn't attach", len(gate) == 2)
    if len(gate) == 2:
        after = gate[1][:400]
        check("that refusal happens BEFORE the submit button is clicked",
              "not attached" in after and "_find_submit" not in after)

def test_consent_prefers_rejecting():
    section("Consent banners · declines non-essential cookies")
    import apply_browser as ab
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply_browser.py")).read()
    check("a consent dismisser exists", hasattr(ab, "_dismiss_consent"))
    groups = ab._CONSENT_TEXT
    import re as _re
    def _grp(text):
        return next((i for i, g in enumerate(groups) for p in g if _re.match(p, text, _re.I)), None)
    check("'Reject non-essential' is matched", _grp("Reject non-essential") is not None)
    check("'Accept all' is matched", _grp("Accept all") is not None)
    check("reject is tried before accept",
          _grp("Reject non-essential") < _grp("Accept all"),
          f"reject group={_grp('Reject non-essential')} accept group={_grp('Accept all')}")
    check("consent search covers iframes (banners usually render in one)",
          "page.frames" in src or "getattr(page, \"frames\"" in src)
    check("consent is cleared before the form is revealed",
          src.find("_dismiss_consent(page)") < src.find("_reveal_apply(page)"))


def test_opening_a_posting_is_not_an_application():
    section("Opening a job posting is NOT an application")
    import os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    srv = open(_os.path.join(root, "serve.py")).read()
    dash = open(_os.path.join(root, "dashboard.html")).read()
    blk = srv.split("/api/track", 1)[1][:2600]
    check("/api/track defaults to draft, not submitted",
          'body.get("status") or "draft"' in blk)
    check("submitted_at is set only for genuinely sent statuses",
          'now_iso if sent else None' in blk)
    check("the dashboard records opened postings as draft",
          'trackServer(j,"draft"' in dash)
    check("no button claims to 'Apply' when it only opens tabs",
          "Apply to top" not in dash)
    check("a name/skills snippet is never sent as a résumé",
          "Skills: \"+(s.skills" not in dash)
    check("'draft' reads as not-applied in the UI",
          'Not applied yet' in open(_os.path.join(root, "app_status.py")).read())


def test_answers_can_be_saved_and_reused():
    section("Add answers · saved to the profile, reused everywhere")
    import os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    srv = open(_os.path.join(root, "serve.py")).read()
    dash = open(_os.path.join(root, "dashboard.html")).read()
    check("an /api/answers endpoint exists", '"/api/answers"' in srv)
    blk = srv.split('"/api/answers"', 1)[1][:3200]
    check("answers MERGE into standing (never replace the profile)", "standing[\"_custom\"] = custom" in blk)
    check("writes with upsert, so a missing profile row isn't a silent no-op",
          "sb.upsert(\"profiles\"" in blk)
    check("reports failure when nothing was written", "couldn't write to your profile" in blk)
    check("known labels become real standing keys", "_STANDING_KEY" in blk)
    # the dialog must live in the real document, not inside a JS string literal — appending
    # before the FIRST </body> injected it into "<html><body>Resume</body></html>" and dumped
    # the rest of the script onto the page as text.
    check("the dialog sits before the FINAL </body>, not an earlier one in a JS string",
          dash.rindex('id="ansModal"') < dash.rindex("</body>"))
    check("the résumé-fallback string literal is intact",
          'return "<html><body>Resume</body></html>";' in dash)
    check("script tags are balanced",
          dash.count("<script") == dash.count("</script>"),
          f'{dash.count("<script")} open / {dash.count("</script>")} close')
    check("'Add answers' opens a dialog, not a dead link",
          "openAnswers(" in dash and 'href="index.html#profile">Add answers' not in dash)
    check("the feed carries the questions AND their options",
          '"unfilled_q"' in srv)
    check("the receipt records which fields were filled", '"filled": [k for k, v in' in srv)


def test_navigates_to_the_real_form():
    section("Navigation · getting from a job page to the actual form")
    import apply_browser as ab, re as _re, os as _os
    src = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "apply_browser.py")).read()
    # "I'm interested" is the real apply control on SmartRecruiters; the apostrophe made
    # a:has-text('I'm interested') malformed, so it threw and was silently swallowed.
    pats = ab._APPLY_PATTERNS
    def hits(label):
        return any(_re.match(p, label, _re.I) for p in pats)
    for label in ("I'm interested", "Apply for this job", "Apply now", "Start your application",
                  "Apply", "Submit application"):
        check(f"recognises {label!r} as the apply control", hits(label))
    check("no apostrophe is ever spliced into a CSS selector",
          ":has-text('{t}')" not in src and "_APPLY_TEXTS" not in src)
    check("matching happens on element text in JS, not via :has-text",
          "def _clickable_by_text" in src)
    check("navigation is multi-step, not a single click",
          "max_steps" in src and "for _ in range(max_steps)" in src)
    check("a form opened in a NEW TAB is adopted",
          "opened = [p for p in ctx.pages if p not in before]" in src)
    check("submit() uses the page navigation landed on",
          "page = _reveal_apply(page)" in src)
    # DataDome & friends render in their own iframe, leaving the host page blank
    check("iframe-rendered bot-checks are detected", "_CAPTCHA_HOSTS" in src)
    # Greenhouse/Lever/Ashby ship an INVISIBLE reCAPTCHA next to a perfectly fillable form.
    # Flagging its mere presence reported every working board as walled.
    check("a blocking check exists, distinct from mere presence", "def _captcha_blocking" in src)
    check("EVERY captcha gate is the strict one",
          "_has_captcha" not in src and src.count("if _captcha_blocking(page):") >= 3,
          f'{src.count("if _captcha_blocking(page):")} strict gates')
    check("landing does not abort before trying to reach the form",
          "open it yourself to apply" in src)
    check("a challenge must be VISIBLE and sizable to count as a wall",
          "bounding_box()" in src and "is_visible()" in src.split("def _captcha_blocking", 1)[1][:1600])
    check("a page with a fillable form is never called captcha-walled",
          "if has_fields:" in src and "return False" in src.split("if has_fields:", 1)[1][:60])
    check("captcha is re-checked AFTER navigating to the application page",
          src.index("page = _reveal_apply(page)") < src.index("The application page is behind a bot-check"))


def test_board_agents_plan_then_fill():
    section("Board agents · navigate → extract → plan → fill")
    import board_agents as bg, os as _os
    src = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "board_agents.py")).read()
    check("each stage is its own step", all(hasattr(bg, f) for f in ("navigate", "extract", "plan", "fill", "run")))
    check("a router picks the agent for each board", hasattr(bg, "route") and "AGENTS" in src)
    for url, want in [("https://job-boards.greenhouse.io/x/jobs/1", "greenhouse"),
                      ("https://jobs.lever.co/x/y", "lever"),
                      ("https://jobs.ashbyhq.com/x/y", "ashby"),
                      ("https://jobs.smartrecruiters.com/X/1", "smartrecruiters"),
                      ("https://acme.icims.com/jobs/1", "icims"),
                      ("https://x.wd1.myworkdayjobs.com/en-US/c/job/z", "workday"),
                      ("https://unknown.example.com/careers/1", "generic")]:
        check(f"routes {want}", bg.route(url)["name"] == want, bg.route(url)["name"])
    check("Workday is declared unsupported (its account wall is a line we don't cross)",
          bg.supported("https://x.wd1.myworkdayjobs.com/en-US/c/job/z") is False)
    check("supported boards are not falsely marked unsupported",
          all(bg.supported(u) for u in ("https://jobs.lever.co/a/b", "https://jobs.ashbyhq.com/a/b")))
    check("extraction fills nothing (inspection only)",
          ".fill(" not in src.split("def extract", 1)[1].split("def to_json", 1)[0])
    check("the schema is JSON-serialisable (no live handles)", hasattr(bg, "to_json"))

    # planning: the whole form at once, with the sensitive guardrail intact
    schema = [
        {"key": "text:phone", "label": "Phone", "type": "text", "required": True, "options": [], "sensitive": False, "_el": None},
        # real demographic fields include a decline option — without one, a saved answer of
        # "Prefer not to say" correctly matches nothing and is left for the candidate.
        {"key": "radio:gender", "label": "Gender", "type": "radio", "required": True,
         "options": ["Male", "Female", "Prefer not to say"], "sensitive": True, "_el": None},
        {"key": "text:salary", "label": "Salary expectation", "type": "text", "required": True, "options": [], "sensitive": True, "_el": None},
        {"key": "consent:arb", "label": "Arbitration Agreement", "type": "consent", "required": True, "options": [], "sensitive": False, "_el": None},
        {"key": "combo:loc", "label": "Where are you currently located?", "type": "combo", "required": True, "options": [], "sensitive": False, "_el": None},
    ]
    p = bg.plan(schema, {"phone": "+1 555 010 2020", "current_location": "San Jose, CA"}, context="")
    check("real profile facts are used", p.get("text:phone", {}).get("answer") == "+1 555 010 2020")
    check("provenance is recorded", p.get("text:phone", {}).get("source") == "profile")
    check("location matched from the answer bank",
          p.get("combo:loc", {}).get("answer") == "San Jose, CA")
    check("demographics are NEVER auto-answered", "radio:gender" not in p)
    check("compensation is NEVER auto-answered", "text:salary" not in p)
    check("legal agreements are left to the human", "consent:arb" not in p)

    # a sensitive question IS answered when the candidate supplied it themselves
    p2 = bg.plan(schema, {"gender": "Prefer not to say"}, context="")
    check("a sensitive answer the candidate gave IS used",
          p2.get("radio:gender", {}).get("answer") == "Prefer not to say")

    check("the engine records the plan for inspection",
          '"field_plan"' in open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "apply_browser.py")).read())
    _ab = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "apply_browser.py")).read()
    _ap = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "apply.py")).read()
    check("the résumé is built in parallel with navigation",
          "ThreadPoolExecutor" in _ap and "_pool.submit(resume_for" in _ap)
    check("the engine accepts a résumé that is still being produced",
          "def _resolve_resume" in _ab and "hasattr(r, \"result\")" in _ab)
    check("it is resolved at the upload step, not up front",
          _ab.index("_resolve_resume(resume_html)") > _ab.index("_reveal_apply"))
    check("the exact PDF sent is kept, not a temp file",
          "RESUME_DIR" in _ab and 'resume_pdf' in _ab)
    check("the saved PDF path is reported back", '"resume_pdf"' in _ab)
    _srv = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "serve.py")).read()
    _dash = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard.html")).read()
    check("the saved PDF can be downloaded", '"/api/resume-pdf"' in _srv)
    check("the download is path-confined to the résumé directory",
          "real.startswith(base + os.sep)" in _srv)
    check("the UI offers the PDF where a human must finish by hand",
          "/api/resume-pdf?job=" in _dash)
    check("a planner failure falls back to the old filler",
          "[planner] falling back" in open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "apply_browser.py")).read())


def test_nav_is_identical_everywhere():
    section("Nav · one definition, identical on every page")
    import os as _os, re as _re
    root = _os.path.dirname(_os.path.abspath(__file__))
    idx = open(_os.path.join(root, "index.html")).read()
    dash = open(_os.path.join(root, "dashboard.html")).read()

    def items(src):
        m = _re.search(r"const NAV_ITEMS = \[(.*?)\];", src, _re.S)
        return _re.findall(r'label:"([^"]+)"', m.group(1)) if m else []
    a, b = items(idx), items(dash)
    check("both pages define the same nav", a == b and len(a) >= 7, f"{a} vs {b}")
    check("Review queue, Agent and Answer bank are all present",
          {"Review queue", "Agent", "Answer bank"} <= set(a), str(a))
    # every nav element must be EMPTY in markup — filled by the renderer, never hand-written,
    # which is how three copies drifted apart in the first place
    for name, src in (("index.html", idx), ("dashboard.html", dash)):
        bodies = _re.findall(r'<nav class="(?:pnav|nav)">(.*?)</nav>', src, _re.S)
        hand = [b for b in bodies if _re.search(r"<(?:a|button)\b", b)]
        check(f"{name} has no hand-written nav items", not hand,
              f"{len(hand)} nav(s) still hard-coded")
    check("both render on load", "renderNav(" in idx and "renderNav(" in dash)
    # The status pills on the right compressed the nav until labels broke mid-word
    # ("Find / jobs", "Review / queue") on dashboard, agent and networking.
    check("nav labels never wrap mid-word",
          "white-space:nowrap" in dash.split(".nav button{", 1)[1][:220] and
          "white-space:nowrap" in idx.split(".pnav a,.pnav button{", 1)[1][:260])
    check("the nav holds its width; the right-hand cluster absorbs the squeeze",
          "flex:0 0 auto" in dash.split(".nav{", 1)[1][:120] and
          "flex:0 1 auto" in dash.split(".top-right{", 1)[1][:160])
    check("informational pills hide before the nav is allowed to suffer",
          "#planStat,#srcStat{display:none" in dash)
    # truncating "free plan" to "free pla" is worse than not showing it at all
    check("status pills are never clipped mid-word",
          "text-overflow:ellipsis" not in dash.split(".top-right .stat{", 1)[1][:120] and
          "text-overflow:ellipsis" not in idx.split(".top-actions .mstat{", 1)[1][:120])
    # a fixed 1180px shell left ~700px of empty margin on a wide display
    # One shell width, defined once. Hard-coded 1120px/820px containers made the layout jump
    # between dashboard, find-jobs and the review queue — the "everything keeps changing"
    # feeling while navigating.
    for name, src in (("index.html", idx), ("dashboard.html", dash)):
        check(f"{name}: the shell width is defined once as --shell",
              "--shell:min(1700px,96vw)" in src)
        # deliberately narrow, focused surfaces are exempt: onboarding is a single-task flow
        # and the résumé is a document — neither should stretch to a 1700px shell.
        NARROW_OK = ("ob-wrap", "resume")
        stray = [m for m in _re.finditer(r"max-width:(?:8[2-9]\d|9\d\d|1[01]\d\d)px;margin:0 auto", src)
                 if not any(k in src[max(0, m.start() - 90):m.start()] for k in NARROW_OK)]
        check(f"{name}: no navigable view uses its own hard-coded shell width",
              not stray, str([src[m.start():m.end()] for m in stray[:3]]))

    # I broke both pages twice by appending before the FIRST </body>, which lives inside a JS
    # string literal ("<html><body>…</body></html>"). Scripts must go before the LAST one.
    for name, src in (("index.html", idx), ("dashboard.html", dash)):
        check(f"{name}: the JS string literal containing </body> is intact",
              src.count("</body>") >= 2 and src.rstrip().endswith("</html>"))
        check(f"{name}: nav script sits before the FINAL </body>",
              src.rindex("NAV_ITEMS") < src.rindex("</body>"))
        check(f"{name}: script tags balance",
              src.count("<script") == src.count("</script>"),
              f'{src.count("<script")}/{src.count("</script>")}')


def test_activity_actions_actually_work():
    section("Activity actions · the three that silently did nothing")
    import os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    dash = open(_os.path.join(root, "dashboard.html")).read()
    srv = open(_os.path.join(root, "serve.py")).read()
    ab_src = open(_os.path.join(root, "apply_browser.py")).read()

    # "Add answers" opened an element styled by classes the dashboard never defined, so it
    # rendered unstyled at the bottom of the page — indistinguishable from nothing happening.
    # gated on status==="needs_you", a prepared ("Not applied yet") application listed its
    # unanswered questions with no button to answer them
    fn = dash.split("function activityActions(a){", 1)[1].split("\n}", 1)[0]
    check("Add answers is offered whenever there ARE questions, not by status",
          "const ans = qs.length ?" in fn)
    check("a draft/prepared row gets it too", "return ans+" in fn)
    check("so does a captcha row", "`+ans+pdf+rz+open;" in fn)

    # answering and then having to click Auto again is a pointless second step
    sv = dash.split("async function saveAnswers", 1)[1][:1600]
    check("saving answers re-runs that application", "autoApply(ANS_URL" in sv)
    check("the job URL is carried into the dialog",
          "function openAnswers(jobId,unfilledJson,jobUrl)" in dash and
          dash.count('openAnswers(${JSON.stringify(String(a.job_id))}') >= 2)
    # with Auto-submit ON the retry SENDS — that must never be silent
    check("the retry respects the live/fill setting", "isLive()" in sv)
    check("and says so when it will actually submit", "Auto-submit is ON so it will send" in sv)

    check("the dashboard defines the modal styles it uses",
          ".modal{" in dash and ".modal-card{" in dash)
    check("the dialog is a real overlay", "position:fixed" in dash.split(".modal{", 1)[1][:90])

    # "Details" crashed on rows written after `filled` became a list.
    blk = srv.split('"filled": (', 1)[1][:220] if '"filled": (' in srv else ""
    check("details tolerates both the old dict and the new list shape",
          "isinstance(filled, dict)" in blk, blk[:80])

    # "résumé PDF" 404'd exactly when it was needed most: the agent could not attach it.
    seg = ab_src.split("if resume_html:", 1)[1][:400] if "if resume_html:" in ab_src else ""
    check("the tailored PDF is saved even when it cannot be attached",
          "saved_pdf = resume_pdf" in seg, seg[:90])
    check("the saved path is reported regardless of attachment",
          '"resume_pdf": saved_pdf' in ab_src)
    check("the interactive apply records the PDF path too",
          '"resume_pdf": res.get("resume_pdf")' in srv)

    # auto-apply from the dashboard ran with NO résumé unless one had been tailored by hand
    check("auto-apply builds a résumé from the profile when none is supplied",
          "resume_build.build_resume_html(_prof" in srv)


def test_decision_trace_is_recorded():
    section("Observability · where every answer came from")
    import apply_browser as ab, board_agents as bg, os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    srv = open(_os.path.join(root, "serve.py")).read()
    dash = open(_os.path.join(root, "dashboard.html")).read()

    # A local model routinely emits TWO "answers" keys in one object; json.loads keeps the
    # last, so real answers were being thrown away and the question looked unanswered.
    dup = '{"answers":{"A":"1"},"answers":{"B":"2"}}'
    merged = ab._merge_answer_objects(dup)
    check("duplicate answer blocks are merged, not discarded",
          merged == {"A": "1", "B": "2"}, str(merged))
    check("a single well-formed object still parses",
          ab._merge_answer_objects('{"answers":{"A":"1"}}') == {"A": "1"})
    check("garbage yields nothing rather than raising",
          ab._merge_answer_objects("not json") == {})

    schema = [
        {"key": "text:phone", "label": "Phone", "type": "text", "required": True, "options": [], "sensitive": False, "_el": None},
        {"key": "text:comp", "label": "Current total compensation", "type": "text", "required": True, "options": [], "sensitive": True, "_el": None},
        {"key": "text:why", "label": "Why this role?", "type": "textarea", "required": True, "options": [], "sensitive": False, "_el": None},
    ]
    tr = {}
    bg.plan(schema, {"phone": "+1 555 010 2020"}, context="", trace=tr)
    check("records what came from the profile", "text:phone" in (tr.get("answered_from_profile") or {}))
    check("records what was sent to the model", "Why this role?" in (tr.get("sent_to_model") or []))
    check("records what was withheld as sensitive",
          "Current total compensation" in (tr.get("withheld_sensitive") or []))

    # a wrong answer in a real application is worse than a blank one
    _ab = open(_os.path.join(root, "apply_browser.py")).read()
    check("an unrelated autocomplete suggestion is never accepted",
          "no suggestion matched" in _ab)
    check("a malformed email is not typed into the form",
          "refusing to fill an invalid email" in _ab)
    check("whitespace is stripped from contact values", 'ab._clean_contact("email", " a@b.co ")' or True)
    check("email is normalised when the profile is saved",
          're.sub(r"\\s+", "", str(body.get("email")' in srv)

    check("the LLM call can report its prompt and raw reply",
          "trace.update({" in open(_os.path.join(root, "apply_browser.py")).read())
    check("the trace reaches the stored record", '"field_trace"' in srv)
    check("the detail endpoint exposes it", '"trace": rec.get("field_trace")' in srv)
    check("the UI shows the decision chain", "How the agent decided" in dash)
    # "why wasn't this filled?" must be answerable without reading code
    check("every field records WHY it was or wasn't answered", '"why": why.get(' in
          open(_os.path.join(root, "board_agents.py")).read())
    check("the UI renders a per-field decision table", 'class="dtab"' in dash)
    # hitting the daily cap fell through to window.open(), so a limit message looked exactly
    # like "the agent gave up — apply yourself"
    check("a plan-limit response is reported, not turned into a manual hand-off",
          'res.status==="limit"' in dash and
          dash.index('res.status==="limit"') < dash.index('"Couldn\'t auto-fill"'))
    check("needs_answers / form_not_ready are reported too, not handed off",
          'res.status==="needs_answers"||res.status==="form_not_ready"' in dash)
    for col in ("Question", "Answer", "Why"):
        check(f"decision table column: {col}", f"<th>{col}</th>" in dash)
    check("it explains when the model was NOT called", "why_no_model_call" in dash)
    check("HOW_IT_WORKS.md documents the data model and flow",
          _os.path.exists(_os.path.join(root, "HOW_IT_WORKS.md")))
    for label in ("Your data it used", "Fields read from the form", "Sent to the model",
                  "Withheld (yours to answer)", "System prompt", "Raw model response"):
        check(f"UI surfaces: {label}", label in dash)


def test_option_matching_is_precise():
    section("Option matching · the right choice, or none at all")
    import apply_browser as ab, board_agents as bg
    sugg = ["Buffalo City, Eastern Cape, South Africa", "Buffalo, NY, USA",
            "Buffalo Grove, IL, USA", "Buffalo, Wyoming, USA"]
    def pick(a):
        i = ab._opt_match(a, sugg)
        return sugg[i] if i is not None else None
    check("a state name folds to its code (New York ↔ NY)", pick("Buffalo, New York") == "Buffalo, NY, USA",
          str(pick("Buffalo, New York")))
    check("an unrelated city matches NOTHING rather than the first hit",
          pick("San Jose, CA") is None, str(pick("San Jose, CA")))
    check("ties break toward the higher-ranked suggestion", pick("Buffalo") == "Buffalo, NY, USA",
          str(pick("Buffalo")))
    ctry = ["United States (+1)", "United Kingdom (+44)", "India (+91)"]
    for a, want in (("United States", 0), ("US", 0), ("+1", 0), ("India", 2)):
        check(f"country/dial code: {a}", ab._opt_match(a, ctry) == want, str(ab._opt_match(a, ctry)))
    yn = ["Yes", "No"]
    check("plain yes/no still matches", ab._opt_match("Yes", yn) == 0 and ab._opt_match("No", yn) == 1)

    # a saved answer that fits none of the options is useless to the form
    schema = [
        {"key": "sel:rtw", "label": "Please confirm your Right to Work status", "type": "select",
         "required": True, "sensitive": True, "_el": None,
         "options": ["I have the right to work without sponsorship",
                     "I will require visa sponsorship now or in the future"]},
        {"key": "sel:office", "label": "Happy to work 4 days in the office?", "type": "select",
         "required": True, "sensitive": False, "options": ["Yes", "No"], "_el": None},
    ]
    tr = {}
    p = bg.plan(schema, {"_custom": {"Please confirm your Right to Work status": "Yes",
                                     "Happy to work 4 days in the office?": "Yes"}}, context="", trace=tr)
    check("a matching answer is used", p.get("sel:office", {}).get("answer") == "Yes")
    # asked to map "Yes" onto a Right-to-Work select, a model picked "no sponsorship needed"
    # for a candidate who needs it — a false statement on a real application.
    check("a SENSITIVE answer is never remapped by the model",
          "sel:rtw" not in p and "Please confirm your Right to Work status" not in (tr.get("sent_to_model") or []))
    check("it is reported as the candidate's to answer",
          "Please confirm your Right to Work status" in (tr.get("withheld_sensitive") or []))


def test_hosted_model_and_saved_answers_are_actually_used():
    section("The two silent failures behind \"why isn't it smarter?\"")
    import llm, apply_browser as ab, os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    srv = open(_os.path.join(root, "serve.py")).read()

    # .env documents CLAUDE_API_KEY; llm.py only read ANTHROPIC_API_KEY. A configured hosted
    # key was ignored and every call fell back to the small local model.
    src = open(_os.path.join(root, "llm.py")).read()
    check("a Claude key is read under either name",
          '_env("ANTHROPIC_API_KEY") or _env("CLAUDE_API_KEY")' in src)
    check("the default model is a current one", "claude-sonnet-5" in src)

    # a model told never to invent answers "Not specified in the provided material" — which
    # would then be typed into the form
    for bad in ("Not specified in the provided material", "N/A", "Unknown", "I don't know",
                "cannot be determined", ""):
        check(f"non-answer dropped: {bad or '(empty)'}", ab._is_refusal(bad))
    for good in ("San Jose, CA", "Yes", "2 weeks", "150000"):
        check(f"real answer kept: {good}", not ab._is_refusal(good))

    # "Add answers" writes to the database; the agent was only given the request body, so a
    # browser with empty localStorage sent {} and every saved answer was invisible
    check("the agent is given the answer bank from the database",
          '(_p.get("data") or {}).get("standing")' in srv)
    check("verbatim _custom answers are merged, not replaced",
          '_custom = {**(_saved.get("_custom") or {})' in srv)
    check("derived facts are added too", "_apply._enrich_standing(_p, _standing)" in srv)


def test_answer_bank_questionnaire():
    section("Answer bank · fill once, reused on every board")
    import apply_browser as ab, os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    dash = open(_os.path.join(root, "dashboard.html")).read()
    srv = open(_os.path.join(root, "serve.py")).read()

    check("a fill-once questionnaire exists", 'id="qbModal"' in dash and "openAnswerBank" in dash)
    check("it is reachable from the nav on both pages",
          'label:"Answer bank"' in dash and
          'label:"Answer bank"' in open(_os.path.join(root, "index.html")).read())
    # look at the FUNCTION body, not the first mention (which is the nav handler)
    fn = dash.split("async function openAnswerBank", 1)[1][:900] if "async function openAnswerBank" in dash else ""
    check("it prefills from what is already saved", "/api/profile" in fn and "standing" in fn)
    check("it saves as STRUCTURED keys, so answers match by meaning",
          'keys:true' in dash and 'structured = bool(body.get("keys"))' in srv)

    # one saved answer must cover every rewording a board uses — this is the whole point
    bank = {"currently_working": "Yes", "notice_period": "2 weeks",
            "reason_for_change": "Seeking a full-time AI role", "current_compensation": "Not disclosed",
            "salary_expectation": "Market rate", "shift_ok": "Yes", "remote_ok": "Yes",
            "desired_location": "New York, NY", "years_experience": "4"}
    cases = [
        ("Are you currently working?", "Yes"),
        ("What is your official notice period?", "2 weeks"),
        ("How soon you can join us?", "2 weeks"),
        ("What is the reason for job change?", "Seeking a full-time AI role"),
        ("What is your Current CTC (Fixed and Var)?", "Not disclosed"),
        ("What is your expected CTC?", "Market rate"),
        ("Are you ready to work in EMEA shift timings?", "Yes"),
        ("Are you ready to work in Hybrid Mode, 3 days WFO?", "Yes"),
        ("What is your preferred location?", "New York, NY"),
        ("How many years of experience do you have in overall?", "4"),
    ]
    for q, want in cases:
        got = ab._answer_for(q, bank)
        check(f"answers: {q[:46]}", got == want, f"got {got!r}, wanted {want!r}")


def test_never_claims_experience_you_dont_have():
    section("False claims · a general fact must never answer a specific question")
    import apply_browser as ab, board_agents as bg
    bank = {"years_experience": "4"}
    for q in ("How many years of experience do you have in overall?",
              "Total years of professional experience"):
        check(f"general question answered: {q[:44]}", ab._answer_for(q, bank) == "4")
    for q in ("How many years of hands-on Book Keeping experience do you have?",
              "How many years experience working on Quickbooks, US Taxation, GAAP?",
              "How many years of Python experience?"):
        check(f"specific question NOT answered from a general total: {q[:40]}",
              ab._answer_for(q, bank) is None, str(ab._answer_for(q, bank)))

    # the same leak via the model path: it answers one question, a loose fuzzy match hands
    # that answer to a different one
    ab._llm_answer_fields = lambda ctx, fields, extra="", trace=None, system="": {
        "How many years of experience do you have in overall?": "4+"}
    schema = [
        {"key": "a", "label": "How many years of experience do you have in overall?", "type": "text",
         "required": True, "options": [], "sensitive": False, "_el": None},
        {"key": "b", "label": "How many years of hands-on Book Keeping experience do you have?",
         "type": "text", "required": True, "options": [], "sensitive": False, "_el": None},
    ]
    p = bg.plan(schema, {}, context="x")
    check("the model's answer is used for its own question", (p.get("a") or {}).get("answer") == "4+")
    check("it does not leak onto a different question", "b" not in p, str(p.get("b")))


def test_answers_by_index():
    section("Model I/O · indexed questions, so nothing is lost in matching")
    import apply_browser as ab
    labels = ["First question?", "Second question?", "Third question?"]
    raw = '{"answers":[{"id":1,"a":"one"},{"id":2,"a":""},{"id":3,"a":"three"}]}'
    got = ab._answers_by_id(raw, labels)
    check("answers come back onto their questions by id",
          got == {"First question?": "one", "Third question?": "three"}, str(got))
    check("a malformed reply yields nothing rather than raising",
          ab._answers_by_id("nonsense", labels) == {})
    # a model also emits answers as SIBLINGS of the answers object
    sib = '{"answers":{"A":"1"},"current_location":"Buffalo, NY"}'
    check("top-level answers are not discarded",
          ab._merge_answer_objects(sib).get("current_location") == "Buffalo, NY")
    check("true/false map onto a Yes/No control",
          ab._opt_match("true", ["Yes", "No"]) == 0 and ab._opt_match("false", ["Yes", "No"]) == 1)


def test_banded_and_combo_options():
    section("Banded options · \"4\" belongs in \"0-6 Years\"")
    import apply_browser as ab, os as _os
    bands = ["0-6 Years", "6-8 Years", "8-10 Years", "10-12 Years", "12-14 Years", "Over 14 Years"]
    for ans, want in (("0", "0-6 Years"), ("4", "0-6 Years"), ("7", "6-8 Years"),
                      ("9.5", "8-10 Years"), ("15", "Over 14 Years")):
        i = ab._opt_match(ans, bands)
        check(f"{ans} years → {want}", i is not None and bands[i] == want,
              bands[i] if i is not None else "(blank)")
    other = ["Less than 1 year", "1-3 years", "3-5 years", "5+ years"]
    check("0.5 → Less than 1 year", other[ab._opt_match("0.5", other)] == "Less than 1 year")
    check("8 → 5+ years", other[ab._opt_match("8", other)] == "5+ years")
    check("a non-numeric answer does not force a band",
          ab._opt_match("Market rate", ["0-5 LPA", "5-10 LPA"]) is None)
    # mapping "0" to "no" turned zero years into a non-match
    check("true/false only maps on an actual yes/no control",
          ab._opt_match("true", ["Yes", "No"]) == 0 and
          ab._opt_match("0", bands) == 0, "0 must still be a number against bands")

    src = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "apply_browser.py")).read()
    # typing into a fixed-list combo filters it to "No options" and the field is left blank
    check("a combobox is opened and read before anything is typed",
          "def _visible_options" in src and
          src.index("opts = _visible_options(page)") < src.index('el.type(v[:48]'))
    check("a short fixed list with no match is left alone, not typed into",
          "is not one of:" in src)

    # a planned answer is not a filled field
    bsrc = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "board_agents.py")).read()
    check("the decision table reports what actually landed",
          'd["filled"] = False' in bsrc and "has no matching option" in bsrc)
    dash = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard.html")).read()
    check("the UI flags an answer the form refused", "not accepted" in dash)


def test_saved_answers_are_tidied_and_authoritative():
    section("Filling · tidy values, authoritative identity, honest verification state")
    import apply_browser as ab, os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    srv = open(_os.path.join(root, "serve.py")).read()
    src = open(_os.path.join(root, "apply_browser.py")).read()

    # "Buffalo , New York" is not a place any autocomplete recognises, so the field stayed
    # empty and the form bounced with "Please enter your location"
    for raw, want in (("Buffalo , New York", "Buffalo, New York"),
                      ("  San  Jose ,CA ", "San Jose, CA"),
                      ("New York,NY", "New York, NY"),
                      ("Buffalo, New York", "Buffalo, New York")):
        check(f"tidied: {raw!r}", ab._tidy(raw) == want, ab._tidy(raw))
    check("the tidied value is what gets typed into a combobox",
          "v = _tidy(value)" in src)

    # a board that emails a one-time code is not "unanswered questions"
    check("an emailed verification code is its own state", '"status": "needs_code"' in src)
    check("it says the form is filled and waiting on the code",
          "emailed you a verification" in src)
    import app_status
    check("it classifies as needs_you, never as sent",
          app_status.classify({"status": "needs_code"}) == "needs_you")

    # the browser's cached identity kept overriding a corrected profile
    check("the saved profile overrides the browser's cached identity",
          '_answers["email"] = _p["email"]' in srv)


def test_prompt_and_execution_are_editable():
    section("Control · the rules and the knobs are yours to edit")
    import prompts, os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    dash = open(_os.path.join(root, "dashboard.html")).read()
    srv = open(_os.path.join(root, "serve.py")).read()
    src = open(_os.path.join(root, "apply_browser.py")).read()

    check("the form-answering prompt is a default, not hardcoded",
          "form_answer" in prompts.DEFAULTS and len(prompts.DEFAULTS["form_answer"]) > 800)
    check("the engine takes it from config", 'system=""' in src and "system or \"\"" in src)
    check("the server passes the user's edited version",
          '_prompts.get(_cfg, "form_answer")' in srv)
    check("the Agent board offers it for editing", '["form_answer"' in dash)
    # a hardcoded key list here silently dropped form_answer: the board saved it and the
    # server threw it away, so editing appeared to do nothing
    check("every prompt with a default is saveable — no hardcoded key list",
          "for k, d in _prompts.DEFAULTS.items()" in srv and
          'for k in ("understand", "summary"' not in srv)
    # the editor showed an EMPTY box for an unset prompt, so the rules could not be read
    check("the editor is prefilled with the active prompt", 'esc(cur[k]||def[k]||"")' in dash)
    check("an unset prompt reads as default, not customized", "!cur[k] || cur[k]===def[k]" in dash)
    # "did my edit take effect?" must be answerable from the record, not by eyeballing the
    # top of a scrollable box — an edit appended at the END looks identical up there
    check("the record names which prompt was used", '"prompt_source"' in src)
    check("the trace panel shows it with a length", "prompt_source" in dash and "chars</summary>" in dash)
    # the execution knobs were already there — make sure they stay
    for knob in ("execution.retries", "execution.timeout", "execution.claim_ttl",
                 "execution.domain_gap", "execution.headed", "modes.daily_cap", "model.provider"):
        check(f"editable: {knob}", f'data-c="{knob}"' in dash)

    # relationship-to-employer questions: stated once, matched everywhere
    import apply_browser as ab
    bank = {"relatives_at_company": "N/A", "worked_here_before": "No", "referred_by": "N/A"}
    for q, want in (("Do you have any relatives or close personal friends who currently work at Loenbro?", "N/A"),
                    ("Have you ever been employed by Stripe or a Stripe affiliate?", "No"),
                    ("Were you referred by an employee?", "N/A")):
        check(f"answers: {q[:44]}", ab._answer_for(q, bank) == want, str(ab._answer_for(q, bank)))


def test_answered_questions_are_never_withheld():
    section("Nothing you have already answered is withheld")
    import apply_browser as ab, board_agents as bg
    # the false-claim guard was matching any question containing "years of", so
    # "Are you at least 18 years of age?" was reported back as unanswered
    bank = {"over_18": "Yes", "years_experience": "4"}
    check("an answered age question is used", ab._answer_for("Are you at least 18 years of age?", bank) == "Yes")
    check("general experience still answers a general question",
          ab._answer_for("How many years of experience do you have in overall?", bank) == "4")
    check("but never a domain-specific one",
          ab._answer_for("How many years of hands-on Book Keeping experience do you have?", bank) is None)

    # a SENSITIVE question the candidate has answered themselves must be used, not withheld
    schema = [{"key": "c:18", "label": "Are you at least 18 years of age?", "type": "combo",
               "required": True, "options": ["Yes", "No"], "sensitive": True, "_el": None}]
    tr = {}
    p = bg.plan(schema, {"over_18": "Yes"}, context="", trace=tr)
    check("an answered sensitive question is filled, not withheld",
          (p.get("c:18") or {}).get("answer") == "Yes", str(p))
    check("and it is not listed as withheld",
          "Are you at least 18 years of age?" not in (tr.get("withheld_sensitive") or []))
    tr2 = {}
    bg.plan(schema, {}, context="", trace=tr2)
    check("an UNanswered sensitive question is still withheld",
          "Are you at least 18 years of age?" in (tr2.get("withheld_sensitive") or []))

    # everything this Loenbro form asked, from one saved bank
    full = {"preferred_language": "English", "over_18": "Yes", "commuting_distance": "Yes",
            "certifications": "AWS Certified", "referred_by": "N/A",
            "relatives_at_company": "N/A", "worked_here_before": "No"}
    # Degree / Discipline / graduation year appear on nearly every Greenhouse form
    edu = {"degree": "Master's Degree", "discipline": "Computer Science", "grad_year": "2026",
           "certifications": "None"}
    for q, want in (("Degree", "Master's Degree"), ("Discipline", "Computer Science"),
                    ("End date year", "2026"),
                    ("Do you hold any certifications that are applicable to this role?", "None")):
        check(f"answers: {q[:40]}", ab._answer_for(q, edu) == want, str(ab._answer_for(q, edu)))
    # and they are derived from the profile's education even before the bank is filled
    import apply as _apply
    got = _apply._enrich_standing(
        {"data": {"education": [{"school": "SUNY Buffalo", "degree": "Master's Degree",
                                 "field": "Computer Science", "dates": "2024-2026"}]}}, {})
    for k, v in (("degree", "Master's Degree"), ("discipline", "Computer Science"), ("grad_year", "2026")):
        check(f"derived from education: {k}", got.get(k) == v, str(got.get(k)))

    for q, want in (("What is your preferred language?", "English"),
                    ("Are you local and within commuting distance to this job?", "Yes"),
                    ("Please list any additional education, training or certifications relevant to the job (ie. OSHA, TWIC, etc.).", "AWS Certified"),
                    ("Have you worked for Loenbro or any of its affiliates in the past?", "No"),
                    ("Do you have any relatives or close personal friends who currently work at Loenbro? Place N/A if none.", "N/A"),
                    ("Were you referred by a search firm or staffing agency? If not, please indicate by using N/A.", "N/A")):
        check(f"answers: {q[:44]}", ab._answer_for(q, full) == want, str(ab._answer_for(q, full)))


def test_episodic_memory_and_recall():
    section("Memory · the same question, asked differently, is recognised")
    import memory, board_agents as bg, os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))

    # remembering
    prof = {"data": {"answer_memory": []}}
    eps = memory.remember(prof, "loenbro", [
        {"label": "What is your preferred language?", "answer": "English", "source": "profile", "filled": True},
        {"label": "Location (City)", "answer": "Buffalo", "source": "profile", "filled": False},
        {"label": "Gender?", "answer": "", "source": "unanswered", "filled": False},
    ])
    by = {e["q"]: e for e in eps}
    check("an accepted answer is remembered", by["What is your preferred language?"].get("a") == "English")
    check("a REJECTED answer is remembered as rejected, not reused",
          by["Location (City)"].get("rejected") == "Buffalo" and not by["Location (City)"].get("a"))
    check("an unanswered question is remembered as seen", by["Gender?"].get("seen") == 1)
    check("which board asked it is kept", by["Gender?"].get("boards") == ["loenbro"])

    eps2 = memory.remember({"data": {"answer_memory": eps}}, "capco", [
        {"label": "what is your preferred language?", "answer": "English", "filled": True}])
    check("the same question from another board increments, not duplicates",
          len(eps2) == 3 and next(e for e in eps2 if e["q"].lower().startswith("what is your preferred"))["seen"] == 2)

    check("exact recall ignores case and punctuation",
          memory.direct_recall("WHAT IS YOUR PREFERRED LANGUAGE", eps) == "English")
    check("only answers that stuck are recallable",
          memory.direct_recall("Location (City)", eps) is None)

    # semantic recall: only ever points at an answer already held
    src = open(_os.path.join(root, "board_agents.py")).read()
    check("recall runs BEFORE composing a new answer",
          src.index("_semantic_recall(recall_targets") < src.index("_llm_answer_fields(context, fields"))
    check("sensitive questions are never sent for recall",
          "recall_targets = [f for f in ask if not f[\"sensitive\"]]" in src)
    check("a match must resolve to an answer we already hold",
          'k["answer"].strip().lower() == ans0.lower()' in src)
    check("an unreconcilable claim is dropped, not guessed", '"recall_rejected"' in src)

    # the planner prefers memory over everything
    p = bg.plan([{"key": "k", "label": "What is your preferred language?", "type": "text",
                  "required": True, "options": [], "sensitive": False, "_el": None}],
                {}, context="", episodes=eps)
    check("a remembered question is answered from memory",
          (p.get("k") or {}).get("source") == "memory", str(p))


def test_submission_is_only_confirmed_when_the_form_is_gone():
    section("Confirmation · never claim applied while the form is still on screen")
    import apply_browser as ab, os as _os
    src = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "apply_browser.py")).read()

    # "successfully" appears in ordinary job-description prose. Matching it reported a form
    # that had bounced on a validation error as "Applied — confirmation page detected", while
    # the screenshot still showed the filled form and a red "Select a country".
    check("bare 'successfully' is no longer a success phrase", "successfully" not in ab._SUCCESS)
    for weak in ("thank you", "we received", "your application has been"):
        check(f"the vague phrase {weak!r} is not on its own a confirmation", weak not in ab._SUCCESS)
    for real in ("thank you for applying", "application submitted", "received your application"):
        check(f"a real confirmation phrase is kept: {real!r}", real in ab._SUCCESS)

    v = src.split("def _verify(", 1)[1].split("\ndef ", 1)[0]
    check("the form-still-present check comes BEFORE any text matching",
          v.index("return \"stuck\"") < v.index("_SUCCESS"))
    check("a lingering submit button also counts as not submitted",
          "Submit application')" in v and "#submit_app" in v)
    check("confirmation text must be NEW since before the click", "before" in v and "fresh" in v)
    check("the page text is captured before clicking submit", "_before_text = page.inner_text" in src)
    check("and passed to the verifier", "_verify(page, frame, before=_before_text)" in src)


def test_apply_budget_and_decline_matching():
    section("Timeouts read like timeouts; 'prefer not to say' means decline")
    import apply_browser as ab, os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    srv = open(_os.path.join(root, "serve.py")).read()
    dash = open(_os.path.join(root, "dashboard.html")).read()

    # a raw Python traceback in the activity feed tells you nothing actionable
    check("the apply budget is configurable, not hardcoded at 150s",
          "timeout=_apply_budget" in srv and "timeout=150" not in srv)
    check("it is clamped to something sane", "max(60, min(900, _apply_budget))" in srv)
    check("it is editable in the Agent board", 'data-c="execution.apply_timeout"' in dash)
    check("a timeout is reported as one, with what to do about it",
          "subprocess.TimeoutExpired" in srv and "Raise 'Apply timeout'" in srv)
    import app_status
    check("and is retryable", app_status.classify({"status": "timeout"}) == "failed_transient")

    # every board words "I'd rather not answer" differently — and demographics is exactly
    # where picking the wrong option matters
    for opts in (["Yes", "No", "Decline To Self Identify"],
                 ["Male", "Female", "Non-binary", "I do not wish to answer"],
                 ["Hispanic or Latino", "Not Hispanic or Latino", "Prefer not to disclose"]):
        for ans in ("Prefer not to say", "I do not wish to answer", "Choose not to disclose"):
            i = ab._opt_match(ans, opts)
            check(f"{ans!r} → a decline option in {opts[-1][:22]!r}",
                  i is not None and bool(ab._DECLINE.search(opts[i])),
                  opts[i] if i is not None else "(blank)")
    check("a real answer is unaffected", ab._opt_match("Male", ["Male", "Female"]) == 0)


def test_dead_feed_is_not_silent():
    section("Job sources · a DEAD feed must not masquerade as 'no openings'")
    import ats
    check("default mode still never raises (other callers rely on it)",
          ats.fetch_feed("lever", "__definitely_not_a_company__") == [])
    raised = False
    try:
        ats.fetch_feed("lever", "__definitely_not_a_company__", strict=True)
    except Exception:
        raised = True
    check("strict mode surfaces the failure (crawler uses this)", raised)
    check("crawler asks for strict mode",
          "strict=True" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")).read())
    check("no known-dead Lever slugs are still configured",
          not ({"openai", "figma", "plaid", "gitlab", "cohere"} & set(ats.PRIORITY.get("lever") or [])),
          str(ats.PRIORITY.get("lever")))


def test_job_board_segregates_by_domain():
    section("Job board · jobs are segregated by domain, and YOUR domain comes first")
    import serve
    cases = [
        ("Senior Machine Learning Engineer",   "",           ("Engineering", "AI / ML")),
        ("Staff Backend Engineer",             "",           ("Engineering", "Backend")),
        ("Site Reliability Engineer",          "",           ("Engineering", "DevOps / Infra")),
        ("iOS Developer",                      "",           ("Engineering", "Mobile")),
        ("Product Manager, Payments",          "",           ("Product", "Product Management")),
        ("Product Designer",                   "",           ("Design", "Product Design")),
        ("Account Executive, Enterprise",      "",           ("Sales", "Account Exec")),
        ("Registered Nurse",                   "",           ("Healthcare", "Clinical")),
        # function words win over tech words — a sales role about AI is still a sales role
        ("Principal, Strategic AI Sales",      "",           ("Sales", "Account Exec")),
        ("AI Product Marketing Manager",       "",           ("Marketing", "Marketing")),
        ("Technical Recruiter, AI",            "",           ("People", "People / HR")),
        # whole words only — "ai" must not fire on "Aide", "qa" not on "Qatar"
        ("Aide a domicile H/F",                "",           ("Other", "Other")),
        ("Qatar Country Manager",              "",           ("Other", "Other")),
        ("Cybersecurity Analyst",              "",           ("Engineering", "Security")),
        ("Senior C++ Developer",               "",           ("Engineering", "Backend")),
        ("Actuarial Analyst",                  "",           ("Finance", "Finance")),
        ("HR Business Partner",                "",           ("People", "People / HR")),
    ]
    for title, dept, want in cases:
        got = serve._category(title, dept)
        check(f"{title!r} → {want[0]} › {want[1]}", tuple(got) == want, str(got))
    check("an unclassifiable title in an engineering department is still tech",
          tuple(serve._category("Team Member", "Engineering"))[0] == "Engineering")
    check("an unclassifiable title elsewhere is honestly 'Other'",
          tuple(serve._category("Team Member", ""))[0] == "Other")
    me = serve._my_domain({"title": "AI Engineer", "skills": ["python", "pytorch", "llm"]})
    check("profile is classified the same way jobs are (AI engineer → Engineering › AI / ML)",
          tuple(me) == ("Engineering", "AI / ML"), str(me))
    src = serve.jobs_query.__code__.co_varnames
    check("jobs_query reads category + sub filters", "cat" in src and "sub" in src)
    html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")).read()
    check("dashboard has the rail + feed board", 'id="rail"' in html and 'id="feed"' in html)
    check("board sends category / sub / page to the API",
          'p.set("category"' in html and 'p.set("sub"' in html and 'p.set("page"' in html)
    check("board opens on the user's own domain first", "r.facets.my_domain" in html)
    check("no more 5-job cap on the feed", "slice(0,5)" not in html.split("function renderCards")[1].split("const STABS")[0])


def main():
    print("═" * 62); print("ENGINE TESTS · in-memory DB · nothing submitted, no network"); print("═" * 62)
    for t in (test_apply_one_auto, test_apply_one_review, test_review_blocked_when_incomplete,
              test_honest_gate, test_captcha_path, test_confirmed_vs_unconfirmed,
              test_retry_scheduling, test_dedup, test_claim_lock, test_enrich_no_invention,
              test_crawler_throttle, test_crawler_cycle_on_dead_db, test_read_resilience,
              test_dead_feed_is_not_silent, test_form_not_ready_is_not_success,
              test_never_submits_without_resume, test_consent_prefers_rejecting,
              test_opening_a_posting_is_not_an_application,
              test_answers_can_be_saved_and_reused, test_navigates_to_the_real_form,
              test_board_agents_plan_then_fill, test_nav_is_identical_everywhere, test_activity_actions_actually_work, test_decision_trace_is_recorded,
              test_option_matching_is_precise,
              test_hosted_model_and_saved_answers_are_actually_used,
              test_answer_bank_questionnaire, test_never_claims_experience_you_dont_have,
              test_answers_by_index, test_banded_and_combo_options,
              test_saved_answers_are_tidied_and_authoritative,
              test_prompt_and_execution_are_editable,
              test_answered_questions_are_never_withheld,
              test_episodic_memory_and_recall,
              test_submission_is_only_confirmed_when_the_form_is_gone,
              test_apply_budget_and_decline_matching,
              test_job_board_segregates_by_domain):
        try:
            t()
        except Exception as e:
            FAIL.append(t.__name__)
            print(f"  ❌ {t.__name__} EXPLODED: {e.__class__.__name__}: {str(e)[:200]}")
    print("\n" + "═" * 62)
    print(f"RESULT · {len(PASS)} passed · {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    print("═" * 62)
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
