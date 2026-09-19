# Hardening notes — phase 1/4 (foundation)

Self-derived AC + DoD, hardened with the 9 gap-classes and a `qa-engineer` adversarial matrix
(134 candidate cases). Phase 1 is DETERMINISTIC ONLY — no LLM node anywhere.

## Self-derived AC

| AC | Given / When / Then |
|---|---|
| AC-1 | Given no repo, when phase 1 completes, then `ai-booking-agent` is a git repo on `feature/booking-agent`, local identity `Jerome Hipolito <jeromehipolito.github@gmail.com>`, remote = public `jeromejhipolito/ai-booking-agent`, and the sibling repo's `git status` is byte-unchanged. |
| AC-2 | Given the machine, when the stack is up, then n8n answers on `localhost:5678` and `booking-pg-local` accepts connections on `5545` (db `salon_booking`). |
| AC-3 | Given a fresh DB, when `booking-schema.sql` is applied, then all 9 tables exist; two overlapping **confirmed** appointments for one stylist → `23P01`; different stylists → OK; back-to-back (`ends_at == starts_at`) → OK; a **cancelled** row does not block its window; `ends_at <= starts_at` → CHECK violation. Re-applying the file is a no-op. |
| AC-4 | Given the schema, when `seed-demo-salon.sql` runs, then 4 stylists / 6 services / per-weekday hours / 5 future appointments exist; running it again changes no counts; seeded appointments are always in the future; rows not marked `source='seed'` are never touched. |
| AC-5 | Given the seeded salon, when `20_tool_find_availability {service_id, date, stylist_id?}` runs, then it returns open slots inside that stylist's hours for that weekday, stepped by the service duration, never overlapping a confirmed appointment, never in the past, filtered to `stylist_id` when given. Unknown service → `ok:false service_not_found`. |
| AC-6 | Given an open slot, when `21_tool_create_booking` runs, then a confirmed row is created and `ok:true` + `ref` returned; the same `booking_key` again returns the SAME booking with `idempotent_replay:true` and creates no second row; an overlapping slot returns `ok:false slot_taken` **without aborting the execution**. |
| AC-7 | Given a confirmed appointment, when `22_tool_cancel_booking {booking_ref}` runs, then status → `cancelled`, `ok:true` + the freed `{stylist_id, service_id, starts_at, ends_at}`; unknown ref → `booking_not_found`; already cancelled → `already_cancelled`; the freed window is immediately bookable again. |
| AC-8 | Given a window + service, when `23_tool_find_stylist_alternatives` runs, then it returns only stylists who perform that service, whose hours cover the window, with no overlapping confirmed appointment, excluding `exclude_stylist_id`, ranked deterministically. None free → `ok:true` with an empty list. |
| AC-9 | Every tool returns exactly `{ok:true, data}` or `{ok:false, reason}` — never both, never a bare throw; every instant is `timestamptz` UTC and every response carries both a UTC and an `Asia/Manila` rendering; no tool contains an LLM node; every hot-path node has `continueOnFail` + `alwaysOutputData`. |
| AC-10 | No secret appears in any committed file; `.gitignore` carries no bare `*.sql`; the schema/seed files are tracked. |

## Hardened items (from the 9 classes + the adversarial matrix) — all ENG, all must-build

