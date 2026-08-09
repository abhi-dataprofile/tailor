#!/usr/bin/env python3
"""Resume Tailor local server + internal apply engine.
Serves the app AND exposes localhost-only endpoints that fetch ATS forms and
submit applications server-side (no browser sandbox). Greenhouse supported;
CAPTCHA-protected boards are detected and reported for manual apply.
Every submission writes a receipt to applications.log. DRY_RUN=1 to test."""
import envload  # noqa: F401 — loads .env into os.environ before anything reads it
import json, os, re, sys, time, uuid, hashlib, urllib.request, urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
import subprocess
from app_status import classify   # single source of truth for application lifecycle status

PORT = int(os.environ.get("PORT", "8765"))   # hosts (Render/Railway/Fly) inject $PORT
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) ResumeTailor/1.0"}
HERE = os.path.dirname(os.path.abspath(__file__))

def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace"), dict(r.headers)

# ============================================================
#  JOB DISCOVERY — scan public ATS feeds across a bundled
#  dataset of ~15.9k company career pages (Greenhouse / Lever /
#  Ashby), filter postings by role keywords, rank client-side.
#  Cursor/batch based so the browser streams results in.
# ============================================================
# Well-known companies surfaced first so early batches carry strong matches.
PRIORITY = {
  "greenhouse": ["stripe","openai","anthropic","notion","ramp","vercel","datadog","figma","brex",
    "databricks","airbnb","coinbase","robinhood","instacart","doordash","reddit","discord","dropbox",
    "cloudflare","gitlab","hashicorp","asana","benchling","gusto","retool","samsara","affirm","chime",
    "sofi","twilio","okta","elastic","mongodb","zapier","webflow","calendly","grammarly","duolingo"],
  "lever": ["openai","anthropic","figma","databricks","plaid","reddit","gitlab","benchling","retool",
    "affirm","confluent","canva","duolingo","cohere","sourcegraph"],
  "ashby": ["openai","notion","ramp","linear","plaid","reddit","snowflake","benchling","confluent",
    "zapier","deel","pinecone","weaviate","cohere","runway","perplexity","harvey","cursor","replit"],
}

def _load_slugs(vendor):
    p = os.path.join(HERE, "data", vendor + "_companies.json")
    try:
        with open(p) as f: return list(dict.fromkeys(json.load(f)))
    except Exception: return []

def build_companies():
    """Combined, priority-ordered list of (vendor, slug), de-duplicated per vendor."""
    out, seen = [], set()
    per = {v: _load_slugs(v) for v in ("greenhouse", "lever", "ashby")}
    for v in ("greenhouse", "lever", "ashby"):
        present = set(per[v])
        for slug in PRIORITY.get(v, []):
            if slug in present and (v, slug) not in seen:
                seen.add((v, slug)); out.append((v, slug))
    for v in ("greenhouse", "lever", "ashby"):
        for slug in per[v]:
            if (v, slug) not in seen:
                seen.add((v, slug)); out.append((v, slug))
    return out

COMPANIES = build_companies()
_FEED_CACHE = {}          # (vendor,slug) -> (ts, [postings])
_DESC_CACHE = {}          # url -> desc  (greenhouse descriptions, fetched lazily for matches only)
FEED_TTL = 6 * 3600

