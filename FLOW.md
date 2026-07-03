# GPS Agent - Complete Issues & Flows Documentation

## 🎯 Overview

This document maps ALL possible issues, cases, and complete conversation flows in the GPS Agent system. Use this to understand, test, and debug every scenario.

---

## 📊 Issue Categories

### 1. **GPS Offline Issues** (Root Cause Analysis)
- Battery Issue
- Main Power Disconnected  
- Unknown/Other Cause

### 2. **Vehicle Status** (When Running)
- RUNNING (vehicle actively moving)
- GPS_DAMAGED (device damaged)
- GPS_REMOVED (device removed)

### 3. **Vehicle Status** (When Not Running)
- WORKSHOP (in repair shop)
- ACCIDENT (been in accident)

### 4. **Resolution Paths**
- Owner troubleshoots (self)
- Driver handles (driver involvement)
- Service booking required (physical damage)

---

## 🔄 Complete Flow Maps

## FLOW 1: BATTERY ISSUE - OWNER TROUBLESHOOTS

```
START
  ↓
[System detects] Battery Low Issue
  ↓
Bot shows: "Vehicle GPS offline. Battery low detected."
     Offers: SELF (owner) or DRIVER (contact driver)
  ↓
User: "SELF" (owner will fix)
  ↓
Bot: "Battery check kijiye. Charge/recharge kar ke 'Done' likhiye."
  ↓
--- WAIT_DONE STATE ---
  ↓
User: "Done"
  ↓
Bot: [Verify GPS online]
  ├─ YES → GPS_FIXED_CLOSE → COMPLETED
  └─ NO  → ASK_PHYSICAL_DAMAGE
           ↓
           User: "YES" (battery damaged)
           ↓
           ASK_CURRENT_LOCATION
           ↓
           [Move to SERVICE BOOKING FLOW]
           
Alternative: User says "DRIVER" → [Go to DRIVER HANDOFF FLOW]
Alternative: User says "Help" → Show troubleshooting steps
Alternative: User says "Driver" mid-troubleshooting → [Go to DRIVER HANDOFF FLOW]
```

---

## FLOW 2: BATTERY ISSUE - DRIVER HANDLES

```
START
  ↓
[System detects] Battery Low Issue
  ↓
Bot shows: "Vehicle GPS offline. Battery low detected."
     Offers: SELF or DRIVER
  ↓
User: "DRIVER" (or "driver se baat karo")
  ↓
--- DRIVER CONFIRM STATE ---
  ↓
Bot checks: Is driver on file?
  ├─ YES → Shows driver details + phone
  │        ↓
  │        User: "YES" confirm
  │        ↓
  │        TRANSFER_TO_DRIVER
  │        ↓
  │        Driver gets troubleshooting message
  │        Owner notified
  │        ↓
  │        WAIT_DONE (tracking driver)
  │        ↓
  │        Driver: "Done"
  │        ↓
  │        [Same GPS verification as FLOW 1]
  │
  └─ NO  → No driver on file
           ↓
           ASK_NEW_DRIVER
           ↓
           User: "Ramesh 9876543210"
           ↓
           [Update driver details]
           ↓
           [Same flow as YES branch above]

Alternative: User provides different phone → Uses new number instead
Alternative: User declines to confirm → ASK_NEW_DRIVER
```

---

## FLOW 3: MAIN POWER DISCONNECTED

```
START
  ↓
[System detects] Main Power Issue
  ↓
Bot: "Vehicle GPS offline. Main power possibly disconnected."
     "Battery khud charge/check kar ke 'Done' likhiye."
  ↓
--- SAME PATHS AS BATTERY FLOW ---
  │
  ├─ SELF troubleshoots
  │  ├─ Verify power connections
  │  ├─ Check fuse
  │  ├─ Restart device
  │  └─ Say "Done"
  │
  └─ DRIVER handles
     [Same driver flow as FLOW 2]

GPS Verification:
  ├─ Fixed → Close case
  └─ Not Fixed → Ask about physical damage
```

---

