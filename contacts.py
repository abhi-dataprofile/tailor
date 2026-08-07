"""contacts.py — verified hiring-contact discovery with provider fallback.

Primary → fallback order (first one with a key that returns results wins):
    Apollo.io  (APOLLO_API_KEY)   — best coverage, people search + email
    Hunter.io  (HUNTER_API_KEY)   — domain email search (needs a domain)
    People Data Labs (PDL_API_KEY) — enrichment fallback

All stdlib. If no key is set, find() returns [] and the app falls back to
template outreach (no crash).
"""
import os, json, urllib.request, urllib.parse, urllib.error

APOLLO_KEY = os.environ.get("APOLLO_API_KEY", "")
HUNTER_KEY = os.environ.get("HUNTER_API_KEY", "")
PDL_KEY    = os.environ.get("PDL_API_KEY", "")

DEFAULT_TITLES = ["recruiter", "technical recruiter", "talent", "talent acquisition",
                  "hiring manager", "engineering manager", "head of engineering", "people"]

def available():
    out = []
    if APOLLO_KEY: out.append("apollo")
    if HUNTER_KEY: out.append("hunter")
    if PDL_KEY:    out.append("pdl")
    return out

def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

# ---------- providers ----------
def apollo_contacts(company, domain, titles, limit=5):
    body = {"api_key": APOLLO_KEY, "page": 1, "per_page": limit, "person_titles": titles}
    if domain:
        body["q_organization_domains"] = domain
    else:
        body["q_organization_name"] = company
    j = _post("https://api.apollo.io/api/v1/mixed_people/search", body, {"Cache-Control": "no-cache"})
    out = []
    for p in (j.get("people") or [])[:limit]:
        out.append({"name": p.get("name"), "title": p.get("title"),
                    "email": p.get("email"), "email_status": p.get("email_status"),
                    "linkedin": p.get("linkedin_url"), "company": company, "source": "apollo"})
    return out

def hunter_contacts(company, domain, titles, limit=5):
    if not domain:
        return []
    j = _get(f"https://api.hunter.io/v2/domain-search?domain={urllib.parse.quote(domain)}"
             f"&limit={limit}&api_key={HUNTER_KEY}")
    out = []
    for e in ((j.get("data") or {}).get("emails") or [])[:limit]:
        name = ((e.get("first_name") or "") + " " + (e.get("last_name") or "")).strip()
        ver = (e.get("verification") or {}).get("status")
        out.append({"name": name or None, "title": e.get("position"), "email": e.get("value"),
                    "email_status": "verified" if ver == "valid" else (e.get("confidence")),
                    "linkedin": e.get("linkedin"), "company": company, "source": "hunter"})
    return out

def pdl_contacts(company, domain, titles, limit=5):
    # People Data Labs person search (SQL-ish); returns people without guaranteed email
    q = {"query": {"bool": {"must": [{"term": {"job_company_name": company.lower()}}]}}, "size": limit}
    j = _post("https://api.peopledatalabs.com/v5/person/search", q, {"X-Api-Key": PDL_KEY})
    out = []
    for p in (j.get("data") or [])[:limit]:
        out.append({"name": p.get("full_name"), "title": p.get("job_title"),
                    "email": (p.get("work_email") or (p.get("emails") or [{}])[0].get("address")),
                    "email_status": "pdl", "linkedin": p.get("linkedin_url"),
                    "company": company, "source": "pdl"})
    return out

_ORDER = [("apollo", APOLLO_KEY, apollo_contacts),
          ("hunter", HUNTER_KEY, hunter_contacts),
          ("pdl", PDL_KEY, pdl_contacts)]

def find(company, domain=None, titles=None, limit=5):
    """Return up to `limit` contacts, trying each configured provider until one yields results."""
    titles = titles or DEFAULT_TITLES
    if not domain and company:
        # naive domain guess for domain-only providers (Hunter); harmless if wrong
        domain = "".join(ch for ch in company.lower() if ch.isalnum()) + ".com"
    for name, key, fn in _ORDER:
        if not key:
            continue
        try:
            r = fn(company, domain, titles, limit)
            if r:
                return r
        except Exception:
            continue
    return []
