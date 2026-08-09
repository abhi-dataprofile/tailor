#!/usr/bin/env python3
"""apply.py — automated background applications for opted-in users.

Operator-run. For every user who turned auto-apply ON, it submits to their top
matched jobs with no per-application UI — fully hands-off — reusing the hardened
engine in serve.py (thorough logging, idempotency, guards).

Non-negotiable safety rails (kept even in full-auto, because they prevent
misrepresenting the candidate, not because of UI):
  * Legal / comp / demographic answers (work authorization, sponsorship, salary,
    EEO) come ONLY from the user's explicit standing answers — never guessed by a
    model. If a required one is missing, that job is recorded 'awaiting_review'
    and skipped (never fabricated).
  * Idempotent — never double-applies (dedup by url/apply_id).
  * CAPTCHA / unsupported board -> recorded 'manual' with the link, not dropped.
  * Rate-limited, with a per-user cap per run.

The user opts in by setting profiles.data.auto_apply = {enabled, min_score,
max_per_run} and profiles.data.standing = {work_authorized, needs_sponsorship,
salary_expectation, ...}.

Run:  python3 apply.py            # all opted-in users
      python3 apply.py --user ID  # one user
      python3 apply.py --dry      # synthetic, no network/DB (shows the safety logic)
"""
import envload  # noqa: F401 — loads .env
import os, time, uuid, argparse, json, re, datetime
import serve                      # reuse the hardened, tested apply engine (no server starts on import)
import supabase_client as sb
import llm
import ats_official
from app_status import classify, SETTLED

MAX_RETRIES    = int(os.environ.get("APPLY_MAX_RETRIES", "3"))
CLAIM_TTL_MIN  = int(os.environ.get("APPLY_CLAIM_TTL_MIN", "15"))   # a 'filling' claim older than this = crashed worker → reclaimable
DOMAIN_MIN_GAP = float(os.environ.get("APPLY_DOMAIN_GAP", "8"))     # min seconds between applies to the SAME board

def _now():
    return datetime.datetime.now(datetime.timezone.utc)

def _iso(dt):
    return dt.isoformat()

_DOMAIN_LAST = {}

def _throttle(url):
    """Politeness / anti-hammer: keep a minimum gap between applies to the same board."""
    try:
        host = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(url or "").netloc.lower()
    except Exception:
        host = ""
    dom = ".".join(host.split(".")[-2:]) if host else ""
    if not dom:
        return
    wait = DOMAIN_MIN_GAP - (time.time() - _DOMAIN_LAST.get(dom, 0))
    if wait > 0:
        time.sleep(wait)
    _DOMAIN_LAST[dom] = time.time()

def _claim(user_id, job_id):
    """Atomically claim a job (move its application row to 'filling') so two overlapping
    workers can't both apply. Returns True (claimed), False (someone else holds it or it's
    settled), or None (claim column not present → caller uses the legacy dedup fallback)."""
    try:
        now = _now(); now_iso = _iso(now)
        # win by CREATING the row (no row exists yet for this job)
        created = sb.upsert("applications", [{"user_id": user_id, "job_id": job_id,
                  "status": "filling", "claimed_at": now_iso, "human_in_loop": False}],
                  on_conflict="user_id,job_id", update=False)
        if created:
            return True
        # row exists — claim ONLY if it's retryable, or a stale 'filling' from a dead worker
        cutoff = _iso(now - datetime.timedelta(minutes=CLAIM_TTL_MIN))
        got = sb.update("applications",
              {"user_id": f"eq.{user_id}", "job_id": f"eq.{job_id}",
               "or": f"(status.eq.failed_transient,and(status.eq.filling,claimed_at.lt.{cutoff}))"},
              {"status": "filling", "claimed_at": now_iso})
        return bool(got)
    except Exception:
        return None   # e.g. claimed_at column not migrated yet → degrade to legacy dedup