## FLOW 4: UNKNOWN CAUSE (ASK_VEHICLE_STATUS)

```
START
  ↓
[System can't determine cause from telemetry]
  ↓
Bot: "Vehicle ki status kya hai?"
     Options:
     1) Workshop me
     2) Accident hua hai
     3) Vehicle chal rahi hai
     4) GPS device damaged
     5) GPS device removed
  ↓
--- USER SELECTS STATUS ---

┌─────────────────────────────────────────────────────────────┐
│                                                             │
│ STATUS 1: WORKSHOP                                          │
│ ↓                                                           │
│ Bot: "Kab tak vehicle workshop mein hogi?"                 │
│ User: "5 July 2026" (or "kal", "2 din baad")              │
│ Bot: "Date save kar liya. Case close."                     │
│ → COMPLETED                                                │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ STATUS 2: ACCIDENT                                          │
│ ↓                                                           │
│ Bot: "Accident ke baad vehicle kab running ho jayegi?"     │
│ User: "10 din baad"                                        │
│ Bot: "Date save kar liya. Case close."                     │
│ → COMPLETED                                                │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ STATUS 3: RUNNING                                           │
│ ↓                                                           │
│ ASK_CURRENT_LOCATION                                        │
│ ↓ [Move to SERVICE BOOKING FLOW]                          │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ STATUS 4: GPS_DAMAGED                                       │
│ ↓                                                           │
│ ASK_CURRENT_LOCATION                                        │
│ ↓ [Move to SERVICE BOOKING FLOW]                          │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ STATUS 5: GPS_REMOVED                                       │
│ ↓                                                           │
│ ASK_CURRENT_LOCATION                                        │
│ ↓ [Move to SERVICE BOOKING FLOW]                          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## FLOW 5: SERVICE BOOKING (COMPLETE)

```
START (from physical damage or running vehicle status)
  ↓
ASK_CURRENT_LOCATION
  ↓
Bot: "Vehicle abhi kis location par hai?"
User: "Delhi" (or extracts from previous context)
  ↓
ASK_DESTINATION_LOCATION
  ↓
Bot: "Kahan ja rahe hain?" (or "Kahan service chahiye?")
User: "Pune"
  ↓
ASK_SERVICE_CITY_CONFIRMATION
  ↓
Bot: "Kya hum Pune mein service book kar dein?"
  ├─ User: "YES"
  │  ↓
  │  ASK_SERVICE_DATE
  │  ↓
  │  [Extracted service location = Pune]
  │
  └─ User: "NO"
     ↓
     ASK_SERVICE_CITY_PREFERENCE
     ↓
     Bot: "Kaun se city mein service chahiye?"
     User: "Bangalore"
     ↓
     ASK_SERVICE_DATE
     ↓
     [Extracted service location = Bangalore]

--- SERVICE DATE ---
  ↓
Bot: "Service kab kar dein? (Aaj? Kal? Specific date?)"
  ├─ User: "nahi" (decline)
  │  ↓
  │  [Set declined_service_booking=True]
  │  → COMPLETED (NO booking)
  │
  └─ User: "Kal" (or date value)
     ↓
     [Set service_date = tomorrow]
     ↓
     ASK_SERVICE_TIME_WINDOW
     ↓
     Bot: "Kitne baje se kitne baje available ho?"
     ├─ User: "nahi"
     │  ↓
     │  [Set declined_service_time=True]
     │  ↓
     │  ASK_CONTACT_PERSON
     │
     └─ User: "10:00 se 2:00" or "Morning" or "Afternoon"
        ↓
        [Set service_time_window]
        ↓
        ASK_CONTACT_PERSON

--- DRIVER CHECK ---
  ↓
