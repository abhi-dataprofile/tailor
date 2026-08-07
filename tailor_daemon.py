#!/usr/bin/env python3
"""tailor_daemon.py — ONE background process that fully owns the job data.

This is the single thing you run; Tailor takes care of the rest on a loop:

  1. FETCH     worker.run_cycle()      — crawl due career pages (≤6h freshness),
                                         idempotent upsert (only new postings).
  2. MAINTAIN  maintain()              — data hygiene the crawler alone can't do:
                                         · close postings that vanished from feeds
                                           (is_open=false when not seen for 2× the
                                           freshness window)
                                         · prune ancient closed rows (>60d)
                                         · heartbeat into crawl_runs (observability)
  3. MATCH+TAILOR  pipeline.run()      — score new jobs per user, tailor top picks.
  4. AUTO-APPLY    apply.run()         — submit for opted-in users (guards intact).

Each phase is fault-isolated: one failing never stops the others. Ctrl-C to stop.

Run:   python3 tailor_daemon.py                 # the whole loop, forever
       python3 tailor_daemon.py --once          # single pass
       DAEMON_APPLY=0 python3 tailor_daemon.py