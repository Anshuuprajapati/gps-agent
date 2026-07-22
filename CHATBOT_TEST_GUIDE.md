# Full Chatbot Test Guide — GPS Support Agent

This supersedes `MANUAL_TEST_SCENARIOS.md`, which predates this session's
changes (`engine_version`, `pending_action_json`, the ticket status
lifecycle, and several new conversation behaviors). Everything below
reflects the bot as it exists right now.

**Important — don't hand-paste raw CSV rows.** `data/mock_sessions.csv`'s
actual on-disk column order does **not** match the order columns are
listed in `core/session_manager.py`'s `COLUMNS` (columns were appended
over time as features were added, never reordered) — a hand-typed CSV
line built from the code's logical column list will silently land in the
wrong fields. Every scenario below instead uses
`session_manager.create_session(...)`, which takes field names as keyword
arguments and can never misalign columns, regardless of the file's actual
on-disk order. This is the same function `reset_session.py` and the real
outage-trigger job already use.

How to use this doc: each scenario gives you (1) a one-line Python
snippet to seed the session, and (2) the exact messages to send. The bot
reads the CSV fresh on every message, so you don't need to restart the
server between scenarios.

**Replace `<YOUR_PHONE>`** everywhere with the real WhatsApp number you're
testing from, digits-only, country code, no `+` (e.g. `919876543210`).

## Setup

```bash
uvicorn main:app --reload --port 8000
```

Reset a vehicle back to a clean `START` state:
```bash
python reset_session.py MH12AB1234 --full
```

Fire the first proactive alert for a session already sitting at `START`:
```bash
curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AB1234
```

If you ever need the CSV's *actual* current column order (e.g. to read
a row by eye), check the live file rather than trusting any doc:
```bash
head -1 data/mock_sessions.csv
```

---

## 1. Battery / main power alerts (unchanged)

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0001',
    last_location='Nagpur', timestamp='2026-07-21 09:00:00', gpstime='21 July 2026 09:00',
    main_powervoltage='10.8', ismainpoerconnected='1', gpsStatus='0')
"
curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AA0001
```
**Expect:** low-battery alert (voltage < 11.5) with **Self** / **Driver** buttons.

| You send | Bot should reply |
|---|---|
| *(tap "Self")* | "Battery terminals check kijiye..." |
| `Done` | Re-checks telemetry; voltage still 10.8 → **ASK_PHYSICAL_DAMAGE** |

Main power disconnected is the same shape — set `ismainpoerconnected='0'`
instead of dropping voltage, and expect wiring-specific wording.

---

## 2. Vehicle status — all six outcomes

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0002',
    last_location='Nagpur', main_powervoltage='12.6', ismainpoerconnected='1', gpsStatus='0',
    current_state='ASK_VEHICLE_STATUS')
"
```

| You send | Status | Bot should do |
|---|---|---|
| `Gaadi workshop mein hai` | WORKSHOP | asks expected date it'll be out |
| `Accident ho gaya hai` | ACCIDENT | asks expected date it'll be running again — same as WORKSHOP, never the service-booking flow |
| `GPS nikal diya hai` | GPS_REMOVED | starts booking flow (ASK_CURRENT_LOCATION) |
| `GPS device damage ho gaya hai` | GPS_DAMAGED | asks Yes/No to book a GPS repair |
| `Vehicle chal rahi hai bas GPS nahi chal raha` | GPS_DAMAGED | same as above |
| `abhi pata nahi kaha hai, jab aayegi tab bata denge` | **DEFER_UNKNOWN** (new this session) | acknowledges, saves a default follow-up date (tomorrow), closes to COMPLETED — does **not** loop/fall back to a generic clarification |
| `pta ni kaha hai gadi, aane pe batayenge` (Hinglish variant) | DEFER_UNKNOWN | same as above — confirms this isn't a hardcoded keyword match, it's LLM-classified |

**GPS_DAMAGED + "on the way" combo** (regression check): send
`Gps not working h but abhi vehicle on the way h jab aayegi tb inform krunga`
— must go straight to a closed/deferred case with a save-the-date message,
not the GPS-repair Yes/No buttons.

---

## 3. Direct technician dispatch — zero-friction flow (new this session)