| # | Class | Hardened requirement | Matrix case |
|---|---|---|---|
| H-1 | data contract | Slot/appointment times are computed with explicit `AT TIME ZONE 'Asia/Manila'`; **no `::date` / `date_trunc` on a `timestamptz` without a zone**, so output is identical under any Postgres session TimeZone or n8n container TZ. | 1,2,17,48 |
| H-2 | data contract | Every timestamp in a response is rendered twice, session-independently: `*_utc` via `to_char(… AT TIME ZONE 'UTC')` ending in `Z`, and `*_local` in Manila. No naked local strings. | 11 |
| H-3 | ambiguous term | A `starts_at` with no offset is interpreted as **Asia/Manila** (fixed +08:00 — PH has no DST) by *both* create and alternatives, and the resolved instant is echoed back. | 6,7,8 |
| H-4 | integration assumption | `ends_at` is **always** derived from `service.duration_min`; a caller-supplied `ends_at`/duration is ignored, so the EXCLUDE guard cannot be bypassed. | 28 |
| H-5 | negative | `create_booking` validates business hours: start before open, end after close, or a closed weekday → `outside_business_hours`. (Availability and create must never disagree.) | 50,51,52 |
| H-6 | negative | `create_booking` validates the stylist performs the service → `stylist_does_not_perform_service`. | 56 |
| H-7 | negative | `create_booking` refuses a start in the past → `starts_in_the_past`. | 96 |
| H-8 | idempotency | Same `booking_key` + same params → the original booking, `idempotent_replay:true`. Same key + **different** params → `ok:false booking_key_conflict` (never silently confirm the wrong booking). | 72,73,74 |
| H-9 | idempotency | The `booking_key` insert uses `ON CONFLICT (booking_key) DO NOTHING` + re-select, so a concurrent duplicate can never leak `23505`. | 82 |
| H-10 | concurrency | The overlap pre-check is in the same statement (`WHERE NOT EXISTS`), and the EXCLUDE constraint is the backstop; a lost race surfaces as `slot_taken`, never a raw `23P01`. | 81,84 |
| H-11 | state | `cancel_booking` discriminates `booking_not_found` / `already_cancelled` / `cannot_cancel_completed`, and its `UPDATE … WHERE status='confirmed'` makes concurrent double-cancel yield exactly one winner. | 85,107 |
| H-12 | empty/boundary | A service no stylist performs → `ok:true` with `slots: []` and `qualified_stylists: 0` (not a full grid, not an error). | 54,55 |
| H-13 | empty/boundary | A stylist with no `business_hours` row for that weekday contributes zero slots (INNER JOIN, never "open 24h"). | 41,42,43 |
| H-14 | boundary | Slots are excluded unless the **whole** `[start, start+duration)` fits before closing and clears every confirmed appointment — so a 45-min service is never offered in a 30-min gap. | 30,32,34 |
| H-15 | negative | Malformed input (missing/blank/wrong-typed ids, `date:"tomorrow"`, `2026-02-30`, blank `booking_key`, `ends_at <= starts_at`) → a named reason (`invalid_input` / `invalid_date` / `invalid_timestamp` / `invalid_window`), never a Postgres `22P02`/`22008` leaking out. | 64,65,76,89,91,93,109 |
| H-16 | data contract | `reason` is a **closed enum**. A DB outage or credential error maps to `db_unavailable`; no raw driver text, connection string, password or SQLSTATE is ever echoed. | 114,115,116 |
| H-17 | data contract | Every tool query returns **exactly one row, always** (scalar subqueries + aggregates, no possibly-empty FROM), so the caller can never receive zero items — and `alwaysOutputData` guarantees an item even on failure. | 90,117 |
| H-18 | parity/fan-out | The Code nodes iterate `$input.all()` and pair back by index (`$('…').all()[i]`), so a multi-item call is never silently collapsed to item 0. | 100 + trap #4 |
| H-19 | security | Every query is parameterised (`$1…$n`); no `{{ }}` interpolation into SQL. Optional params are passed as explicit `null` (never `undefined` — the node **skips** undefined array entries and shifts every later parameter). | 94,120 |
| H-20 | integration | Ordering has a unique final tie-break (`… , stylist_id`), so repeated identical calls return an identical order. | 62 |
| H-21 | availability | Past slots are never offered (`starts_at > now()`), and `now()` is Postgres' clock — one authoritative clock for both the filter and the past-check. | 96,97 |
| H-22 | operability | `statement_timeout` is set on the database so a blocked write fails loudly instead of hanging a sub-workflow. | 88 |
| H-23 | seed | The seed advances no identity sequence by hand (`GENERATED ALWAYS AS IDENTITY`) and uses `SEED-*` refs disjoint from the app's `BK-*` sequence, so the first app insert cannot collide. | 125 |

## Decisions taken (no safe-default blocker — logged, not asked)

| Decision | Chosen default | Why |
|---|---|---|
| Naked `starts_at` (no offset) | interpret as Asia/Manila | PH has no DST, the agent thinks in local time, and the resolved instant is echoed back for verification. |
| Off-grid start times (10:07) | accepted verbatim if hours+overlap pass | never silently round a client to a different time; phase 3 reassignment reuses the original instant. |
| Split shifts / lunch breaks / overnight hours | **not supported** in phase 1 | `business_hours` PK `(stylist_id, weekday)` + `CHECK (closes_at > opens_at)` make them impossible by construction rather than silently wrong. Documented limitation. |
| `completed` / `no_show` appointments | do not block a window (only `confirmed` does) | one rule, applied identically by availability, create and alternatives. |
| Same client in two chairs at once | allowed | no AC asks for it; adding a client-level exclusion would block legitimate multi-service visits. |
| Phone normalisation (`0917…` vs `+63917…`) | stored as given, no normalisation | client identity is `(channel, channel_user_id)`, not the phone. |
| Availability responses are time-varying | accepted | the past-slot filter depends on `now()` by design; every other field is deterministic. |

## Affected-area blast radius

**0 affected areas.** This is a brand-new repo with no dependents: no existing file is modified, no
shared library consumed, no other workflow calls these sub-workflows yet (phase 2 will be their first
caller). The only adjacent systems are (a) the shared local n8n instance — which gains four new
workflows and touches no existing one, and (b) the sibling `messenger-ai-support` repo, which is
read-only here and whose `git status` is asserted unchanged as part of AC-1.

## DoD

- [ ] D1 schema + seed apply cleanly on a fresh DB and are re-runnable
- [ ] D2 four tool workflows + the dev harness import into n8n and validate
- [ ] D3 every AC + hardened item live-verified through a real n8n execution, payloads captured
- [ ] D4 README + tool-contract docs written
- [ ] D5 secret scan clean; files staged by name; no bare `*.sql` ignore
- [ ] D6 branch pushed; remote HEAD == local HEAD
- [ ] D7 sibling repo untouched
- [ ] D8 zero LLM nodes in any phase-1 workflow
