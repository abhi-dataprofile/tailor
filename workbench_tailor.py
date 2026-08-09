"""workbench_tailor.py — drive the REAL workbench tailoring headlessly.

So the agent and the interactive workbench use ONE tailoring engine. We inject the
candidate's profile into the workbench's localStorage, open index.html in a headless
browser, run the same autoTailor() the user runs, and extract the built one-page résumé.

Best-effort: returns résumé HTML or None. apply.resume_for falls back to the robust
server pipeline (pipeline.tailor_full) whenever this returns None — reliability never
regresses.
"""
import os, json


def _state_from_profile(profile):
    data = profile.get("data") or {}
    skills = profile.get("skills") or []
    return {
        "name": profile.get("name", ""), "title": profile.get("title", ""),
        "email": profile.get("email", ""), "contact": profile.get("contact", ""),
        "summary": profile.get("summary", ""),
        "skills": ", ".join(skills) if isinstance(skills, list) else str(skills or ""),
        "exp": data.get("exp") or [], "proj": data.get("proj") or [],
        "education": data.get("education") or [], "certs": data.get("certs") or [],
        "customSections": data.get("custom_sections") or [], "links": data.get("links") or [],
        "phone": data.get("phone", ""), "address": data.get("address") or {},
        "memory": profile.get("memory") or {}, "standing": data.get("standing") or {},
        # point the workbench's model at local Ollama so autoTailor can use AI
        "cfg": {"url": os.environ.get("OLLAMA_URL", "http://localhost:11434"), "provider": "ollama"},
    }

_DRIVE_JS = """async (jd) => {
  try {
    const ta = document.getElementById('jdText');
    if (!ta) return null;
    ta.value = jd;
    if (window.autoTailor) { await window.autoTailor(); }
    else if (window.analyzeJD) { await window.analyzeJD(); window.buildResume && window.buildResume(); }
    const rd = document.getElementById('resumeDoc');
    return rd ? rd.outerHTML : null;
  } catch (e) { return null; }
}"""


def tailor_via_workbench(profile, job, base_url=None, timeout_ms=120000):
    data = profile.get("data") or {}
    if not (data.get("exp")):
        return None                      # nothing to tailor from → let the server path handle it
    jd = (job.get("description") or "").strip()
    if not jd:
        return None
    base_url = base_url or ("http://127.0.0.1:" + os.environ.get("PORT", "8765"))
    payload = json.dumps({"state": _state_from_profile(profile)})
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            pg = ctx.new_page()
            pg.set_default_timeout(timeout_ms)
            # seed the profile BEFORE any script runs, so the workbench boots with it
            pg.add_init_script("try{localStorage.setItem('tailor_ollama', %s)}catch(e){}" % json.dumps(payload))
            pg.goto(base_url + "/index.html", wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(2200)     # let boot + Ollama detection settle
            try:
                html = pg.evaluate(_DRIVE_JS, jd)
            except Exception:
                html = None
            try: ctx.close(); browser.close()
            except Exception: pass
            if html and len(html) > 400:
                return html
    except Exception:
        return None
    return None
