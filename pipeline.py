#!/usr/bin/env python3
"""pipeline.py — operator-run per-user processing.

Runs on your (the operator's) machine, using the service key + any LLM provider,
and pushes per-user results to Supabase. This is the "run pipelines on my laptop,
upload to Supabase, per user" loop:

  for each user:
    1. MATCH  — score the user's profile against jobs they haven't been matched to
                yet (cheap, incremental — only NEW jobs), write user_jobs.
    2. TAILOR — for their top-N unseen matches, rewrite a targeted summary + role
                bullets via llm.py (any provider), write tailorings.
    3. DRAFT  — optionally stage human-in-the-loop application drafts.

Cost discipline: matching is cheap SQL-side overlap; the expensive LLM step runs
only for top matches and is cached per (user, job) so it never repeats.

Run:  python3 pipeline.py                 # all users, live
      python3 pipeline.py --user <id>     # one user
      python3 pipeline.py --top 8         # tailor top-8 per user
      python3 pipeline.py --dry           # synthetic user+job, no DB (proves match+tailor)
"""
import envload  # noqa: F401 — loads .env
import os, re, argparse, time, json
import llm
import supabase_client as sb

TAILOR_TOP = int(os.environ.get("TAILOR_TOP", "5"))
CANDIDATE_LIMIT = int(os.environ.get("CANDIDATE_LIMIT", "800"))   # newest open jobs considered/run

# ---------- matching (same contract as the app: absolute skill overlap) ----------
_NONTECH = re.compile(r"\b(assistant|recruiter|coordinator|counsel|attorney|accountant|payroll|bookkeeper|receptionist)\b", re.I)

def score(job, myskills):
    js = set((s or "").lower() for s in (job.get("skills") or []))
    sc = min(92, len(js & myskills) * 13)
    if _NONTECH.search((job.get("title") or "").lower()):
        sc = min(sc, 18)
    return max(5, min(100, sc))

# ---------- tailoring (LLM, any provider) ----------
_SUM_SYS = ("You are an expert resume writer. Rewrite the candidate's professional summary to target the "
            "role. 2-3 sentences, concise resume tone, no first-person pronouns, lead with fit. Use ONLY facts "
            "provided — never invent employers, titles, numbers, or tech. Output ONLY the summary text.")
_BUL_SYS = ("You are an expert resume writer. Given a target role and the candidate's skills, write 3 concrete, "
            "resume-ready achievement bullets that would make them a strong fit — past tense, strong action verb, "
            "name the specific tool/skill, one realistic round metric each. "
            'Respond with STRICT JSON only: {"bullets": ["…", "…", "…"]}.')

def _parse_json(text):
    import json as _j, re as _re
    try:
        return _j.loads(text)
    except Exception:
        m = _re.search(r"[\[{][\s\S]*[\]}]", text or "")
        if m:
            try:
                return _j.loads(m.group(0))
            except Exception:
                return None
    return None

def _which_provider():
    if llm.PROVIDER in ("claude", "gemini", "openai", "custom", "ollama"):
        return llm.PROVIDER
    return (llm.available() or ["none"])[0]

def _cfg(profile):
    return (profile.get("data") or {}).get("orchestration") or {}

def tailor(profile, job):
    import prompts
    cfg = _cfg(profile)
    sum_sys = prompts.get(cfg, "summary") or _SUM_SYS
    temp = float((cfg.get("tailor") or {}).get("temp") or 0.4)
    skills = ", ".join(profile.get("skills") or [])
    ctx = (f"CANDIDATE SUMMARY: {profile.get('summary','')}\nCANDIDATE SKILLS: {skills}\n"
           f"TARGET ROLE: {job.get('title','')}\nROLE DESCRIPTION: {(job.get('description') or '')[:2000]}")
    summary = llm.gen(sum_sys, ctx, temp=temp, max_tokens=220)
    obj = _parse_json(llm.gen(_BUL_SYS, ctx, json_mode=True, temp=0.5, max_tokens=400)) or {}
    bullets = obj.get("bullets") if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
    bullets = [b for b in (bullets or []) if isinstance(b, str)][:3]
    return {"summary": summary.strip(), "bullets": bullets, "provider": _which_provider()}

# ---------- full tailoring: rewrite the candidate's REAL history to target the role ----------
_REWRITE_SYS = (
    "You are an expert resume writer tailoring a candidate's REAL experience bullets to a target role. "
    "Rewrite each bullet as a strong, impact-oriented achievement: start with a past-tense action verb, "
    "surface the parts most relevant to the role, and KEEP every fact and every real number. Do NOT invent "
    "employers, tools, technologies, or metrics that aren't in the original. One line each. "
    'Return STRICT JSON: {"bullets":[...]} — same length and order as the input.')

def _clean_bullet(x, fallback):
    """Coerce a model-returned bullet to a clean string; on anything malformed (JSON
    artifact, object, absurd length) keep the candidate's REAL original bullet."""
    if isinstance(x, str):
        s = x
    elif isinstance(x, dict):
        s = next((v for v in x.values() if isinstance(v, str) and v.strip()), "")
    else:
        s = str(x)
    s = s.strip().strip('"').strip()
    if not s or s[:1] in "{[" or "':" in s or '":' in s or len(s) > 400:
        return fallback
    return s