def submit_application(job, answers, resume_html, dry, standing=None):
    """Pick the best available submission backend for this job:
       1) official employer API (if this company is in connectors.json),
       2) headless browser (Playwright) when APPLY_BROWSER=1 — handles Greenhouse,
          Lever and Ashby, filling legal Qs from `standing` and free-text via the LLM,
       3) the legacy unauthenticated form engine (Greenhouse-only)."""
    cfg = ats_official.resolve(job)
    if cfg:
        r = ats_official.submit(job, answers, resume_html, cfg)
        if r.get("status") != "not_available":
            r["backend"] = "official:" + cfg.get("ats", "?")
            return r
    if os.environ.get("APPLY_BROWSER") == "1":
        try:
            import apply_browser
            r = apply_browser.submit(job, answers, resume_html, standing=standing or {}, dry=dry)
            r["backend"] = "browser"
            return r
        except Exception as e:
            print("  [browser] unavailable:", str(e)[:120])
    r = serve.gh_apply(job["url"], answers, resume_html)
    r["backend"] = "legacy_form"
    return r

MIN_SCORE   = int(os.environ.get("APPLY_MIN_SCORE", "45"))
MAX_PER_RUN = int(os.environ.get("APPLY_MAX_PER_USER", "10"))
RATE_SLEEP  = float(os.environ.get("APPLY_RATE_SLEEP", "4"))

def builtins_from(profile):
    nm = (profile.get("name") or "").split()
    return {"first_name": nm[0] if nm else "", "last_name": " ".join(nm[1:]),
            "email": profile.get("email") or "",
            "phone": (profile.get("contact") or "").split("·")[0].strip()}

def standing_lookup(standing, label):
    """A user's explicit answer for a sensitive question, or None (never guessed)."""
    low = (label or "").lower()
    for key, val in (standing or {}).items():
        kl = key.lower()
        if kl in low or all(w in low for w in kl.split()):
            return val
    if "sponsor" in low or "visa" in low:                 return (standing or {}).get("needs_sponsorship")
    if "authoriz" in low and "work" in low:               return (standing or {}).get("work_authorized")
    if "salary" in low or "compensation" in low:          return (standing or {}).get("salary_expectation")
    return None

def _parse_json(t):
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", t or "")
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None

def fill_answers(job, profile, ans):
    """Fill required questions: sensitive -> standing only; others -> model from real data (blank if unknown)."""
    board, jid = serve.gh_ids(job["url"])
    try:
        q = serve.gh_questions(board, jid)
    except Exception:
        return ans, []
    standing = (profile.get("data") or {}).get("standing") or {}
    blocked, nonsens = [], []
    for question in q.get("questions", []):
        label, required = question.get("label") or "", question.get("required")
        sensitive = bool(serve.SENSITIVE.search(label))
        for f in question.get("fields", []):
            name, ftype = f.get("name"), f.get("type")
            if ftype in serve.FILE_TYPES or name in ans or not required:
                continue
            if sensitive:
                v = standing_lookup(standing, label)
                if v not in (None, ""):
                    ans[name] = str(v)
                else:
                    blocked.append(label)
            else:
                nonsens.append(question); break
    if nonsens:
        try:
            sysp = ("Fill job-application questions ONLY from the candidate's data. If a fact isn't present, "
                    "use an empty string — never guess. Multiple-choice: reply with exactly one provided option. "
                    'STRICT JSON: {"answers":{"<field name>":"<answer>"}}.')
            userp = ("CANDIDATE:\n" + (profile.get("summary") or "") + "\nSKILLS: " + ", ".join(profile.get("skills") or [])
                     + "\nFACTS: " + json.dumps(standing) + "\nQUESTIONS:\n" + json.dumps(
                         [{"label": q2.get("label"), "fields": [{"name": f.get("name"), "type": f.get("type"),
                           "options": [v.get("label") for v in (f.get("values") or [])]} for f in q2.get("fields", [])]}
                          for q2 in nonsens]))
            obj = _parse_json(llm.gen(sysp, userp, json_mode=True, temp=0, max_tokens=800)) or {}
            for k, v in (obj.get("answers") or {}).items():
                if isinstance(v, str) and v.strip():
                    ans[k] = v
        except Exception:
            pass
    return ans, blocked

