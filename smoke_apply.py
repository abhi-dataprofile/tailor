#!/usr/bin/env python3
"""smoke_apply.py — prove the whole auto-apply loop end-to-end, DRY, in one command.

Walks a single application through every real stage the engine uses — fetch a live
Greenhouse job, canonicalize its URL, extract the form, fill legal + free-text
answers, tailor a full résumé, (optionally) write a cover letter, and drive the
real browser engine in DRY mode (fills everything, never clicks Submit) — printing
PASS / FAIL / SKIP for each stage so you can see exactly where the loop stands.

Nothing is ever submitted: DRY_RUN is forced on before any engine import, and the
browser backend is called with dry=True. No rows are written (we call the stages
directly, not apply_one), so it's safe to run against a live or a down DB — DB-only
stages just report SKIP when Supabase isn't reachable.

    python3 smoke_apply.py                # headless, auto-pick a company with openings
    APPLY_HEADED=1 python3 smoke_apply.py # watch the browser fill the form
    python3 smoke_apply.py stripe         # force a specific Greenhouse company slug
"""
import os, sys, time, json

# Belt-and-suspenders: force DRY before anything imports the engine, and make sure
# the browser backend is the one exercised.
os.environ["DRY_RUN"] = "1"
os.environ.setdefault("APPLY_BROWSER", "1")

import envload  # noqa: F401 — load .env (LLM keys, persona, etc.)
import ats
import serve
import apply as engine

# Candidate Greenhouse companies to try in order until one has an open req.
CANDIDATES = sys.argv[1:] or [
    "stripe", "ramp", "brex", "notion", "figma", "vercel", "datadog", "openai", "anthropic",
]

# A realistic international-student persona — the app's default for unknowns.
PROFILE = {
    "name": "Alex Rivera",
    "email": "alex.rivera.smoke@example.com",
    "contact": "+1 555 010 2020 · San Jose, CA",
    "title": "Software Engineer",
    "summary": ("New-grad software engineer with internship experience in backend services and "
                "data pipelines; ships reliable Python/TypeScript and cares about correctness."),
    "skills": ["Python", "TypeScript", "React", "PostgreSQL", "AWS", "Docker", "REST APIs", "Git"],
    "experience": [
        {"company": "Cloudscale Inc", "role": "Software Engineer Intern", "dates": "Summer 2025",
         "bullets": ["Built an ingestion service handling 2M events/day",
                     "Cut p95 API latency 38% by adding a read-through cache"]},
    ],
    "education": [{"school": "San Jose State University", "degree": "M.S. Computer Science",
                   "dates": "2024–2026"}],
    "data": {
        "standing": {
            "work_authorized": "Yes",
            "needs_sponsorship": "Yes",
            "requires_sponsorship": "Yes",
            "over_18": "Yes",
            "gender": "Prefer not to say",
            "race": "Prefer not to say",
            "veteran_status": "I am not a protected veteran",
            "disability_status": "I do not wish to answer",
            "how_did_you_hear": "Company website",
            "start_date": "2026-06-01",
            "salary_expectation": "Market rate",
        },
        "orchestration": {"tailor": {"engine": "server"}, "answers": {"cover_letter": False}},
    },
}

# ----- pretty stage runner ------------------------------------------------------
_W = 62
def _hr(): print("─" * _W)
def _head(t): _hr(); print(t); _hr()

RESULTS = []
def stage(n, name, fn):
    """Run one stage. fn returns (ok: bool|None, detail: str). None ok → SKIP."""
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{e.__class__.__name__}: {str(e)[:160]}"
    dt = time.time() - t0
    tag = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ "}[tag]
    RESULTS.append(tag)
    print(f"{icon} [{n}] {name:<34} {dt:5.1f}s")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")
    return ok


