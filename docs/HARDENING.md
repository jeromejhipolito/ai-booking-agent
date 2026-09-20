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

---

# Hardening notes — phase 2/4 (the conversational layer)

Same method: self-derived AC, the 9 gap-classes, and a `qa-engineer` adversarial matrix
(95 candidate cases) triaged into must-builds, logged decisions and later-phase work.

## Self-derived AC

| AC | Given / When / Then |
|---|---|
| AC-1 | A greeting gets a warm reply, calls no tool and books nothing. |
| AC-2 | A booking request is slot-filled ONE field per turn, read back in full, and creates a row only after an explicit yes. |
| AC-3 | Asking for an occupied slot is declined in plain language and real, bookable alternatives are offered. |
| AC-4 | A cancel is read back for confirmation, then frees the window. |
| AC-5 | A question about the salon is answered only from the retrieved notes, with the real numbers. |
| AC-6 | A question the notes do not cover — or that is not about the salon at all — is declined, not answered. |
| AC-7 | The model's free text is never the source of a booking, a price or a confirmation. |

## Hardened items (all ENG, all built)

| # | Class | Requirement | Matrix case |
|---|---|---|---|
| H-A | state/lifecycle | A create is authorised by `bot_user_profile.pending_booking` — a row written only when the agent actually showed a read-back, expiring after 30 minutes — never by the model's `confirm` flag. | 22, 58, 59 |
| H-B | idempotency | `booking_key` is minted with the read-back and stored in it, so a double-sent yes replays one booking, while a genuine re-book after a cancel gets a fresh key and succeeds. | 41, 42 |
| H-C | security | Model-authored text (the only text a customer sees for `kb`/`chitchat`) is scrubbed of booking references and confirmation wording, the data-not-instructions rule is restated AFTER the notes, and the agent refuses anything outside the salon's remit. | 1, 2, 4, 5, 6, 7 |
| H-D | permission | A cancel only ever resolves against the caller's OWN upcoming appointments; someone else's reference reads as not found and is never confirmed to exist. | 8, 9 |
| H-E | data contract | Before any read-back or create, the caller's existing appointments are checked for the same service at the same moment — "you already have that" instead of "someone took it". | 94 |
| H-F | integration | A failed embedding never becomes a retrieval: no vector, no notes, and the reply says it cannot look that up rather than answering from arbitrary chunks. | 76 |
| H-G | ambiguous term | Service and stylist names are matched exactly (plus a small Filipino alias list), never to the nearest catalogue entry; an unmatched name is reported as unknown with the real options. | 23, 24, 25 |
| H-H | boundary | Dates must be a real calendar day, not past, within 90 days; times must be a real 24-hour clock. | 26, 27, 29, 30 |
| H-I | boundary | Messages are capped at 1200 characters, and an empty one never reaches the embedder. | 45, 47 |
| H-J | state | The half-built booking is persisted server-side as a `draft`, so a turn where the model loses the thread does not lose the customer's answers. | 18, 77, 78 |
| H-K | ambiguous term | An auto-assigned stylist is not treated as a preference: only a stylist the customer actually asked for is carried into a changed date. | 11, 89 |
| H-L | negative | A bare affirmative can never be a change of mind, and a change of mind can never be a confirmation — a modified detail produces a NEW read-back. | 10, 12, 14 |
| H-M | integration | A rate-limited or unreachable model is retried, then reported honestly as "busy", never as "I didn't understand" — which would make the customer rephrase a perfectly good message. | 77 |
| H-N | data contract | Slot-grid misses ("2pm" on a 45-minute grid) offer the nearest real slot as an explicit yes/no rather than a bare refusal. | 28 |

## Decisions taken (logged, not asked)

| Decision | Chosen default | Why |
|---|---|---|
| KB retrieval threshold | **0.35 noise floor, top 3, and the prompt decides** — not the 0.55 the sibling repo uses | Measured on this corpus: in-scope questions score 0.395–0.656 and off-topic ones 0.282–0.491. The distributions overlap, so no threshold can separate them; at 0.55, eight of eleven legitimate questions would have been refused. A threshold that cannot be the decision-maker should not be asked to be one. |
| Confirmation gate | the customer's own words, matched against an explicit affirmative list | An injected instruction can make the model emit `confirm:true`; it cannot make the customer type "yes". "maybe" and "i think so" are deliberately not on the list. |
| Off-grid start times | offered the nearest slot within 45 minutes, as a yes/no | Every natural request ("2pm") misses a 45-minute grid anchored at opening. Refusing them all is technically correct and useless. |
| Third-party bookings | the appointment carries the named person, and the caller can still cancel it | Booking for a partner or a child is the common case, not an edge case. |
| Telegram adapter activation | shipped **inactive** | Only one poller may run per bot token, and this machine already runs another bot on the only token available. Activating it needs a dedicated @BotFather token. |