def resume_for(user_id, job_id, profile, job=None):
    """A COMPLETE, job-targeted résumé for this application — never the old barebones stub.
      1. a résumé the user explicitly saved for this job (tailorings.resume_html) wins;
      2. otherwise build a full résumé from the profile, using the tailored summary if we
         have one — tailoring the summary ON DEMAND (LLM) when it's missing and a model is
         configured; falling back to the profile's own summary when it isn't."""
    import resume_build
    rows = sb.select("tailorings", {"user_id": f"eq.{user_id}", "job_id": f"eq.{job_id}",
                                    "select": "resume_html,summary,bullets"})
    r = rows[0] if rows else {}
    if r.get("resume_html"):
        return r["resume_html"]              # a résumé already tailored/saved for this job
    # FULL tailor on demand: rewrite the candidate's real history to the role, build the
    # ~one-page résumé, and cache the rendered HTML so we never re-tailor this job.
    if job is not None:
        try:
            import pipeline, llm
            if llm.available():
                tprofile, meta = pipeline.tailor_full(profile, job)
                html = resume_build.build_resume_html(tprofile)
                try:
                    sb.upsert("tailorings", [{"user_id": user_id, "job_id": job_id,
                              "summary": meta.get("summary"), "resume_html": html,
                              "provider": meta.get("provider")}], on_conflict="user_id,job_id", update=True)
                except Exception:
                    pass
                return html
        except Exception:
            pass   # tailoring failed → fall back to a complete, untailored résumé below
    return resume_build.build_resume_html(profile, r)

def _record(user_id, job, res, ans, apply_id, blocked, resume_html=""):
    import hashlib as _h
    rsha = _h.sha256((resume_html or "").encode("utf-8")).hexdigest()[:16] if resume_html else ""
    rec = {"apply_id": apply_id, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "user": user_id, "auto": True,
           "job": job.get("title"), "job_id": job["id"], "url": job["url"], "vendor": job.get("vendor"),
           "backend": res.get("backend"), "dry_run": bool(serve.DRY_RUN), "answers": ans, "ok": bool(res.get("ok")),
           "status": res.get("status"), "detail": res.get("detail"), "screenshot": res.get("screenshot"),
           "resume_sha": rsha, "resume_len": len(resume_html or ""),
           "submitted_fields": res.get("submitted"), "sensitive_sent": res.get("sensitive_sent"), "blocked": blocked}
    serve.log_receipt(rec)
    try:
        status = classify(res)
        sent = status in ("confirmed", "submitted_unconfirmed")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        prior = sb.select("applications", {"user_id": f"eq.{user_id}", "job_id": f"eq.{job['id']}", "select": "attempts,receipt"})
        attempts = ((prior[0].get("attempts") if prior else 0) or 0) + 1
        # keep a status-change timeline across attempts (in-depth tracking) — cap to last 20
        events = ((prior[0].get("receipt") or {}).get("events") if prior else None) or []
        events = (events + [{"at": now_iso, "status": status, "detail": res.get("detail"),
                             "backend": res.get("backend")}])[-20:]
        rec["events"] = events
        row = {"user_id": user_id, "job_id": job["id"], "status": status, "human_in_loop": False,
               "answers": ans, "receipt": rec, "attempts": attempts, "next_retry_at": None,
               "submitted_at": now_iso if sent else None,
               "confirmed_at": now_iso if status == "confirmed" else None}
        if (sent or status == "awaiting_review") and resume_html:
            row["resume_html"] = resume_html   # snapshot exactly what we sent / prepared

        # ONLY genuine transient failures are retried (never a form we already sent).
        if status == "failed_transient" and attempts < MAX_RETRIES:
            row["next_retry_at"] = _iso(_now() + datetime.timedelta(hours=min(6, 2 ** attempts)))
        sb.upsert("applications", [row], on_conflict="user_id,job_id", update=True)
    except Exception:
        pass

