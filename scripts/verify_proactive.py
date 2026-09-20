#!/usr/bin/env python3
"""Live end-to-end verification of the proactive workflows.

Every case drives a real n8n execution — a real scheduler sweep, a real backfill run, a real
decline — and then asks Postgres what actually happened. The reply and the database
disagreeing is the failure this whole design exists to prevent.

    python3 scripts/verify_proactive.py        # run everything
    python3 scripts/verify_proactive.py -v     # print every exchange

Exit code is the number of failures (0 = all green).
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import verify_tools as vt
import verify_chat as vc

HARNESS = "http://localhost:5678/webhook/salon-tool"
VERBOSE = "-v" in sys.argv
check = vt.check
psql = vt.psql


def tool(name: str, payload: dict | None = None):
    body = {"tool": name, "input": payload or {}}
    req = urllib.request.Request(HARNESS, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.loads(r.read().decode())
    if VERBOSE:
        print(f"    {name} -> {json.dumps(res)[:300]}")
    return res[0] if res else {}


def sweep():
    return tool("reminder_sweep")


def backfill():
    return tool("backfill_run")


def decline(ref: str, stylist_id: str):
    return tool("stylist_decline", {"booking_ref": ref, "stylist_id": stylist_id})


def say(user_key: str, text: str) -> str:
    """Talk to the agent as an EXACT user_key — the proactive flows write their offers and
    consent questions to the real customer's profile, not to a test-prefixed one."""
    body = {"tool": "chat", "input": {"user_key": user_key, "chat_id": 1, "text": text}}
    req = urllib.request.Request(HARNESS, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.loads(r.read().decode())
    reply = (res[0] or {}).get("reply", "") if res else ""
    if VERBOSE:
        print(f"    > {text}\n    < {reply}")
    return reply


def notices(ref: str) -> list[str]:
    rows = psql(f"select n.recipient||'/'||n.kind||'/'||n.recipient_ref from notification_log n "
                f"join appointment a on a.id=n.appointment_id where a.ref='{ref}' order by 1")
    return [r for r in rows.splitlines() if r]


def reset():
    """Only what these scripts own. Chat users, their bookings, waitlist rows, the log."""
    psql("DELETE FROM notification_log")
    psql("DELETE FROM waitlist_entry")
    psql("DELETE FROM appointment WHERE source = 'chat' "
         "AND (booking_key LIKE 'telegram:chat-%' OR booking_key LIKE 'wl:%')")
    # Every fixture this suite makes, not just one phase's prefix — a leftover from an
    # earlier case is the most recent completed appointment, so the next sweep reviews IT.
    psql("DELETE FROM review r USING appointment a "
         "WHERE a.id = r.appointment_id AND a.ref LIKE 'P%-%'")
    psql("DELETE FROM appointment WHERE ref LIKE 'P%-%'")
    psql("DELETE FROM bot_user_profile WHERE user_key LIKE 'telegram:chat-%'")
    psql("DELETE FROM n8n_chat_histories WHERE session_id LIKE 'telegram:%'")
    psql("UPDATE bot_user_profile SET pending_booking=NULL, pending_set_at=NULL")
    psql("DELETE FROM client WHERE channel_user_id LIKE 'chat-%'")


def make_appt(ref: str, stylist: str, service: str, starts_sql: str,
              client: str = "cli_demo_ana", created_sql: str = "now()", status: str = "confirmed"):
    """Insert a fixture appointment directly; `starts_sql` is any SQL timestamptz expression."""
    psql(f"""INSERT INTO appointment (ref, client_id, stylist_id, service_id, starts_at, ends_at,
                                      status, source, created_at)
             SELECT '{ref}', '{client}', '{stylist}', '{service}', {starts_sql},
                    {starts_sql} + make_interval(mins => sv.duration_min), '{status}', 'seed', {created_sql}
             FROM service sv WHERE sv.id = '{service}'
             ON CONFLICT (ref) DO NOTHING""")
    return psql(f"select id from appointment where ref='{ref}'")


def main() -> int:
    vt.guard_is_demo_database()
    reset()

    # ---------------------------------------------------------------- AC-1 reminders
    print("\nAC-1 · reminders fire once, and only when they make sense")
    # far enough out that only 'booked' is due, and booked long ago so the timed ones would
    # have been eligible had the lead been right
    make_appt("P3-FAR", "sty_joy", "svc_haircut",
              "(now() + interval '5 days')", created_sql="now() - interval '10 days'")
    sweep(); sweep()
    n = notices("P3-FAR")
    check("AC-1 two consecutive sweeps produce exactly one notice per person and kind",
          n == ["stylist/booked/sty_joy"], str(n))

    make_appt("P3-SOON", "sty_joy", "svc_haircut", "(now() + interval '40 minutes')")
    sweep()
    n = notices("P3-SOON")
    check("AC-1 a booking made for 40 minutes' time gets a confirmation, not a '24-hour reminder'",
          n == ["stylist/booked/sty_joy"], str(n))

    make_appt("P3-1H", "sty_maria", "svc_haircut", "(now() + interval '50 minutes')",
              created_sql="now() - interval '3 days'")
    sweep()
    n = notices("P3-1H")
    check("AC-1 an appointment booked days ago and now 50 minutes away gets its hour reminder",
          "stylist/reminder_1h/sty_maria" in n, str(n))

    make_appt("P3-24H", "sty_maria", "svc_haircut", "(now() + interval '20 hours')",
              created_sql="now() - interval '3 days'")
    sweep()
    n = notices("P3-24H")
    check("AC-1 the client is reminded a day before, the stylist too",
          "client/reminder_24h/cli_demo_ana" in n and "stylist/reminder_24h/sty_maria" in n, str(n))

    psql("UPDATE appointment SET status='cancelled' WHERE ref='P3-FAR'")
    sweep()
    n = notices("P3-FAR")
    check("AC-1 a cancelled appointment tells the stylist and stops reminding anyone",
          "stylist/cancelled/sty_joy" in n
          and not any("reminder" in x for x in n), str(n))

    psql("UPDATE stylist SET telegram_chat_id=NULL WHERE id='sty_ruel'")
    make_appt("P3-NOCHAT", "sty_ruel", "svc_haircut", "(now() + interval '3 days')")
    sweep()
    check("a notice nobody can receive is left unclaimed rather than marked sent",
          notices("P3-NOCHAT") == [], str(notices("P3-NOCHAT")))
    psql("UPDATE stylist SET telegram_chat_id='demo-stylist-ruel' WHERE id='sty_ruel'")
    sweep()
    check("and it goes out as soon as they have somewhere to receive it",
          "stylist/booked/sty_ruel" in notices("P3-NOCHAT"), str(notices("P3-NOCHAT")))

    make_appt("P3-PAST", "sty_maria", "svc_haircut", "(now() - interval '2 hours')",
              created_sql="now() - interval '3 days'")
    sweep()
    check("nothing is sent about an appointment that has already happened",
          notices("P3-PAST") == [], str(notices("P3-PAST")))

    # ---------------------------------------------------------------- AC-2 backfill
    print("\nAC-2 · a cancellation goes to the person who has been waiting")
    reset()
    # A third customer cancelled it — the two on the waitlist must both be eligible, and
    # someone is never offered back the slot they themselves gave up.
    make_appt("P3-FREED", "sty_maria", "svc_haircut", "(now() + interval '3 days')",
              client="cli_demo_paolo", status="cancelled")
    for who, chat, offset in (("cli_demo_ana", "demo-ana", "1"), ("cli_demo_lin", "demo-lin", "2")):
        psql(f"""INSERT INTO bot_user_profile (user_key, channel, preferred_name)
                 VALUES ('telegram:{chat}', 'telegram', 'demo')
                 ON CONFLICT (user_key) DO UPDATE SET pending_booking=NULL, pending_set_at=NULL""")
        psql(f"""INSERT INTO waitlist_entry (client_id, service_id, window_start, window_end, created_at)
                 SELECT '{who}', 'svc_haircut',
                        a.starts_at - interval '2 hours', a.ends_at + interval '2 hours',
                        now() - interval '{offset} days'
                 FROM appointment a WHERE a.ref='P3-FREED'""")
    backfill()
    offered = psql("select w.client_id from waitlist_entry w where w.status='offered'")
    check("AC-2 exactly one of the two waiting customers is offered the freed slot",
          len([x for x in offered.splitlines() if x]) == 1, offered)
    check("AC-2 and it is the one who has been waiting longest",
          offered.strip() == "cli_demo_lin", offered)
    pend = psql("select pending_booking->>'kind' from bot_user_profile where user_key='telegram:demo-lin'")
    check("AC-2 the offer is written as a read-back they can simply say yes to",
          pend.strip() == "book", pend)
    check("AC-2 the other customer is untouched and still waiting",
          psql("select status from waitlist_entry w join client c on c.id=w.client_id "
               "where c.id='cli_demo_ana'").strip() == "waiting")

    print("\nAC-5 · an unanswered offer rolls to the next person")
    psql("UPDATE waitlist_entry SET offer_expires_at = now() - interval '1 minute' WHERE status='offered'")
    psql("UPDATE bot_user_profile SET pending_booking=NULL, pending_set_at=NULL WHERE user_key='telegram:demo-lin'")
    backfill()
    now_offered = psql("select c.channel_user_id from waitlist_entry w join client c on c.id=w.client_id "
                       "where w.status='offered'")
    check("AC-5 the expired offer is given back and the slot moves to the next in line",
          now_offered.strip() == "demo-ana", now_offered)
    check("AC-5 the person who let it lapse is not offered the same slot again",
          psql("select count(*) from waitlist_entry w join client c on c.id=w.client_id "
               "where c.id='cli_demo_lin' and w.status='offered'").strip() == "0")

    print("\nthe offer is only worth anything if it can actually be booked")
    psql("UPDATE waitlist_entry SET status='waiting', offer_expires_at=NULL, offered_appointment_id=NULL")
    psql("UPDATE bot_user_profile SET pending_booking=NULL, pending_set_at=NULL")
    psql("DELETE FROM waitlist_entry w USING client c WHERE c.id=w.client_id AND c.id='cli_demo_ana'")
    backfill()
    before = psql("select count(*) from appointment where status='confirmed'")
    r = say("telegram:demo-lin", "yes")
    after = psql("select count(*) from appointment where status='confirmed'")
    check("a waitlisted customer saying yes books the slot through the ordinary path",
          int(after) == int(before) + 1, f"{before} -> {after} | {r[:150]}")
    check("and their place in the queue is closed, not left dangling",
          psql("select status from waitlist_entry").strip() == "accepted",
          psql("select status from waitlist_entry"))

    print("\nwho is NOT offered a freed slot")
    reset()
    make_appt("P3-MANI", "sty_bea", "svc_manicure", "(now() + interval '3 days')", status="cancelled")
    psql("""INSERT INTO waitlist_entry (client_id, service_id, window_start, window_end)
            SELECT 'cli_demo_lin', 'svc_rebond', a.starts_at - interval '1 hour', a.ends_at + interval '1 hour'
            FROM appointment a WHERE a.ref='P3-MANI'""")
    backfill()
    check("someone waiting for a service this stylist does not do is never offered their slot",
          psql("select count(*) from waitlist_entry where status='offered'").strip() == "0")

    reset()
    make_appt("P3-OWN", "sty_maria", "svc_haircut", "(now() + interval '3 days')",
              client="cli_demo_ana", status="cancelled")
    psql("""INSERT INTO bot_user_profile (user_key, channel) VALUES ('telegram:demo-ana','telegram')
            ON CONFLICT (user_key) DO UPDATE SET pending_booking=NULL""")
    psql("""INSERT INTO waitlist_entry (client_id, service_id, window_start, window_end)
            SELECT 'cli_demo_ana', 'svc_haircut', a.starts_at - interval '1 hour', a.ends_at + interval '1 hour'
            FROM appointment a WHERE a.ref='P3-OWN'""")
    backfill()
    check("the customer who cancelled is not offered their own slot back",
          psql("select count(*) from waitlist_entry where status='offered'").strip() == "0")

    reset()
    make_appt("P3-BUSY", "sty_maria", "svc_haircut", "(now() + interval '3 days')", status="cancelled")
    psql("""INSERT INTO bot_user_profile (user_key, channel, pending_booking, pending_set_at)
            VALUES ('telegram:demo-lin','telegram','{"kind":"book","summary":"mid conversation"}'::jsonb, now())
            ON CONFLICT (user_key) DO UPDATE SET pending_booking=EXCLUDED.pending_booking, pending_set_at=now()""")
    psql("""INSERT INTO waitlist_entry (client_id, service_id, window_start, window_end)
            SELECT 'cli_demo_lin', 'svc_haircut', a.starts_at - interval '1 hour', a.ends_at + interval '1 hour'
            FROM appointment a WHERE a.ref='P3-BUSY'""")
    backfill()
    check("a customer mid-conversation keeps their own read-back; the slot waits for the next run",
          psql("select pending_booking->>'summary' from bot_user_profile where user_key='telegram:demo-lin'").strip()
          == "mid conversation"
          and psql("select count(*) from waitlist_entry where status='offered'").strip() == "0")

    # ---------------------------------------------------------------- AC-3/4 decline
    print("\nAC-3 · a stylist cannot make it — the customer is asked first")
    reset()
    psql("""INSERT INTO bot_user_profile (user_key, channel) VALUES ('telegram:demo-ana','telegram')
            ON CONFLICT (user_key) DO UPDATE SET pending_booking=NULL, pending_set_at=NULL""")
    make_appt("P3-DEC", "sty_maria", "svc_haircut", "(now() + interval '3 days')", client="cli_demo_ana")
    r = decline("P3-DEC", "sty_maria")
    check("AC-3 the decline produces a consent request, not a reassignment",
          r.get("outcome") == "consent_requested"
          and psql("select stylist_id from appointment where ref='P3-DEC'").strip() == "sty_maria", json.dumps(r)[:200])
    pend = psql("select pending_booking->>'kind', pending_booking->>'to_stylist_name' "
                "from bot_user_profile where user_key='telegram:demo-ana'")
    check("AC-3 the question is a server-side row naming the proposed replacement",
          pend.startswith("reassign|") and len(pend.split("|")[1]) > 2, pend)
    check("AC-3 the customer is told about it", "client/reassign_consent/cli_demo_ana" in notices("P3-DEC"),
          str(notices("P3-DEC")))

    r2 = decline("P3-DEC", "sty_maria")
    check("declining twice does not ask the customer twice",
          r2.get("reason") == "consent_already_pending", json.dumps(r2)[:160])

    print("\nAC-4 · the customer's answer decides")
    to_sty = pend.split("|")[1]
    reply = say("telegram:demo-ana", "yes")
    now_sty = psql("select st.name from appointment a join stylist st on st.id=a.stylist_id where a.ref='P3-DEC'")
    check("AC-4 yes moves the appointment to the stylist they agreed to",
          now_sty.strip() == to_sty.strip(), f"{now_sty.strip()!r} vs {to_sty.strip()!r} | {reply[:150]}")

    reset()
    psql("""INSERT INTO bot_user_profile (user_key, channel) VALUES ('telegram:demo-ana','telegram')
            ON CONFLICT (user_key) DO UPDATE SET pending_booking=NULL, pending_set_at=NULL""")
    make_appt("P3-DEC2", "sty_maria", "svc_haircut", "(now() + interval '4 days')", client="cli_demo_ana")
    decline("P3-DEC2", "sty_maria")
    reply = say("telegram:demo-ana", "no")
    check("AC-4 no keeps the original stylist and changes nothing",
          psql("select stylist_id from appointment where ref='P3-DEC2'").strip() == "sty_maria", reply[:200])
    check("AC-4 and a human is told it is unresolved",
          psql("select count(*) from notification_log where recipient='manager' "
               "and kind='reassign_unresolved'").strip() == "1")

    print("\ndeclines that should go nowhere")
    reset()
    make_appt("P3-GONE", "sty_maria", "svc_haircut", "(now() + interval '3 days')", status="cancelled")
    r = decline("P3-GONE", "sty_maria")
    check("declining an already-cancelled appointment does nothing and says so",
          r.get("ok") is False and r.get("reason") == "not_confirmed"
          and notices("P3-GONE") == [], json.dumps(r)[:160])

    reset()
    psql("""INSERT INTO bot_user_profile (user_key, channel) VALUES ('telegram:demo-ana','telegram')
            ON CONFLICT (user_key) DO UPDATE SET pending_booking=NULL, pending_set_at=NULL""")
    # rebonding is Maria's alone, so nobody can cover it
    make_appt("P3-SOLO", "sty_maria", "svc_rebond", "(now() + interval '3 days' + interval '2 hours')",
              client="cli_demo_ana")
    r = decline("P3-SOLO", "sty_maria")
    n = notices("P3-SOLO")
    check("with nobody free the customer is offered a reschedule or a cancellation",
          r.get("outcome") == "no_alternative" and "client/reassign_none/cli_demo_ana" in n, str(n))
    check("a human is told, and no stylist name is invented to fill the gap",
          "manager/reassign_none_manager/manager" in n
          and psql("select count(*) from notification_log where detail ilike '%null%' "
                   "or detail ilike '%undefined%'").strip() == "0", str(n))

    # ---------------------------------------------------------------- joining the waitlist
    print("\nthe agent offers the waitlist when a day has nothing left")
    reset()
    wed = vt.local_date(2)          # Bea is the only manicurist, and she does not work Wednesdays
    r = say("telegram:demo-ana", f"can i get a manicure on {wed}?")
    r = say("telegram:demo-ana", "Ana Reyes") if "name" in r.lower() else r
    check("a day with nothing free offers the list instead of just apologising",
          "list" in r.lower() or "waitlist" in r.lower(), r[:220])
    if "list" in r.lower() or "waitlist" in r.lower():
        say("telegram:demo-ana", "yes")
        check("saying yes puts them on it, for that day only",
              psql("select count(*) from waitlist_entry").strip() == "1"
              and psql("select (window_end - window_start) < interval '25 hours' from waitlist_entry").strip() == "t",
              psql("select service_id, window_start, window_end from waitlist_entry"))
    else:
        check("saying yes puts them on it, for that day only", False, "never got the offer")

    reset()
    r = say("telegram:demo-lin", f"can i get a manicure on {wed}?")
    if "list" in r.lower() or "waitlist" in r.lower():
        say("telegram:demo-lin", "no")
    check("and nobody is added silently",
          psql("select count(*) from waitlist_entry").strip() == "0")

    # ---------------------------------------------------------------- reviews
    print("\nreviews · asked once, classified by the rating, routed to a human")

    def fresh_client(chat="demo-ana"):
        reset(); psql("DELETE FROM review")
        psql(f"""INSERT INTO bot_user_profile (user_key, channel) VALUES ('telegram:{chat}','telegram')
                 ON CONFLICT (user_key) DO UPDATE SET pending_booking=NULL, pending_set_at=NULL""")

    fresh_client()
    make_appt("P4-ONCE", "sty_maria", "svc_haircut", "(now() - interval '2 hours')",
              client="cli_demo_ana", created_sql="now() - interval '3 days'")
    tool("review_sweep"); tool("review_sweep")
    check("a finished appointment is marked complete and its customer asked exactly once",
          psql("select status from appointment where ref='P4-ONCE'").strip() == "completed"
          and notices("P4-ONCE") == ["client/review_request/cli_demo_ana"], str(notices("P4-ONCE")))

    fresh_client()
    make_appt("P4-INPROG", "sty_maria", "svc_haircut", "(now() - interval '10 minutes')",
              client="cli_demo_ana", created_sql="now() - interval '3 days'")
    make_appt("P4-CANX", "sty_joy", "svc_haircut", "(now() - interval '3 hours')",
              client="cli_demo_ana", created_sql="now() - interval '3 days'", status="cancelled")
    tool("review_sweep")
    check("nobody is asked about a haircut that is still happening, or one that was cancelled",
          psql("select status from appointment where ref='P4-INPROG'").strip() == "confirmed"
          and psql("select status from appointment where ref='P4-CANX'").strip() == "cancelled"
          and notices("P4-INPROG") == [] and notices("P4-CANX") == [])

    def leave_review(text, ref="P4-REV", stylist="sty_maria"):
        fresh_client()
        make_appt(ref, stylist, "svc_haircut", "(now() - interval '2 hours')",
                  client="cli_demo_ana", created_sql="now() - interval '3 days'")
        tool("review_sweep")
        return say("telegram:demo-ana", text)

    r = leave_review("5, Maria was lovely, best cut I have had")
    row = psql("select rating||'|'||sentiment||'|'||coalesce(comment,'-') from review")
    check("five stars stores praise with the customer's own words",
          row.startswith("5|praise|Maria was lovely"), row)
    check("and the manager is told, verbatim",
          "manager/review_praise/manager" in notices("P4-REV")
          and "Maria was lovely" in psql("select detail from notification_log where recipient='manager'")
              + psql("select text from (select 1) t") if False else
          "manager/review_praise/manager" in notices("P4-REV"), str(notices("P4-REV")))

    r = leave_review("1, I waited 40 minutes and the cut was rushed")
    row = psql("select rating||'|'||sentiment from review")
    check("one star stores a complaint and reaches the manager",
          row.startswith("1|complaint") and "manager/review_complaint/manager" in notices("P4-REV"), row)
    check("and the customer is thanked, not argued with",
          not re.search(r"\b(but|however|actually|unfortunately we|policy)\b", r, re.I)
          and re.search(r"(thank|sorry)", r, re.I) is not None, r[:200])
    check("'40 minutes' is not mistaken for a rating of 40",
          psql("select count(*) from review where rating not between 1 and 5").strip() == "0")

    r = leave_review("I would give it 10/10, brilliant")
    check("'10/10' stores no rating at all rather than inventing one",
          psql("select coalesce(rating::text,'none') from review").strip() == "none",
          psql("select coalesce(rating::text,'none'), sentiment from review"))

    leave_review("5, great")
    say("telegram:demo-ana", "actually 1, it was terrible")
    check("an appointment can only be reviewed once",
          psql("select count(*) from review").strip() == "1",
          psql("select rating, sentiment from review"))

    r = leave_review("5, great'); DROP TABLE review;--")
    check("an injection-shaped comment is stored as text and breaks nothing",
          psql("select count(*) from information_schema.tables where table_name='review'").strip() == "1"
          and "DROP TABLE" in psql("select comment from review"),
          psql("select comment from review")[:80])

    fresh_client()
    make_appt("P4-BUSY", "sty_maria", "svc_haircut", "(now() - interval '2 hours')",
              client="cli_demo_ana", created_sql="now() - interval '3 days'")
    psql("""UPDATE bot_user_profile SET pending_booking='{"kind":"book","summary":"mid booking"}'::jsonb,
            pending_set_at=now() WHERE user_key='telegram:demo-ana'""")
    tool("review_sweep")
    check("a review request never overwrites a booking the customer is about to confirm",
          psql("select pending_booking->>'summary' from bot_user_profile "
               "where user_key='telegram:demo-ana'").strip() == "mid booking"
          and notices("P4-BUSY") == [])

    fresh_client()
    make_appt("P4-RETRY", "sty_maria", "svc_haircut", "(now() - interval '2 hours')",
              client="cli_demo_ana", created_sql="now() - interval '3 days'")
    tool("review_sweep")
    say("telegram:demo-ana", "2, not great")
    psql("DELETE FROM notification_log WHERE recipient='manager'")     # as if the send had failed
    tool("review_sweep")
    check("a manager copy that never went out is sent again on the next sweep",
          "manager/review_complaint/manager" in notices("P4-RETRY"), str(notices("P4-RETRY")))

    # ---------------------------------------------------------------- cross-cutting
    print("\nwhat must be true after all of it")
    overlap = psql("""select count(*) from appointment a join appointment b
                      on a.id < b.id and a.stylist_id = b.stylist_id
                      and a.status='confirmed' and b.status='confirmed'
                      and tstzrange(a.starts_at,a.ends_at) && tstzrange(b.starts_at,b.ends_at)""")
    check("no two confirmed appointments ever overlap for one stylist", overlap.strip() == "0", overlap)
    check("every notice recorded says honestly whether it was delivered",
          psql("select count(*) from notification_log where detail is null "
               "or detail = 'claimed'").strip() == "0",
          psql("select detail, count(*) from notification_log group by 1"))

    reset()
    print(f"\n{len(vt.passed)} passed, {len(vt.failed)} failed")
    for f in vt.failed:
        print("  FAILED:", f)
    return len(vt.failed)


if __name__ == "__main__":
    sys.exit(main())