def _strip_html(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()

def _iso_date(s):
    """ISO timestamp string -> 'YYYY-MM-DD' (or '')."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s or "")
    return m.group(1) if m else ""

def _ms_date(ms):
    """epoch milliseconds -> 'YYYY-MM-DD' (or '')."""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ms) / 1000))
    except Exception:
        return ""

# --- visa-scoped sponsorship signal ---------------------------------------
# "no"  = the posting explicitly says it does NOT sponsor work visas
# "yes" = the posting explicitly says it WILL sponsor
# ""    = silent (unknown) — most postings. Detection is deliberately strict:
#         it only fires on sentences that are about work visas/immigration, and
#         ignores unrelated "sponsorship" (e.g. export-license) to avoid false flags.
_VISA = re.compile(r"(visa|h-?1b|h1-b|immigrat|work authoriz|employment authoriz|green card|work permit|right to work|sponsorship for employment)", re.I)
_NEGCUE = re.compile(r"\b(not|no|non|unable|cannot|can'?t|won'?t|will not|do not|does not|doesn'?t|without|neither|unfortunately)\b", re.I)
_POSCUE = re.compile(r"(will sponsor|can sponsor|do sponsor|happy to sponsor|able to sponsor|offer(?:s|ing)?[^.]{0,25}sponsorship|provide(?:s)?[^.]{0,25}sponsorship|sponsorship (?:is )?available|we sponsor|open to sponsor)", re.I)

def sponsorship(text):
    if not text or "sponsor" not in text.lower():
        return ""
    verdict = ""
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        low = s.lower()
        if "sponsor" not in low or "export" in low or not _VISA.search(s):
            continue
        if _POSCUE.search(s):
            return "yes"
        if _NEGCUE.search(s):
            verdict = "no"
    return verdict

def fetch_gh_desc(slug, jid, url):
    """Fetch one Greenhouse posting on demand (cheap, ~5KB). Returns (desc, sponsor).
    Sponsorship is scored on the FULL text (the clause is usually a footer) before the
    description is truncated for display/scoring. Cached by url."""
    if url in _DESC_CACHE:
        return _DESC_CACHE[url]
    desc, spon = "", ""
    try:
        body, _ = http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}")
        full = _strip_html(json.loads(body).get("content", ""))
        spon = sponsorship(full)
        desc = full[:4000]
    except Exception:
        pass
    _DESC_CACHE[url] = (desc, spon)
    return desc, spon

def fetch_feed(vendor, slug):
    """Return [{title,url,desc,location}] for one company; cached with TTL. Never raises.
    Greenhouse is fetched WITHOUT bodies (12x smaller/5x faster); descriptions are pulled
    later only for postings that match the role — see discover()."""
    key = (vendor, slug); now = time.time()
    hit = _FEED_CACHE.get(key)
    if hit and now - hit[0] < FEED_TTL:
        return hit[1]
    out = []
    try:
        if vendor == "greenhouse":
            body, _ = http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            for x in (json.loads(body).get("jobs") or []):
                locn = (x.get("location") or {}).get("name", "")
                out.append({"title": x.get("title", ""), "url": x.get("absolute_url", ""), "desc": "",
                            "gh_id": x.get("id"), "sponsor": "",   # sponsor filled with desc later (see discover)
                            "date": _iso_date(x.get("first_published") or x.get("updated_at", "")),
                            "remote": "remote" in locn.lower(),
                            "location": locn, "company": slug, "vendor": vendor})
        elif vendor == "lever":
            body, _ = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            for x in (json.loads(body) or []):
                full = x.get("descriptionPlain") or _strip_html(x.get("description", ""))
                desc = full[:4000]
                locn = (x.get("categories") or {}).get("location", "")
                out.append({"title": x.get("text", ""), "url": x.get("hostedUrl", ""), "desc": desc,
                            "sponsor": sponsorship(full), "date": _ms_date(x.get("createdAt")),
                            "remote": (x.get("workplaceType", "") or "").lower() == "remote" or "remote" in locn.lower(),
                            "location": locn, "company": slug, "vendor": vendor})
        elif vendor == "ashby":
            body, _ = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            for x in (json.loads(body).get("jobs") or []):
                full = x.get("descriptionPlain") or _strip_html(x.get("descriptionHtml", ""))
                desc = full[:4000]
                out.append({"title": x.get("title", ""), "url": x.get("jobUrl") or x.get("applyUrl", ""), "desc": desc,
                            "sponsor": sponsorship(full), "date": _iso_date(x.get("publishedAt", "")),
                            "remote": bool(x.get("isRemote")) or (x.get("workplaceType", "") or "").lower() == "remote",
                            "location": x.get("location", ""), "company": slug, "vendor": vendor})
    except Exception:
        out = []
    _FEED_CACHE[key] = (now, out)
    return out

def discover(terms, vendors, offset, batch, loc):
    """Scan COMPANIES[offset:offset+batch], keep postings whose title matches a role term."""
    terms = [t for t in (terms or []) if len(t) >= 3]
    loc = (loc or "").strip().lower()
    pool = [c for c in COMPANIES if not vendors or c[0] in vendors]
    total = len(pool)
    window = pool[offset:offset + batch]
    PER_COMPANY = 8      # keep results diverse — one huge board can't flood the list
    MAX_MATCHES = 200
    DESC_BUDGET = MAX_MATCHES   # enrich every returned match (needed for sponsorship + scoring)
    matches, scanned = [], 0
    with ThreadPoolExecutor(max_workers=24) as ex:
        for feed in ex.map(lambda c: fetch_feed(c[0], c[1]), window):
            scanned += 1
            kept = 0
            for p in feed:
                if kept >= PER_COMPANY:
                    break
                t = (p.get("title") or "").lower()
                if terms and not any(term in t for term in terms):
                    continue
                if loc:
                    if loc not in (t + " " + (p.get("location") or "")).lower():
                        continue
                if p.get("url"):
                    matches.append(p); kept += 1
        matches = matches[:MAX_MATCHES]
        # pull descriptions only for matched Greenhouse postings, up to a budget (cheap per-job fetch)
        need = [p for p in matches if p.get("vendor") == "greenhouse" and not p.get("desc") and p.get("gh_id")][:DESC_BUDGET]
        for p, (desc, spon) in zip(need, ex.map(lambda p: fetch_gh_desc(p["company"], p["gh_id"], p["url"]), need)):
            p["desc"] = desc
            p["sponsor"] = spon
    for p in matches:
        p.pop("gh_id", None)
    nxt = offset + batch
    return {"ok": True, "matches": matches, "scanned": scanned,
            "offset": offset, "next": (nxt if nxt < total else None), "total": total}

def gh_ids(url):
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?token=)?([\w.-]+)/jobs/(\d+)", url) or \
        re.search(r"boards\.greenhouse\.io/([\w.-]+)/jobs/(\d+)", url)
    return (m.group(1), m.group(2)) if m else (None, None)

def gh_questions(board, jid):
    body, _ = http_get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}?questions=true")
    return json.loads(body)

def multipart(fields, files):
    b = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields:
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, fname, data, ctype in files:
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
        out += data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"

# Questions that must NEVER be auto-answered by the model — legal / demographic /
# comp. If any required question matches, the flow refuses and asks the human.
SENSITIVE = re.compile(
    r"(sponsor|visa|work authoriz|authoriz[^.]{0,20}\bwork\b|require[^.]{0,20}sponsor|"
    r"salary|compensation expectation|desired (?:pay|salary|compensation)|"
    r"gender|race|ethnic|hispanic|latino|veteran|disabilit|"
    r"felony|criminal|background check|security clearance|date of birth|"
    r"\bover 18\b|at least 18)", re.I)
FILE_TYPES = {"attachment", "input_file", "file"}

APP_LOG = os.path.join(HERE, "applications.log")

def already_applied(url, apply_id):
    """Idempotency: has this exact job/attempt already succeeded? Prevents double-submit."""
    if not os.path.exists(APP_LOG):
        return None
    try:
        with open(APP_LOG) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                # A prior attempt counts as "already applied" if it was accepted
                # (ok) OR merely SENT (clicked submit but unconfirmed) — we must
                # never re-submit a form that already left our hands.
                already = rec.get("ok") or rec.get("sent") or rec.get("status") in ("submitted", "unconfirmed")
                if already and rec.get("status") not in ("dry_run", "dry_prepared") and \
                   (rec.get("apply_id") == apply_id or rec.get("url") == url):
                    return rec
    except Exception:
        return None
    return None

def canonical_apply_url(job):
    """The clean, automatable apply form. Greenhouse jobs are often stored as a company
    careers-page embed (monks.com/careers/…, mongodb.com/careers/…) that adds cookie walls,
    bot-detection, and a non-standard DOM — the source of most 'captcha' / 'no submit button'
    failures. Rewrite those to the direct Greenhouse-hosted form."""
    j = job or {}
    if j.get("vendor") == "greenhouse" and j.get("company_slug") and j.get("external_id"):
        return f"https://job-boards.greenhouse.io/{j['company_slug']}/jobs/{j['external_id']}"
    return j.get("url")

def gh_apply(url, answers, resume_html):
    """Submit to Greenhouse. Returns a rich result incl. exactly which fields were
    sent and which sensitive questions were flagged — for a full audit trail."""
    board, jid = gh_ids(url)
    if not board:
        return {"ok": False, "status": "unsupported", "detail": "Only Greenhouse is supported by the engine — apply via the link."}
    page, _ = http_get(f"https://boards.greenhouse.io/{board}/jobs/{jid}")
    if "recaptcha" in page.lower() or "captcha" in page.lower():
        return {"ok": False, "status": "captcha", "detail": "This board uses a CAPTCHA — must be applied to manually."}
    tok = re.search(r'name="authenticity_token" value="([^"]+)"', page)
    if not tok:
        # Modern Greenhouse boards drop the classic CSRF token; a blind POST is
        # rejected. Fail loudly instead of silently double-submitting garbage.
        return {"ok": False, "status": "unsupported_form",
                "detail": "This board doesn't expose the classic apply form — apply via the link."}
    q = gh_questions(board, jid)
    fields = [("authenticity_token", tok.group(1))]
    unanswered, sensitive_blocked, submitted = [], [], []
    for question in q.get("questions", []):
        label = question.get("label") or ""
        required = question.get("required")
        is_sensitive = bool(SENSITIVE.search(label))
        for f in question.get("fields", []):
            name, ftype = f.get("name"), f.get("type")
            if ftype in FILE_TYPES:          # résumé / cover letter go in the file part, not here
                continue
            val = answers.get(name)
            if val is None or str(val).strip() == "":
                if required:
                    (sensitive_blocked if is_sensitive else unanswered).append(
                        {"name": name, "label": label, "type": ftype, "sensitive": is_sensitive,
                         "options": [v.get("label") for v in (f.get("values") or [])][:20]})
                continue                     # never send empty optional fields
            fields.append((name, str(val)))
            submitted.append({"name": name, "label": label, "sensitive": is_sensitive})
    if sensitive_blocked:
        return {"ok": False, "status": "needs_review",
                "detail": "Sensitive questions (work authorization, salary, demographics) must be answered by you.",
                "missing": sensitive_blocked}
    if unanswered:
        return {"ok": False, "status": "needs_answers", "detail": "Required questions missing.", "missing": unanswered}
    files = [("resume", "resume.html", resume_html.encode(), "text/html")]
    body, ctype = multipart(fields, files)
    audit = {"submitted": submitted, "resume_format": "html",
             "sensitive_sent": [s["name"] for s in submitted if s["sensitive"]]}
    if DRY_RUN:
        return {"ok": True, "status": "dry_run", **audit,
                "detail": f"DRY RUN — would POST {len(fields)} fields + résumé to {board}/{jid}"}
    req = urllib.request.Request(f"https://boards.greenhouse.io/{board}/jobs/{jid}", data=body,
                                 headers={**UA, "Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            ok = r.status in (200, 302)
            # A 2xx/3xx means the POST was accepted — we SENT it — but Greenhouse
            # returns no machine-readable confirmation here, so never claim confirmed.
            return {"ok": ok, "status": "submitted" if ok else f"http_{r.status}",
                    "sent": ok, "confirmed": False,
                    "detail": f"Sent (HTTP {r.status}); no confirmation returned — verify manually." if ok else f"HTTP {r.status}",
                    "http_code": r.status, **audit}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        return {"ok": False, "status": f"http_{e.code}", "http_code": e.code, **audit,
                "detail": f"Board rejected (HTTP {e.code}){': ' + detail if detail else ''}"}
    except Exception as e:
        return {"ok": False, "status": "network_error", "detail": str(e)[:200], **audit}

def log_receipt(rec):
    with open(APP_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")

def _mirror_application(user, job_id, res, answers, rec, resume_html=""):
    """Write the honest lifecycle row into the per-user applications table so the
    Activity view reflects exactly what the agent did. No-op without a DB + job_id.
    Never overwrites a real send with a lesser status (e.g. a later retry that errors)."""
    if not (sb and sb.is_configured() and job_id):
        return
    try:
        status = classify(res)
        sent = status in ("confirmed", "submitted_unconfirmed")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        prior = sb.select("applications", {"user_id": f"eq.{user}", "job_id": f"eq.{job_id}",
                                           "select": "attempts,status,submitted_at,receipt"}) or []
        p = prior[0] if prior else {}
        # idempotency: once sent/confirmed, don't downgrade on a subsequent attempt
        if p.get("status") in ("confirmed",) and status != "confirmed":
            return
        events = ((p.get("receipt") or {}).get("events") or [])
        rec = dict(rec or {}); rec["events"] = (events + [{"at": now_iso, "status": status,
                     "detail": rec.get("detail"), "backend": rec.get("backend")}])[-20:]
        row = {"user_id": user, "job_id": job_id, "status": status,
               "answers": answers, "receipt": rec,
               "attempts": ((p.get("attempts") or 0) + 1),
               "submitted_at": (p.get("submitted_at") or (now_iso if sent else None)),
               "confirmed_at": now_iso if status == "confirmed" else None}
        if sent and resume_html:
            row["resume_html"] = resume_html
        sb.upsert("applications", [row], on_conflict="user_id,job_id", update=True)
    except Exception:
        pass

# phrases that mark an email as an application confirmation (not a rejection/newsletter)
_CONFIRM_PHRASES = ("thank you for applying", "thanks for applying", "application received",
    "received your application", "we received your application", "your application has been received",
    "thank you for your application", "thanks for your interest", "successfully submitted",
    "we've received your application", "application was submitted", "your application for",
    "application to", "thank you for submitting", "we have received your application")

def _mark_confirmed(user, job_id, source):
    """Promote a submitted_unconfirmed application to confirmed (never touches other states)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = sb.select("applications", {"user_id": f"eq.{user}", "job_id": f"eq.{job_id}",
                                      "select": "status,receipt"}) or []
    if not rows:
        return False
    rec = rows[0].get("receipt") or {}
    rec["confirmed_via"] = source
    sb.upsert("applications", [{"user_id": user, "job_id": job_id, "status": "confirmed",
              "confirmed_at": now, "receipt": rec}], on_conflict="user_id,job_id", update=True)
    return True

def confirm_from_email(user_id, msg):
    """If an inbound email is an application confirmation, match it to one of the user's
    submitted_unconfirmed applications and promote it to confirmed. Conservative — needs a
    real signal (company/title match), so a generic 'thanks' can't confirm the wrong job."""
    if not (sb and sb.is_configured() and user_id):
        return None
    text = ((msg.get("subject") or "") + " " + (msg.get("body") or "")).lower()
    if not any(p in text for p in _CONFIRM_PHRASES):
        return None
    frm = (msg.get("from_addr") or "").lower()
    apps = sb.select("applications", {"user_id": f"eq.{user_id}", "status": "eq.submitted_unconfirmed",
            "select": "job_id,receipt,submitted_at", "order": "submitted_at.desc", "limit": "100"}) or []
    if not apps:
        return None
    ids = [str(a["job_id"]) for a in apps if a.get("job_id")]
    jobs = {}
    if ids:
        for j in sb.select("jobs", {"id": f"in.({','.join(ids)})",
                                    "select": "id,company_slug,title,url"}) or []:
            jobs[j["id"]] = j
    best, best_score = None, 0
    for a in apps:
        j = jobs.get(a["job_id"], {}); rec = a.get("receipt") or {}
        company = (j.get("company_slug") or "").lower()
        title = (j.get("title") or rec.get("job") or "").lower()
        url = (j.get("url") or rec.get("url") or "").lower()
        score = 0
        if company and (company in text or company in frm):
            score += 3
        for dom in ("greenhouse", "lever", "ashby", "workday"):
            if dom in frm and dom in url:
                score += 1
        tw = re.findall(r"[a-z]{4,}", title)[:6]
        if tw and sum(1 for w in tw if w in text) >= 2:
            score += 2
        if score > best_score:
            best, best_score = a, score
    # require a genuine signal (company match, or title-words + ATS-domain) to avoid false confirms
    if best and best_score >= 3:
        try:
            if _mark_confirmed(user_id, best["job_id"], "email: " + (msg.get("subject") or "")[:120]):
                return best["job_id"]
        except Exception:
            return None
    return None

def reconcile_confirmations(user=None, hours=168):
    """Safety-net sweep: re-run the email matcher over recent inbound mail, catching
    confirmations that arrived before their application row existed (or a missed webhook).
    Returns the number of applications promoted to confirmed."""
    if not (sb and sb.is_configured()):
        return 0
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    q = {"select": "user_id,from_addr,subject,body", "received_at": f"gte.{since}",
         "order": "received_at.desc", "limit": "1000"}
    if user:
        q["user_id"] = f"eq.{user}"
    n = 0
    for e in sb.select("emails", q) or []:
        try:
            if confirm_from_email(e.get("user_id"), {"from_addr": e.get("from_addr"),
                                                     "subject": e.get("subject"), "body": e.get("body")}):
                n += 1
        except Exception:
            continue
    return n

# ============================================================
#  SUPABASE-BACKED READ/WRITE API (jobs index + user profile)
#  Read path: query the crawler-populated `jobs` table, rank by
#  profile↔job skill overlap (matching done internally), attach
#  the user's per-job state. Degrades gracefully if unconfigured.
# ============================================================
try:
    import ats
    import supabase_client as sb
except Exception:
    ats = None; sb = None
try:
    import contacts, billing, inbox
except Exception:
    contacts = billing = inbox = None
FREE_APPLY_LIMIT = int(os.environ.get("FREE_APPLY_LIMIT", "20"))

def _user_plan(user):
    try:
        if sb and sb.is_configured():
            rows = sb.select("profiles", {"user_id": f"eq.{user}", "select": "plan"})
            return (rows[0].get("plan") if rows and rows[0].get("plan") else "free")
    except Exception:
        pass
    return "local" if user == "local" else "free"

def _apply_gate(user):
    plan = _user_plan(user)
    if plan != "free":
        return True, plan
    today = time.strftime("%Y-%m-%d"); n = 0
    if os.path.exists(APP_LOG):
        try:
            for line in open(APP_LOG):
                try: r = json.loads(line)
                except Exception: continue
                if r.get("user") == user and r.get("backend") == "browser" and str(r.get("at", "")).startswith(today):
                    n += 1
        except Exception:
            pass
    return (n < FREE_APPLY_LIMIT), plan

def _req_user(handler):
    """Signed-in user_id from the Bearer token (multi-tenant), else 'local' (single-operator)."""
    if not (sb and sb.auth_enabled()):
        return "local"
    auth = handler.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    u = sb.auth_user(token) if token else None
    return u["id"] if u and u.get("id") else "local"

def _profile(user="local"):
    rows = sb.select("profiles", {"user_id": f"eq.{user}", "select": "skills,title,memory,name,email"})
    return rows[0] if rows else {"skills": [], "title": "", "memory": {}}

def _score(job, myskills):
    js = set((s or "").lower() for s in (job.get("skills") or []))
    sc = min(92, len(js & myskills) * 13)                       # absolute skill overlap
    t = (job.get("title") or "").lower()
    if re.search(r"\b(assistant|recruiter|coordinator|counsel|attorney|accountant|payroll|bookkeeper|receptionist)\b", t):
        sc = min(sc, 18)                                        # de-rank clearly non-technical roles
    return max(5, min(100, sc))

US_ALIASES = {"us", "u.s.", "u.s.a.", "usa", "united states", "united states of america",
              "america", "united-states", "usa."}

def _loc_param(loc):
    """Return (postgrest_op, pattern) for a location filter.
    Fixes 'US' matching 'Austin': matches whole words, and treats US aliases as
    equivalent (country name OR a ', XX' uppercase US state code)."""
    low = loc.strip().lower()
    if low in US_ALIASES:
        # match 'US' / 'USA' / 'United States' as whole words (word-bounded → no 'Austin' false match);
        # all US aliases collapse to the same pattern so 'US' and 'United States' return identical results
        return "imatch", r"\y(usa?|united states)\y"
    return "imatch", r"\y" + re.escape(loc.strip()) + r"\y"

# slim column set for the list/scoring pool — deliberately excludes description & meta
# (the big fields), so a page loads fast; descriptions are fetched only for the shown page.
_SLIM = ("id,source_uid,vendor,company_slug,title,location,country,remote,url,"
         "sponsorship,posted_at,first_seen_at,department,employment_type,compensation,skills")
_POOL = 300      # rank within the most-recent N matching postings ("latest first") — fast
_PAGE = 60

def _recency_bonus(posted_at):
    if not posted_at:
        return 0
    try:
        days = (time.time() - time.mktime(time.strptime(posted_at[:10], "%Y-%m-%d"))) / 86400
    except Exception:
        return 0
    if days <= 2:   return 25
    if days <= 7:   return 16
    if days <= 30:  return 8
    if days <= 90:  return 3
    return 0

_POOL_CACHE = {}   # filter-key -> (expiry_ts, rows). Pool is user-independent, so it's shared.
_POOL_TTL = 45     # seconds

def _cached_pool(params):
    key = json.dumps(params, sort_keys=True)
    now = time.time()
    hit = _POOL_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    rows = sb.select("jobs", params)
    _POOL_CACHE[key] = (now + _POOL_TTL, rows)
    if len(_POOL_CACHE) > 300:                                   # bound memory: drop expired
        for k in [k for k, v in list(_POOL_CACHE.items()) if v[0] <= now]:
            _POOL_CACHE.pop(k, None)
    return rows

_USER_CACHE = {}   # user -> (expiry, profile, {job_id:status})

def _user_ctx(user):
    now = time.time(); hit = _USER_CACHE.get(user)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    prof = _profile(user)
    states = {r["job_id"]: r["status"] for r in
              sb.select("user_jobs", {"user_id": f"eq.{user}", "select": "job_id,status"})}
    _USER_CACHE[user] = (now + 30, prof, states)
    return prof, states

def _inlist(vals):
    """PostgREST in.(...) with each value double-quoted (handles spaces/commas)."""
    return "in.(" + ",".join('"' + v.replace('"', "") + '"' for v in vals) + ")"

def _seniority(title):
    t = (title or "").lower()
    if re.search(r"\b(intern|internship)\b", t): return "intern"
    if re.search(r"\b(senior|sr|staff|principal|lead|director|head|vp|architect|manager|distinguished)\b", t): return "experienced"
    if re.search(r"\b(junior|jr|entry|associate|new grad|graduate|apprentice|early career)\b", t): return "entry"
    return "mid"

# --- applyability: can the agent take this job end-to-end? ---
_AUTO_VENDORS = ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee")
_APPLY_SIG = {}   # user -> (expiry, blocked_companies, manual_companies) — learned from past attempts

def _apply_signals(user):
    """Companies where this user's past attempts hit a captcha (→ assisted) or a dead end
    like no-submit/unsupported (→ manual). Lets us stop calling a board 'auto' once we've
    learned it won't complete. Cached briefly."""
    now = time.time(); hit = _APPLY_SIG.get(user)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    blocked_co, manual_co = set(), set()
    try:
        apps = sb.select("applications", {"user_id": f"eq.{user}", "select": "job_id,status",
                "status": "in.(blocked_captcha,failed_permanent)"}) or []
        bids = [str(a["job_id"]) for a in apps if a.get("status") == "blocked_captcha" and a.get("job_id")]
        mids = [str(a["job_id"]) for a in apps if a.get("status") == "failed_permanent" and a.get("job_id")]
        idmap = {}
        allids = list({*bids, *mids})
        if allids:
            for j in sb.select("jobs", {"id": f"in.({','.join(allids)})", "select": "id,company_slug"}) or []:
                idmap[str(j["id"])] = j.get("company_slug")
        blocked_co = {idmap.get(i) for i in bids if idmap.get(i)}
        manual_co = {idmap.get(i) for i in mids if idmap.get(i)}
    except Exception:
        pass
    _APPLY_SIG[user] = (now + 60, blocked_co, manual_co)
    return blocked_co, manual_co

def _applyability(vendor, company_slug, blocked_co, manual_co):
    """auto = agent completes it end-to-end · assisted = agent fills, you solve a captcha ·
    manual = unsupported / account-required (Workday, iCIMS-account) — we won't pretend."""
    if company_slug in manual_co:
        return "manual"
    if company_slug in blocked_co:
        return "assisted"
    return "auto" if (vendor or "").lower() in _AUTO_VENDORS else "manual"

def jobs_query(qs, user="local"):
    """Fast, ranked, filterable, paginated query over the shared jobs index."""
    def one(k, d=""): return (qs.get(k) or [d])[0].strip()
    def multi(k): v = one(k); return [x.strip() for x in v.split("|") if x.strip()] if v else []
    terms = [t.strip() for t in one("q").lower().split(",") if len(t.strip()) >= 2]
    loc = one("loc"); vendor = one("vendor").lower()
    countries = multi("country"); companies = multi("company"); etypes = multi("etype")
    workplace = [w.strip().lower() for w in one("workplace").split(",") if w.strip()]
    jobtypes = [j.strip().lower() for j in one("jobtype").split(",") if j.strip()]
    days = one("days"); mins = one("mins")
    sort = one("sort", "relevant"); remote = one("remote") in ("1", "true")
    spon = one("sponsor"); only = one("only").lower()   # 'auto' → only end-to-end doable jobs
    try: page = max(0, int(one("page", "0")))
    except Exception: page = 0
    params = {"select": _SLIM, "is_open": "eq.true",
              "order": "posted_at.desc.nullslast", "limit": str(_POOL)}
    if spon == "hide":                  params["sponsorship"] = "neq.no"
    elif spon == "yes":                 params["sponsorship"] = "eq.yes"
    if etypes:                          params["employment_type"] = _inlist(etypes)
    if companies:                       params["company_slug"] = _inlist(companies) if len(companies) > 1 else f"ilike.*{companies[0]}*"
    if countries:                       params["country"] = _inlist(countries)
    if vendor in ("greenhouse", "lever", "ashby"): params["vendor"] = f"eq.{vendor}"
    wset = set(w.replace("on-site", "onsite") for w in workplace)
    if wset == {"remote"}:              params["remote"] = "eq.true"
    elif wset == {"onsite"}:            params["remote"] = "eq.false"
    elif remote:                        params["remote"] = "eq.true"
    if mins.isdigit() and int(mins) > 0:
        params["first_seen_at"] = f"gte.{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - int(mins)*60))}"
    elif days.isdigit() and int(days) > 0:
        params["posted_at"] = f"gte.{time.strftime('%Y-%m-%d', time.gmtime(time.time() - int(days)*86400))}"
    if loc:
        op, pat = _loc_param(loc); params["location"] = f"{op}.{pat}"
    if terms:
        params["or"] = "(" + ",".join(f"title.ilike.*{t}*" for t in terms) + ")"
    pool = _cached_pool(params)   # user-independent filter result, cached briefly → fast pagination & multi-user
    if jobtypes:                  # seniority/job-type is derived from the title, filtered here
        pool = [j for j in pool if _seniority(j.get("title", "")) in jobtypes]
    prof, states = _user_ctx(user)
    myskills = set((s or "").lower() for s in (prof.get("skills") or []))
    blocked_co, manual_co = _apply_signals(user)
    for j in pool:
        j["score"] = _score(j, myskills)
        j["status"] = states.get(j["id"], "new")
        j["applyability"] = _applyability(j.get("vendor"), j.get("company_slug"), blocked_co, manual_co)
    if only == "auto":                                  # "only suggest jobs we can do end-to-end"
        pool = [j for j in pool if j["applyability"] == "auto"]
    elif only in ("assisted", "applyable"):             # auto + assisted (agent does the work; you may finish a captcha)
        pool = [j for j in pool if j["applyability"] in ("auto", "assisted")]
    if sort == "new":
        pool.sort(key=lambda j: (j.get("posted_at") or "", j["score"]), reverse=True)
    elif sort == "match":
        pool.sort(key=lambda j: (j["score"], j.get("posted_at") or ""), reverse=True)
    else:   # 'relevant' (default): accurate match, freshest first — a blend
        pool.sort(key=lambda j: (j["score"] + _recency_bonus(j.get("posted_at")),
                                 j.get("posted_at") or ""), reverse=True)
    # facets from the whole matched pool
    from collections import Counter
    ets = Counter(j.get("employment_type") for j in pool if j.get("employment_type"))
    comps = Counter(j.get("company_slug") for j in pool if j.get("company_slug"))
    ctry = Counter(j.get("country") for j in pool if j.get("country"))
    facets = {"employment_types": [e for e, _ in ets.most_common(12)],
              "countries": [c for c, _ in ctry.most_common(40)],
              "companies": [c for c, _ in comps.most_common(50)],
              "vendors": sorted({j.get("vendor") for j in pool if j.get("vendor")})}
    total = len(pool)
    start = page * _PAGE
    page_jobs = pool[start:start + _PAGE]
    # NOTE: descriptions are intentionally omitted from the list (they're the big field).
    # The card doesn't need them; /api/job fetches the full description on demand at Add/Apply.
    return {"ok": True, "count": total, "page": page, "size": _PAGE,
            "has_more": start + _PAGE < total, "facets": facets, "jobs": page_jobs}

def job_detail(qs):
    """Full single job (with description) — fetched on demand when the user adds/applies."""
    try: jid = int((qs.get("id") or ["0"])[0])
    except Exception: jid = 0
    if not jid:
        return {"ok": False, "detail": "no id"}
    rows = sb.select("jobs", {"select": "*", "id": f"eq.{jid}", "limit": "1"})
    return {"ok": bool(rows), "job": rows[0] if rows else None}

from app_status import LABELS as _STATUS_LABELS

def applications_feed(user):
    """The Activity feed: every application the agent (or the user) has attempted,
    newest first, each with its honest status, the issue detail, and whether a
    screenshot is available. This is what makes the auto-apply agent observable."""
    # NB: confirmed_at is added by the lifecycle migration; select a set that also
    # works pre-migration so the feed shows existing rows immediately.
    apps = sb.select("applications", {
        "user_id": f"eq.{user}",
        "select": "job_id,status,attempts,submitted_at,created_at,next_retry_at,receipt",
        "order": "created_at.desc", "limit": "400"}) or []
    ids = [str(a["job_id"]) for a in apps if a.get("job_id")]
    jobs = {}
    if ids:
        for j in sb.select("jobs", {"id": f"in.({','.join(ids)})",
                                    "select": "id,title,company_slug,vendor,url,location"}) or []:
            jobs[j["id"]] = j
    # cheap presence check: which of these have a saved résumé snapshot (job_id only, no bulky html)
    have_resume = {r["job_id"] for r in (sb.select("applications",
                   {"user_id": f"eq.{user}", "resume_html": "not.is.null", "select": "job_id"}) or [])}
    out = []
    for a in apps:
        rec = a.get("receipt") or {}
        j = jobs.get(a["job_id"], {})
        st = a.get("status") or "draft"
        label, tone = _STATUS_LABELS.get(st, (st, "neutral"))
        out.append({
            "job_id": a["job_id"],
            "title": j.get("title") or rec.get("job") or "(job)",
            "company": j.get("company_slug") or "",
            "vendor": j.get("vendor") or rec.get("backend") or "",
            "url": j.get("url") or rec.get("url") or "",
            "location": j.get("location") or "",
            "status": st, "label": label, "tone": tone,
            "detail": rec.get("detail") or "",
            "backend": rec.get("backend") or "",
            "attempts": a.get("attempts") or 0,
            "at": a.get("submitted_at") or a.get("created_at") or "",
            "next_retry_at": a.get("next_retry_at") or "",
            "unfilled": [u.get("label") for u in (rec.get("unfilled_required") or []) if isinstance(u, dict) and u.get("label")][:6],
            "has_shot": bool(rec.get("screenshot")),
            "has_resume": a["job_id"] in have_resume,
        })
    out.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    counts = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"ok": True, "count": len(out), "counts": counts, "applications": out}

