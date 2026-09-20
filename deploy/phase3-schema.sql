-- Phase 3 — what proactive automation needs on top of booking-schema.sql + agent-schema.sql.
-- Apply after both. Idempotent.

-- A notice goes to a PERSON, not to a role. Keyed only on (appointment_id, recipient, kind),
-- the send-once guard silently suppressed two things it should never have suppressed:
--   * after an appointment is reassigned, the NEW stylist's 'booked' notice looks like a
--     duplicate of the old stylist's and is never sent — they are never told they have a
--     client;
--   * when a waitlist offer expires and rolls to the next person, the second client's offer
--     looks like a duplicate of the first's and is never sent, so the queue silently stalls.
-- Adding who it was for fixes both, and keeps the first recipient's audit trail intact.
ALTER TABLE notification_log ADD COLUMN IF NOT EXISTS recipient_ref text;

UPDATE notification_log SET recipient_ref = 'legacy' WHERE recipient_ref IS NULL;
ALTER TABLE notification_log ALTER COLUMN recipient_ref SET DEFAULT 'unknown';
ALTER TABLE notification_log ALTER COLUMN recipient_ref SET NOT NULL;

ALTER TABLE notification_log DROP CONSTRAINT IF EXISTS notification_once;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'notification_once_per_person') THEN
    ALTER TABLE notification_log
      ADD CONSTRAINT notification_once_per_person
      UNIQUE (appointment_id, recipient, recipient_ref, kind);
  END IF;
END $$;

-- Waitlist bookkeeping the backfill needs:
--   * a deterministic queue order, so two runs on the same data pick the same person;
--   * "this entry already had its chance at this freed slot", so an expired offer rolls
--     forward instead of looping back to the same person.
CREATE INDEX IF NOT EXISTS waitlist_queue_idx
  ON waitlist_entry (service_id, created_at, id) WHERE status = 'waiting';
CREATE INDEX IF NOT EXISTS waitlist_offered_idx
  ON waitlist_entry (offered_appointment_id) WHERE status = 'offered';