def _rewrite_bullets(bullets, job, sys_prompt=None):
    bullets = [str(b).strip() for b in (bullets or []) if str(b).strip()]
    if not bullets:
        return bullets
    ctx = (f"TARGET ROLE: {job.get('title','')}\nROLE DESCRIPTION: {(job.get('description') or '')[:1400]}\n"
           f"BULLETS (JSON): {json.dumps(bullets)}")
    try:
        obj = _parse_json(llm.gen(sys_prompt or _REWRITE_SYS, ctx, json_mode=True, temp=0.3, max_tokens=600))
        out = obj.get("bullets") if isinstance(obj, dict) else (obj if isinstance(obj, list) else None)
        if isinstance(out, list) and len(out) == len(bullets):
            return [_clean_bullet(o, bullets[i]) for i, o in enumerate(out)]
    except Exception:
        pass
    return bullets   # any failure → keep the candidate's real bullets verbatim

def _rank_projects(projects, job):
    """Order the candidate's projects by keyword overlap with the role — most relevant first."""
    jd = ((job.get("title") or "") + " " + (job.get("description") or "")).lower()
    jtok = set(re.findall(r"[a-z][a-z0-9+.#]{2,}", jd))
    def rel(p):
        t = ((p.get("name") or "") + " " + (p.get("desc") or "")).lower()
        return len(set(re.findall(r"[a-z][a-z0-9+.#]{2,}", t)) & jtok)
    return sorted([p for p in (projects or []) if p.get("name") or p.get("desc")], key=rel, reverse=True)

def tailor_full(profile, job, max_bullets_per_role=6, max_projects=3):
    """Produce a fully-tailored profile: role-targeted summary, the candidate's REAL experience
    bullets rewritten to the role, and projects ranked by fit. Caps bullets/projects to lean
    toward one page WITHOUT dropping real roles. Uses the config's editable prompts + settings."""
    import prompts
    cfg = _cfg(profile)
    tcfg = cfg.get("tailor") or {}
    mbpr = int(tcfg.get("bullets_per_role") or max_bullets_per_role)
    bullets_sys = prompts.get(cfg, "bullets") or _REWRITE_SYS
    data = profile.get("data") or {}
    summary = tailor(profile, job).get("summary") or profile.get("summary") or ""
    exp = []
    for e in (data.get("exp") or []):
        rb = _rewrite_bullets(e.get("bullets") or [], job, bullets_sys)
        exp.append({**e, "bullets": rb[:mbpr]})
    proj = _rank_projects(data.get("proj") or [], job)[:max_projects]
    tprofile = {**profile, "summary": summary, "data": {**data, "exp": exp, "proj": proj}}
    return tprofile, {"summary": summary, "provider": _which_provider()}

# ---------- live (Supabase) ----------
def process_user(user_id, top):
    prof = (sb.select("profiles", {"user_id": f"eq.{user_id}", "select": "*"}) or [{}])[0]
    myskills = set((s or "").lower() for s in (prof.get("skills") or []))
    if not myskills:
        print(f"  [{user_id}] no skills on profile — skipping"); return
    seen = {r["job_id"] for r in sb.select("user_jobs", {"user_id": f"eq.{user_id}", "select": "job_id"})}
    jobs = sb.select("jobs", {"select": "id,title,skills,description", "is_open": "eq.true",
                              "order": "first_seen_at.desc", "limit": str(CANDIDATE_LIMIT)})
    fresh = [j for j in jobs if j["id"] not in seen]           # incremental: only NEW jobs
    if fresh:
        sb.upsert("user_jobs", [{"user_id": user_id, "job_id": j["id"], "score": score(j, myskills),
                                 "status": "new"} for j in fresh], on_conflict="user_id,job_id", update=False)
    tailored = {r["job_id"] for r in sb.select("tailorings", {"user_id": f"eq.{user_id}", "select": "job_id"})}
    todo = [j for j in sorted(fresh, key=lambda j: score(j, myskills), reverse=True)
            if j["id"] not in tailored][:top]
    for j in todo:
        t = tailor(prof, j)
        sb.upsert("tailorings", [{"user_id": user_id, "job_id": j["id"], "summary": t["summary"],
                                  "bullets": t["bullets"], "provider": t["provider"]}],
                  on_conflict="user_id,job_id", update=True)
    print(f"  [{user_id}] scored {len(fresh)} new · tailored {len(todo)} (provider={llm.PROVIDER})")

def run(user, top):
    users = [{"user_id": user}] if user else sb.select("profiles", {"select": "user_id"})
    if not users:
        print("no profiles found."); return
    print(f"[pipeline] {len(users)} user(s) · top-{top} tailored each")
    for u in users:
        try:
            process_user(u["user_id"], top)
        except Exception as e:
            print(f"  [{u['user_id']}] error: {str(e)[:160]}")

# ---------- dry (no Supabase): prove match + tailor via llm.py ----------
def dry():
    profile = {"summary": "Frontend engineer who builds fast, accessible web apps.",
               "skills": ["React", "TypeScript", "CSS", "HTML", "REST APIs", "Node.js"]}
    job = {"title": "Senior Frontend Engineer", "skills": ["react", "typescript", "graphql", "css"],
           "description": "Build responsive React + TypeScript interfaces, own features end to end, "
                          "collaborate with designers, improve performance and CI/CD."}
    print("provider:", llm.PROVIDER, "| available:", llm.available())
    print("match score:", score(job, {s.lower() for s in profile["skills"]}), "%")
    print("tailoring via LLM…")
    t = tailor(profile, job)
    print("\nSUMMARY:\n ", t["summary"])
    print("\nBULLETS:")
    for b in t["bullets"]:
        print("  -", b)
    print(f"\n[dry] OK (provider={t['provider']}). Set SUPABASE_* to run live for all users.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user")
    ap.add_argument("--top", type=int, default=TAILOR_TOP)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.dry or not sb.is_configured():
        if not sb.is_configured() and not a.dry:
            print("Supabase not configured — running --dry.\n")
        return dry()
    run(a.user, a.top)

if __name__ == "__main__":
    main()