This is the "no further questions, just create the ticket" path, and it
now recognizes far more phrasings than a literal "send tech."

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0003',
    last_location='Nagpur', main_powervoltage='12.6', ismainpoerconnected='1', gpsStatus='0',
    current_state='ASK_VEHICLE_STATUS')
"
```

| You send | Bot should do |
|---|---|
| `Gadi Punjabi Bagh me hai aaj ladka bhej do` | Extracts location (Punjabi Bagh) + creates the ticket **immediately** — no date/time/contact questions, defaults filled silently (today's date, "+2 hours" time window, driver-on-file or `NOT_PROVIDED` contact) |
| `Hmm gps is not working send your person in Punjab Bagh` | Same — "send your person" is not a literal keyword match, this specifically tests the LLM-classified `DIRECT_TECH_DISPATCH` path, not the regex fast path |
| `koi bhej do abhi` (no location yet) | Asks **only** for location (the one hard-blocking field), then creates the ticket the instant it's given — still no date/time/contact questions |

**Verify in `data/tickets.csv`:** a new row appears right after the
location is known, with `service_date`/`service_time` already filled
(not blank), and `status` = `ASSIGNED` (or `OPEN` if it's a GPS_DAMAGED
case with no engineer assigned).

---

## 4. Ticket-status inquiry (new this session)

First create a ticket (via scenario 3, or scenario 8's full booking flow),
note its `ticket_id` from `data/tickets.csv`, then seed a session that
already owns it:
```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0004',
    last_location='Nagpur', current_state='COMPLETED',
    ticket_id='TKT-EXAMPLE1', engineer_id='ENG001', engineer_name='Ramesh Kumar', engineer_phone='919000000001')
"
```
(Replace `TKT-EXAMPLE1` with the real ticket_id.)

| You send | Bot should do |
|---|---|
| `kya meri koi complaint register hai?` | Looks up the session's own `ticket_id` and replies with real status/location/date/engineer — not a generic "send your ticket ID" fallback |
| `TKT-EXAMPLE1 ki details batao` | Looks up that **exact** ticket ID directly (regex short-circuit, no LLM call needed) — works even for a ticket that isn't this session's own |
| `iski status kya hai` (fresh session, no ticket at all) | Graceful "ticket nahi mila" reply, not a crash or a made-up answer |

---

## 5. Generic acknowledgments (new this session)

Reuse scenario 2's session (or re-seed it).

| You send | Bot should do |
|---|---|
| `ok` | Short acknowledgment only — does **not** re-dump the full WORKSHOP/ACCIDENT/RUNNING/GPS menu |
| `thanks` | Same — short ack |
| `thik hai` / `theek hai` | Same |
| `ok workshop me hai` | Must **not** be treated as a bare ack (contains a real answer) — should classify as WORKSHOP normally |

---

## 6. General/off-topic question mid-flow (trimmed replay, new this session)

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0006',
    last_location='Nagpur', current_state='WAIT_DONE', root_cause='BATTERY')
"
```

| You send | Bot should do |
|---|---|
| `Aap kitne baje tak available ho?` | Answers from `data/knowledge_base.md`, then replays **only the last line** of the pending question — not the entire original multi-paragraph alert |
| `Aapka WhatsApp business ka baap kaun hai` (not in KB) | "Iska jawab abhi available nahi hai..." fallback — never invents an answer |

---

## 7. Driver handoff (phone normalization + carve-outs)

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0007',
    last_location='Nagpur', main_powervoltage='10.8', ismainpoerconnected='1', gpsStatus='0',
    current_state='ASK_HANDLER')
