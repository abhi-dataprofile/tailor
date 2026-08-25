#!/usr/bin/env python3
"""mytest/run.py — run the auto-apply agent with YOUR profile, against a real job.

Fills a real application form with your real details and shows you exactly what it filled,
what it couldn't, and a screenshot of the finished form. It does NOT submit unless you
explicitly pass --live.

    # 1. put your details in mytest/profile.json (copy profile.example.json)
    # 2. pick any job URL from a supported board and run:

    .venv/bin/python mytest/run.py --url https://job-boards.greenhouse.io/acme/jobs/123
    .venv/bin/python mytest/run.py --url <URL> --watch      # watch Chrome fill it live
    .venv/bin/python mytest/run.py --find greenhouse        # or let it pick a real job for you

    .venv/bin/python mytest/run.py --url <URL> --watch --live   # actually submit (asks first)

Supported boards: greenhouse · lever · ashby · smartrecruiters · recruitee
"""
import os, sys, json, argparse, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

def _load_profile(path):
    if not os.path.exists(path):
        sys.exit(f"\n  No profile found at {path}\n"
                 f"  Copy the template and fill in your details:\n"
                 f"      cp {os.path.join('mytest','profile.example.json')} {os.path.join('mytest','profile.json')}\n")
    with open(path) as f:
        prof = json.load(f)
    prof = {k: v for k, v in prof.items() if not k.startswith("_")}
    data = prof.get("data") or {}
    prof["data"] = {k: v for k, v in data.items() if not k.startswith("_comment")}
    st = prof["data"].get("standing") or {}
    prof["data"]["standing"] = {k: v for k, v in st.items() if not k.startswith("_comment")}
    if not prof.get("name") or prof.get("name") == "Your Full Name":
        sys.exit("  Your profile still has the template values — fill in profile.json first.")
    return prof

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="the job's application URL")
    ap.add_argument("--find", metavar="VENDOR", help="pick a real open job from this board instead")
    ap.add_argument("--profile", default=os.path.join(HERE, "profile.json"))
    ap.add_argument("--watch", action="store_true", help="show the Chrome window while it fills")
    ap.add_argument("--live", action="store_true", help="ACTUALLY SUBMIT (prompts for confirmation)")
    ap.add_argument("--cover-letter", action="store_true", help="also generate a cover letter")
    args = ap.parse_args()

    if not args.url and not args.find:
        ap.error("give me a job: --url <application URL>  (or --find greenhouse)")

    # DRY unless the human explicitly asked for a live submit, right here, at the keyboard.
    os.environ["DRY_RUN"] = "0" if args.live else "1"
    os.environ["APPLY_BROWSER"] = "1"
    if args.watch or args.live:
        os.environ["APPLY_HEADED"] = "1"          # a live submit is always watched

    import envload  # noqa: F401
    import ats, serve, apply as engine

    profile = _load_profile(args.profile)
    if args.cover_letter:
        profile["data"].setdefault("orchestration", {}).setdefault("answers", {})["cover_letter"] = True

    print("─" * 66)
    print(f"  Applicant : {profile.get('name')}  <{profile.get('email')}>")
    print(f"  Mode      : {'LIVE — WILL SUBMIT' if args.live else 'dry run — fills only, never submits'}")
    print("─" * 66)

    # ---- pick the job -------------------------------------------------------
    if args.find:
        job = None
        for slug in (ats.PRIORITY.get(args.find) or [])[:10]:
            feed = list(ats.fetch_feed(args.find, slug))
            if feed:
                job = next((j for j in feed if "engineer" in (j.get("title") or "").lower()), feed[0])
                break
        if not job:
            sys.exit(f"  Couldn't find an open job on {args.find}.")
    else:
        job = {"id": "manual", "title": "(from your URL)", "url": args.url,
               "vendor": "", "company_slug": "", "description": ""}
        job["vendor"] = next((v for v in ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee")
                              if v in args.url), "")
    canon = serve.canonical_apply_url(job) or job["url"]
    job["url"] = canon
    print(f"  Job       : {job.get('title')}\n  URL       : {job['url']}\n")

    # ---- build YOUR résumé for this job -------------------------------------
    print("  → tailoring your résumé to this job…")
    t0 = time.time()
    import pipeline, resume_build
    try:
        tprofile, tmeta = pipeline.tailor_full(profile, job)
        resume = resume_build.build_resume_html(tprofile, tmeta)
    except Exception as e:
        print(f"    (tailoring unavailable: {str(e)[:80]} — using your résumé as written)")
        resume = resume_build.build_resume_html(profile, {})
    out_resume = os.path.join(HERE, "out_resume.html")
    with open(out_resume, "w") as f:
        f.write(resume)
    print(f"    résumé ready ({len(resume)} bytes, {time.time()-t0:.0f}s) → {out_resume}")

    cover = ""
    if args.cover_letter:
        try:
            cover = pipeline.cover_letter(profile, job)
            print(f"    cover letter ready ({len(cover)} chars)")
        except Exception as e:
            print(f"    (cover letter failed: {str(e)[:60]})")

    # ---- fill the form ------------------------------------------------------
    standing = engine._enrich_standing(profile, (profile.get("data") or {}).get("standing") or {})
    answers = engine.builtins_from(profile)

    if args.live:
        print("\n" + "!" * 66)
        print("  LIVE SUBMIT — this sends a REAL application to a REAL employer,")
        print(f"  as {profile.get('name')} <{profile.get('email')}>. It cannot be undone.")
        print("!" * 66)
        if input("  Type SUBMIT to go ahead: ").strip() != "SUBMIT":
            print("  Cancelled — nothing was sent."); return

    print("\n  → opening the form and filling it…")
    res = engine.submit_application(job, answers, resume, dry=not args.live,
                                    standing=standing, cover_letter=cover)

    # ---- report -------------------------------------------------------------
    print("\n" + "─" * 66)
    print(f"  Status    : {res.get('status')}   (backend: {res.get('backend')})")
    print(f"  Detail    : {(res.get('detail') or '')[:200]}")
    filled = res.get("filled") or {}
    if filled:
        print(f"  Filled    : " + ", ".join(k for k, v in filled.items() if v))
    print(f"  Résumé attached: {'yes' if res.get('resume_attached') else 'no'}")
    warnings = res.get("warnings") or []
    if warnings:
        if res.get("form_ready"):
            print("\n  ⚠️  The form opened, but it isn't complete:")
        else:
            print("\n  ⚠️  The agent never reached a real application form here:")
        for w in warnings:
            print(f"    · {w}")
        print("\n  Often the job page links out to the employer's own form, or the form is\n"
              "  behind a login. Open the URL yourself and use the direct apply link.")
        if res.get("screenshot"):
            print(f"\n  What the agent saw:\n    {res['screenshot']}")
        print("─" * 66)
        return
    missing = res.get("unfilled_required") or []
    if missing:
        print(f"\n  {len(missing)} required question(s) it could not answer for you:")
        for m in missing:
            print(f"    · {str(m.get('label'))[:110]}")
            if m.get("options"):
                print(f"        options: {', '.join(map(str, m['options'][:8]))}")
        print("\n  Add answers for these under data.standing._custom in profile.json,")
        print("  then re-run — it will use them (and reuse them on other boards).")
    else:
        print("\n  ✅ Every required question was answered — nothing left for you to fill.")
    if res.get("screenshot"):
        print(f"\n  Screenshot of the filled form:\n    {res['screenshot']}")
    print("─" * 66)

if __name__ == "__main__":
    main()
