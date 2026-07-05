# Manual Test Script — GPS Support Agent (WhatsApp)

How to use this doc: each scenario gives you (1) a row to paste into
`data/mock_sessions.csv`, and (2) the exact messages to send from WhatsApp.
The bot reads the CSV fresh on every message, so you don't need to restart
the server between scenarios — just edit the row and start texting.

**Replace `<YOUR_PHONE>` everywhere** with the real WhatsApp number you're
testing from, in the same digits-only format Meta uses (country code, no
`+`, no spaces — e.g. `919876543210`).

CSV column order (for reference — paste rows exactly in this order):
```
phone_number,vehicle_no,last_location,timestamp,gpstime,main_powervoltage,ismainpoerconnected,gpsStatus,driver_name,driver_phone,current_location,vehicle_state,current_state,handler,extracted_appointment_date,extracted_service_location,root_cause,physical_damage,contact_person,contact_number,service_date,service_time,ticket_id,engineer_id,destination_location,service_city_confirmed,service_date_step,service_time_window,driver_contact_confirmed,awaiting_alternate_contact,last_prompt_text,pending_quick_date
```

---

## 1. Low Battery

**CSV row** (voltage below 11.5 = low battery):
```
<YOUR_PHONE>,MH12AA0001,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,10.8,1,0,,,,,START,OWNER,,,,,,,,,,,,,,,,,
```
**Trigger it:**
```
curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AA0001
```
**Expect:** WhatsApp message about low battery, with **Self** / **Driver** buttons.

**Chat script:**
| You send | Bot should reply |
|---|---|
| *(tap "Self")* | "Battery terminals check kijiye..." (ASK_CHECK_BATTERY) |
| `Done` | Re-checks telemetry. Since CSV voltage is still 10.8, expect **ASK_PHYSICAL_DAMAGE** buttons |
| *(tap "No")* | "Thik hai, ek baar aur try kijiye..." — back to WAIT_DONE |

---

## 2. Battery charged → GPS recovered

**CSV row** (bot already at WAIT_DONE, telemetry now fine):
```
<YOUR_PHONE>,MH12AA0002,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,12.6,1,1,,,,,WAIT_DONE,OWNER,,,BATTERY,,,,,,,,,,,,,,
```
**Chat script:**
| You send | Bot should reply |
|---|---|
| `Done` | "GPS wapas online aa gaya hai..." → case closes (state COMPLETED) |
| `Done` again | "Yeh case pehle se close ho chuka hai..." |

---

## 3. Battery charged → still no GPS

**CSV row** (voltage now fine, but gpsStatus still 0):
```
<YOUR_PHONE>,MH12AA0003,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,12.6,1,0,,,,,WAIT_DONE,OWNER,,,BATTERY,,,,,,,,,,,,,,
```
**Chat script:**
| You send | Bot should reply |
|---|---|
| `Done` | Underlying issue is resolved but GPS still offline → moves to **ASK_VEHICLE_STATUS** (not physical damage) |

---

## 4. Main Power disconnected

**CSV row:**
```
<YOUR_PHONE>,MH12AA0004,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,12.6,0,0,,,,,START,OWNER,,,,,,,,,,,,,,,,,
```
```
curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AA0004
```
**Expect:** Main power disconnected alert, Self/Driver buttons. Continue same as scenario 1 but with power-specific wording ("wiring check kijiye" etc).

---

## 5. Vehicle in workshop

**CSV row:**
```
<YOUR_PHONE>,MH12AA0005,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,12.6,1,0,,,,,ASK_VEHICLE_STATUS,OWNER,,,UNKNOWN,,,,,,,,,,,,,,
```
**Chat script:**
| You send | Bot should reply |
|---|---|
| `Gaadi workshop mein hai` | Asks expected date it'll be out of the workshop |
| `15 July tak` | Confirms date saved, case closes |

---

## 6. Vehicle accident

Same row as #5. 
| You send | Bot should reply |
|---|---|
| `Accident ho gaya hai gaadi ka` | Asks expected date (accident-specific wording) |
| `3-4 din lagenge` | Saves date, closes |

---

## 7. GPS removed

Same row as #5.
| You send | Bot should reply |
|---|---|
| `GPS nikal diya hai humne` | Moves straight to **ASK_CURRENT_LOCATION** (booking flow starts) |

---

## 8. GPS damaged

Same row as #5.
| You send | Bot should reply |
|---|---|
| `GPS device damage ho gaya hai` | Same as above — ASK_CURRENT_LOCATION |

---

## 9. Driver handover — the important one (tests the phone-normalization fix)