"
curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AA0007
```
| You send | Bot should do |
|---|---|
| *(tap "Driver")* | Asks for driver name + number |
| `Ramesh 9876543210` (bare 10-digit) | Confirms driver notified; **check `driver_phone` in the CSV is saved as `919876543210`**, not the bare number |
| *(from the driver's own WhatsApp, `9876543210`)* `Done` | Recognized as the **same** session (this is the phone-normalization fix) — proceeds with troubleshooting |

**Carve-out check:** at `ASK_CONTACT_PERSON` with a driver already on
file, send `driver se contact karlo` — must set the driver as the
on-site contact, **not** trigger a full conversation handoff to the
driver (these are two different things and share the word "driver").

**Driver-change check:** at any state, send `driver change karna hai` —
must jump straight to asking for new driver details, regardless of what
was being asked before (this is a global interrupt, same as handoff).

---

## 8. Full service booking flow

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0008',
    last_location='Nagpur', current_state='ASK_CURRENT_LOCATION')
"
```
| You send | Bot should reply |
|---|---|
| `Nagpur bypass ke paas hu` | Asks destination |
| `Pune ja rahe hain` | Suggests Pune as service city |
| `Haan Pune theek hai` | Asks for service date |
| `Kal` | Asks for time |
| `Shaam 5 baje` | Asks for contact person |
| `Site manager Rahul` | Asks for contact number |
| `9876500000` | Shows full booking summary |
| `Haan sahi hai` | Ticket confirmed, engineer assigned |

**Give everything at once** (bulk extraction fast path) — instead of the
step-by-step script above, from the same starting state send:
`Nagpur se Pune jaana hai kal shaam 5 baje, contact Rahul 9876500000` —
should skip straight to the booking summary in one turn.

---

## 9. Session resume / `handle_completed` re-engagement

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0009',
    last_location='Nagpur', current_state='COMPLETED',
    current_location='Nagpur Bypass', destination_location='Pune', vehicle_state='RUNNING',
    extracted_service_location='Pune', service_city_confirmed='TRUE',
    service_date='2026-07-22', service_time_window='05:00 PM',
    contact_person='Site Manager', contact_number='9876500000',
    ticket_id='TKT-EXAMPLE2', engineer_id='ENG001', engineer_name='Ramesh Kumar', engineer_phone='919000000001')
"
```
| You send | Bot should do |
|---|---|
| `Haan sab thik hai, dhanyavaad` | Short ack, case stays closed |
| `Nahi vehicle abhi bhi workshop me hai, 25 tarikh tak aayegi` | Re-classifies vehicle status, updates the booking summary in place, **stays COMPLETED** — doesn't reset to START |
| `Booking me correction karni hai` | Reopens `ASK_BOOKING_CORRECTION` |
| `Service city Pune ki jagah Nagpur, date 25 July` | Bulk-extraction applies both corrections at once |

---

## 10. Conversation memory across turns/restarts

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0010',
    last_location='Nagpur', current_state='ASK_CURRENT_LOCATION')
"
```
1. `Nagpur bypass ke paas hu` → asks destination.
2. `Pune jaana hai` → asks/suggests service city.
3. Check `conversation_summary` in the CSV — should hold only the last
   ~5 turns (`USER:`/`BOT:` lines), not the entire transcript.
4. Restart the server (`Ctrl+C`, re-run `uvicorn`).
5. Send `haan wahi` — bot should resume from the saved CSV row exactly
   where it left off, not restart the conversation.

---

## 11. Regression checks (fast, no second phone needed)

**Duplicate webhook delivery** — same `id` twice should only reply once:
```bash
curl -X POST http://127.0.0.1:8000/webhook -H "Content-Type: application/json" -d '{
  "entry": [{"changes": [{"value": {"messages": [{
    "id": "wamid.MANUALTEST001", "from": "<YOUR_PHONE>", "text": {"body": "Done"}
  }]}}]}]
}'
```
Run it again unchanged — second call must return `{"status":"duplicate_ignored"}`.

**Driver's reply without a second phone** (uses `driver_phone` from §7):
```bash
curl -X POST http://127.0.0.1:8000/webhook -H "Content-Type: application/json" -d '{
  "entry": [{"changes": [{"value": {"messages": [{
    "id": "wamid.MANUALTEST002", "from": "919876543210", "text": {"body": "Done"}
  }]}}]}]
}'
```
Must be recognized as the driver continuing the case, not "no active case."

**Physical-damage button skips the LLM entirely** — covered by the
automated suite, not manually observable: `pytest test.py -k physical_damage_button -v`.

---

## 12. Multi-line/edge phrasing sweep

