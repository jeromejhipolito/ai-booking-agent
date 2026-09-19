-- Runs once on first Postgres startup (docker-entrypoint-initdb.d).
-- pgvector: the phase-2 knowledge base + conversation memory embed into it.
CREATE EXTENSION IF NOT EXISTS vector;
-- btree_gist: lets the appointment EXCLUDE constraint mix `stylist_id WITH =` (btree)
-- and a tstzrange overlap (gist) in one index. Without it the constraint cannot be created.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- A blocked write (someone holding an uncommitted overlapping insert) must fail loudly rather
-- than hang a booking sub-workflow until the caller times out.
ALTER DATABASE salon_booking SET statement_timeout = '10s';
