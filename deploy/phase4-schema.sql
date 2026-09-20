-- Phase 4 — what review capture needs. Apply after phase3-schema.sql. Idempotent.

-- One review per appointment, enforced by the database rather than by a check-then-insert.
-- Two replies arriving together ("5, lovely" then "actually 2, it was bad") would otherwise
-- race past an application-level guard and store both, and the manager would be told twice
-- about one haircut.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'review_once_per_appointment') THEN
    -- Keep the first review if duplicates already exist, so the constraint can be added.
    DELETE FROM review r USING review r2
     WHERE r.appointment_id = r2.appointment_id AND r.id > r2.id;
    ALTER TABLE review ADD CONSTRAINT review_once_per_appointment UNIQUE (appointment_id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS review_stylist_idx ON review (stylist_id, created_at DESC);