def agent_stats(user):
    """Evals for the control board: application outcomes, success rate, backend/vendor mix,
    and recent crawl cycles."""
    from collections import Counter
    apps = sb.select("applications", {"user_id": f"eq.{user}",
            "select": "job_id,status,created_at,receipt"}) or []
    counts = Counter(a.get("status") for a in apps)
    total = len(apps)
    applied = counts.get("confirmed", 0)
    sent = counts.get("submitted_unconfirmed", 0)
    needs = counts.get("needs_you", 0) + counts.get("awaiting_review", 0)
    captcha = counts.get("blocked_captcha", 0)
    failed = counts.get("failed_transient", 0) + counts.get("failed_permanent", 0)
    reached = applied + sent                         # got the application in
    by_backend = Counter((a.get("receipt") or {}).get("backend") for a in apps
                         if (a.get("receipt") or {}).get("backend"))
    crawl = sb.select("crawl_runs", {"select": "new_jobs,updated_jobs,errors,finished_at,started_at",
                                     "order": "id.desc", "limit": "12"}) or []
    return {"ok": True, "total": total, "applied": applied, "sent": sent, "needs_you": needs,
            "captcha": captcha, "failed": failed,
            "success_rate": round(100 * reached / total) if total else 0,
            "counts": {k: v for k, v in counts.items() if k},
            "by_backend": dict(by_backend), "crawl": crawl, "config": agent_config(user)}

