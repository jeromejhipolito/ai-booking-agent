-- Phase 2 — what the conversational layer needs on top of booking-schema.sql.
--
-- Deliberately small. The chat window itself lives in n8n's own `n8n_chat_histories`
-- table (created and owned by the Postgres Chat Memory node), NOT here — so "clear this
-- customer's memory" means truncating THAT table too, not just these rows.
--
-- Apply after booking-schema.sql. Idempotent.

-- Who we are talking to, per channel. The salon's `client` table is about people who have
-- booked; this is about people who have *messaged*. They are joined by phone/name at
-- booking time, and a chat user who never books never becomes a client row.
CREATE TABLE IF NOT EXISTS bot_user_profile (
  user_key       text PRIMARY KEY,            -- channel-scoped: 'telegram:12345'
  channel        text NOT NULL,
  preferred_name text,                        -- NULL = we have not been told yet
  phone          text,
  -- The booking we last read back to this person, and when. THIS is what authorises a
  -- create — not the model's `confirm` flag. A small LLM will happily emit confirm:true on
  -- the first message, or copy it forward out of its own history; a row here only exists
  -- because the agent actually showed a summary and asked. It also expires, so a "yes"
  -- arriving an hour and twenty turns later books nothing.
  pending_booking jsonb,
  pending_set_at  timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Existing installs (the table shipped without these in an earlier apply).
ALTER TABLE bot_user_profile ADD COLUMN IF NOT EXISTS pending_booking jsonb;
ALTER TABLE bot_user_profile ADD COLUMN IF NOT EXISTS pending_set_at  timestamptz;

-- The salon FAQ, embedded. Every turn retrieves from this — there is no search *tool* the
-- model could decide to skip, which is what stops a small model from inventing opening
-- hours. bge-m3 is 1024-dim; halfvec is float16, half the storage for no useful loss.
CREATE TABLE IF NOT EXISTS kb_chunk (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug       text NOT NULL UNIQUE,            -- stable id from the source file, so re-ingest updates in place
  heading    text,
  content    text NOT NULL,
  embedding  halfvec(1024) NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_chunk_embedding_hnsw
  ON kb_chunk USING hnsw (embedding halfvec_cosine_ops);
