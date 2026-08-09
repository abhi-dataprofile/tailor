"""ats.py — shared ATS ingestion library (Greenhouse / Lever / Ashby).

Single source of truth for: the bundled company dataset, fetching public
job feeds, and extracting structured signals (skills, visa sponsorship,
posting date) server-side at ingest time. Imported by worker.py (crawler)
and by serve.py (live discovery). Pure stdlib — no third-party deps.
"""
import json, os, re, time, html, urllib.request
from concurrent.futures import ThreadPoolExecutor
import geo

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) ResumeTailor/1.0"}

# ---- well-known companies crawled first (strong early matches) ----
PRIORITY = {
    "greenhouse": ["stripe","openai","anthropic","notion","ramp","vercel","datadog","figma","brex",
        "databricks","airbnb","coinbase","robinhood","instacart","doordash","reddit","discord","dropbox",
        "cloudflare","gitlab","hashicorp","asana","benchling","gusto","retool","samsara","affirm","chime",
        "sofi","twilio","okta","elastic","mongodb","zapier","webflow","calendly","grammarly","duolingo"],
    "lever": ["openai","anthropic","figma","databricks","plaid","reddit","gitlab","benchling","retool",
        "affirm","confluent","canva","duolingo","cohere","sourcegraph"],
    "ashby": ["openai","notion","ramp","linear","plaid","reddit","snowflake","benchling","confluent",
        "zapier","deel","pinecone","weaviate","cohere","runway","perplexity","harvey","cursor","replit"],
    "smartrecruiters": ["BoschGroup","SGS","Equinox","PublicStorage","Accor","Experian","WesternDigital","Colliers","Visa","WeWork","Wayfair"],
    "recruitee": ["bunq","personio","gorgias"],
}

_VENDORS = ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee")

# ---- skill taxonomy (kept in sync with index.html's TAX) ----
TAX = {
 "languages":["javascript","typescript","python","java","c++","c#","go","golang","rust","ruby","php","swift","kotlin","scala","r","sql","bash"],
 "frontend":["react","angular","vue","svelte","next.js","nextjs","redux","html","css","sass","tailwind","webpack","vite","figma","accessibility","wcag","storybook"],
 "backend":["node.js","nodejs","express","django","flask","fastapi","spring","spring boot",".net","rails","laravel","graphql","rest","rest api","grpc","microservices","kafka","rabbitmq","redis","websockets","serverless"],
 "data":["postgresql","mysql","mongodb","dynamodb","elasticsearch","snowflake","bigquery","redshift","etl","spark","hadoop","airflow","dbt","tableau","power bi","looker","pandas","numpy","pinecone","weaviate","milvus","chroma","databricks"],
 "ml":["machine learning","deep learning","tensorflow","pytorch","keras","scikit-learn","nlp","computer vision","llm","llms","transformers","mlops","xgboost","lightgbm","data science","a/b testing","rag","retrieval augmented generation","vector search","vector database","langchain","llamaindex","embeddings","fine-tuning","hugging face","agentic","prompt engineering","semantic search","rlhf","gradient boosting","recommendation systems","feature engineering","model deployment"],
 "cloud":["aws","azure","gcp","google cloud","docker","kubernetes","k8s","terraform","ansible","ci/cd","jenkins","github actions","lambda","ec2","s3","devops","prometheus","grafana","datadog","linux"],
 "mobile":["ios","android","react native","flutter","swiftui","jetpack compose","xcode"],
 "design":["ux","ui","user research","wireframing","prototyping","sketch","design systems","usability testing"],
 "pm":["product management","roadmap","agile","scrum","kanban","jira","okrs","kpis","go-to-market","stakeholder management"],
 "marketing":["seo","sem","google analytics","content marketing","email marketing","hubspot","ppc","google ads"],
 "methods":["tdd","code review","git","unit testing","integration testing","pair programming"],
}
_ALL_SKILLS = sorted({s for v in TAX.values() for s in v}, key=len, reverse=True)
_SKILL_RE = {s: re.compile(r"(?<![a-z0-9+#.])" + re.escape(s) + r"(?![a-z0-9+#])", re.I) for s in _ALL_SKILLS}

def extract_skills(text, limit=40):
    """Established skills/tools named in the text (taxonomy-matched)."""
    low = (text or "").lower()
    out = [s for s in _ALL_SKILLS if _SKILL_RE[s].search(low)]
    return out[:limit]

