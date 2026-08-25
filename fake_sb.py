"""fake_sb.py — an in-memory stand-in for supabase_client, for tests.

Implements just enough PostgREST semantics for the engine's real queries (eq / lt / is.null /
in.(…) / the nested or(...and(...)) claim filter, plus on_conflict upsert with merge- vs
ignore-duplicates). That's what lets `apply_one`, `_claim` and `_record` run for real —
orchestration, claim races, status timelines — without a live database.

Install with `fake_sb.install()`, which patches the module object every caller imported.
"""
import re, itertools

TABLES = {}
_ids = itertools.count(1)

def reset():
    TABLES.clear()

def _rows(table):
    return TABLES.setdefault(table, [])

# ---------- filter matching ----------
def _s(v):
    """Render a Python value the way PostgREST compares it (True → 'true', not 'True')."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)

def _cmp(val, expr):
    """Apply one PostgREST filter expression to a value."""
    if expr.startswith("eq."):   return _s(val) == expr[3:]
    if expr.startswith("neq."):  return _s(val) != expr[4:]
    if expr.startswith("lt."):   return val is not None and str(val) < expr[3:]
    if expr.startswith("gt."):   return val is not None and str(val) > expr[3:]
    if expr.startswith("lte."):  return val is not None and str(val) <= expr[4:]
    if expr.startswith("gte."):  return val is not None and str(val) >= expr[4:]
    if expr == "is.null":        return val is None
    if expr == "not.is.null":    return val is not None
    if expr.startswith("in."):
        inner = expr[3:].strip("()")
        return _s(val) in [x.strip().strip('"') for x in inner.split(",") if x.strip()]
    return True

def _split_top(s):
    """Split a PostgREST boolean list on commas that are NOT inside parentheses."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(": depth += 1
        elif ch == ")": depth -= 1
        if ch == "," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out if x.strip()]

def _bool_match(row, expr):
    """Evaluate `and(a.eq.1,b.lt.2)` / `or(...)` / a bare `col.op.value`."""
    expr = expr.strip()
    m = re.match(r"^(and|or)\((.*)\)$", expr, re.S)
    if m:
        kind, inner = m.group(1), m.group(2)
        parts = [_bool_match(row, p) for p in _split_top(inner)]
        return all(parts) if kind == "and" else any(parts)
    col, _, rest = expr.partition(".")
    return _cmp(row.get(col), rest)

def _match(row, params):
    for k, v in (params or {}).items():
        if k in ("select", "order", "limit", "offset", "on_conflict"):
            continue
        if k == "or":
            if not _bool_match(row, "or" + v if not v.startswith("(") else "or" + v):
                return False
            continue
        if k == "and":
            if not _bool_match(row, "and" + v):
                return False
            continue
        if not _cmp(row.get(k), str(v)):
            return False
    return True

# ---------- the supabase_client API ----------
FAIL = {"on": False, "detail": "Supabase 522: error code: 522"}

def _guard():
    if FAIL["on"]:
        raise RuntimeError(FAIL["detail"])

def is_configured():
    return True

def select(table, params=None, timeout=45):
    _guard()
    out = [dict(r) for r in _rows(table) if _match(r, params)]
    lim = (params or {}).get("limit")
    return out[:int(lim)] if lim else out

def upsert(table, rows, on_conflict, update=True):
    """Returns the rows actually written — [] when update=False and they all already exist
    (that's exactly how _claim detects 'someone else owns this')."""
    _guard()
    keys = [k.strip() for k in on_conflict.split(",")]
    written = []
    for new in rows:
        existing = next((r for r in _rows(table)
                         if all(_s(r.get(k)) == _s(new.get(k)) for k in keys)), None)
        if existing is None:
            row = dict(new); row.setdefault("id", next(_ids))
            _rows(table).append(row); written.append(dict(row))
        elif update:
            existing.update(new); written.append(dict(existing))
        # update=False + exists → ignored, NOT written (no entry in `written`)
    return written

def update(table, params, patch, minimal=False):
    _guard()
    hit = [r for r in _rows(table) if _match(r, params)]
    for r in hit:
        r.update(patch)
    return [] if minimal else [dict(r) for r in hit]

def insert(table, rows, return_rep=True):
    _guard()
    out = []
    for new in rows:
        row = dict(new); row.setdefault("id", next(_ids))
        _rows(table).append(row); out.append(dict(row))
    return out if return_rep else []

def auth_user(token):
    return None

def auth_enabled():
    return False

def install():
    """Point every module that did `import supabase_client as sb` at this fake."""
    import sys, supabase_client
    for name in ("select", "upsert", "update", "insert", "is_configured", "auth_user", "auth_enabled"):
        setattr(supabase_client, name, globals()[name])
    return supabase_client