Run each of these from `ASK_VEHICLE_STATUS` (scenario 2's session) as a
quick smoke test that nothing has regressed to a dead-end fallback:

| Message | Must NOT produce |
|---|---|
| `abhi pta ni kha hai jab aayegi tab btayenge` | "samajh nahi paaya" / generic fallback loop |
| `Bhai GPS band hai, gadi Punjabi Bagh me hai, aaj banda bhej do` | Any question about date/time/contact before the ticket is created |
| `kya meri koi complain register hai?` | The canned "please send your ticket ID" reply when the session already has its own ticket |
| `ok` | The full vehicle-status button menu being re-sent |

---

## 13. Multiple vehicles per owner (known limitation — do NOT expect this to work)

The session store is keyed by `phone_number` alone (one row per phone).
If you trigger outages for two vehicles under the same `<YOUR_PHONE>` at
once, the second `create_session`/`update_session` call will silently
overwrite the first case's row. This is a known, documented gap (not
something this pass fixed) — don't file it as a new bug, and don't test
concurrent multi-vehicle cases under one phone number expecting them to
coexist.

---

## 14. Ticket lifecycle (new this session — backend only, no chat trigger yet)

`data/tickets.csv`'s `status` column now actually transitions
(`OPEN → ASSIGNED → IN_PROGRESS → RESOLVED → CLOSED`) instead of being
frozen at `"ASSIGNED"` forever, but nothing in the live chat flow calls
these yet (that's part of the tool-calling engine still behind a flag —
see §15). To exercise it directly, from the project root:

```bash
python -c "
from services import ticket_service as t
tid = 'TKT-XXXXXXXX'   # paste a real ticket_id from data/tickets.csv
print(t.update_ticket_status(tid, 'IN_PROGRESS', note='engineer en route'))
print(t.close_ticket(tid, note='issue resolved'))
"
```
Try an illegal jump (e.g. `update_ticket_status(tid, 'RESOLVED')` on a
fresh `OPEN` ticket, or any transition on an already-`CLOSED` one) —
should raise `ValueError`, not silently no-op.

Automated coverage: `pytest tests/test_ticket_lifecycle.py -v`.

---

## 15. v2 tool-calling engine (new this session — opt-in per session, not live yet)

There's a second conversation engine (`core/agent_engine.py`) sitting
behind a per-session flag, not yet handling any real traffic
(`AGENT_ENGINE_DEFAULT_FOR_NEW` defaults to off in `config.py`/`.env`).
To manually try it over real WhatsApp for **one specific session**
without changing anything global, flip that row's `engine_version` after
creating it (it's read once per session and pinned — editing it mid-
conversation has no effect until a fresh session is created):

```bash
python -c "
from core import session_manager
session_manager.create_session('<YOUR_PHONE>', 'MH12AA0015', last_location='Nagpur', engine_version='v2')
"
```

This engine reasons about which backend "tool" to run each turn instead
of following the fixed per-state script, so behavior should be
*equivalent*, not necessarily worded identically, to the scenarios above.
It's new and not yet battle-tested against real traffic — treat any
divergence from the legacy engine's behavior as a bug report, and note
which engine (`engine_version` in the CSV) you were on.

Automated coverage: `pytest tests/test_agent_engine.py tests/test_tool_schema_validation.py tests/test_voice_compat_contract.py -v`.

---

## 16. Automated test suite

```bash
python -m pytest test.py test_realtime_scenarios.py tests/ -q
```
Current known baseline: **16 pre-existing, unrelated failures** (date-
rollover assumptions in a few tests that hardcode a specific "today"),
the rest passing. If you see a *different* set of failing test names
after a change, that's a real regression — compare the failing-test list,
not just the count.

**Watch out:** running the suite writes real rows into `data/tickets.csv`
via a few tests that don't use a tmp-path fixture (a known pre-existing
gap). Run `git checkout -- data/tickets.csv` afterward if you see
unexpected `MH12AB1234`/`MH12AA0001`-style rows appear.

---

## Quick reference: forcing a specific state without replaying the whole conversation

You never have to walk through the entire flow to test a specific state —
just find or create the session, set `current_state` (and whatever
fields that state depends on), and save it:
```bash
python -c "
from core import session_manager
session = session_manager.find_session('<YOUR_PHONE>')
session['current_state'] = 'ASK_SERVICE_DATE'
session_manager.update_session(session)
"
```
The bot has no memory beyond that CSV row (plus `conversation_summary`,
which is cosmetic context for the LLM, not state).
