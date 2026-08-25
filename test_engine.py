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
              test_never_submits_without_resume, test_consent_prefers_rejecting):
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