import prompts as _prompts

def _envd(k, d):
    v = os.environ.get(k)
    try: return type(d)(v) if v not in (None, "") else d
    except Exception: return d

def orchestration(user):
    """The FULL orchestration config the control board edits — merged: defaults ← env ← stored.
    Read by the background agent (apply.py) and the tailoring pipeline."""
    prof = sb.select("profiles", {"user_id": f"eq.{user}", "select": "data", "limit": "1"}) or []
    data = (prof[0].get("data") if prof else {}) or {}
    o = data.get("orchestration") or {}
    aa = data.get("auto_apply") or {}
    st = data.get("standing") or {}
    def g(sec, k, d): return ((o.get(sec) or {}).get(k)) if (o.get(sec) or {}).get(k) is not None else d
    pr = o.get("prompts") or {}
    return {
        "modes": {"enabled": bool(aa.get("enabled")), "mode": aa.get("mode") or "auto",
                  "min_score": aa.get("min_score", _envd("APPLY_MIN_SCORE", 45)),
                  "max_per_run": aa.get("max_per_run", _envd("APPLY_MAX_PER_USER", 10)),
                  "daily_cap": g("modes", "daily_cap", 0), "paused": bool(g("modes", "paused", False))},
        "crawl": {"refresh_hours": g("crawl", "refresh_hours", _envd("JOB_REFRESH_HOURS", 1)),
                  "batch": g("crawl", "batch", _envd("CRAWL_BATCH", 90)),
                  "concurrency": g("crawl", "concurrency", _envd("CRAWL_CONCURRENCY", 16))},
        "match": {"skill_weight": g("match", "skill_weight", 13),
                  "recency_boost": bool(g("match", "recency_boost", True)),
                  "pool": g("match", "pool", 300)},
        "tailor": {"one_page": g("tailor", "one_page", "prefer"),
                   "bullets_per_role": g("tailor", "bullets_per_role", 6),
                   "temp": g("tailor", "temp", 0.4)},
        "answers": {"persona": st.get("_persona", "") or os.environ.get("APPLY_PERSONA", ""),
                    "answer_prompt": st.get("_answer_prompt", ""),
                    "cover_letter": bool(g("answers", "cover_letter", False)),
                    "choice_default_both": bool(g("answers", "choice_default_both", True))},
        "execution": {"headed": bool(g("execution", "headed", os.environ.get("APPLY_HEADED") == "1")),
                      "persist": bool(g("execution", "persist", os.environ.get("APPLY_PERSIST", "1") != "0")),
                      "domain_gap": g("execution", "domain_gap", _envd("APPLY_DOMAIN_GAP", 8)),
                      "retries": g("execution", "retries", _envd("APPLY_MAX_RETRIES", 3)),
                      "claim_ttl": g("execution", "claim_ttl", _envd("APPLY_CLAIM_TTL_MIN", 15)),
                      "timeout": g("execution", "timeout", 45)},
        "model": {"provider": g("model", "provider", os.environ.get("LLM_PROVIDER", "auto")),
                  "model": g("model", "model", ""), "temp": g("model", "temp", 0.4)},
        "prompts": {k: (pr.get(k) if (pr.get(k) or "").strip() else _prompts.DEFAULTS.get(k, ""))
                    for k in ("understand", "summary", "bullets", "projects", "answer", "cover_letter")},
        "prompt_defaults": _prompts.DEFAULTS,
    }