If driver on file:
  ├─ ASK_DRIVER_CONTACT_CONFIRMATION
  │  ├─ User: "YES" (use driver contact)
  │  │  ↓
  │  │  [contact_person = driver name]
  │  │  [contact_number = driver phone]
  │  │  ↓
  │  │  CONFIRM_SUMMARY
  │  │
  │  └─ User: "NO" (use different contact)
  │     ↓
  │     ASK_ALTERNATE_CONTACT
  │     ↓
  │     Bot: "Contact person ka naam + phone bhejiye"
  │     User: "Anshu 1234567890"
  │     ↓
  │     [contact_person = Anshu]
  │     [contact_number = 1234567890]
  │     ↓
  │     CONFIRM_SUMMARY
  │
  └─ No driver on file:
     ↓
     ASK_CONTACT_PERSON
     ↓
     Bot: "Site par engineer kis contact se baat kare?"
     User: "Owner"
     ↓
     ASK_CONTACT_NUMBER
     ↓
     Bot: "Contact number?"
     User: "9876543210"
     ↓
     CONFIRM_SUMMARY

--- CONFIRM SUMMARY ---
  ↓
Bot shows:
  ✅ Location: Delhi
  ✅ Service: Pune
  ✅ Date: Tomorrow
  ✅ Time: 10:00-2:00
  ✅ Contact: Anshu (1234567890)
  
  "Kya yeh sahi hai? YES ya NO?"
  
  ├─ User: "YES"
  │  ↓
  │  CREATE TICKET
  │  ├─ Assign engineer
  │  ├─ Generate ticket ID
  │  └─ Send confirmation
  │  ↓
  │  Bot: "Ticket TKT-ABC123 ban gaya!"
  │  "Engineer Rajesh (9876543210) assign kiya gaya."
  │  ↓
  │  COMPLETED
  │
  └─ User: "NO"
     ↓
     ASK_BOOKING_CORRECTION
     ↓
     Bot: "Kaunsi detail galat hai?"
     User: "Time 5 PM"
     ↓
     [Update: service_time_window = "5 PM"]
     ↓
     CONFIRM_SUMMARY (show again)
     ↓
     [Loop back to YES/NO]

--- If user doesn't confirm corrections ---
  ↓
Bot: "Thik hai, aap kaunsi detail fix karna chahte hain?"
  ├─ Phone number → Extract and update
  ├─ Location → Extract and update
  ├─ Time → Extract and update
  ├─ Date → Extract and update
  └─ Service city → Extract and update
     ↓
     Back to CONFIRM_SUMMARY
```

---

## FLOW 6: PHYSICAL DAMAGE DETECTION

```
START (from GPS troubleshooting path)
  ↓
Bot: "Kya battery/wiring physically kharab hai?"
     Options: YES or NO
  ↓
  ├─ User: "YES"
  │  ↓
  │  [Set physical_damage = YES]
  │  ↓
  │  ASK_CURRENT_LOCATION
  │  ↓ [Move to SERVICE BOOKING FLOW]
  │
  └─ User: "NO"
     ↓
     [Set physical_damage = NO]
     ↓
     WAIT_DONE
     ↓
     Bot: "Ek baar aur try kijiye. Battery/power check kijiye."
     ↓
     [User tries again, says "Done"]
     ↓
     [Same GPS verification]
```

---

## FLOW 7: DRIVER HANDOFF (MID-CONVERSATION)

```
START (User in WAIT_DONE, troubleshooting)
  ↓
Bot: "Battery charge kar raha hai?"
  ↓
User: "Driver se baat karo"
  ↓
Bot detects: "WANT_DRIVER" intent
  ↓
_start_driver_handoff()
  ↓
  ├─ Driver on file
  │  ├─ Shows driver details
  │  └─ Asks confirmation
  │
  └─ No driver on file
     └─ Asks for new driver details

--- TRANSFER COMPLETE ---
  ↓
Owner: "Driver se seedha baat kar rahe hain."
Driver: "Battery check karenge, 'Done' likh kar batayenge."
  ↓
WAIT_DONE (now tracking driver)
```

---

## 🚨 Edge Cases & Special Scenarios

### EDGE CASE 1: User Says "Nahi" Multiple Times

```
Bot: "Service date?"
User: "nahi"
  ↓
Bot: "Time?"
  ↓
OLD BOT: Still asks even after "nahi"
NEW BOT: [Set declined_service_booking=True]
         Skips to COMPLETED