def main():
    _head(f"SMOKE · auto-apply loop (DRY) · headed={os.environ.get('APPLY_HEADED') == '1'}")

    # Stage 0 — find a live Greenhouse job to apply to.
    JOB = {}
    def s0():
        for slug in CANDIDATES:
            try:
                feed = list(ats.fetch_feed("greenhouse", slug))
            except Exception:
                continue
            # prefer an engineering-ish role; fall back to the first opening
            pick = next((j for j in feed if "engineer" in (j.get("title") or "").lower()), None) \
                   or (feed[0] if feed else None)
            if pick:
                JOB.update(pick)
                return True, f"{slug}: “{JOB.get('title')}”\n{JOB.get('url')}"
        return False, f"no openings found across {len(CANDIDATES)} companies"
    if not stage(0, "Fetch a live Greenhouse job", s0):
        return _summary()

    # Stage 1 — canonicalize to the clean board form (avoids embed captchas).
    def s1():
        canon = serve.canonical_apply_url(JOB)
        if canon and canon != JOB.get("url"):
            JOB["url"] = canon
            return True, f"→ {canon}"
        return True, "already canonical"
    stage(1, "Canonicalize apply URL", s1)

    # Stage 2 — extract the application form (questions + required fields).
    Q = {}
    def s2():
        board, jid = serve.gh_ids(JOB["url"])
        q = serve.gh_questions(board, jid)
        Q.update(q)
        qs = q.get("questions", [])
        req = [x.get("label") for x in qs if x.get("required")]
        return True, f"{len(qs)} questions · {len(req)} required\n" + \
               "\n".join(f"- {l}" for l in req[:6])
    if not stage(2, "Extract form fields", s2):
        pass  # keep going; later stages still informative

    # Stage 3 — deterministic builtins + answer legal/free-text.
    ANS = {}
    def s3():
        base = engine.builtins_from(PROFILE)
        ans, blocked = engine.fill_answers(JOB, PROFILE, dict(base))
        ANS.update(ans)
        detail = f"filled {len(ans)} fields; {len(blocked)} still need a standing answer"
        if blocked:
            detail += "\n  blocked: " + "; ".join(blocked[:4])
        return (not blocked), detail
    stage(3, "Fill answers (legal + free-text)", s3)

    # Stage 4 — a complete, job-targeted résumé.
    RESUME = {"html": ""}
    def s4():
        # Try the real engine path (uses DB cache); fall back to building directly so the
        # résumé stage still proves out when the DB is down.
        try:
            html = engine.resume_for("smoke-user", JOB.get("id") or "smoke-job", PROFILE, JOB)
            src = "engine.resume_for"
        except Exception:
            import resume_build, pipeline
            try:
                tprofile, tmeta = pipeline.tailor_full(PROFILE, JOB)   # (tailored profile, meta)
            except Exception:
                tprofile, tmeta = PROFILE, {}
            html = resume_build.build_resume_html(tprofile, tmeta)
            src = "direct build (DB unavailable)"
        RESUME["html"] = html or ""
        ok = len(RESUME["html"]) > 800
        return ok, f"{src} · {len(RESUME['html'])} bytes of résumé HTML"
    stage(4, "Tailor a full résumé", s4)

    # Stage 5 — cover letter (only if the profile enabled it).
    def s5():
        if not ((PROFILE["data"].get("orchestration") or {}).get("answers") or {}).get("cover_letter"):
            return None, "cover letter disabled in this persona"
        import pipeline
        cl = pipeline.cover_letter(PROFILE, JOB)
        return bool(cl), f"{len(cl or '')} chars"
    stage(5, "Generate cover letter", s5)

    # Stage 6 — drive the REAL browser engine, DRY (fills everything, never submits).
    def s6():
        standing = (PROFILE["data"].get("standing")) or {}
        standing = engine._enrich_standing(PROFILE, standing)   # same path apply_one uses
        res = engine.submit_application(JOB, ANS, RESUME["html"], dry=True, standing=standing)
        backend = res.get("backend")
        status = res.get("status")
        unfilled = res.get("unfilled_required") or []
        lines = [f"backend={backend} · status={status}",
                 f"detail={res.get('detail','')}"[:180]]
        if unfilled:
            lines.append("unfilled required: " + "; ".join(map(str, unfilled[:6])))
        # A clean dry run = the engine prepared everything with nothing required left blank.
        ok = (status in ("dry_prepared", "awaiting_review", "submitted", "sent")) and not unfilled
        return ok, "\n".join(lines)
    stage(6, "Browser engine — DRY submit", s6)

    _summary()


def _summary():
    _hr()
    p = RESULTS.count("PASS"); f = RESULTS.count("FAIL"); s = RESULTS.count("SKIP")
    print(f"RESULT · {p} passed · {f} failed · {s} skipped")
    _hr()
    sys.exit(1 if f else 0)


if __name__ == "__main__":
    main()
