"""supabase_client.py — tiny PostgREST client (stdlib only).

Credentials come from the environment, never from code or the browser
(least-privilege: the service key stays server-side):
    SUPABASE_URL          e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  the service_role key (Project Settings → API)

is_configured() lets callers degrade gracefully when Supabase isn't set up
yet, so the rest of the app keeps working.
"""
import envload  # noqa: F401 — ensure .env is loaded no matter who imports us first
import json, os, urllib.request, urllib.parse, urllib.error

URL  = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
KEY  = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY") or ""
ANON = os.environ.get("SUPABASE_ANON_KEY") or ""     # public key, safe for the browser

def is_configured():
    return bool(URL and KEY)

def auth_enabled():
    return bool(URL and ANON)

def auth_user(token):
    """Resolve a GoTrue access token → user dict (id, email), or None. Used to
    scope requests to the signed-in user (multi-tenant)."""
    if not (token and URL and ANON):
        return None
    try:
        req = urllib.request.Request(URL + "/auth/v1/user",
                                     headers={"apikey": ANON, "Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None

def _headers(extra=None):
    h = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h

def _request(method, path, params=None, body=None, prefer=None, timeout=45):
    if not is_configured():
        raise RuntimeError("Supabase not configured (set SUPABASE_URL and SUPABASE_SERVICE_KEY)")
    url = URL + "/rest/v1/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="*,.()")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=_headers({"Prefer": prefer} if prefer else None))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Supabase {e.code}: {detail}")

def select(table, params=None, timeout=45):
    return _request("GET", table, params=params, timeout=timeout) or []

def upsert(table, rows, on_conflict, update=True):
    """Idempotent insert. update=False → 'ignore-duplicates' (only genuinely new rows)."""
    if not rows:
        return []
    resolution = "merge-duplicates" if update else "ignore-duplicates"
    return _request("POST", table, params={"on_conflict": on_conflict},
                    body=rows, prefer=f"resolution={resolution},return=representation") or []

def update(table, params, patch, minimal=False):
    # minimal=True → Prefer: return=minimal (no body echoed back). Essential for bulk
    # updates so the server doesn't ship every updated row (with big columns) back.
    prefer = "return=minimal" if minimal else "return=representation"
    return _request("PATCH", table, params=params, body=patch, prefer=prefer) or []

def insert(table, rows, return_rep=True):
    return _request("POST", table, body=rows,
                    prefer="return=representation" if return_rep else "return=minimal")
