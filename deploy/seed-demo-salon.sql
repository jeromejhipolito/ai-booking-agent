-- Demo fixture: "Studio Kalye", a 4-chair salon in Quezon City (Asia/Manila).
--
-- Idempotent by construction:
--   * catalogue rows upsert on their natural key, so re-running never duplicates them;
--   * demo appointments are deleted by `source = 'seed'` and re-inserted relative to NEXT MONDAY,
--     so the fixture is always in the future no matter when you run it. Only seeded rows are
--     touched — a real booking made through the agent has source 'chat' and is never deleted.
--
-- Apply after booking-schema.sql.

-- ---------------------------------------------------------------------------
-- stylists
-- ---------------------------------------------------------------------------
-- telegram_chat_id is where a stylist's shift reminders go. These are demo values: with no
-- TELEGRAM_BOT_TOKEN configured nothing is actually sent, but every notice is still recorded
-- in notification_log, which is what the send-exactly-once guard is built on.
INSERT INTO stylist (id, name, title, bio, rating, telegram_chat_id) VALUES
  ('sty_maria', 'Maria Santos',  'Senior Colourist', 'Twelve years in colour correction and balayage. Trained in Seoul.', 4.9, 'demo-stylist-maria'),
  ('sty_joy',   'Joy Ramirez',   'Senior Stylist',   'Precision cuts and keratin treatments. Teaches at the academy on Mondays.', 4.8, 'demo-stylist-joy'),
  ('sty_ruel',  'Ruel Bautista', 'Master Barber',    'Classic barbering, fades and beard work. Fifteen years on the chair.', 4.9, 'demo-stylist-ruel'),
  ('sty_bea',   'Bea Cruz',      'Stylist',          'Blow-dry styling, hair spa and nail care. Fastest hands in the studio.', 4.7, 'demo-stylist-bea')
ON CONFLICT (id) DO UPDATE
  SET name = EXCLUDED.name, title = EXCLUDED.title, bio = EXCLUDED.bio, rating = EXCLUDED.rating,
      telegram_chat_id = COALESCE(stylist.telegram_chat_id, EXCLUDED.telegram_chat_id);

-- ---------------------------------------------------------------------------
-- services
-- ---------------------------------------------------------------------------
INSERT INTO service (id, name, duration_min, price_php, description) VALUES
  ('svc_haircut',  'Haircut',            45,  450.00, 'Consultation, wash, cut and finish.'),
  ('svc_color',    'Hair Colour',       120, 2500.00, 'Full-head colour including toner.'),
  ('svc_rebond',   'Rebond & Reform',   240, 4500.00, 'Full rebonding. Please arrive with dry, unwashed hair.'),
  ('svc_blowdry',  'Blow Dry & Style',   30,  350.00, 'Wash and blow-dry styling.'),
  ('svc_manicure', 'Manicure',           45,  300.00, 'Classic manicure with gel or regular polish.'),
  ('svc_hairspa',  'Hair Spa',           60,  900.00, 'Deep-conditioning scalp and hair treatment.')
ON CONFLICT (id) DO UPDATE
  SET name = EXCLUDED.name, duration_min = EXCLUDED.duration_min,
      price_php = EXCLUDED.price_php, description = EXCLUDED.description;

-- ---------------------------------------------------------------------------
-- who performs what — deliberately uneven, so "find another stylist" is a real question
--   * svc_rebond   : Maria only        → no alternative exists
--   * svc_manicure : Bea only          → no alternative exists
--   * svc_haircut  : Maria, Joy, Ruel, Bea
-- ---------------------------------------------------------------------------
INSERT INTO stylist_service (stylist_id, service_id) VALUES
  ('sty_maria','svc_haircut'), ('sty_maria','svc_color'), ('sty_maria','svc_rebond'), ('sty_maria','svc_hairspa'),
  ('sty_joy','svc_haircut'),   ('sty_joy','svc_color'),   ('sty_joy','svc_blowdry'),  ('sty_joy','svc_hairspa'),
  ('sty_ruel','svc_haircut'),
  ('sty_bea','svc_haircut'),   ('sty_bea','svc_blowdry'), ('sty_bea','svc_manicure'), ('sty_bea','svc_hairspa')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- opening hours (salon-local wall clock; weekday 0 = Sunday, Postgres DOW)