# ---- visa-scoped sponsorship signal: 'yes' | 'no' | 'unknown' ----
_VISA = re.compile(r"(visa|h-?1b|h1-b|immigrat|work authoriz|employment authoriz|green card|work permit|right to work|sponsorship for employment)", re.I)
_NEGCUE = re.compile(r"\b(not|no|non|unable|cannot|can'?t|won'?t|will not|do not|does not|doesn'?t|without|neither|unfortunately)\b", re.I)
_POSCUE = re.compile(r"(will sponsor|can sponsor|do sponsor|happy to sponsor|able to sponsor|offer(?:s|ing)?[^.]{0,25}sponsorship|provide(?:s)?[^.]{0,25}sponsorship|sponsorship (?:is )?available|we sponsor|open to sponsor)", re.I)

def sponsorship(text):
    if not text or "sponsor" not in text.lower():
        return "unknown"
    verdict = "unknown"
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        low = s.lower()
        if "sponsor" not in low or "export" in low or not _VISA.search(s):
            continue
        if _POSCUE.search(s):
            return "yes"
        if _NEGCUE.search(s):
            verdict = "no"
    return verdict

def _strip_html(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()

def _iso_date(s):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s or "")
    return m.group(1) if m else None

def _ms_date(ms):
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ms) / 1000))
    except Exception:
        return None

def _iso_ts(s):
    """Normalize any ISO-ish timestamp to a lexically-comparable 'YYYY-MM-DDTHH:MM:SS' (UTC)."""
    if not s:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", str(s))
    if m:
        return m.group(1) + "T" + m.group(2)
    d = _iso_date(str(s))
    return (d + "T00:00:00") if d else ""

def _ms_iso(ms):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(ms) / 1000))
    except Exception:
        return ""

def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

# ---- company dataset ----
def _load_slugs(vendor):
    try:
        with open(os.path.join(HERE, "data", vendor + "_companies.json")) as f:
            return list(dict.fromkeys(json.load(f)))
    except Exception:
        return []

def load_companies():
    """[(vendor, slug, priority)] — priority>0 = well-known, crawled first."""
    out, seen = [], set()
    per = {v: _load_slugs(v) for v in _VENDORS}
    # PRIORITY companies are seeded even if not in the bundled slug file (curated new sources)
    for v in _VENDORS:
        present = set(per[v])
        for slug in PRIORITY.get(v, []):
            if (v, slug) not in seen and (slug in present or v in ("smartrecruiters", "recruitee")):
                seen.add((v, slug)); out.append((v, slug, 100))
    for v in _VENDORS:
        for slug in per[v]:
            if (v, slug) not in seen:
                seen.add((v, slug)); out.append((v, slug, 0))
    return out

# ---- feed fetch → normalized job dicts (full descriptions + fine-grained detail) ----
def _norm(vendor, slug, external_id, title, url, location, remote, desc,
          department="", team="", employment_type="", compensation="",
          updated_at="", posted_at="", meta=None):
    full = desc or ""
    return {
        "source_uid": f"{vendor}:{slug}:{external_id}",
        "vendor": vendor, "company_slug": slug, "external_id": str(external_id),
        "title": title or "", "url": url or "", "location": location or "",
        "remote": bool(remote) or ("remote" in (location or "").lower()),
        "description": full[:8000],
        "skills": extract_skills(full),
        "sponsorship": sponsorship(full),
        "country": geo.country_of(location or ""),   # normalized country for accurate location filtering
        # fine-grained detail for downstream matching / filtering / optimisation
        "department": (department or "")[:120],
        "team": (team or "")[:120],
        "employment_type": (employment_type or "")[:60],
        "compensation": (compensation or "")[:200],
        "updated_at": _iso_ts(updated_at) or "",
        "posted_at": posted_at or "",
        "meta": {k: v for k, v in (meta or {}).items() if v},   # raw extras kept as jsonb
    }

def _greenhouse(slug, timeout):
    data = json.loads(_http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout))
    for x in (data.get("jobs") or []):
        deps = [d.get("name") for d in (x.get("departments") or []) if d.get("name")]
        offs = [o.get("name") or o.get("location") for o in (x.get("offices") or []) if (o.get("name") or o.get("location"))]
        yield _norm("greenhouse", slug, x.get("id"), x.get("title"), x.get("absolute_url"),
                    (x.get("location") or {}).get("name", ""), False, _strip_html(x.get("content", "")),
                    department=", ".join(deps), updated_at=x.get("updated_at", ""),
                    posted_at=_iso_date(x.get("first_published") or x.get("updated_at", "")),
                    meta={"requisition_id": x.get("requisition_id"), "departments": deps, "offices": offs,
                          "internal_job_id": x.get("internal_job_id"), "data_compliance": x.get("data_compliance")})

