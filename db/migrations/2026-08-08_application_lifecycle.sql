-- ============================================================
--  Migration: honest application lifecycle
--  Run once in Supabase → SQL Editor. Safe to re-run (idempotent).
--
--  WHY: the old status set couldn't tell "we clicked submit" apart from
--  "the employer confirmed receipt", so the UI showed a "submitted" it
--  couldn't stand behind. This widens applications.status to the real
--  lifecycle and adds confirmed_at. Existing rows are remapped in place.
-- ============================================================

-- 1) widen the CHECK to the new vocabulary (legacy values kept valid)
alter table applications drop constraint if exists applications_status_check;
alter table applications
  add constraint applications_status_check check (status in (
    'draft','queued','filling','needs_you','blocked_captcha',
    'submitted_unconfirmed','confirmed','failed_transient','failed_permanent',
    -- legacy, so pre-migration rows remain valid:
    'awaiting_review','approved','submitted','failed','manual'
  ));

-- 2) new column: when a success page / confirmation email verified the send
alter table applications add column if not exists confirmed_at timestamptz;

-- 3) remap existing rows to the new vocabulary.
--    NB: old 'submitted' was NEVER verified, so it becomes *unconfirmed* (honest).
update applications set status = case status
  when 'submitted'        then 'submitted_unconfirmed'
  when 'manual'           then 'blocked_captcha'
  when 'awaiting_review'  then 'needs_you'
  when 'approved'         then 'confirmed'
  when 'failed'           then 'failed_transient'
  else status
end
where status in ('submitted','manual','awaiting_review','approved','failed');
