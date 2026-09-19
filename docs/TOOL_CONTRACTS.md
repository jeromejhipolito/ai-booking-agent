# Tool contracts

The four sub-workflows the chat agent calls. Each takes one JSON item on an Execute Workflow
Trigger and returns one JSON item.

**Universal rules**

- The response is **exactly** `{"ok": true, "data": {…}}` or `{"ok": false, "reason": "<enum>"}`.
  Never both keys, never neither, never a thrown error — a caller always receives an item.
- `reason` is a closed enum (below). Raw driver text is never echoed; a database problem is
  `db_unavailable` and a query that hits the 10 s `statement_timeout` is `timeout`.
- Instants are ISO-8601. Sent **with** an offset (`+08:00`, `+0800`, `+08` or `Z`) they are taken
  at face value; sent **without** one they are read as salon-local `Asia/Manila` (no DST, so
  exactly `+08:00`). The resolved instant always comes back as `*_utc`.
- Every timestamp in a response appears twice: `*_utc` (ends in `Z`) and `*_local`
  (`YYYY-MM-DD HH:MM` in Asia/Manila).
- Passing an array of items fans out: N items in, N index-aligned items out.

---

## 20 · find availability

```jsonc
// in
{ "service_id": "svc_haircut", "date": "2026-09-22", "stylist_id": "sty_maria" }  // stylist_id optional
```
```jsonc
// out
{ "ok": true, "data": {
  "service": { "id": "svc_haircut", "name": "Haircut", "duration_min": 45, "price_php": 450 },
  "date": "2026-09-22",
  "timezone": "Asia/Manila",
  "qualified_stylists": 3,        // 0 = nobody performs this service on this weekday
  "slots": [
    { "stylist_id": "sty_maria", "stylist_name": "Maria Santos",
      "starts_at_utc": "2026-09-22T01:00:00Z", "ends_at_utc": "2026-09-22T01:45:00Z",
      "starts_at_local": "2026-09-22 09:00", "ends_at_local": "09:45" }
  ]
} }
```

`date` is a **salon-local calendar date**. Slots start at the stylist's opening time and step by
the service duration; a slot is only offered if the whole service fits before closing **and**
clears every confirmed appointment, so a 45-minute service is never offered into a 30-minute gap.
Past slots are never returned.

An empty `slots` with `qualified_stylists: 0` means nobody performs that service that day — a
different answer from "everyone is booked", and the agent should say so differently.

`reason`: `invalid_input` · `invalid_date` · `service_not_found` · `stylist_not_found` ·
`stylist_does_not_perform_service` · `db_unavailable` · `timeout`

---

## 21 · create booking

```jsonc
// in
{ "booking_key": "tg-8412-1758271200",           // caller-supplied idempotency key, required
  "client": { "name": "Ana Reyes", "phone": "09171234567",
              "channel": "telegram", "channel_user_id": "8412" },
  "service_id": "svc_haircut", "stylist_id": "sty_maria",
  "starts_at": "2026-09-22T14:15:00+08:00",
  "notes": "first visit" }
```
```jsonc
// out
{ "ok": true, "data": {
  "idempotent_replay": false,
  "ref": "BK-00042", "status": "confirmed",
  "client_id": "cli_9f2a…", "client_name": "Ana Reyes",
  "stylist_id": "sty_maria", "stylist_name": "Maria Santos",
  "service_id": "svc_haircut", "service_name": "Haircut",
  "duration_min": 45, "price_php": 450,
  "starts_at_utc": "2026-09-22T06:15:00Z", "ends_at_utc": "2026-09-22T07:00:00Z",
  "starts_at_local": "2026-09-22 14:15", "ends_at_local": "15:00"
} }
```

- **`ends_at` is always derived from `service.duration_min`.** A caller cannot supply a shorter
  end time to slip past the overlap guard.
- **Idempotency.** The same `booking_key` with the same parameters returns the original booking
  with `idempotent_replay: true` and creates nothing. The same key with *different* parameters is
  `booking_key_conflict` — the one thing worse than failing is telling a client they are booked
  for a time they did not ask for.
- **Client identity** is `(channel, channel_user_id)` when both are given, so a returning chat
  user is recognised instead of duplicated. Phone numbers are stored as typed, not normalised.
- **The race.** The overlap pre-check lives in the same statement as the insert, so the normal
  losing case returns `slot_taken` deterministically. If two transactions get past it at once the
  `EXCLUDE` constraint decides, and that also surfaces as `slot_taken`.

`reason`: `invalid_input` · `invalid_timestamp` · `service_not_found` · `stylist_not_found` ·
`stylist_does_not_perform_service` · `starts_in_the_past` · `outside_business_hours` ·
`slot_taken` · `booking_key_conflict` · `not_created` · `db_unavailable` · `timeout`

---

## 22 · cancel booking

```jsonc
// in
{ "booking_ref": "BK-00042", "cancel_reason": "client rescheduled" }
```
```jsonc
// out
{ "ok": true, "data": { "freed": {
  "ref": "BK-00042",
  "stylist_id": "sty_maria", "stylist_name": "Maria Santos",
  "service_id": "svc_haircut", "service_name": "Haircut",
  "client_id": "cli_9f2a…", "client_name": "Ana Reyes",
  "starts_at_utc": "2026-09-22T06:15:00Z", "ends_at_utc": "2026-09-22T07:00:00Z",
  "starts_at_local": "2026-09-22 14:15", "ends_at_local": "15:00"
} } }
```

`freed` is the whole point: phase 3 hands it straight to the waitlist matcher. The update is
`… WHERE status = 'confirmed'`, so two racing cancels produce exactly one winner and one
`already_cancelled`. The window is bookable again the instant it returns.

`reason`: `invalid_input` · `booking_not_found` · `already_cancelled` ·
`cannot_cancel_completed` · `cannot_cancel_no_show` · `db_unavailable` · `timeout`

---

## 23 · find stylist alternatives

```jsonc
// in
{ "service_id": "svc_haircut",
  "starts_at": "2026-09-22T15:00:00+08:00", "ends_at": "2026-09-22T15:45:00+08:00",
  "exclude_stylist_id": "sty_maria" }                      // optional
```
```jsonc
// out
{ "ok": true, "data": {
  "window": { "starts_at_utc": "…", "ends_at_utc": "…",
              "starts_at_local": "2026-09-22 15:00", "ends_at_local": "15:45" },
  "alternatives": [
    { "stylist_id": "sty_joy", "stylist_name": "Joy Ramirez",
      "title": "Senior Stylist", "rating": 4.8 }
  ]
} }
```

Returns only stylists who perform the service, whose opening hours cover the **whole** window, and
who have nothing confirmed overlapping it. Ranked by rating, then name, then id — the final id key
makes repeated identical calls byte-identical, so the agent never appears to change its mind.

An empty `alternatives` is `ok: true`. "Nobody else is free" is an answer, and phase 3 needs to
tell the client that plainly rather than apologising for a failure.

`reason`: `invalid_input` · `invalid_timestamp` · `invalid_window` · `service_not_found` ·
`db_unavailable` · `timeout`