## Affected-area blast radius

Phase 1's four tools are the only existing code this phase consumes, and it consumes them
through their published contract without modification — `git diff` touches no `workflows/2*.json`.
The `bot_user_profile` and `kb_chunk` tables are new. The shared local n8n gains two workflows
and one route on the dev harness; no existing workflow is edited. **Re-verified:** phase 1's
own suite still passes unchanged after all phase-2 work.

## Hardened items added during phase-2 verification

Everything below was found by running the suite against a deliberately weak self-hosted model.
Each one is a defect in the code *around* the model, and each would have shipped unnoticed
behind a sharper one — which is the argument for testing with the weak one.

| # | Class | Requirement | How it surfaced |
|---|---|---|---|
| H-O | data contract | The service is derived from the customer's own words first (names, English and Filipino aliases, and affix-tolerant roots so `magpagupit` → haircut); a model-proposed service is accepted only when the words support it or it matches the one already settled. | The model substituted a real service for "Brazilian blowout", and separately dropped "haircut" out of a sentence that plainly contained it. |
| H-P | permission | Only a booking reference **the customer typed** is honoured. A reference the model echoed out of the conversation window is ignored. | An echoed stale `BK-…` beat the unambiguous "your one upcoming appointment" rule, so "cancel my appointment" answered "I can't find that booking". |
| H-Q | ambiguous term | Agreement is tokenised — every word must be an agreement word or a filler, at least one carrying the agreement. | An exact-phrase list could never cover "Opo, sige"; tokenising covers it while still refusing "yes, but make it 3pm", which contains words that are neither. |
| H-R | negative | Times are parsed out of whatever came back (`09:00`, `9:00 in the morning`, `2pm`, `14:00-14:45`) rather than pattern-matched, and a validation failure is never sticky. | A rejected time was carried forward every turn, so the agent asked for the time forever while the customer kept answering it. |
| H-S | state | A bare name or phone number answering a question we just asked is claimed by the code whatever field the model filed it under. | The model put the customer's own name in `preferred_stylist`, so telling the agent your name got "I don't have a stylist by that name". |
| H-T | state | A half-built draft is only a baseline while the customer stays on the same service; asking for something else drops it rather than merging underneath. | A stale draft leaked its service into an unrelated later request. |

## Decision taken on the chat model

**Shipped:** a self-hosted `qwen2.5:7b-instruct` on Ollama, with `Chat Model (Groq)` and
`Chat Model (OpenRouter)` wired and disabled beside it on the canvas.

Groq's `openai/gpt-oss-20b` is faster and noticeably sharper at the extraction. It is not the
default for two reasons. First, both hosted free tiers meter **tokens** per minute, and a
multi-turn suite of 40 assertions exceeds that within about a minute — measured, after pacing
attempts from 1.5s up to 22s between messages, which only stretched the run to an hour without
completing it. Second, and more to the point: everything that is allowed to be wrong here is the
model, so the default should be the weakest plausible one. Swapping to a hosted model is two
clicks and can only improve the numbers.

---

# Hardening notes — phase 3/4 (proactive automation)

Same method: self-derived AC, the 9 gap-classes, a `qa-engineer` adversarial matrix (83 cases)
triaged into must-builds, logged decisions and later-phase work.

## Self-derived AC

| AC | Given / When / Then |
|---|---|
| AC-1 | A stylist notice fires exactly once per (appointment, person, kind), even across two consecutive sweeps — and only when it makes sense to send it. |
| AC-2 | Cancelling a booked slot with two waitlisted customers leaves exactly one of them holding it, with no overlapping rows. |
| AC-3 | A stylist declining asks the customer's permission before anything changes. |
| AC-4 | The customer's "yes" reassigns; "no" keeps the original stylist and puts it in front of a human. |
| AC-5 | An offer nobody answers rolls to the next person, and never loops back. |

## Two schema defects the matrix found before they shipped