def _lever(slug, timeout):
    data = json.loads(_http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout))
    for x in (data or []):
        cat = x.get("categories") or {}
        desc = x.get("descriptionPlain") or _strip_html(x.get("description", ""))
        yield _norm("lever", slug, x.get("id"), x.get("text"), x.get("hostedUrl"),
                    cat.get("location", ""), (x.get("workplaceType", "") or "").lower() == "remote", desc,
                    department=cat.get("department", ""), team=cat.get("team", ""),
                    employment_type=cat.get("commitment", ""),
                    updated_at=_ms_iso(x.get("updatedAt")), posted_at=_ms_date(x.get("createdAt")),
                    meta={"all_locations": cat.get("allLocations"), "workplace_type": x.get("workplaceType"),
                          "lists": [l.get("text") for l in (x.get("lists") or [])]})

def _ashby(slug, timeout):
    data = json.loads(_http_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout))
    for x in (data.get("jobs") or []):
        desc = x.get("descriptionPlain") or _strip_html(x.get("descriptionHtml", ""))
        comp = x.get("compensation") or {}
        comp_s = comp.get("compensationTierSummary") or comp.get("summary") or ""
        yield _norm("ashby", slug, x.get("id"), x.get("title"), x.get("jobUrl") or x.get("applyUrl"),
                    x.get("location", ""), bool(x.get("isRemote")), desc,
                    department=x.get("department", ""), team=x.get("team", ""),
                    employment_type=x.get("employmentType", ""), compensation=comp_s,
                    updated_at=x.get("updatedAt", ""), posted_at=_iso_date(x.get("publishedAt", "")),
                    meta={"secondary_locations": x.get("secondaryLocations"),
                          "should_display_comp": x.get("shouldDisplayCompensationOnJobBoard")})

def _smartrecruiters(slug, timeout):
    # public postings API. List is summary-only (no description) — crawl list pages and
    # construct the posting URL; cap per company so one big employer can't dominate a cycle.
    off = 0
    while True:
        data = json.loads(_http_get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={off}", timeout))
        items = data.get("content") or []
        for x in items:
            loc = x.get("location") or {}
            locstr = loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), (loc.get("country") or "").upper()]))
            yield _norm("smartrecruiters", slug, x.get("id"), x.get("name"),
                        f"https://jobs.smartrecruiters.com/{slug}/{x.get('id')}",
                        locstr, bool(loc.get("remote")), "",
                        department=(x.get("department") or {}).get("label", ""),
                        employment_type=(x.get("typeOfEmployment") or {}).get("label", ""),
                        posted_at=_iso_date(x.get("releasedDate", "")), updated_at=x.get("releasedDate", ""),
                        meta={"function": (x.get("function") or {}).get("label"),
                              "industry": (x.get("industry") or {}).get("label"),
                              "experience": (x.get("experienceLevel") or {}).get("label"),
                              "ref": x.get("refNumber"), "remote": loc.get("remote"), "hybrid": loc.get("hybrid")})
        total = data.get("totalFound", 0); off += len(items)
        if not items or off >= total or off >= 800:
            break

def _recruitee(slug, timeout):
    data = json.loads(_http_get(f"https://{slug}.recruitee.com/api/offers/", timeout))
    for x in (data.get("offers") or []):
        desc = _strip_html((x.get("description") or "") + " " + (x.get("requirements") or ""))
        loc = x.get("location") or ", ".join(filter(None, [x.get("city"), (x.get("country_code") or "").upper()]))
        yield _norm("recruitee", slug, x.get("id"), x.get("title"),
                    x.get("careers_apply_url") or x.get("careers_url"),
                    loc, bool(x.get("hybrid")) or "remote" in (loc or "").lower(), desc,
                    department=x.get("department", ""), employment_type=x.get("employment_type_code", ""),
                    posted_at=_iso_date(x.get("created_at", "")), updated_at=x.get("created_at", ""),
                    meta={"country_code": x.get("country_code"), "experience": x.get("experience_code"),
                          "education": x.get("education_code"), "company": x.get("company_name")})

_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
             "smartrecruiters": _smartrecruiters, "recruitee": _recruitee}

def fetch_feed(vendor, slug, timeout=30, since=None):
    """Return normalized postings for one company. Never raises — returns [].

    `since` (ISO timestamp) enables incremental fetch: only postings whose
    updated_at is newer than `since` are returned. Postings with no updated_at
    are always included (can't prove they're unchanged)."""
    fn = _FETCHERS.get(vendor)
    if not fn:
        return []
    try:
        out = [d for d in fn(slug, timeout) if d["url"] and d["external_id"] != "None"]
    except Exception:
        return []
    if since:
        out = [d for d in out if not d.get("updated_at") or d["updated_at"] >= since]
    return out