def apply_one(user_id, profile, job, review=False, force_live=False):
    """review=True → tailor + fill + screenshot but DON'T submit; record 'awaiting_review'
    (ready for the user's one-click Submit). force_live=True → the user clicked Submit on a
    reviewed job, so send it. DRY_RUN is an absolute operator kill-switch and always wins."""
    apply_id = uuid.uuid4().hex
    # apply via the clean Greenhouse form, not a company careers-page embed (avoids most
    # spurious captcha / no-submit-button failures).
    canon = serve.canonical_apply_url(job)
    if canon and canon != job.get("url"):
        job = dict(job); job["url"] = canon
    if serve.already_applied(job["url"], apply_id):
        # applied before (e.g. interactively) — settle the row instead of leaving it 'filling'
        _record(user_id, job, {"ok": True, "status": "submitted", "sent": True, "confirmed": False,
                "detail": "Already applied earlier — not re-submitting."}, {}, apply_id, [])
        return "already_applied"
    ans = builtins_from(profile)
    ans, blocked = fill_answers(job, profile, ans)
    if blocked:
        _record(user_id, job, {"ok": False, "status": "needs_review",
                "detail": "Missing standing answers: " + "; ".join(blocked[:4])}, ans, apply_id, blocked)
        return "needs_review"
    resume = resume_for(user_id, job["id"], profile, job)
    standing = (profile.get("data") or {}).get("standing") or {}   # answer bank for the browser engine
    _throttle(job["url"])                    # keep a polite gap between hits on the same board
    dry = bool(serve.DRY_RUN) or (review and not force_live)
    res = submit_application(job, ans, resume, dry=dry, standing=standing)
    # review-mode clean prepare (nothing required missing) → awaiting_review = ready to Submit
    if review and not force_live and res.get("status") == "dry_prepared" and not (res.get("unfilled_required") or []):
        res = {**res, "status": "awaiting_review", "ok": True,
               "detail": "Prepared and ready — review and click Submit."}
    _record(user_id, job, res, ans, apply_id, [], resume)
    return (res.get("backend", "?") + ":" + str(res.get("status")))

def submit_reviewed(user_id, job_id):
    """User clicked Submit on a reviewed job → send it live with the prepared answers."""
    prof = (sb.select("profiles", {"user_id": f"eq.{user_id}", "select": "*"}) or [{}])[0]
    jr = sb.select("jobs", {"id": f"eq.{job_id}", "select": "*"})
    if not jr:
        return {"ok": False, "detail": "job not found"}
    st = apply_one(user_id, prof, jr[0], review=False, force_live=True)
    return {"ok": True, "result": st}

def run(user):
    users = [{"user_id": user}] if user else [u for u in sb.select("profiles", {"select": "user_id"})]
    for u in users:
        prof = (sb.select("profiles", {"user_id": f"eq.{u['user_id']}", "select": "*"}) or [{}])[0]
        cfg = (prof.get("data") or {}).get("auto_apply") or {}
        if not cfg.get("enabled"):
            continue
        minsc, cap = cfg.get("min_score", MIN_SCORE), cfg.get("max_per_run", MAX_PER_RUN)
        review = (cfg.get("mode") or "auto") == "review"   # 'review' → prepare only, wait for Submit
        ujs = sb.select("user_jobs", {"user_id": f"eq.{u['user_id']}", "select": "job_id,score", "order": "score.desc", "limit": "200"})
        done = {r["job_id"] for r in sb.select("applications", {"user_id": f"eq.{u['user_id']}", "select": "job_id,status"})
                if r.get("status") in SETTLED}
        n = 0
        for uj in ujs:
            if n >= cap:
                break
            jid = uj["job_id"]
            if (uj.get("score") or 0) < minsc:
                continue
            # ATOMIC CLAIM: move the row to 'filling' so a second (overlapping) worker skips it.
            claimed = _claim(u["user_id"], jid)
            if claimed is False:                    # settled, or held by another worker
                continue
            if claimed is None and jid in done:      # pre-migration fallback: legacy dedup
                continue
            jr = sb.select("jobs", {"id": f"eq.{jid}", "select": "*"})
            if not jr:
                continue
            job = jr[0]
            # Greenhouse works via the legacy engine; Lever/Ashby (and others) need the
            # browser engine. If it's off, non-Greenhouse can't be auto-submitted — record
            # it honestly as manual rather than pretending, and don't burn the claim.
            if job.get("vendor") != "greenhouse" and os.environ.get("APPLY_BROWSER") != "1":
                _record(u["user_id"], job, {"ok": False, "status": "unsupported",
                        "detail": "Needs the browser engine (set APPLY_BROWSER=1) — queued manual."}, {}, uuid.uuid4().hex, [])
                continue
            st = apply_one(u["user_id"], prof, job, review=review)
            print(f"  [{u['user_id'][:8]}] {str(job.get('title',''))[:40]:40} -> {st}{' (review)' if review else ''}")
            n += 1
            time.sleep(RATE_SLEEP)
        print(f"[{u['user_id'][:8]}] attempted {n} (min_score={minsc}, cap={cap})")

