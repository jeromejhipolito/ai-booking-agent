#!/usr/bin/env python3
"""Live end-to-end verification of the four booking tools.

Every case below fires a REAL n8n execution through the dev harness webhook
(`90_dev_test_harness`), which calls the real sub-workflow, which hits the real Postgres.
Nothing here stubs a layer — if it passes, the tool works.

    python3 scripts/verify_tools.py            # run everything
    python3 scripts/verify_tools.py -v         # also print each response

Exit code is the number of failures (0 = all green).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
HARNESS = "http://localhost:5678/webhook/salon-tool"
CONTAINER = "booking-pg-local"
DB = ["-U", "n8n", "-d", "salon_booking"]
VERBOSE = "-v" in sys.argv

passed: list[str] = []
failed: list[str] = []


# --------------------------------------------------------------------------- plumbing
def psql(sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", *DB, "-Atc", sql],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip()


def call(tool: str, payload: dict | None = None, many: list[dict] | None = None):
    body = {"tool": tool}
    if many is not None:
        body["inputs"] = many
    else:
        body["input"] = payload or {}
    req = urllib.request.Request(
        HARNESS, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode())
    if VERBOSE:
        print(f"    -> {json.dumps(res)[:400]}")
    return res if many is not None else res[0]


def check(name: str, cond: bool, detail: str = ""):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail and not cond else ''}")


def contract_ok(r: dict) -> bool:
    """{ok:true, data} or {ok:false, reason} — never both, never neither."""
    if not isinstance(r, dict) or "ok" not in r:
        return False
    if r["ok"]:
        return "data" in r and "reason" not in r
    return "reason" in r and "data" not in r


contract_violations: list[str] = []


def R(label: str, tool: str, payload: dict):
    r = call(tool, payload)
    if not contract_ok(r):
        contract_violations.append(f"{label}: {json.dumps(r)[:120]}")
    return r


# --------------------------------------------------------------------------- fixture
OWNED_KEYS = ("verify-", "race-", "doc-", "telegram:chat-")
"""Booking keys the verification scripts own and may destroy. `telegram:chat-` is what
verify_chat.py mints, and both scripts share this fixture — leaving it out made a chat
run's own leftovers look like a live salon's real data and refuse the next run."""


def guard_is_demo_database():
    """Refuse to run anywhere that holds real bookings.

    reset() deletes chat-created appointments, which is exactly right against the demo fixture
    and catastrophic against a live salon. Two conditions must hold: the demo seed is present,
    and every chat booking already belongs to this script.
    """
    if psql("select count(*) from stylist where id in ('sty_maria','sty_joy','sty_ruel','sty_bea')") != "4":
        raise SystemExit(
            "refusing to run: this database does not contain the demo seed "
            "(deploy/seed-demo-salon.sql). verify_tools.py deletes chat bookings and is only "
            "safe against a demo fixture."
        )
    stray = psql(
        "select count(*) from appointment where source = 'chat' and ("
        "booking_key is null or " +
        " and ".join(f"booking_key not like '{k}%'" for k in OWNED_KEYS) + ")"
    )
    if stray != "0":
        raise SystemExit(
            f"refusing to run: {stray} chat booking(s) here were not created by this script. "
            "That looks like real data — point this at a throwaway database."
        )


def reset():
    """Back to the seeded state: drop what earlier runs booked, re-apply the seed."""
    psql("DELETE FROM appointment WHERE source = 'chat' AND ("
         + " or ".join(f"booking_key like '{k}%'" for k in OWNED_KEYS) + ")")
    psql("DELETE FROM client WHERE id LIKE 'cli_%' AND id NOT LIKE 'cli_demo_%'")
    seed = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", *DB, "-q", "-v", "ON_ERROR_STOP=1"],
        # Resolved against the repo, not the shell's cwd — the script must work from anywhere.
        stdin=open(ROOT / "deploy" / "seed-demo-salon.sql"), capture_output=True, text=True,
    )
    if seed.returncode != 0:
        raise RuntimeError("seed failed: " + seed.stderr)


def local_date(offset_days: int) -> str:
    """A salon-local calendar date, N days after next Monday."""
    return psql(
        "select (date_trunc('week', ((now() at time zone 'Asia/Manila')::date + 7)::timestamp)::date"
        f" + {offset_days})::text"
    )


def main() -> int:
    guard_is_demo_database()
    reset()
    TUE, MON, WED = local_date(1), local_date(0), local_date(2)

    # ---------------------------------------------------------------- 20 availability
    print("\n20 · find availability")
    r = R("avail/base", "find_availability", {"service_id": "svc_haircut", "date": TUE, "stylist_id": "sty_maria"})
    slots = r.get("data", {}).get("slots", [])
    starts = [s["starts_at_local"][-5:] for s in slots]
    check("AC-5 slots are inside business hours and stepped by the service duration",
          r["ok"] and starts[:1] == ["09:00"] and all(s["ends_at_local"] <= "18:00" for s in slots),
          str(starts))
    check("H-14 a slot overlapping a confirmed appointment is never offered "
          "(Maria is booked 10:00-12:00 and 13:00-13:45)",
          all(s not in starts for s in ("09:45", "10:30", "11:15", "12:45", "13:30")), str(starts))
    check("H-14 the gap before a booking is only offered when the whole service fits "
          "(12:00 fits before 13:00, 12:45 does not)", "12:00" in starts, str(starts))
    check("H-2 every slot carries both a UTC and a Manila rendering",
          all(s["starts_at_utc"].endswith("Z") and len(s["starts_at_local"]) == 16 for s in slots))

    r = R("avail/all", "find_availability", {"service_id": "svc_haircut", "date": TUE})
    check("AC-5 without stylist_id every qualified stylist is considered",
          r["ok"] and len({s["stylist_id"] for s in r["data"]["slots"]}) > 1)

    r = R("avail/closed", "find_availability", {"service_id": "svc_haircut", "date": MON, "stylist_id": "sty_maria"})
    check("H-13 a stylist with no hours that weekday yields zero slots, not an open day",
          r["ok"] and r["data"]["slots"] == [] and r["data"]["qualified_stylists"] == 0)

    r = R("avail/unknown-service", "find_availability", {"service_id": "svc_nope", "date": TUE})
    check("AC-5 unknown service → service_not_found", r == {"ok": False, "reason": "service_not_found"})

    r = R("avail/bad-date", "find_availability", {"service_id": "svc_haircut", "date": "tomorrow"})
    check("H-15 date 'tomorrow' → invalid_date (no Postgres 22P02 leaks out)",
          r == {"ok": False, "reason": "invalid_date"})

    r = R("avail/impossible-date", "find_availability", {"service_id": "svc_haircut", "date": "2026-02-30"})
    check("H-15 a calendar-impossible date → invalid_date", r == {"ok": False, "reason": "invalid_date"})

    r = R("avail/no-service-id", "find_availability", {"date": TUE})
    check("H-15 missing service_id → invalid_input", r == {"ok": False, "reason": "invalid_input"})

    r = R("avail/wrong-stylist", "find_availability",
          {"service_id": "svc_manicure", "date": TUE, "stylist_id": "sty_maria"})
    check("H-6 a stylist who does not perform the service → stylist_does_not_perform_service",
          r == {"ok": False, "reason": "stylist_does_not_perform_service"})

    psql("INSERT INTO service (id,name,duration_min,price_php) VALUES "
         "('svc_verify_orphan','Orphan',30,100) ON CONFLICT (id) DO NOTHING")
    r = R("avail/no-qualified", "find_availability", {"service_id": "svc_verify_orphan", "date": TUE})
    check("H-12 a service no stylist performs → ok with zero slots, not a full grid",
          r["ok"] and r["data"]["slots"] == [] and r["data"]["qualified_stylists"] == 0)
    psql("DELETE FROM service WHERE id='svc_verify_orphan'")

    r = R("avail/past", "find_availability", {"service_id": "svc_haircut", "date": "2020-01-07"})
    check("H-21 a past date offers nothing", r["ok"] and r["data"]["slots"] == [])

    # ---------------------------------------------------------------- 21 create
    print("\n21 · create booking")
    who = {"name": "Verify Client", "phone": "09170000000", "channel": "telegram", "channel_user_id": "verify-1"}
    base = {"booking_key": "verify-a", "client": who, "service_id": "svc_haircut",
            "stylist_id": "sty_maria", "starts_at": f"{TUE}T14:15:00+08:00"}

    r = R("create/ok", "create_booking", base)
    check("AC-6 a free slot books and returns a reference",
          r["ok"] and r["data"]["ref"].startswith("BK-") and r["data"]["idempotent_replay"] is False,
          json.dumps(r)[:160])
    ref_a = r["data"]["ref"] if r["ok"] else None
    check("H-4 ends_at is derived from the service duration, not from the caller",
          r["ok"] and r["data"]["starts_at_local"].endswith("14:15") and r["data"]["ends_at_local"] == "15:00")

    r = R("create/replay", "create_booking", base)
    check("AC-6 the same booking_key replays the original booking and creates no second row",
          r["ok"] and r["data"]["ref"] == ref_a and r["data"]["idempotent_replay"] is True)
    check("AC-6 exactly one row exists for that booking_key",
          psql("select count(*) from appointment where booking_key='verify-a'") == "1")

    r = R("create/key-conflict", "create_booking", {**base, "starts_at": f"{TUE}T16:00:00+08:00"})
    check("H-8 the same key with different params is refused, never silently confirmed",
          r == {"ok": False, "reason": "booking_key_conflict"})

    r = R("create/taken", "create_booking", {**base, "booking_key": "verify-b"})
    check("AC-6 an overlapping slot returns slot_taken without aborting the run",
          r == {"ok": False, "reason": "slot_taken"})

    r = R("create/adjacent", "create_booking",
          {**base, "booking_key": "verify-c", "starts_at": f"{TUE}T15:00:00+08:00"})
    check("AC-3 a booking starting exactly when another ends is allowed (half-open range)", r["ok"])

    r = R("create/naked-ts", "create_booking",
          {**base, "booking_key": "verify-d", "starts_at": f"{TUE}T16:30:00"})
    check("H-3 a timestamp with no offset is read as Asia/Manila (+08:00) and echoed back resolved",
          r["ok"] and r["data"]["starts_at_utc"] == f"{TUE}T08:30:00Z"
          and r["data"]["starts_at_local"].endswith("16:30"), json.dumps(r)[:160])

    r = R("create/before-open", "create_booking",
          {**base, "booking_key": "verify-e", "starts_at": f"{TUE}T08:00:00+08:00"})
    check("H-5 before opening time → outside_business_hours", r == {"ok": False, "reason": "outside_business_hours"})

    r = R("create/past-close", "create_booking",
          {**base, "booking_key": "verify-f", "starts_at": f"{TUE}T17:30:00+08:00"})
    check("H-5 a service that would run past closing → outside_business_hours",
          r == {"ok": False, "reason": "outside_business_hours"})

    r = R("create/closed-day", "create_booking",
          {**base, "booking_key": "verify-g", "starts_at": f"{MON}T10:00:00+08:00"})
    check("H-5 a weekday the stylist does not work → outside_business_hours",
          r == {"ok": False, "reason": "outside_business_hours"})

    r = R("create/past", "create_booking",
          {**base, "booking_key": "verify-h", "starts_at": "2020-01-07T10:00:00+08:00"})
    check("H-7 a start in the past → starts_in_the_past", r == {"ok": False, "reason": "starts_in_the_past"})

    r = R("create/not-performed", "create_booking",
          {**base, "booking_key": "verify-i", "service_id": "svc_manicure",
           "starts_at": f"{WED}T10:00:00+08:00"})
    check("H-6 create refuses a stylist who does not perform the service",
          r == {"ok": False, "reason": "stylist_does_not_perform_service"})

    r = R("create/unknown-stylist", "create_booking",
          {**base, "booking_key": "verify-j", "stylist_id": "sty_ghost"})
    check("AC-6 unknown stylist → stylist_not_found", r == {"ok": False, "reason": "stylist_not_found"})

    r = R("create/bad-ts", "create_booking", {**base, "booking_key": "verify-k", "starts_at": "next tuesday"})
    check("H-15 an unparseable timestamp → invalid_timestamp", r == {"ok": False, "reason": "invalid_timestamp"})

    r = R("create/blank-key", "create_booking", {**base, "booking_key": "   "})
    check("H-15 a blank booking_key → invalid_input", r == {"ok": False, "reason": "invalid_input"})

    r = R("create/no-name", "create_booking", {**base, "booking_key": "verify-l", "client": {"phone": "0917"}})
    check("H-15 a client with no name → invalid_input", r == {"ok": False, "reason": "invalid_input"})

    # ---------------------------------------------------------------- 22 cancel
    print("\n22 · cancel booking")
    r = R("cancel/ok", "cancel_booking", {"booking_ref": ref_a, "cancel_reason": "verification"})
    freed = r.get("data", {}).get("freed", {})
    check("AC-7 cancelling returns the freed stylist + window",
          r["ok"] and freed.get("stylist_id") == "sty_maria" and freed.get("starts_at_local", "").endswith("14:15"),
          json.dumps(r)[:200])
    check("AC-7 the row is now cancelled",
          psql(f"select status from appointment where ref='{ref_a}'") == "cancelled")

    r = R("cancel/again", "cancel_booking", {"booking_ref": ref_a})
    check("H-11 cancelling twice → already_cancelled", r == {"ok": False, "reason": "already_cancelled"})

    r = R("cancel/unknown", "cancel_booking", {"booking_ref": "BK-99999"})
    check("AC-7 an unknown reference → booking_not_found", r == {"ok": False, "reason": "booking_not_found"})

    r = R("cancel/blank", "cancel_booking", {"booking_ref": ""})
    check("H-15 a blank reference → invalid_input", r == {"ok": False, "reason": "invalid_input"})

    r = R("cancel/rebook", "create_booking", {**base, "booking_key": "verify-m"})
    check("AC-7 the freed window is immediately bookable again", r["ok"], json.dumps(r)[:160])

    psql("UPDATE appointment SET status='completed' WHERE booking_key='verify-m'")
    r = R("cancel/completed", "cancel_booking",
          {"booking_ref": psql("select ref from appointment where booking_key='verify-m'")})
    check("H-11 a completed appointment cannot be cancelled",
          r == {"ok": False, "reason": "cannot_cancel_completed"})

    # ---------------------------------------------------------------- 23 alternatives
    print("\n23 · find stylist alternatives")
    win = {"service_id": "svc_haircut",
           "starts_at": f"{TUE}T15:00:00+08:00", "ends_at": f"{TUE}T15:45:00+08:00"}

    r = R("alt/exclude", "find_stylist_alternatives", {**win, "exclude_stylist_id": "sty_maria"})
    ids = [a["stylist_id"] for a in r.get("data", {}).get("alternatives", [])]
    check("AC-8 the excluded stylist never appears", r["ok"] and "sty_maria" not in ids and ids, str(ids))
    check("H-20 ranking is by rating, with a deterministic tie-break",
          ids == sorted(ids, key=lambda i: -float(psql(f"select rating from stylist where id='{i}'"))), str(ids))
    check("AC-8 repeated identical calls return an identical order",
          [a["stylist_id"] for a in call("find_stylist_alternatives",
                                         {**win, "exclude_stylist_id": "sty_maria"})["data"]["alternatives"]] == ids)

    r = R("alt/busy", "find_stylist_alternatives",
          {"service_id": "svc_haircut", "starts_at": f"{TUE}T14:15:00+08:00", "ends_at": f"{TUE}T15:00:00+08:00"})
    ids = [a["stylist_id"] for a in r["data"]["alternatives"]]
    check("AC-8 a stylist with a confirmed appointment in the window is excluded",
          r["ok"] and "sty_joy" not in ids, str(ids))

    r = R("alt/none", "find_stylist_alternatives",
          {"service_id": "svc_manicure", "starts_at": f"{TUE}T16:00:00+08:00",
           "ends_at": f"{TUE}T16:45:00+08:00", "exclude_stylist_id": "sty_bea"})
    check("AC-8 nobody free is ok:true with an empty list, not an error",
          r["ok"] and r["data"]["alternatives"] == [])

    r = R("alt/after-close", "find_stylist_alternatives",
          {"service_id": "svc_haircut", "starts_at": f"{TUE}T21:00:00+08:00", "ends_at": f"{TUE}T21:45:00+08:00"})
    check("H-13 a window outside opening hours returns nobody", r["ok"] and r["data"]["alternatives"] == [])

    r = R("alt/backwards", "find_stylist_alternatives",
          {"service_id": "svc_haircut", "starts_at": f"{TUE}T16:00:00+08:00", "ends_at": f"{TUE}T15:00:00+08:00"})
    check("H-15 a window that ends before it starts → invalid_window",
          r == {"ok": False, "reason": "invalid_window"})

    r = R("alt/zero", "find_stylist_alternatives",
          {"service_id": "svc_haircut", "starts_at": f"{TUE}T16:00:00+08:00", "ends_at": f"{TUE}T16:00:00+08:00"})
    check("H-15 a zero-length window → invalid_window (it would overlap nothing)",
          r == {"ok": False, "reason": "invalid_window"})

    # ---------------------------------------------------------------- cross-cutting
    print("\ncross-cutting")
    multi = call("cancel_booking", many=[{"booking_ref": "BK-99991"}, {"booking_ref": ""},
                                         {"booking_ref": "BK-99993"}])
    check("H-18 a 3-item call returns 3 index-aligned results (no silent fan-out collapse)",
          len(multi) == 3 and [m["reason"] for m in multi] ==
          ["booking_not_found", "invalid_input", "booking_not_found"], json.dumps(multi)[:200])

    before = psql("select count(*) from appointment")
    r = R("inject/service", "find_availability",
          {"service_id": "x'; DROP TABLE appointment; --", "date": TUE})
    r2 = R("inject/ref", "cancel_booking", {"booking_ref": "x'); DELETE FROM appointment; --"})
    tables = psql("select count(*) from information_schema.tables where table_name='appointment'")
    check("H-19 SQL injection through either tool changes nothing (queries are parameterised)",
          tables == "1" and psql("select count(*) from appointment") == before
          and r["ok"] is False and r2["ok"] is False)

    reset()
    psql("DELETE FROM appointment WHERE booking_key LIKE 'race-%'")
    race = {"client": who, "service_id": "svc_haircut", "stylist_id": "sty_maria",
            "starts_at": f"{TUE}T14:15:00+08:00"}
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda i: call("create_booking", {**race, "booking_key": f"race-{i}"}), range(8)))
    oks = [x for x in res if x.get("ok")]
    check("H-10 eight concurrent bookings of one slot → exactly one wins, the rest read slot_taken",
          len(oks) == 1
          and all(x.get("reason") == "slot_taken" for x in res if not x.get("ok"))
          and psql(f"select count(*) from appointment where starts_at = '{TUE}T14:15:00+08:00'"
                   " and stylist_id='sty_maria' and status='confirmed'") == "1",
          json.dumps([x.get("reason", "ok") for x in res]))

    psql("DELETE FROM appointment WHERE booking_key LIKE 'race-%'")
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda _: call("create_booking", {**race, "booking_key": "race-same"}), range(8)))
    refs = {x["data"]["ref"] for x in res if x.get("ok")}
    check("H-9 eight concurrent calls with ONE booking_key → one row, one reference, no duplicate-key error",
          len(refs) <= 1 and psql("select count(*) from appointment where booking_key='race-same'") == "1"
          and all(x.get("ok") or x.get("reason") in ("slot_taken",) for x in res),
          json.dumps([x.get("reason", "ok") for x in res]))

    # The whole slot grid is built with an explicit AT TIME ZONE, so a server whose session
    # timezone is anything else must produce the same bytes. This is the invariant that a
    # stray `::date` on a timestamptz breaks, silently, by a whole day near local midnight.
    q = {"service_id": "svc_haircut", "date": TUE, "stylist_id": "sty_maria"}
    before = json.dumps(call("find_availability", q))
    psql("ALTER DATABASE salon_booking SET TimeZone='America/New_York'")
    try:
        after = json.dumps(call("find_availability", q))
    finally:
        psql("ALTER DATABASE salon_booking RESET TimeZone")
    check("H-1 availability is byte-identical with the database session on America/New_York",
          before == after)

    check("H-17 every response matched the {ok} + {data|reason} contract",
          not contract_violations, "; ".join(contract_violations[:3]))

    reset()
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print("  FAILED:", f)
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
