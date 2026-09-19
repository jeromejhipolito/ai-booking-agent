-- Salon booking schema.
--
-- Design rule that drives everything here: **booking correctness is the database's job.**
-- The chat agent (phase 2+) is an LLM and will occasionally be wrong; the EXCLUDE constraint
-- on `appointment` cannot be. Two confirmed appointments can never overlap for one stylist,
-- no matter what the model, a race between two chats, or a hand-written query tries to do.
--
-- Time model: every instant is `timestamptz` (stored UTC). Business hours are salon-LOCAL
-- wall-clock (`time`), converted with `AT TIME ZONE 'Asia/Manila'` at query time, so the
-- salon keeps opening at 09:00 local across DST changes in any other zone.
--
-- Apply after init-db.sql (which creates the extensions). Idempotent — safe to re-run.

-- ---------------------------------------------------------------------------
-- people + catalogue
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stylist (
  id               text PRIMARY KEY,                    -- 'sty_maria'
  name             text NOT NULL,
  title            text,                                -- 'Senior Colourist'
  bio              text,
  rating           numeric(2,1) CHECK (rating >= 0 AND rating <= 5),
  telegram_chat_id text,                                -- where reminders go (phase 3)
  active           boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS service (
  id           text PRIMARY KEY,                        -- 'svc_haircut'
  name         text NOT NULL,
  duration_min integer NOT NULL CHECK (duration_min > 0 AND duration_min <= 600),
  price_php    numeric(10,2) NOT NULL CHECK (price_php >= 0),
  description  text,
  active       boolean NOT NULL DEFAULT true
);

-- Which stylist can perform which service. A service nobody performs is bookable by nobody.
CREATE TABLE IF NOT EXISTS stylist_service (
  stylist_id text NOT NULL REFERENCES stylist(id) ON DELETE CASCADE,
  service_id text NOT NULL REFERENCES service(id) ON DELETE CASCADE,
  PRIMARY KEY (stylist_id, service_id)
);

-- Per-stylist opening hours, in SALON-LOCAL wall clock. weekday uses Postgres DOW: 0 = Sunday.
-- A weekday with no row = that stylist does not work that day.
CREATE TABLE IF NOT EXISTS business_hours (
  stylist_id text NOT NULL REFERENCES stylist(id) ON DELETE CASCADE,
  weekday    smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  opens_at   time NOT NULL,
  closes_at  time NOT NULL,
  PRIMARY KEY (stylist_id, weekday),
  CONSTRAINT business_hours_open_before_close CHECK (closes_at > opens_at)
);

CREATE TABLE IF NOT EXISTS client (
  id              text PRIMARY KEY,                     -- 'cli_<random>'
  name            text NOT NULL,
  phone           text,
  channel         text,                                 -- 'telegram' | 'viber' | 'walk_in'
  channel_user_id text,                                 -- the platform's user id
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One client row per (channel, channel_user_id) so a returning chat user is recognised.
-- Partial: walk-ins carry no channel identity and must not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS client_channel_identity_uidx
  ON client (channel, channel_user_id)
  WHERE channel IS NOT NULL AND channel_user_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- appointments — the double-booking guard lives here
-- ---------------------------------------------------------------------------

-- Human-facing booking references come from a sequence, not from random(): a client reads
-- "BK-00042" back over the phone, and a collision on the UNIQUE ref can never happen.
CREATE SEQUENCE IF NOT EXISTS appointment_ref_seq START 1;

CREATE TABLE IF NOT EXISTS appointment (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ref         text NOT NULL UNIQUE,                     -- human-facing, e.g. 'BK-00042'
  booking_key text UNIQUE,                              -- caller-supplied idempotency key
  client_id   text NOT NULL REFERENCES client(id),
  stylist_id  text NOT NULL REFERENCES stylist(id),
  service_id  text NOT NULL REFERENCES service(id),
  starts_at   timestamptz NOT NULL,
  ends_at     timestamptz NOT NULL,
  status      text NOT NULL DEFAULT 'confirmed'
              CHECK (status IN ('confirmed','cancelled','completed','no_show')),
  source      text NOT NULL DEFAULT 'chat',             -- 'chat' | 'seed' | 'reassign'
  notes       text,
  cancelled_at   timestamptz,
  cancel_reason  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT appointment_ends_after_starts CHECK (ends_at > starts_at),

  -- THE guard. `tstzrange(a,b)` defaults to '[)' — half-open — so a 10:00-11:00 booking and an
  -- 11:00-12:00 booking do NOT overlap and back-to-back slots stay bookable. Only CONFIRMED rows
  -- participate, so cancelling genuinely frees the window.
  CONSTRAINT appointment_no_double_booking
    EXCLUDE USING gist (
      stylist_id WITH =,
      tstzrange(starts_at, ends_at) WITH &&
    ) WHERE (status = 'confirmed')
);

CREATE INDEX IF NOT EXISTS appointment_stylist_window_idx
  ON appointment (stylist_id, starts_at) WHERE status = 'confirmed';
CREATE INDEX IF NOT EXISTS appointment_client_idx ON appointment (client_id, starts_at DESC);

-- ---------------------------------------------------------------------------
-- phase 3+ tables — created now so the schema is applied once, used later
-- ---------------------------------------------------------------------------

-- A client who wanted a full slot, so a cancellation can be offered on to them in order.
CREATE TABLE IF NOT EXISTS waitlist_entry (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  client_id              text NOT NULL REFERENCES client(id),
  service_id             text NOT NULL REFERENCES service(id),
  preferred_stylist_id   text REFERENCES stylist(id),
  window_start           timestamptz NOT NULL,
  window_end             timestamptz NOT NULL,
  status                 text NOT NULL DEFAULT 'waiting'
                         CHECK (status IN ('waiting','offered','accepted','declined','expired','cancelled')),
  offered_appointment_id bigint REFERENCES appointment(id) ON DELETE SET NULL,
  offer_expires_at       timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT waitlist_window_valid CHECK (window_end > window_start)
);

CREATE INDEX IF NOT EXISTS waitlist_open_idx
  ON waitlist_entry (service_id, window_start) WHERE status = 'waiting';

CREATE TABLE IF NOT EXISTS review (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  appointment_id bigint REFERENCES appointment(id) ON DELETE SET NULL,
  client_id      text NOT NULL REFERENCES client(id),
  stylist_id     text REFERENCES stylist(id),
  rating         smallint CHECK (rating BETWEEN 1 AND 5),
  comment        text,
  sentiment      text CHECK (sentiment IN ('praise','neutral','complaint')),
  escalated_at   timestamptz,                           -- when the manager was told
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Send-exactly-once ledger for every outbound notice (phase 3 reminders rely on the UNIQUE).
CREATE TABLE IF NOT EXISTS notification_log (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  appointment_id bigint REFERENCES appointment(id) ON DELETE CASCADE,
  recipient      text NOT NULL CHECK (recipient IN ('stylist','client','manager')),
  kind           text NOT NULL,                         -- 'booked' | 'reminder_24h' | 'reminder_1h' | ...
  channel        text,
  detail         text,
  sent_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT notification_once UNIQUE (appointment_id, recipient, kind)
);