```

### EDGE CASE 2: User Provides All Info in One Message

```
User: "Running hai, delhi me hai, pune ja rahi hai"
  ↓
NEW BOT: [Extracts all]
         Skips to service city confirmation
         
OLD BOT: Asks each question separately
```

### EDGE CASE 3: Invalid Data in Summary

```
OLD BOT: 
  Date: nahi
  Time: nahi
  
NEW BOT:
  [Only shows valid fields]
```

### EDGE CASE 4: User Changes Mind About Driver

```
User confirms: "YES" to driver
  ↓
Mid-conversation: "Actually no, use different number"
  ↓
Bot: Asks for new driver details
  ↓
Updates contact info
```

### EDGE CASE 5: User Provides Corrections

```
Summary shown with incorrect data
User: "No, time 5 PM hai"
  ↓
Bot: Extracts "5 PM"
  ↓
Updates service_time_window
  ↓
Shows corrected summary
```

---

## 📋 Complete Decision Tree

```
GPS Issue Detected
│
├─→ BATTERY_ISSUE
│   ├─→ Owner: TROUBLESHOOT (WAIT_DONE)
│   │   ├─→ GPS Fixed: CLOSE
│   │   └─→ GPS Not Fixed: Physical Damage?
│   │       ├─→ YES: SERVICE BOOKING
│   │       └─→ NO: Try Again
│   │
│   └─→ Driver: HANDOFF (WAIT_DONE)
│       ├─→ GPS Fixed: CLOSE
│       └─→ GPS Not Fixed: Physical Damage?
│
├─→ MAIN_POWER_DISCONNECTED
│   ├─→ Owner: TROUBLESHOOT (same as battery)
│   └─→ Driver: HANDOFF (same as battery)
│
└─→ UNKNOWN_CAUSE: ASK_VEHICLE_STATUS
    ├─→ WORKSHOP: Ask Expected Date → CLOSE
    ├─→ ACCIDENT: Ask Expected Date → CLOSE
    ├─→ RUNNING: SERVICE BOOKING
    ├─→ GPS_DAMAGED: SERVICE BOOKING
    └─→ GPS_REMOVED: SERVICE BOOKING

SERVICE BOOKING FLOW:
├─→ Extract Location (Current + Destination)
├─→ Confirm Service City
├─→ Ask Service Date
├─→ Ask Service Time
├─→ Confirm Driver Contact OR Ask New Contact
├─→ Show Summary
├─→ Confirm Booking
├─→ Create Ticket
└─→ COMPLETE
```

---

## 🧪 Test Scenarios

### Scenario 1: Battery Issue - Quick Fix
```
Issue: Battery
User chooses: SELF
User says: Done
GPS: Online
Result: CLOSED ✅
```

### Scenario 2: Battery Issue - Physical Damage
```
Issue: Battery
User chooses: SELF
User says: Done
GPS: Still Offline
User says: YES (damaged)
Starts: SERVICE BOOKING
Result: TICKET CREATED ✅
```

### Scenario 3: Vehicle Running - Full Booking
```
Status: RUNNING
User at Delhi, going to Pune
Booking: Tomorrow, 10-2 PM, Contact: Anshu 1234567890
Result: TICKET CREATED ✅
```

### Scenario 4: User Declines Booking
```
Status: RUNNING
Service Date Question: User says "nahi"
Bot: Stops asking, closes case
Result: COMPLETED (NO BOOKING) ✅
```

### Scenario 5: Multiple Corrections
```
Summary shown: Date tomorrow, Time morning
User: "No time wrong, 5 PM"
Bot: Updates time
Shows corrected summary
User: YES
Result: TICKET CREATED ✅
```

### Scenario 6: Driver Mid-Transfer
```
Troubleshooting ongoing
User: "Driver se baat karo"
Driver details shown
Driver says: DONE
GPS: Fixed
Result: CLOSED ✅
```

---

## 📊 State Transitions Summary

```
START
  ↓