def run_retry():
    """Re-attempt transient failures whose backoff has elapsed; report the manual queue."""
    now = _iso(_now())
    # recover crashed workers: a 'filling' claim older than the TTL → back to retryable
    try:
        stale = _iso(_now() - datetime.timedelta(minutes=CLAIM_TTL_MIN))
        sb.update("applications", {"status": "eq.filling", "claimed_at": f"lt.{stale}"},
                  {"status": "failed_transient"})
    except Exception:
        pass
    due = sb.select("applications", {"status": "eq.failed_transient", "select": "user_id,job_id,attempts",
                                     "or": f"(next_retry_at.is.null,next_retry_at.lt.{now})", "limit": "300"})
    retried = 0
    for a in due:
        if (a.get("attempts") or 0) >= MAX_RETRIES:
            continue
        prof = (sb.select("profiles", {"user_id": f"eq.{a['user_id']}", "select": "*"}) or [{}])[0]
        jr = sb.select("jobs", {"id": f"eq.{a['job_id']}", "select": "*"})
        if not jr or not ((prof.get("data") or {}).get("auto_apply") or {}).get("enabled"):
            continue
        st = apply_one(a["user_id"], prof, jr[0])
        print(f"  retry [{a['user_id'][:8]}] job {a['job_id']} -> {st}")
        retried += 1
        time.sleep(RATE_SLEEP)
    confirmed = serve.reconcile_confirmations()   # promote sent→applied from confirmation emails
    manual = sb.select("applications", {"status": "eq.blocked_captcha", "select": "user_id,job_id", "limit": "500"})
    review = sb.select("applications", {"status": "eq.needs_you", "select": "user_id,job_id", "limit": "500"})
    unconf = sb.select("applications", {"status": "eq.submitted_unconfirmed", "select": "user_id,job_id", "limit": "500"})
    print(f"[retry] re-attempted {retried} · confirmed by email: {confirmed} · captcha queue (need a human): {len(manual)} · "
          f"awaiting your answers: {len(review)} · sent but unconfirmed: {len(unconf)}")

def dry():
    profile = {"name": "Alex Morgan", "email": "alex@morgan.io", "contact": "+1 555 0100",
               "summary": "Frontend engineer.", "skills": ["React", "TypeScript"],
               "data": {"standing": {"work_authorized": "Yes", "needs_sponsorship": "No", "salary_expectation": "$150,000"},
                        "auto_apply": {"enabled": True}}}
    st = profile["data"]["standing"]
    print("builtins    ->", builtins_from(profile))
    print("sponsorship ->", standing_lookup(st, "Will you require the company to sponsor you for a work permit?"))
    print("work auth   ->", standing_lookup(st, "Are you authorized to work in the location(s) you selected?"))
    print("salary      ->", standing_lookup(st, "What are your salary expectations?"))
    print("unprovided  ->", standing_lookup(st, "Have you ever been convicted of a felony?"), "(None -> job skipped as needs_review, NOT fabricated)")
    print("\n[dry] Sensitive answers come only from the user's standing data; anything missing blocks that job.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--retry", action="store_true", help="re-attempt transient failures; report the manual queue")
    ap.add_argument("--reconcile", action="store_true", help="scan confirmation emails; promote sent→applied")
    a = ap.parse_args()
    if a.dry or not sb.is_configured():
        if not sb.is_configured() and not a.dry:
            print("Supabase not configured — running --dry.\n")
        return dry()
    if a.reconcile:
        print(f"[reconcile] confirmed by email: {serve.reconcile_confirmations()}"); return
    if a.retry:
        return run_retry()
    run(a.user)

if __name__ == "__main__":
    main()