**The send-once key was keyed on a role, not a person.** `UNIQUE (appointment_id, recipient, kind)`
looks right until an appointment is reassigned: the new stylist's `booked` notice is indistinguishable
from the old stylist's, so it is suppressed and **the stylist is never told they have a client**. The
same key breaks the waitlist queue — when an offer expires and rolls on, the second customer's offer
looks like a duplicate of the first's and is silently dropped, so AC-5 passes on paper and stalls in
practice. `notification_log.recipient_ref` and a four-column key fix both and keep the first
recipient's audit trail.

**A claim that was never delivered marked the notice sent forever.** Claim-then-send is right — it is
what makes exactly-once work — but only if a failed send **gives the claim back**. Otherwise the one
reminder that mattered is the one permanently suppressed.

## Hardened items (all ENG, all built)

| # | Class | Requirement | Matrix case |
|---|---|---|---|
| P-A | data contract | The send-once key includes WHO the notice was for, so a reassignment or a rolled offer is not mistaken for a duplicate. | 30, 31, 36, 37 |
| P-B | state | A failed send releases its claim; a claim is never taken for something undeliverable (nobody to send to) in the first place. | 22, 23, 24, 25 |
| P-C | permission | `39` derives the recipient from the appointment (or the named waitlist entry) — never from a `chat_id` the caller passed, which could address the wrong customer. | 29 |
| P-D | state/lifecycle | A reminder for an appointment cancelled between the sweep and the send is suppressed at claim time, not sent. | 5, 6, 34 |
| P-E | boundary | A timed reminder is only due if the booking existed before its window opened — a booking made for this afternoon gets a confirmation, not a "reminder" about what you just did. | 9, 10, 11, 12 |
| P-F | negative | A stylist is told when an appointment is cancelled, not only when one is made. | 33 |
| P-G | concurrency | The backfill claims the customer's read-back FIRST and the queue place second, so a slot is never held for half an hour for someone who was never told. | 52, 54 |
| P-H | concurrency | One live offer per freed window, and one outstanding offer per customer — a customer has one pending row, so two offers would be ambiguous. | 45, 46, 60 |
| P-I | idempotency | An entry remembers which appointment it was offered, so an expired offer rolls forward instead of looping back to the same person. | 48 |
| P-J | integration | A freed slot is only offered to someone whose service that stylist performs, who is reachable, who is not already busy in that window, and who did not cancel it themselves. | 38, 39, 54, 59 |
| P-K | boundary | Nothing is offered on a window under an hour away, or already in the past — a 30-minute window to accept a slot starting in eight minutes is not an offer. | 57, 58 |
| P-L | state | Accepting an offer runs the ORDINARY booking path, so the EXCLUDE constraint decides who gets it; losing the race puts the entry back in the queue rather than consuming it. | 40, 43, 47 |
| P-M | permission | A reassignment is authorised by a server-side consent row and the customer's literal yes — the same mechanism a booking read-back uses. | 75, 76 |
| P-N | state/lifecycle | `24` re-checks at the moment of the move: still confirmed, still in the future, still held by the stylist who declined, and the replacement still performs the service, works those hours and is free. | 61, 62, 68, 70 |
| P-O | idempotency | A stylist declining twice asks the customer once. | 63 |
| P-P | negative | With nobody free, the customer is offered a reschedule or a cancellation and a human is told — no name is invented to fill the gap. | 66, 67 |
| P-Q | boundary | A waitlist joined from chat is bounded to the day asked about; an open-ended window would match every slot that ever frees. | 79 |
| P-R | ambiguous term | Nobody is added to a waitlist without saying yes, and saying yes twice for the same day adds one entry. | 77, 78 |
| P-S | integration | A read-back written by a BACKGROUND job is honoured on the customer's "yes" even though it is not in the model's conversation window — authorisation lives in the database, not in the transcript. | 52 |

## Decisions taken (logged, not asked)

