#!/usr/bin/env python3
"""worker.py — incremental ATS crawler.

Keeps the `jobs` table in Supabase continuously fresh (≤ JOB_REFRESH_HOURS,
default 1h) using a due-time work queue instead of one giant periodic sweep:

  * Companies are seeded once from the bundled dataset (idempotent).
  * Each cycle pulls the N *most-due* companies (never crawled, or older than
    the freshness window), fetches their public feeds concurrently, and UPSERTs
    postings keyed by source_uid — so only genuinely NEW postings create rows
    (at-least-once safe / idempotent). Re-seen postings just refresh last_seen_at.
  * Per-company failures are isolated and counted (backoff / auto-disable),
    so one bad feed never fails the batch.
  * Load is spread across time → steady, rate-limited throughput that cycles
    the whole ~15.9k dataset well within the freshness window.

Run:   python3 worker.py            # continuous
       python3 worker.py --once     # a single cycle (e.g. from cron/systemd timer)
       python3 worker.py --dry      # crawl a sample and print, no DB writes
"""
import envload  # noqa: F401 — loads .env
import os, sys, time, argparse, datetime
from concurrent.futures import ThreadPoolExecutor
import ats
import supabase_client as sb

FRESH_HOURS = int(os.environ.get("JOB_REFRESH_HOURS", "1"))   # re-crawl each company at least hourly
BATCH       = int(os.environ.get("CRAWL_BATCH", "90"))     # companies per cycle (90/20s ≈ 4.5/s → ~16k/hr)
CONCURRENCY = int(os.environ.get("CRAWL_CONCURRENCY", "16"))
SLEEP       = int(os.environ.get("CRAWL_SLEEP", "20"))     # seconds between cycles
MAX_FAILS   = 5                                            # auto-disable a feed after N straight errors

def _now():
    return datetime.datetime.now(datetime.timezone.utc)

def _iso(dt):
    return dt.isoformat()

def seed_companies():
    rows = [{"vendor": v, "slug": s, "priority": p} for (v, s, p) in ats.load_companies()]
    for i in range(0, len(rows), 500):
        sb.upsert("companies", rows[i:i + 500], on_conflict="vendor,slug", update=False)
    print(f"[seed] {len(rows)} companies ensured")

def due_companies(limit):
    cutoff = _iso(_now() - datetime.timedelta(hours=FRESH_HOURS))
    return sb.select("companies", {
        "select": "id,vendor,slug,last_crawled_at",
        "active": "eq.true",
        "or": f"(last_crawled_at.is.null,last_crawled_at.lt.{cutoff})",
        "order": "priority.desc,last_crawled_at.asc.nullsfirst",
        "limit": str(limit),
    })

def crawl_company(c):
    """Fetch one company's feed (incrementally — only postings changed since we last crawled)."""
    try:
        # fetch the full feed to catch brand-new postings; `since` trims re-processing of unchanged ones
        jobs = ats.fetch_feed(c["vendor"], c["slug"])
        return (c, jobs, True, "ok")
    except Exception as e:
        return (c, [], False, "error:" + str(e)[:120])

def _row(j, now):
    return {
        "source_uid": j["source_uid"], "vendor": j["vendor"], "company_slug": j["company_slug"],
        "external_id": j["external_id"], "title": j["title"], "location": j["location"],
        "country": j.get("country") or "", "remote": j["remote"], "url": j["url"], "description": j["description"],
        "skills": j["skills"], "sponsorship": j["sponsorship"], "posted_at": j.get("posted_at"),
        "department": j.get("department"), "team": j.get("team"),
        "employment_type": j.get("employment_type"), "compensation": j.get("compensation"),
        "source_updated_at": (j.get("updated_at") or None), "meta": j.get("meta") or {},
        "last_seen_at": now, "is_open": True,
    }