# kept for the existing GET stats.config consumer
def agent_config(user):
    o = orchestration(user)
    m, a = o["modes"], o["answers"]
    return {"enabled": m["enabled"], "mode": m["mode"], "min_score": m["min_score"],
            "max_per_run": m["max_per_run"], "persona": a["persona"], "answer_prompt": a["answer_prompt"]}

def save_agent_config(user, body):
    prof = sb.select("profiles", {"user_id": f"eq.{user}", "select": "data", "limit": "1"}) or []
    data = (prof[0].get("data") if prof else {}) or {}
    o = data.get("orchestration") or {}
    for sec in ("modes", "crawl", "match", "tailor", "answers", "execution", "model", "prompts"):
        if isinstance(body.get(sec), dict):
            o.setdefault(sec, {}).update(body[sec])
    data["orchestration"] = o
    # mirror the fields the RUNNING engine reads today, so changes take effect without refactor
    m = o.get("modes") or {}
    aa = data.get("auto_apply") or {}
    aa["enabled"] = bool(m.get("enabled", aa.get("enabled")))
    aa["mode"] = m.get("mode") if m.get("mode") in ("auto", "review") else aa.get("mode", "auto")
    try: aa["min_score"] = max(0, min(100, int(m.get("min_score", aa.get("min_score", 45)))))
    except Exception: pass
    try: aa["max_per_run"] = max(1, min(100, int(m.get("max_per_run", aa.get("max_per_run", 10)))))
    except Exception: pass
    data["auto_apply"] = aa
    ans = o.get("answers") or {}
    st = data.get("standing") or {}
    if "persona" in ans: st["_persona"] = (ans.get("persona") or "")[:600]
    if "answer_prompt" in ans: st["_answer_prompt"] = (ans.get("answer_prompt") or "")[:1400]
    data["standing"] = st
    sb.upsert("profiles", [{"user_id": user, "data": data,
              "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}],
              on_conflict="user_id", update=True)
    return {"ok": True, "config": orchestration(user)}

def agent_logs(user, limit=60):
    """A live feed of the agent's recent work — each attempt with its status, backend and
    the detail line, newest first (the observable orchestration log)."""
    apps = sb.select("applications", {"user_id": f"eq.{user}",
            "select": "job_id,status,attempts,created_at,submitted_at,receipt",
            "order": "created_at.desc", "limit": str(limit)}) or []
    ids = [str(a["job_id"]) for a in apps if a.get("job_id")]
    titles = {}
    if ids:
        for j in sb.select("jobs", {"id": f"in.({','.join(ids)})", "select": "id,title,company_slug"}) or []:
            titles[j["id"]] = j
    out = []
    for a in apps:
        rec = a.get("receipt") or {}
        j = titles.get(a["job_id"], {})
        st = a.get("status") or "draft"
        label = _STATUS_LABELS.get(st, (st, "neutral"))[0]
        out.append({"job_id": a["job_id"], "status": st, "label": label,
                    "at": a.get("submitted_at") or a.get("created_at") or "",
                    "backend": rec.get("backend") or "", "attempts": a.get("attempts") or 0,
                    "company": j.get("company_slug") or "", "title": j.get("title") or rec.get("job") or "",
                    "detail": rec.get("detail") or ""})
    out.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return {"ok": True, "logs": out}

def application_detail(user, job_id):
    """Everything we know about one application — the full audit: status timeline, every
    answer submitted, the fields filled, backend, attempts, timestamps."""
    rows = sb.select("applications", {"user_id": f"eq.{user}", "job_id": f"eq.{job_id}", "limit": "1",
            "select": "job_id,status,attempts,submitted_at,confirmed_at,created_at,next_retry_at,answers,receipt"}) or []
    if not rows:
        return {"ok": False, "detail": "not found"}
    a = rows[0]; rec = a.get("receipt") or {}
    st = a.get("status") or "draft"
    label, tone = _STATUS_LABELS.get(st, (st, "neutral"))
    jrow = sb.select("jobs", {"id": f"eq.{job_id}", "select": "title,company_slug,url,location,vendor", "limit": "1"})
    j = jrow[0] if jrow else {}
    # answers as an ordered list, hiding builtins that aren't interesting
    ans = a.get("answers") or {}
    answers = [{"q": k, "a": v} for k, v in ans.items() if v not in (None, "") and not str(k).startswith("_")]
    filled = rec.get("filled") or {}
    submitted_fields = rec.get("submitted_fields") or rec.get("submitted") or []
    return {"ok": True, "detail_ok": True,
            "job_id": job_id, "title": j.get("title") or rec.get("job") or "",
            "company": j.get("company_slug") or "", "url": j.get("url") or rec.get("url") or "",
            "location": j.get("location") or "", "vendor": j.get("vendor") or "",
            "status": st, "label": label, "tone": tone,
            "backend": rec.get("backend") or "", "detail": rec.get("detail") or "",
            "attempts": a.get("attempts") or 0,
            "created_at": a.get("created_at") or "", "submitted_at": a.get("submitted_at") or "",
            "confirmed_at": a.get("confirmed_at") or "", "next_retry_at": a.get("next_retry_at") or "",
            "confirmed_via": rec.get("confirmed_via") or "",
            "events": rec.get("events") or [],
            "answers": answers,
            "filled": [k for k, v in filled.items() if v],
            "submitted_fields": [f.get("label") or f.get("name") for f in submitted_fields if isinstance(f, dict)][:20],
            "unfilled": [u.get("label") for u in (rec.get("unfilled_required") or []) if isinstance(u, dict) and u.get("label")],
            "has_shot": bool(rec.get("screenshot")),
            "has_resume": bool(a.get("job_id"))}

class H(SimpleHTTPRequestHandler):
    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)
    def _send_shot(self, user, job_id):
        """Serve the agent's screenshot for THIS user's application on a job — the
        proof of what the form looked like. Gated: we only serve a file that appears
        in this user's own application receipt (no path traversal, no cross-user)."""
        if not (sb and sb.is_configured() and job_id):
            return self._json(404, {"ok": False})
        try:
            rows = sb.select("applications", {"user_id": f"eq.{user}", "job_id": f"eq.{job_id}",
                                              "select": "receipt", "limit": "1"}) or []
            shot = ((rows[0].get("receipt") or {}).get("screenshot")) if rows else None
            if not shot:
                return self._json(404, {"ok": False})
            path = os.path.join(HERE, "applications_out", os.path.basename(shot))  # basename → no traversal
            if not os.path.isfile(path):
                return self._json(404, {"ok": False})
            data = open(path, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "private, max-age=60")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self._json(404, {"ok": False})
    def do_GET(self):
        if self.path.startswith("/api/discover"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            terms = [t.strip().lower() for t in (qs.get("q") or [""])[0].split(",") if t.strip()]
            vendors = [v.strip() for v in (qs.get("vendors") or [""])[0].split(",") if v.strip()]
            loc = (qs.get("loc") or [""])[0]
            try:
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
                batch = min(600, max(10, int((qs.get("batch") or ["50"])[0])))
            except ValueError:
                offset, batch = 0, 250
            try:
                return self._json(200, discover(terms, vendors, offset, batch, loc))
            except Exception as e:
                return self._json(200, {"ok": False, "status": "error", "detail": str(e)[:200]})
        if self.path.startswith("/api/contacts"):
            if not (contacts and contacts.available()):
                return self._json(200, {"ok": False, "status": "no_provider",
                                        "detail": "Set APOLLO_API_KEY or HUNTER_API_KEY to find real contacts."})
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            company = (qs.get("company") or [""])[0]; domain = (qs.get("domain") or [""])[0] or None
            try:
                return self._json(200, {"ok": True, "provider": contacts.available()[0],
                                        "contacts": contacts.find(company, domain)})
            except Exception as e:
                return self._json(200, {"ok": False, "status": "error", "detail": str(e)[:200]})
        if self.path.startswith("/api/inbox/otp"):
            if not (inbox and sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            user = _req_user(self)
            try:
                rows = sb.select("emails", {"user_id": f"eq.{user}", "otp": "not.is.null",
                                            "select": "otp,received_at", "order": "received_at.desc", "limit": "1"})
                return self._json(200, {"ok": True, "otp": rows[0]["otp"] if rows else None})
            except Exception as e:
                return self._json(200, {"ok": False, "detail": str(e)[:150]})
        if self.path.startswith("/api/config"):
            return self._json(200, {"ok": True,
                                    "auth": bool(sb and sb.auth_enabled()),
                                    "supabase": bool(sb and sb.is_configured()),
                                    "supabase_url": sb.URL if sb else "",
                                    "anon_key": sb.ANON if sb else "",
                                    "billing": (billing.provider() if billing else None),
                                    "contacts": (contacts.available() if contacts else []),
                                    "plan": _user_plan(_req_user(self))})
        if urllib.parse.urlparse(self.path).path == "/api/job":
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: return self._json(200, job_detail(qs))
            except Exception as e: return self._json(200, {"ok": False, "detail": str(e)[:160]})
        if urllib.parse.urlparse(self.path).path == "/api/applications":
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            try: return self._json(200, applications_feed(_req_user(self)))
            except Exception as e: return self._json(200, {"ok": False, "detail": str(e)[:200]})
        if urllib.parse.urlparse(self.path).path == "/api/shot":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send_shot(_req_user(self), (qs.get("job") or [""])[0])
        if urllib.parse.urlparse(self.path).path in ("/api/agent/stats", "/api/agent/logs", "/api/agent/orch"):
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            try:
                user = _req_user(self); p = urllib.parse.urlparse(self.path).path
                if p == "/api/agent/stats":
                    return self._json(200, agent_stats(user))
                if p == "/api/agent/orch":
                    return self._json(200, {"ok": True, "config": orchestration(user)})
                return self._json(200, agent_logs(user))
            except Exception as e:
                return self._json(200, {"ok": False, "detail": str(e)[:200]})
        if urllib.parse.urlparse(self.path).path == "/api/application":
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = (qs.get("job") or [""])[0]
            try:
                return self._json(200, application_detail(_req_user(self), jid))
            except Exception as e:
                return self._json(200, {"ok": False, "detail": str(e)[:160]})
        if urllib.parse.urlparse(self.path).path == "/api/application-resume":
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = (qs.get("job") or [""])[0]
            try:
                rows = sb.select("applications", {"user_id": f"eq.{_req_user(self)}", "job_id": f"eq.{jid}",
                                                  "select": "resume_html", "limit": "1"}) or []
                html = rows[0].get("resume_html") if rows else None
                return self._json(200, {"ok": bool(html), "resume_html": html or ""})
            except Exception as e:
                return self._json(200, {"ok": False, "detail": str(e)[:160]})
        if self.path.startswith("/api/jobs") or self.path.startswith("/api/profile"):
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db",
                                        "detail": "Supabase not configured — set SUPABASE_URL and SUPABASE_SERVICE_KEY."})
            try:
                user = _req_user(self)
                if self.path.startswith("/api/profile"):
                    # full profile incl. the `data` blob (standing answers, experience…) so a
                    # new device can pull the answer bank back down. Distinct from _profile()
                    # (the slim version jobs_query uses for matching).
                    rows = sb.select("profiles", {"user_id": f"eq.{user}", "limit": "1",
                            "select": "name,email,title,contact,summary,skills,memory,data,updated_at"})
                    return self._json(200, {"ok": True, "profile": rows[0] if rows else {}})
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._json(200, jobs_query(qs, user))
            except Exception as e:
                return self._json(200, {"ok": False, "status": "error", "detail": str(e)[:200]})
        if self.path.startswith("/api/form"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            url = (qs.get("url") or [""])[0]
            board, jid = gh_ids(url)
            if not board:
                return self._json(200, {"ok": False, "status": "unsupported",
                                        "detail": "Only Greenhouse URLs supported by the engine so far — use the apply link for others."})
            try:
                return self._json(200, {"ok": True, "vendor": "greenhouse", "questions": gh_questions(board, jid)})
            except Exception as e:
                return self._json(200, {"ok": False, "status": "fetch_error", "detail": str(e)[:200]})
        return super().do_GET()
    def do_POST(self):
        if self.path == "/api/profile" or self.path == "/api/jobs/state":
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db",
                                        "detail": "Supabase not configured."})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            user = _req_user(self)          # identity from the token, NOT the request body
            try:
                if self.path == "/api/profile":
                    row = {"user_id": user,
                           "name": body.get("name"), "email": body.get("email"),
                           "title": body.get("title"), "contact": body.get("contact"),
                           "summary": body.get("summary"), "skills": body.get("skills") or [],
                           "memory": body.get("memory") or {}, "data": body.get("data") or {},
                           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    sb.upsert("profiles", [row], on_conflict="user_id", update=True)
                    return self._json(200, {"ok": True})
                # /api/jobs/state — save/apply/dismiss
                row = {"user_id": user, "job_id": body.get("job_id"),
                       "status": body.get("status", "saved"),
                       "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                sb.upsert("user_jobs", [row], on_conflict="user_id,job_id", update=True)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(200, {"ok": False, "status": "error", "detail": str(e)[:200]})
        if self.path == "/api/track":
            # Record a manual/self-apply in the applications table so it shows in the
            # Activity view WITH the résumé the user applied with. Never downgrades a
            # row the agent already confirmed.
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            user = _req_user(self)
            jid = body.get("job_id")
            if not jid:
                return self._json(200, {"ok": False, "detail": "no job_id"})
            try:
                status = body.get("status") or "submitted_unconfirmed"
                resume_html = body.get("resume_html") or ""
                prior = sb.select("applications", {"user_id": f"eq.{user}", "job_id": f"eq.{jid}",
                                                   "select": "status,submitted_at,resume_html"}) or []
                p = prior[0] if prior else {}
                if p.get("status") == "confirmed":
                    return self._json(200, {"ok": True, "kept": "confirmed"})
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                row = {"user_id": user, "job_id": jid, "status": status, "human_in_loop": True,
                       "submitted_at": p.get("submitted_at") or now_iso,
                       "receipt": {"backend": body.get("backend") or "you",
                                   "detail": body.get("detail") or "Marked applied on the company site.",
                                   "url": body.get("url"), "job": body.get("label")}}
                if resume_html or p.get("resume_html"):
                    row["resume_html"] = resume_html or p.get("resume_html")
                sb.upsert("applications", [row], on_conflict="user_id,job_id", update=True)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(200, {"ok": False, "status": "error", "detail": str(e)[:200]})
        if self.path == "/api/confirm":
            # manual override: the user tells us an application was actually received.
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            user = _req_user(self); jid = body.get("job_id")
            if not jid:
                return self._json(200, {"ok": False, "detail": "no job_id"})
            try:
                ok = _mark_confirmed(user, jid, "you marked it applied")
                return self._json(200, {"ok": bool(ok)})
            except Exception as e:
                return self._json(200, {"ok": False, "status": "error", "detail": str(e)[:200]})
        if self.path == "/api/agent/config":
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                return self._json(200, save_agent_config(_req_user(self), body))
            except Exception as e:
                return self._json(200, {"ok": False, "status": "error", "detail": str(e)[:200]})
        if self.path == "/api/apply-submit":
            # user clicked Submit on a reviewed job → send it live (server does the work)
            if not (sb and sb.is_configured()):
                return self._json(200, {"ok": False, "status": "no_db"})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            user = _req_user(self); jid = body.get("job_id")
            if not jid:
                return self._json(200, {"ok": False, "detail": "no job_id"})
            _ok, _plan = _apply_gate(user)
            if not _ok:
                return self._json(200, {"ok": False, "status": "limit",
                                        "detail": f"Daily free limit ({FREE_APPLY_LIMIT}) reached."})
            try:
                import apply
                return self._json(200, apply.submit_reviewed(user, jid))
            except Exception as e:
                return self._json(200, {"ok": False, "status": "error", "detail": str(e)[:200]})
        if self.path == "/api/billing/checkout":
            if not (billing and billing.provider()):
                return self._json(200, {"ok": False, "detail": "No billing provider configured."})
            n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or b"{}")
            user = _req_user(self); host = self.headers.get("Host", "localhost:8765")
            base = f"http://{host}"
            res = billing.checkout(user, body.get("plan", "pro"), base + "/dashboard.html?paid=1",
                                   base + "/dashboard.html", body.get("email"))
            return self._json(200, res)
        if self.path == "/api/billing/webhook":
            n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
            uid, plan, status = (billing.parse_webhook(billing.provider(), raw, self.headers) if billing else (None, None, None))
            if uid and sb and sb.is_configured():
                try:
                    sb.upsert("profiles", [{"user_id": uid, "plan": plan if status == "active" else "free",
                                            "plan_status": status}], on_conflict="user_id", update=True)
                except Exception: pass
            return self._json(200, {"ok": True})
        if self.path == "/api/inbox/inbound":
            n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n) or b"{}"
            try: payload = json.loads(raw)
            except Exception: payload = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
            msg = inbox.normalize(payload, self.headers) if inbox else {}
            confirmed = None
            if msg.get("user_id") and sb and sb.is_configured():
                try:
                    sb.insert("emails", [{"user_id": msg["user_id"], "from_addr": msg["from_addr"],
                                          "subject": msg["subject"], "body": msg["body"], "otp": msg["otp"]}], return_rep=False)
                except Exception: pass
                try:
                    confirmed = confirm_from_email(msg["user_id"], msg)   # "Sent → Applied ✓" when it's a confirmation
                except Exception: confirmed = None
            return self._json(200, {"ok": True, "confirmed_job_id": confirmed})
        if self.path == "/api/apply-browser":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            user = _req_user(self)
            _ok, _plan = _apply_gate(user)
            if not _ok:
                return self._json(200, {"ok": False, "status": "limit",
                                        "detail": f"Daily free limit ({FREE_APPLY_LIMIT}) reached — upgrade to keep auto-applying."})
            venv = os.path.join(HERE, ".venv", "bin", "python")
            py = venv if os.path.exists(venv) else sys.executable
            apply_url = body.get("url", "")
            if body.get("job_id") and sb and sb.is_configured():   # prefer the clean Greenhouse form
                try:
                    jr = sb.select("jobs", {"id": f"eq.{body['job_id']}", "limit": "1",
                                            "select": "vendor,company_slug,external_id,url"})
                    if jr:
                        apply_url = canonical_apply_url(jr[0]) or apply_url
                except Exception:
                    pass
            payload = json.dumps({"job": {"url": apply_url, "title": body.get("label", "")},
                                  "answers": body.get("answers", {}) or {},
                                  "standing": body.get("standing", {}) or {},
                                  "resume_html": body.get("resume_html", "") or "", "dry": not bool(body.get("live"))})
            try:
                p = subprocess.run([py, "-c",
                    "import sys,json,apply_browser as ab;print(json.dumps(ab.submit(**json.load(sys.stdin))))"],
                    input=payload, capture_output=True, text=True, timeout=150, cwd=HERE)
                lines = [l for l in (p.stdout or "").splitlines() if l.strip().startswith("{")]
                res = json.loads(lines[-1]) if lines else {"ok": False, "status": "error",
                       "detail": (p.stderr or "no output — is Playwright set up? see SETUP.md")[:220]}
            except Exception as e:
                res = {"ok": False, "status": "error", "detail": str(e)[:200]}
            rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "user": user, "backend": "browser",
                   "url": body.get("url"), "job": body.get("label"), "job_id": body.get("job_id"),
                   "live": bool(body.get("live")), "ok": bool(res.get("ok")), "status": res.get("status"),
                   "detail": res.get("detail"), "screenshot": res.get("screenshot"),
                   "unfilled_required": res.get("unfilled_required")}
            log_receipt(rec)
            _mirror_application(user, body.get("job_id"), res, body.get("answers", {}) or {}, rec,
                                body.get("resume_html", "") or "")
            return self._json(200, res)
        if self.path == "/api/apply":
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            user = _req_user(self)
            url = payload.get("url", "")
            apply_id = payload.get("apply_id") or uuid.uuid4().hex
            answers = payload.get("answers", {}) or {}
            provenance = payload.get("provenance", {}) or {}   # {field: builtin|llm|user}
            resume_html = payload.get("resume_html", "") or ""
            board, jid = gh_ids(url)
            dup = already_applied(url, apply_id)
            if dup:
                res = {"ok": True, "status": "already_applied",
                       "detail": "Already applied " + dup.get("at", "") + " — not submitting again."}
            else:
                try:
                    res = gh_apply(url, answers, resume_html)
                except Exception as e:
                    res = {"ok": False, "status": "error", "detail": str(e)[:200]}
            # ---- thorough audit record: who, what, provenance, résumé, receipt ----
            rec = {
                "apply_id": apply_id, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "user": user, "job": payload.get("label"), "job_id": payload.get("job_id"),
                "vendor": "greenhouse" if board else "other", "board": board, "jid": jid, "url": url,
                "dry_run": bool(DRY_RUN),
                "answers": answers, "provenance": provenance,
                "resume_format": "html", "resume_bytes": len(resume_html),
                "resume_sha": hashlib.sha256(resume_html.encode()).hexdigest()[:16],
                "submitted_fields": res.get("submitted"),
                "sensitive_sent": res.get("sensitive_sent"),
                "ok": bool(res.get("ok")), "status": res.get("status"),
                "http_code": res.get("http_code"), "detail": res.get("detail"),
            }
            log_receipt(rec)
            # mirror into the per-user applications table when we have a DB + job id
            _mirror_application(user, payload.get("job_id"), res, answers, rec, resume_html)
            res["apply_id"] = apply_id
            return self._json(200, res)
        return self._json(404, {"ok": False})

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    # Default to localhost-only (safe). Set HOST=0.0.0.0 to expose it (e.g. in a container).
    # When exposed, run behind auth — Supabase auth gates the apply endpoints via _req_user.
    from http.server import ThreadingHTTPServer
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Resume Tailor + apply engine on http://{host}:{PORT}  (DRY_RUN={DRY_RUN})")
    ThreadingHTTPServer((host, PORT), H).serve_forever()
