#!/usr/bin/env python3
"""backfill_req.py — compute eligibility flags (US citizenship / clearance / ITAR) for postings
already in the index and store them in jobs.meta.req. New postings get them at crawl time
(ats.requirements); this covers what was crawled before that existed.

    python3 backfill_req.py --days 30        # newest first, 1,000 rows per read, 500 per write
"""
import envload  # noqa
import argparse, datetime, time, sys
import ats, supabase_client as sb

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=30)
ap.add_argument("--sleep", type=float, default=0.4, help="pause between batches — be gentle on the DB")
a = ap.parse_args()
since = (datetime.date.today() - datetime.timedelta(days=a.days)).isoformat()
seen = 0; flagged = 0; t0 = time.time(); last_id = None
while True:
    q = {"select": "id,source_uid,vendor,company_slug,title,url,meta,description", "is_open": "eq.true",
         "posted_at": f"gte.{since}", "meta->req": "is.null", "order": "id.asc", "limit": "1000"}
    if last_id is not None:
        q["id"] = f"gt.{last_id}"
    try:
        rows = sb.select("jobs", q, timeout=60)
    except Exception as e:
        print("read failed, retrying in 10s:", str(e)[:120]); time.sleep(10); continue
    if not rows:
        break
    last_id = rows[-1]["id"]
    out = []
    for r in rows:
        req = ats.requirements(r.get("description") or "")
        meta = dict(r.get("meta") or {}); meta["req"] = req or {"none": True}   # {} would read as 'not computed'
        out.append({"source_uid": r["source_uid"], "vendor": r["vendor"], "company_slug": r["company_slug"],
                    "title": r["title"], "url": r["url"], "meta": meta})
        flagged += 1 if req else 0
    for i in range(0, len(out), 500):
        for attempt in range(3):
            try:
                sb.upsert("jobs", out[i:i+500], on_conflict="source_uid", update=True, minimal=True, timeout=90); break
            except Exception as e:
                print("write failed:", str(e)[:120]); time.sleep(5)
    seen += len(rows)
    print(f"{seen:>7} scanned · {flagged:>6} gated · {time.time()-t0:5.0f}s", flush=True)
    time.sleep(a.sleep)
print(f"done: {seen} postings, {flagged} with a citizenship / clearance / ITAR requirement")