def persist(company, jobs, ok, detail):
    now = _iso(_now())
    last = company.get("last_crawled_at")
    new_count = 0
    if ok and jobs:
        payload = [_row(j, now) for j in jobs]
        # 1) insert brand-new postings only (ignore-duplicates) → exact "new" count, cheap
        inserted = sb.upsert("jobs", payload, on_conflict="source_uid", update=False)
        new_count = len(inserted or [])
        # 2) content refresh: only rows actually changed since we last crawled this company
        changed = [r for r in payload
                   if not last or not r.get("source_updated_at") or str(r["source_updated_at"]) >= str(last)]
        if changed:
            sb.upsert("jobs", changed, on_conflict="source_uid", update=True)
    patch = {"last_crawled_at": now, "last_status": detail}
    if ok:
        patch["fail_count"] = 0
    sb.update("companies", {"id": f"eq.{company['id']}"}, patch)
    if not ok:
        # backoff: bump fail_count; auto-disable after MAX_FAILS
        rows = sb.select("companies", {"select": "fail_count", "id": f"eq.{company['id']}"})
        fc = (rows[0]["fail_count"] if rows else 0) + 1
        sb.update("companies", {"id": f"eq.{company['id']}"},
                  {"fail_count": fc, "active": fc < MAX_FAILS})
    return new_count, len(jobs)

def run_cycle():
    companies = due_companies(BATCH)
    if not companies:
        return 0, 0, 0
    new_total = seen_total = errors = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        results = list(ex.map(crawl_company, companies))
    for (c, jobs, ok, detail) in results:
        if not ok:
            errors += 1
        n, seen = persist(c, jobs, ok, detail)
        new_total += n; seen_total += seen
    return new_total, seen_total, errors

# ---------- dry mode: no Supabase, just prove the crawl+extract works ----------
def dry():
    sample = [c for c in ats.load_companies() if c[2] > 0][:6]
    print(f"[dry] crawling {len(sample)} priority companies (no DB writes)\n")
    for (v, s, _) in sample:
        jobs = ats.fetch_feed(v, s)
        print(f"  {v}/{s}: {len(jobs)} postings")
        for j in jobs[:2]:
            print(f"    - {j['title'][:52]:52} | {j.get('posted_at')} | visa={j['sponsorship']:7} | skills={j['skills'][:6]}")
    print("\n[dry] OK — set SUPABASE_URL + SUPABASE_SERVICE_KEY to persist.")

def backfill_country():
    """One-time: set jobs.country on existing rows from their location text.
    Resumable (only touches rows where country is null) and retries transient blips."""
    import geo
    done = 0
    while True:
        rows = sb.select("jobs", {"select": "id,location", "country": "is.null", "limit": "1000"})
        if not rows:
            break
        buckets = {}
        for r in rows:
            buckets.setdefault(geo.country_of(r.get("location") or ""), []).append(r["id"])
        for country, ids in buckets.items():
            for i in range(0, len(ids), 300):
                chunk = ids[i:i + 300]
                for attempt in range(5):
                    try:
                        sb.update("jobs", {"id": f"in.({','.join(map(str, chunk))})"},
                                  {"country": country}, minimal=True)   # no huge response body
                        break
                    except Exception as e:
                        if attempt == 4:
                            raise
                        print(f"[backfill] retry ({e.__class__.__name__}) in {2*(attempt+1)}s…")
                        time.sleep(2 * (attempt + 1))
                done += len(chunk)
        print(f"[backfill] country set on {done} rows…")
    print(f"[backfill] done — {done} rows updated")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--dry", action="store_true", help="crawl a sample and print, no DB writes")
    ap.add_argument("--backfill", action="store_true", help="set country on existing rows from location, then exit")
    args = ap.parse_args()
    if args.backfill:
        if not sb.is_configured():
            print("Supabase not configured."); return
        return backfill_country()

    if args.dry or not sb.is_configured():
        if not sb.is_configured() and not args.dry:
            print("Supabase not configured — running --dry. Set SUPABASE_URL + SUPABASE_SERVICE_KEY to persist.\n")
        return dry()

    seed_companies()
    if args.once:
        n, seen, err = run_cycle()
        print(f"[cycle] new={n} seen={seen} errors={err}")
        return
    print(f"[worker] running · freshness={FRESH_HOURS}h · batch={BATCH} · every {SLEEP}s (Ctrl-C to stop)")
    while True:
        try:
            n, seen, err = run_cycle()
            ts = _now().strftime("%H:%M:%S")
            print(f"[{ts}] new={n} seen={seen} errors={err}")
            time.sleep(SLEEP if seen else max(SLEEP, 120))  # idle longer when everything's fresh
        except KeyboardInterrupt:
            print("\n[worker] stopped."); return
        except Exception as e:
            print("[worker] cycle error:", str(e)[:160]); time.sleep(SLEEP)

if __name__ == "__main__":
    main()