-- Nobody works every day — Maria and Joy are off Monday, Ruel is off Mon+Tue,
-- Bea works Monday but not Wednesday. That asymmetry is the point: availability has to
-- read these rows, not assume a uniform week.
-- ---------------------------------------------------------------------------
DELETE FROM business_hours WHERE stylist_id IN ('sty_maria','sty_joy','sty_ruel','sty_bea');
INSERT INTO business_hours (stylist_id, weekday, opens_at, closes_at) VALUES
  ('sty_maria',2,'09:00','18:00'), ('sty_maria',3,'09:00','18:00'), ('sty_maria',4,'09:00','18:00'),
  ('sty_maria',5,'09:00','20:00'), ('sty_maria',6,'09:00','20:00'), ('sty_maria',0,'10:00','17:00'),

  ('sty_joy',  2,'09:00','18:00'), ('sty_joy',  3,'09:00','18:00'), ('sty_joy',  4,'09:00','18:00'),
  ('sty_joy',  5,'09:00','20:00'), ('sty_joy',  6,'09:00','20:00'), ('sty_joy',  0,'10:00','17:00'),

  ('sty_ruel', 3,'11:00','20:00'), ('sty_ruel', 4,'11:00','20:00'), ('sty_ruel', 5,'11:00','21:00'),
  ('sty_ruel', 6,'10:00','21:00'), ('sty_ruel', 0,'10:00','18:00'),

  ('sty_bea',  1,'09:00','18:00'), ('sty_bea',  2,'09:00','18:00'), ('sty_bea',  5,'09:00','20:00'),
  ('sty_bea',  6,'09:00','20:00'), ('sty_bea',  0,'10:00','17:00');

-- ---------------------------------------------------------------------------
-- demo clients
-- ---------------------------------------------------------------------------
INSERT INTO client (id, name, phone, channel, channel_user_id) VALUES
  ('cli_demo_ana',  'Ana Reyes',    '09171234567', 'telegram', 'demo-ana'),
  ('cli_demo_paolo','Paolo Mendoza','09181234567', 'telegram', 'demo-paolo'),
  ('cli_demo_lin',  'Lin Tan',      '09191234567', 'telegram', 'demo-lin')
ON CONFLICT (id) DO UPDATE
  SET name = EXCLUDED.name, phone = EXCLUDED.phone;

-- ---------------------------------------------------------------------------
-- existing appointments — next Tue/Wed/Fri, so the demo always has a busy diary ahead of it
-- ---------------------------------------------------------------------------
DELETE FROM appointment WHERE source = 'seed';

WITH anchor AS (
  -- Monday of next week, in salon-local terms
  SELECT date_trunc('week', ((now() AT TIME ZONE 'Asia/Manila')::date + 7)::timestamp)::date AS monday
), slots(ref, client_id, stylist_id, service_id, day_offset, local_time) AS (
  VALUES
    ('SEED-001','cli_demo_ana',  'sty_maria','svc_color',   1, time '10:00'),  -- Tue 10:00-12:00
    ('SEED-002','cli_demo_paolo','sty_ruel', 'svc_haircut', 2, time '11:00'),  -- Wed 11:00-11:45
    ('SEED-003','cli_demo_lin',  'sty_joy',  'svc_haircut', 1, time '14:00'),  -- Tue 14:00-14:45
    ('SEED-004','cli_demo_ana',  'sty_bea',  'svc_manicure',4, time '16:00'),  -- Fri 16:00-16:45
    ('SEED-005','cli_demo_lin',  'sty_maria','svc_haircut', 1, time '13:00')   -- Tue 13:00-13:45
)
INSERT INTO appointment (ref, client_id, stylist_id, service_id, starts_at, ends_at, source, notes)
SELECT
  s.ref, s.client_id, s.stylist_id, s.service_id,
  ((a.monday + s.day_offset + s.local_time) AT TIME ZONE 'Asia/Manila'),
  ((a.monday + s.day_offset + s.local_time) AT TIME ZONE 'Asia/Manila') + make_interval(mins => svc.duration_min),
  'seed',
  'Demo fixture'
FROM slots s
CROSS JOIN anchor a
JOIN service svc ON svc.id = s.service_id
ON CONFLICT (ref) DO NOTHING;
