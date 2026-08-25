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
    blk = srv.split('"/api/answers"', 1)[1][:2200]
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
        {"key": "radio:gender", "label": "Gender", "type": "radio", "required": True, "options": ["Male", "Female"], "sensitive": True, "_el": None},
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
    check("both pages define the same nav", a == b and len(a) == 7, f"{a} vs {b}")
    check("Review queue and Agent are both present", {"Review queue", "Agent"} <= set(a), str(a))
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
    for name, src in (("index.html", idx), ("dashboard.html", dash)):
        check(f"{name}: the shell uses the screen instead of a fixed narrow column",
              "min(1700px, 96vw)" in src)

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
              test_board_agents_plan_then_fill, test_nav_is_identical_everywhere):
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
