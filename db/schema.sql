-- ============================================================
--  Resume Tailor / "Tsenta" — Supabase (Postgres) schema
--  Run once in Supabase → SQL Editor (or `psql`).
--
--  Design notes (distributed-systems principles):
--   * jobs.source_uid is the IDEMPOTENCY KEY. Ingestion is an
--     UPSERT on it, so re-crawling a company inserts only NEW
--     postings and refreshes last_seen_at on existing ones —
--     "fetch only new records" is enforced by the unique key,
--     not by fragile client bookkeeping (at-least-once safe).
--   * Write path (crawler) and read path (API/UI) are separated
--     and share only this schema as the single source of truth.
--   * Per-user state (saved/applied) lives in user_jobs, keyed by
--     (user_id, job_id) so the canonical job row stays shared.
-- ============================================================

create extension if not exists pg_trgm;      -- fast title search (ILIKE / trigram)

-- ---------- companies to crawl (seeded from bundled ATS datasets) ----------
create table if not exists companies (
  id              bigint generated always as identity primary key,
  vendor          text    not null check (vendor in ('greenhouse','lever','ashby')),
  slug            text    not null,
  priority        int     not null default 0,          -- well-known firms crawled first
  active          boolean not null default true,
  last_crawled_at timestamptz,                          -- drives the 6h incremental schedule
  last_status     text,                                 -- 'ok' | 'error:<detail>'
  fail_count      int     not null default 0,           -- backoff / auto-disable bad feeds
  created_at      timestamptz not null default now(),
  unique (vendor, slug)
);
create index if not exists companies_due_idx on companies (last_crawled_at nulls first) where active;

-- ---------- canonical job postings (deduplicated by source_uid) ----------
create table if not exists jobs (
  id            bigint generated always as identity primary key,
  source_uid    text    not null unique,                -- '<vendor>:<slug>:<external_id>'  (idempotency key)
  vendor        text    not null,
  company_slug  text    not null,
  external_id   text,
  title         text    not null,
  location      text,
  remote        boolean not null default false,
  url           text    not null,
  description   text,
  skills        text[]  not null default '{}',          -- extracted server-side at ingest
  sponsorship   text    not null default 'unknown' check (sponsorship in ('yes','no','unknown')),
  posted_at     date,
  -- fine-grained detail for matching / filtering / optimisation
  department      text,
  team            text,
  employment_type text,
  compensation    text,
  source_updated_at timestamptz,                          -- the posting's own updated_at (incremental key)
  meta          jsonb   not null default '{}',            -- raw provider extras (offices, all_locations, req id…)
  first_seen_at timestamptz not null default now(),      -- processing watermark for "new only"
  last_seen_at  timestamptz not null default now(),
  is_open       boolean not null default true            -- flipped false when it drops off the feed
);
create index if not exists jobs_posted_idx  on jobs (posted_at desc nulls last);
create index if not exists jobs_open_idx    on jobs (is_open) where is_open;
create index if not exists jobs_seen_idx    on jobs (first_seen_at desc);
create index if not exists jobs_skills_idx  on jobs using gin (skills);
create index if not exists jobs_title_trgm  on jobs using gin (title gin_trgm_ops);

-- ---------- user profiles (single 'local' user by default; ready for multi-user) ----------
create table if not exists profiles (
  user_id     text primary key default 'local',
  name        text,
  email       text,
  title       text,
  contact     text,
  summary     text,
  skills      text[] not null default '{}',
  memory      jsonb  not null default '{}'::jsonb,       -- tone/length/targets/etc.
  data        jsonb  not null default '{}'::jsonb,       -- full profile blob (experience, projects, education)
  updated_at  timestamptz not null default now()
);

-- ---------- per-user job state + cached match score (LinkedIn-style pipeline) ----------
create table if not exists user_jobs (
  user_id    text   not null,
  job_id     bigint not null references jobs(id) on delete cascade,
  status     text   not null default 'new' check (status in ('new','saved','applied','dismissed')),
  score      int,                                        -- cached profile↔job match %
  note       text,
  updated_at timestamptz not null default now(),
  primary key (user_id, job_id)
);
create index if not exists user_jobs_status_idx on user_jobs (user_id, status);

-- ---------- per-user tailored output (résumé rewrite for a specific job) ----------
create table if not exists tailorings (
  user_id     text   not null,
  job_id      bigint not null references jobs(id) on delete cascade,
  summary     text,
  bullets     jsonb,                                    -- rewritten experience bullets
  resume_html text,
  provider    text,                                     -- which LLM produced it (claude/gemini/openai/custom/ollama)
  created_at  timestamptz not null default now(),
  primary key (user_id, job_id)
);

-- ---------- per-user application record (with human-in-the-loop state) ----------
create table if not exists applications (
  user_id       text   not null,
  job_id        bigint not null references jobs(id) on delete cascade,
  status        text   not null default 'draft'
                 check (status in ('draft','awaiting_review','approved','submitted','failed','manual')),
  human_in_loop boolean not null default true,          -- pause for the user unless they opted out
  answers       jsonb,                                  -- filled application questions
  resume_html   text,                                   -- EXACT résumé submitted (snapshot, survives re-tailoring)
  receipt       jsonb,                                  -- submission result / confirmation
  attempts      int     not null default 0,             -- retry bookkeeping
  next_retry_at timestamptz,                             -- when a transient failure is eligible again
  submitted_at  timestamptz,
  created_at    timestamptz not null default now(),
  primary key (user_id, job_id)
);
create index if not exists applications_retry_idx on applications (status, next_retry_at);

-- ---------- crawl audit log (observability) ----------
create table if not exists crawl_runs (
  id           bigint generated always as identity primary key,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  companies    int,
  new_jobs     int,
  updated_jobs int,
  errors       int
);

-- ============================================================
--  MULTI-TENANT ISOLATION (enable when you add Supabase Auth)
--  Each user sees ONLY their own rows. `jobs`/`companies` are a
--  shared catalog (readable by all authed users, written only by
--  the operator's service-key pipelines). Uncomment to turn on.
-- ============================================================
-- alter table profiles     enable row level security;
-- alter table user_jobs    enable row level security;
-- alter table tailorings   enable row level security;
-- alter table applications enable row level security;
-- -- own-rows-only policies (user_id stores auth.uid()::text):
-- create policy own_profile     on profiles     using (user_id = auth.uid()::text) with check (user_id = auth.uid()::text);
-- create policy own_user_jobs   on user_jobs    using (user_id = auth.uid()::text) with check (user_id = auth.uid()::text);
-- create policy own_tailorings  on tailorings   using (user_id = auth.uid()::text) with check (user_id = auth.uid()::text);
-- create policy own_applications on applications using (user_id = auth.uid()::text) with check (user_id = auth.uid()::text);
-- -- shared read-only catalog:
-- alter table jobs enable row level security;
-- create policy read_jobs on jobs for select using (auth.role() = 'authenticated');

-- ---------- billing / plans (added by contacts+billing+inbox increment) ----------
alter table profiles add column if not exists plan text not null default 'free';
alter table profiles add column if not exists plan_status text;
alter table profiles add column if not exists stripe_customer_id text;

-- ---------- inbound emails (per-user inbox + OTP autofill) ----------
create table if not exists emails (
  id          bigint generated always as identity primary key,
  user_id     text not null,
  from_addr   text,
  subject     text,
  body        text,
  otp         text,
  received_at timestamptz not null default now()
);
create index if not exists emails_user_idx on emails (user_id, received_at desc);