ASK_HANDLER (SELF or DRIVER?)
  ├─→ SELF: WAIT_DONE
  └─→ DRIVER: DRIVER_CONFIRM → TRANSFER_DONE → WAIT_DONE

WAIT_DONE
  ├─→ DONE: Verify GPS
  │   ├─→ Fixed: COMPLETED
  │   └─→ Not Fixed: ASK_PHYSICAL_DAMAGE
  ├─→ NEED_HELP: Show troubleshooting steps
  └─→ WANT_DRIVER: DRIVER_CONFIRM

ASK_PHYSICAL_DAMAGE
  ├─→ YES: ASK_CURRENT_LOCATION
  └─→ NO: Back to WAIT_DONE

ASK_VEHICLE_STATUS
  ├─→ WORKSHOP/ACCIDENT: ASK_EXPECTED_DATE → COMPLETED
  └─→ RUNNING/GPS_DAMAGED/GPS_REMOVED: ASK_CURRENT_LOCATION

SERVICE BOOKING STATES:
  ASK_CURRENT_LOCATION
    ↓
  ASK_DESTINATION_LOCATION
    ↓
  ASK_SERVICE_CITY_CONFIRMATION
    ↓
  ASK_SERVICE_DATE
    ↓
  ASK_SERVICE_TIME_WINDOW
    ↓
  [Driver Confirmation if on file]
    ↓
  ASK_CONTACT_PERSON
    ↓
  ASK_CONTACT_NUMBER
    ↓
  CONFIRM_SUMMARY
    ├─→ YES: Create Ticket → COMPLETED
    └─→ NO: ASK_BOOKING_CORRECTION → CONFIRM_SUMMARY
```

---

## 💾 Session Data Structure

```
session = {
    # Vehicle info
    "vehicle_no": "MH16EF9012",
    "last_location": "Nagpur",
    "gpstime": "2026-06-21 09:30",
    
    # Issue info
    "root_cause": "BATTERY_ISSUE | MAIN_POWER_DISCONNECTED | UNKNOWN",
    "vehicle_state": "RUNNING | WORKSHOP | ACCIDENT | GPS_DAMAGED | GPS_REMOVED",
    "physical_damage": "YES | NO",
    
    # User choice
    "handler": "OWNER | DRIVER",
    
    # Driver info
    "driver_name": "Deepak Singh",
    "driver_phone": "9871234560",
    "driver_contact_confirmed": "TRUE | FALSE",
    
    # Booking info
    "current_location": "Delhi",
    "destination_location": "Pune",
    "extracted_service_location": "Pune",
    "service_date": "2026-07-04",
    "service_time_window": "10:00-14:00",
    
    # Contact info
    "contact_person": "Anshu",
    "contact_number": "1234567890",
    
    # Declined tracking
    "declined_service_booking": True | False,
    "declined_service_time": True | False,
    "declined_driver": True | False,
    
    # Results
    "ticket_id": "TKT-ABC12345",
    "engineer_id": "ENG-001",
    
    # State tracking
    "current_state": "[State name]",
}
```

---

## ✅ Common Outputs

### Successful Closure
- GPS Fixed: "GPS wapas online ho gaya! Case closed."
- Date Saved: "Date save kar liya. Case closed."
- Ticket Created: "Ticket TKT-ABC123 ban gaya!"

### User Declines
- Declines Booking: "Thik hai, zaroorat padne par bataiye."
- Declines Time: Skips to next question
- Declines Driver: Asks for new driver

### Waiting States
- "Battery charge kar ke 'Done' likhiye."
- "Driver se baat kar rahe hain, update milne tak wait kijiye."

---

## 🎯 Key Takeaways

1. **Every path has an end** - Case closes with ticket, date, or decision
2. **Smart extraction** - Bot extracts ALL info from messages
3. **Decline tracking** - "Nahi" stops related questions
4. **No redundant questions** - Info provided once is used throughout
5. **Valid data only** - Summaries show only real values
6. **Driver integration** - Seamless handoff at any time
7. **Multiple corrections** - User can fix any field
8. **Complete ticket creation** - All required data collected before booking

