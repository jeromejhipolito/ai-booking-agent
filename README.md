# AI Booking Agent — a salon that books, cancels and reschedules over chat

An n8n automation where the **conversation is the booking system**. A client messages the salon,
the agent works out what they want, checks the diary, and books it. When a slot frees up it goes
back to whoever was waiting. When a stylist can't make a shift it finds cover — and asks the
client before moving them.

> **Status: phase 1 of 4 — the deterministic foundation.** The schema and the four booking tools
> are built and verified end to end. The chat agent (phase 2), the proactive automation (phase 3)
> and reviews → manager routing (phase 4) land on this branch next.

## The idea that shapes everything here

An LLM is going to be wrong sometimes. So **the LLM is never what makes a booking correct.**

Every decision that must not be wrong — is this slot free, does this stylist do this service, is
the salon even open — is a Postgres constraint or a parameterised query. The model's only job is
to understand a sentence and fill in the arguments. If it hallucinates a slot that is already
taken, the database refuses it and the agent gets back `{"ok": false, "reason": "slot_taken"}`.

The guard is one line:

```sql
EXCLUDE USING gist (
  stylist_id WITH =,
  tstzrange(starts_at, ends_at) WITH &&
) WHERE (status = 'confirmed')
```

Two confirmed appointments can never overlap for one stylist — not through a race between two
chats, not through a confused model, not through a hand-written query. `tstzrange` defaults to a
half-open `[)` range, so a 10:00–10:30 and an 11:00–11:30 booking sit back to back without the
constraint complaining, and no inventory is lost to an off-by-one.

## What's built (phase 1)

```
                    ┌─────────────────────────────────────────┐
  phase 2 agent ───▶│  20 · find availability                 │
   (chat, Groq)     │  21 · create booking                    │──▶ Postgres
                    │  22 · cancel booking                    │    (the actual rules)
                    │  23 · find stylist alternatives         │
                    └─────────────────────────────────────────┘
                       deterministic · no LLM node anywhere
```

| File | What it is |
|---|---|
| `deploy/booking-schema.sql` | 9 tables. The double-booking guard, the send-once notification ledger, the waitlist and review tables phases 3–4 fill in. |
| `deploy/seed-demo-salon.sql` | "Studio Kalye" — 4 stylists, 6 services, uneven per-weekday hours, a diary that is always in the future. |
| `workflows/20…23_tool_*.json` | The four callable sub-workflows. Trigger → validate → one parameterised query → shape the response. |
| `workflows/90_dev_test_harness.json` | Dev-only webhook that calls a tool by name, so the tools can be exercised before any agent exists. |
| `scripts/verify_tools.py` | 51 live assertions against a running stack. |
| `scripts/import_workflows.py` | Pushes `workflows/*.json` into an n8n via its REST API. |

## The response contract

Every tool answers with **exactly one of** these — never both, never a bare exception:

```jsonc
{ "ok": true,  "data": { … } }
{ "ok": false, "reason": "slot_taken" }
```

`reason` is a closed enum. A database outage comes back as `db_unavailable`, never as raw driver
text — that text contains the connection string, and the next thing that reads it is an LLM
talking to a customer.

Times are `timestamptz` (UTC) in the database and are rendered **twice** in every response —
`starts_at_utc` ending in `Z` and `starts_at_local` in the salon's zone. Nothing depends on the
Postgres session timezone or the container clock: point the database at `America/New_York` and
the output is byte-identical. A `starts_at` sent without an offset is read as salon-local
(Manila has no DST, so that is exactly `+08:00`) and the resolved instant is echoed back.

Full per-tool input/output and the complete `reason` enum: **[docs/TOOL_CONTRACTS.md](docs/TOOL_CONTRACTS.md)**.

## Run it locally

```bash
# 1. Postgres with pgvector + btree_gist
docker run -d --name booking-pg-local \
  -e POSTGRES_USER=n8n -e POSTGRES_PASSWORD=n8n -e POSTGRES_DB=salon_booking \
  -p 5545:5432 pgvector/pgvector:pg16

docker exec -i booking-pg-local psql -U n8n -d salon_booking < deploy/init-db.sql
docker exec -i booking-pg-local psql -U n8n -d salon_booking < deploy/booking-schema.sql
docker exec -i booking-pg-local psql -U n8n -d salon_booking < deploy/seed-demo-salon.sql

# 2. n8n on :5678, then create a Postgres credential pointing at localhost:5545
npx n8n start

# 3. import the workflows (Settings → n8n API → create a key)
N8N_API_KEY=... python3 scripts/import_workflows.py --activate

# 4. prove it
python3 scripts/verify_tools.py
```

Two things that will bite you:

- **Sub-workflows must be active.** An Execute Workflow call to an inactive workflow fails with
  *"Workflow is not active and cannot be executed"* — hence `--activate`.
- **Credential ids are per-instance.** The JSON carries the id from the machine it was exported
  from. After importing elsewhere, open each Postgres node once and pick your own credential.

For a real deployment use `deploy/docker-compose.yml` (Postgres + Ollama + n8n + Caddy with
automatic HTTPS) and **deactivate `90 · dev: tool test harness`** — it is an unauthenticated
trigger meant only for local verification.

## Verification

`scripts/verify_tools.py` drives 51 cases through the harness webhook, so every assertion is a
real n8n execution hitting real Postgres — no stubs, no mocked layer.

```
51 passed, 0 failed
```

It covers the happy paths and the parts that actually break in production: a slot offered in a
gap too small for the service, a booking whose service would run past closing, the same
idempotency key replayed with different parameters, eight concurrent bookings of one slot, SQL
injection through both a service id and a booking reference, a three-item call that must not
collapse to one, and the database being switched off mid-call.

## Known limits (deliberate, not oversights)

- **One opening/closing pair per stylist per weekday.** Split shifts, lunch breaks and overnight
  hours are impossible by construction (`PRIMARY KEY (stylist_id, weekday)` +
  `CHECK (closes_at > opens_at)`) rather than silently mishandled.
- **`completed` and `no_show` appointments do not hold a slot.** Only `confirmed` does — one rule,
  applied identically by all three tools that care.
- **A client may hold two appointments at once** with different stylists. No acceptance criterion
  asked for a client-level lock, and adding one would block a legitimate two-service visit.
- **Off-grid start times are accepted verbatim** if the hours and overlap checks pass. A client who
  asks for 16:07 is never silently rounded to 16:00.

## Stack

n8n · PostgreSQL 16 (pgvector, btree_gist) · Docker · Groq + Ollama `bge-m3` from phase 2 ·
Telegram from phase 2.
