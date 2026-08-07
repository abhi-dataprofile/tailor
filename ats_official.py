"""ats_official.py — official ATS API connectors (EMPLOYER-scoped).

IMPORTANT REALITY: Greenhouse Harvest, Ashby, and Workday APIs are *employer*
APIs. They require that specific employer's credentials — there is no public
"apply to any company" API. So these connectors only work for companies that
have partnered with you and given you a key. Configure them in connectors.json:

    { "acme": {"ats": "greenhouse_harvest", "key": "<employer harvest key>"},
      "globex": {"ats": "ashby", "key": "<employer ashby key>"} }

When a job's company isn't in connectors.json, resolve() returns None and the
caller falls back to the browser submitter. This keeps the design honest.
"""
import os, json, base64, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))

def _load_connectors():
    path = os.path.join(HERE, "connectors.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

CONNECTORS = _load_connectors()

def resolve(job):
    """Return the connector config for this job's employer, or None."""
    return CONNECTORS.get((job.get("company_slug") or "").lower())

def _post_json(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.status, r.read().decode("utf-8", "replace")

# ---- Greenhouse Harvest (employer key required) ----
def greenhouse_harvest(job, answers, resume_html, cfg):
    key = cfg.get("key")
    if not key:
        return {"ok": False, "status": "not_available", "detail": "No Harvest key for this employer."}
    auth = base64.b64encode((key + ":").encode()).decode()
    headers = {"Authorization": "Basic " + auth, "On-Behalf-Of": str(cfg.get("on_behalf_of", ""))}
    candidate = {
        "first_name": answers.get("first_name", ""), "last_name": answers.get("last_name", ""),
        "email_addresses": [{"value": answers.get("email", ""), "type": "personal"}],
        "phone_numbers": [{"value": answers.get("phone", ""), "type": "mobile"}],
        "applications": [{"job_id": int(job["external_id"])}] if str(job.get("external_id", "")).isdigit() else [],
        "attachments": [{"filename": "resume.html", "type": "resume",
                         "content": base64.b64encode((resume_html or "").encode()).decode(),
                         "content_type": "text/html"}],
    }
    try:
        code, txt = _post_json("https://harvest.greenhouse.io/v1/candidates", candidate, headers)
        ok = code in (200, 201)
        return {"ok": ok, "status": "submitted" if ok else f"http_{code}", "detail": txt[:200]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": f"http_{e.code}", "detail": e.read().decode('utf-8', 'replace')[:200]}
    except Exception as e:
        return {"ok": False, "status": "error", "detail": str(e)[:160]}

# ---- Ashby / Workday: scaffolded, not implemented (submit endpoints are employer-
#      /tenant-specific and undocumented for third-party apply). Honest stub. ----
def ashby(job, answers, resume_html, cfg):
    return {"ok": False, "status": "not_available",
            "detail": "Ashby employer API connector not implemented — provide the employer's API contract to enable."}

def workday(job, answers, resume_html, cfg):
    return {"ok": False, "status": "not_available",
            "detail": "Workday is tenant-specific — needs a per-employer integration."}

_BACKENDS = {"greenhouse_harvest": greenhouse_harvest, "ashby": ashby, "workday": workday}

def submit(job, answers, resume_html, cfg):
    fn = _BACKENDS.get(cfg.get("ats"))
    if not fn:
        return {"ok": False, "status": "not_available", "detail": "Unknown ATS connector."}
    return fn(job, answers, resume_html, cfg)
