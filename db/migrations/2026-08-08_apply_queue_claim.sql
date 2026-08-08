-- ============================================================
--  Migration: durable auto-apply queue (worker claim-lock)
--  Run once in Supabase → SQL Editor. Safe to re-run.
--
--  Adds applications.claimed_at so a worker can atomically claim a job
--  (status='filling') before working it — two overlapping runs can't
--  double-apply, and a crashed worker's stale claim is reclaimable.
--
--  Fully optional: the app degrades to its prior behavior until this runs.
-- ============================================================
alter table applications add column if not exists claimed_at timestamptz;

-- index the queue-claim lookup (find retryable / stale-filling rows fast)
create index if not exists applications_claim_idx on applications (status, claimed_at);
