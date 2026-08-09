-- ============================================================
--  Migration: additional clean-API ATS sources (SmartRecruiters, Recruitee)
--  Run once in Supabase → SQL Editor. Safe to re-run.
--
--  Widens companies.vendor so the crawler can seed + crawl these sources.
--  (jobs.vendor is already free text — no change needed there.)
-- ============================================================
alter table companies drop constraint if exists companies_vendor_check;
alter table companies
  add constraint companies_vendor_check
  check (vendor in ('greenhouse','lever','ashby','smartrecruiters','recruitee'));