| Decision | Chosen default | Why |
|---|---|---|
| Offer lifetime | 30 minutes, matching the pending read-back's own expiry | Two different clocks on the same promise is how a customer gets told they hold a slot the database has already given away. |
| Minimum lead for a backfill offer | 60 minutes | Below that the offer expires after the appointment starts. |
| `mode` on every Execute Workflow call | `once` (all items in one execution) | n8n deprecates per-item mode. Every **tool** sub-workflow is multi-item safe instead (it iterates `$input.all()` and pairs back by index). The **agent core is deliberately single-turn** — a conversation is one turn by definition — so the Telegram adapter loops with a Split-In-Batches node and hands it one message at a time. Feeding it N messages at once would have answered the first and silently dropped the rest, after their Telegram offset was already consumed. |
| An undeliverable notice | left unclaimed and retried | Marking it sent is worse than leaving it pending: the retry costs nothing, the suppression is permanent. |
| A manager alert with no manager chat configured | still recorded (`manager-desk`), and **not** sent | It is the salon's audit trail; it should not depend on a bot being wired up. The recipient defaults from `MANAGER_TELEGRAM_CHAT_ID`; when that is unset the placeholder is recognised as "nowhere to send" rather than POSTed — a placeholder chat id 400s, releases the claim, and re-sends every sweep forever. |
| Rescheduling an appointment | **not supported** — reminders are keyed to the appointment, not to a version of it | Moving `starts_at` after a reminder fired would need the notice reset too. Out of scope here; documented rather than half-built. |

## Affected-area blast radius

Phase 1's tools are consumed unmodified. Phase 2's core gains three branches (waitlist acceptance,
waitlist join, reassignment consent) and one reordering — an outstanding question the agent asked is
now answered before any guess at the customer's intent. **Re-verified:** phase 1's 51 and phase 2's
40 assertions both still pass unchanged.

---

# Hardening notes — phase 4/4 (reviews)

40 adversarial cases, triaged. Three changed the design.

## Self-derived AC

| AC | Given / When / Then |
|---|---|
| AC-1 | A completed appointment produces exactly one review request. |
| AC-2 | A five-star reply stores praise and tells the manager. |
| AC-3 | A one-star reply stores a complaint, tells the manager, and the reply contains no defence. |
| AC-4 | Three stars is stored, and only escalated if the sentiment reading says complaint. |
| AC-5 | The customer's comment reaches the manager unaltered. |

## Hardened items

| # | Class | Requirement | Matrix case |
|---|---|---|---|
| R-A | idempotency | One review per appointment, enforced by a **UNIQUE constraint** — two replies arriving together ("5, lovely" then "actually 2, it was bad") race straight past a check-then-insert. | 5, 6 |
| R-B | permission | The review is stored against the appointment in the **server-side pending row**, never a reference the customer or the model typed — a leaked ref would otherwise let someone review a stranger's haircut. | 3, 4, 37 |
| R-C | data contract | A rating is a whole 1–5 the customer actually gave. "I'd give it 10/10" and "I waited 40 minutes" store **no rating at all** rather than an invented one; the words then stand on their own. | 19–25 |
| R-D | state/lifecycle | Completion keys on `ends_at`, not `starts_at` — nobody is asked how their cut went while they are still in the chair — and only `confirmed` appointments ever become `completed`. | 8–12 |
| R-E | state | The review ask never overwrites a pending a live conversation is waiting on, and answering it with a booking request drops the ask instead of trapping the customer in it. | 38, 39 |
| R-F | integration | A manager copy that never went out is **re-sent on the next sweep** — 39 releases the claim on a failed send, so the absence of a log row is the signal. Without it the feedback is stored and nobody ever reads it. | 15, 40 |
| R-G | security | The comment is stored and rendered as inert text; the manager's notice is built by code from the stored row, so an injection aimed at whoever reads it is just words. | 34, 35, 36 |
| R-H | boundary | Nobody is asked about a haircut from three months ago, and nobody is asked at three in the morning. | 13 |
| R-I | negative | A weak model that returns nothing for an unusual message does not lose the customer's words — an outstanding review ask takes the raw message as the comment, unless it is plainly a new request. | 31, 32, 36 |

## Decisions taken (logged, not asked)

| Decision | Chosen default | Why |
|---|---|---|
| Who decides praise vs complaint | **the rating**, with the model only breaking a 3-star tie | A number the customer chose is not something a model gets to reinterpret. |
| Praise routed to the manager too | yes | You cannot coach a team on complaints alone, and a stylist who did well should hear it. |
| `escalated_at` | set when the review is stored, meaning "this warranted a human" | Delivery truth lives in `notification_log`; the retry sweep closes the gap between the two. |
| The reply to a complaint | thanks, then silence | No "but", no explanation, no discount offered by a bot. A person follows up. |
| A comment with no rating | stored, classified from the sentiment reading alone | Most people write a sentence and skip the stars. |