This is the scenario that exposes the bug we fixed: the driver's own
reply must be recognized by the bot. **You need a second phone number**
for this (a friend's WhatsApp, a spare SIM, or the curl trick in section
15 below to fake the driver's `from` number without a second phone).

**CSV row** (no driver on file yet):
```
<YOUR_PHONE>,MH12AA0009,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,10.8,1,0,,,,,ASK_HANDLER,OWNER,,,BATTERY,,,,,,,,,,,,,,
```
```
curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AA0009
```
**Chat script (from YOUR_PHONE):**
| You send | Bot should reply |
|---|---|
| *(tap "Driver")* | "Driver ka naam aur mobile number bhejein." |
| `Ramesh 9876543210` (bare 10-digit, no country code — this is the exact input that used to break) | Confirms driver notified, owner sees "transferred" message |

**Now check the CSV** (`data/mock_sessions.csv`) — the `driver_phone` column for this row should read **`919876543210`** (with `91` prefix), not the bare `9876543210` you typed. That's the fix — if it shows the bare number, something regressed.

**Chat script (from the driver's actual phone, `9876543210`'s WhatsApp):**
| Driver sends | Bot should reply |
|---|---|
| `Done` | Bot recognizes this as the SAME session (this is the part that used to fail — "no active case found") and proceeds with the battery troubleshooting steps |

---

## 10. User asks a question mid-flow

**CSV row:**
```
<YOUR_PHONE>,MH12AA0010,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,10.8,1,0,,,,,WAIT_DONE,OWNER,,,BATTERY,,,,,,,,,,,,,,
```
| You send | Bot should reply |
|---|---|
| `Yeh kaise karu?` | Gives battery help steps, **stays in WAIT_DONE** (doesn't advance/reset) |
| `Done` | Proceeds normally from there |

---

## 11. User gives irrelevant input

Same row as #10, or reuse #5's ASK_VEHICLE_STATUS row.
| You send | Bot should reply |
|---|---|
| `asdkjaskjd banana pizza` | Falls back with a clarifying re-prompt, **state doesn't change** |

---

## 12. Session resume after interruption

**CSV row:** same as #4 (main power, START state).
1. Trigger the outage call, tap a button to get to WAIT_DONE.
2. **Stop the server** (Ctrl+C on uvicorn).
3. Wait a bit, restart: `uvicorn main:app --reload --port 8000`.
4. Send `Done` from WhatsApp again.
5. **Expect:** picks up exactly where it left off (state persisted in the CSV, nothing lost).

---

## 13. Complaint / ticket creation

**CSV row** (already at the final confirmation step):
```
<YOUR_PHONE>,MH12AA0013,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,12.6,1,0,,,Nagpur Bypass,,CONFIRM_SUMMARY,OWNER,,Pune,BATTERY,,Site Manager,9876500000,2026-07-06,05:00 PM,,,,,,,,,,
```
| You send | Bot should reply |
|---|---|
| `Haan confirm kar do` | "Ticket TKT-XXXXXXXX confirm ho gaya..." with engineer name/phone |

**Verify:** check `data/tickets.csv` — a new row should exist with a matching `ticket_id`, `vehicle_no = MH12AA0013`, `engineer_id` matching Pune's engineer.

---

## 14. Service booking (full flow)

**CSV row:**
```
<YOUR_PHONE>,MH12AA0014,Nagpur,2026-07-05 09:00:00,05 July 2026 09:00,12.6,1,0,,,,,ASK_CURRENT_LOCATION,OWNER,,,BATTERY,,,,,,,,,,,,,,
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

---

## 15. Knowledge base — general question mid-flow

**CSV row:** same as #10 (WAIT_DONE).
| You send | Bot should reply |
|---|---|
| `Aap kitne baje tak available ho?` | Answers from `data/knowledge_base.md`, **then re-asks** whatever the pending question was — state doesn't change |
| `Aapka WhatsApp business ka baap kaun hai` (something not in the KB) | "Iska jawab abhi available nahi hai, hum team se check karke aapko batayenge." — does NOT make something up |

Try editing `data/knowledge_base.md` — add a new Q&A line, ask about it immediately (no restart needed), confirm the bot picks up the new answer.

---

## 16. Regression checks for the specific bugs we fixed

These don't need a real second phone — use `curl` to fake any `from`
number directly against the webhook, which is the fastest way to test
duplicate-delivery and driver-reply scenarios without a second SIM.

**a) Duplicate webhook delivery (should NOT double-process):**
```bash
curl -X POST http://127.0.0.1:8000/webhook -H "Content-Type: application/json" -d '{
  "entry": [{"changes": [{"value": {"messages": [{
    "id": "wamid.MANUALTEST001",
    "from": "<YOUR_PHONE>",
    "text": {"body": "Done"}
  }]}}]}]
}'
```
Run the **exact same command again** (same `id`). First call replies normally; second call should return `{"status":"duplicate_ignored"}` and you should get only ONE WhatsApp message, not two.

**b) Driver's reply without a second phone** (uses whatever `driver_phone` got saved in scenario 9):
```bash
curl -X POST http://127.0.0.1:8000/webhook -H "Content-Type: application/json" -d '{
  "entry": [{"changes": [{"value": {"messages": [{
    "id": "wamid.MANUALTEST002",
    "from": "919876543210",
    "text": {"body": "Done"}
  }]}}]}]
}'
```
Should be recognized as the driver continuing the same case — not "no active case found."

**c) "Nahi" + a valid number shouldn't get thrown away:**
Get a session to `ASK_ALTERNATE_CONTACT` (set `current_state` to that in the CSV), then send:
```
Nahi driver ka number hi sahi hai 9876543210
```
Bot should save `9876543210` as the contact number — NOT mark it `NOT_PROVIDED`.

**d) Booking correction shouldn't double-extract:**
Get a session to `ASK_BOOKING_CORRECTION`, then send:
```
Service city Pune, phone sahi hai
```
Should update the service city once and move to confirmation — not get confused by the word "phone" also being in the message.

**e) Physical damage button shouldn't waste an LLM call:**
Harder to observe directly over WhatsApp — this one's covered by the automated test suite (`test.py::TestBugFixRegressions::test_physical_damage_button_payload_never_touches_llm`). Run `pytest test.py -v` to confirm it still passes.

---

## Quick reference: forcing a specific state without replaying the whole conversation

You never have to walk through the entire flow to test a specific state —
just edit `current_state` (and whatever fields that state depends on) directly
in `data/mock_sessions.csv`, then send the next message. The bot has no
memory beyond that CSV row.