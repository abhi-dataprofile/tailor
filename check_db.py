#!/usr/bin/env python3
"""check_db.py — is the database reachable, and does it have everything the app needs?

Run this right after pointing .env at a new Supabase project:

    .venv/bin/python check_db.py

It checks connectivity, that every table exists, that the columns the engine writes
are present, and prints row counts. Read-only — it never writes or drops anything.
"""
import sys, time
import envload  # noqa: F401
import supabase_client as sb

# table -> columns the app actually depends on
NEEDED = {
    "companies":    ["vendor", "slug", "active", "last_crawled_at", "fail_count", "priority"],
    "jobs":         ["source_uid", "vendor", "company_slug", "title", "url", "skills", "country", "is_open"],
    "profiles":     ["user_id", "data", "skills", "plan"],
    "user_jobs":    ["user_id", "job_id", "status", "score"],
    "tailorings":   ["user_id", "job_id", "resume_html"],
    "applications": ["user_id", "job_id", "status", "answers", "receipt", "attempts",
                     "next_retry_at", "submitted_at", "confirmed_at", "claimed_at", "resume_html"],
    "crawl_runs":   ["new_jobs", "updated_jobs", "errors"],
    "emails":       ["user_id", "received_at"],
}

def main():
    if not sb.is_configured():
        sys.exit("  SUPABASE_URL / SUPABASE_SERVICE_KEY are not set in .env")
    print("─" * 60)
    print(f"  {sb.URL}")
    print("─" * 60)
    t0 = time.time()
    try:
        sb.select("companies", {"select": "vendor", "limit": "1"}, timeout=20)
        print(f"  ✅ reachable ({time.time()-t0:.1f}s)\n")
    except Exception as e:
        print(f"  ❌ NOT reachable after {time.time()-t0:.1f}s — {str(e)[:90]}")
        print("\n  A 5xx/522 means the project itself is unhealthy (restart it in the")
        print("  dashboard). A 401 means the service key in .env is wrong.")
        sys.exit(1)

    bad = 0
    for table, cols in NEEDED.items():
        try:
            sb.select(table, {"select": ",".join(cols), "limit": "1"}, timeout=20)
        except Exception as e:
            msg = str(e)
            if "does not exist" in msg or "42P01" in msg:
                print(f"  ❌ {table:13} MISSING — run db/setup_fresh.sql")
            else:
                print(f"  ❌ {table:13} {msg[:70]}")
            bad += 1
            continue
        try:                                    # exact count, cheap via a head request
            n = len(sb.select(table, {"select": "*", "limit": "1000"}, timeout=25))
            n = f"{n}+" if n == 1000 else str(n)
        except Exception:
            n = "?"
        print(f"  ✅ {table:13} ok · {n} rows")

    print("─" * 60)
    if bad:
        print(f"  {bad} table(s) not ready — open db/setup_fresh.sql, paste it into")
        print("  the Supabase SQL Editor, and run it. Then re-run this check.")
        sys.exit(1)
    print("  Everything the app needs is present. Start it with ./run.sh --workers")
    print("─" * 60)

if __name__ == "__main__":
    main()
