"""
prompts/templates.py

Every outgoing message the bot sends for a KNOWN state comes from here —
not from the LLM. This keeps replies short, consistent, and predictable.
The LLM is only used to *understand* incoming replies (see llm_handler.py).
"""

T = {
    "BATTERY_ALERT": (
        "Namaste Sir,\n"
        "Vehicle {vehicle_no} se GPS data receive nahi ho raha hai.\n"
        "📍 Last Known Location: {location}\n"
        "🕐 Last Update: {last_update}\n"
        "Hamare diagnostics ke hisaab se vehicle ki battery low lag rahi hai, "
        "jis wajah se GPS device ko proper power nahi mil rahi ho sakti.\n"
        "Kya aap battery khud charge/check karenge ya hum driver se baat karein?"
    ),
    "MAIN_POWER_ALERT": (
        "Namaste Sir,\n"
        "Vehicle {vehicle_no} se GPS data receive nahi ho raha hai.\n"
        "📍 Last Known Location: {location}\n"
        "🕐 Last Update: {last_update}\n"
        "Hamare diagnostics ke hisaab se vehicle ka main power connection "
        "disconnect ho sakta hai, jis wajah se GPS device ko power nahi mil raha.\n"
        "Kya aap connection khud check karenge ya hum driver se baat karein?"
    ),
    "OTHER_ALERT": (
        "Namaste Sir,\n"
        "Vehicle {vehicle_no} se GPS data receive nahi ho raha hai.\n"
        "📍 Last Known Location: {location}\n"
        "🕐 Last Update: {last_update}\n"
        "Kripya batayein ki aapki vehicle ki current status kya hai:"
    ),
    "ASK_CHECK_BATTERY": "Thik hai, kripya battery ko charge/check kar ke bataiye. Ho jaye toh \"Done\" likhein.",
    "ASK_CHECK_POWER": "Thik hai, kripya main power connection, wiring aur fuse check kijiye. Ho jaye toh \"Done\" likhein.",

    "BATTERY_HELP_STEPS": (
        "Koi baat nahi, yeh steps follow kijiye:\n"
        "1) Battery ke dono terminal (connections) check kijiye — tight hain ya nahi.\n"
        "2) Battery ko 30-45 minute charge kijiye, ya vehicle chala kar charge hone dijiye.\n"
        "3) Charging ke baad GPS device ki light dekhiye — blink kare toh sahi hai.\n"
        "Ho jaye toh \"Done\" likh kar bataiye. Agar phir bhi mushkil ho toh \"driver\" likh dijiye, hum driver se baat kar lenge."
    ),
    "MAIN_POWER_HELP_STEPS": (
        "Koi baat nahi, yeh steps follow kijiye:\n"
        "1) GPS device ki wiring check kijiye — koi wire loose ya kata hua toh nahi.\n"
        "2) Fuse box check kijiye — GPS ka fuse blown toh nahi hai.\n"
        "3) Connection tight kar ke device ko restart hone dijiye.\n"
        "Ho jaye toh \"Done\" likh kar bataiye. Agar phir bhi mushkil ho toh \"driver\" likh dijiye, hum driver se baat kar lenge."
    ),
    "WAIT_DONE_NUDGE": "Jab check/charge kar lein, kripya \"Done\" likh kar bhejein. Madad chahiye toh bataiye, ya driver se baat karwani ho toh \"driver\" likh dijiye.",

    "SHOW_DRIVER_DETAILS": (
        "Aapke driver {driver_name} ({driver_phone}) se baat karte hain.\n"
        "Kya yeh details sahi hain? Reply YES ya naye driver ka naam+number bhejein."
    ),
    "TRANSFER_DONE_OWNER": "Dhanyawaad! Hum ab aapke driver se seedha baat kar rahe hain. Update milte hi aapko batayenge.",
    "TRANSFER_DONE_DRIVER": (
        "Namaste {driver_name}! Vehicle {vehicle_no} ka GPS offline hai. "
        "Kripya battery/power check kar ke \"Done\" likhein."
    ),

    "GPS_FIXED_CLOSE": "Badhiya! GPS wapas online aa gaya hai. Case close kar diya gaya hai. Dhanyawaad!",

    "ASK_PHYSICAL_DAMAGE": "Kya battery/wiring physically kharab hai ya replace/repair karni padegi? Reply YES ya NO.",

    "VEHICLE_STATUS_OPTIONS": (
        "1) Workshop me\n2) Accident hua hai\n3) Vehicle chal rahi hai\n"
        "4) GPS device damaged\n5) GPS device removed"
    ),

    "ASK_VEHICLE_STATUS": (
        "Samajh gaya. Ek aur cheez bataiye — vehicle abhi kis condition me hai?\n"
        "1) Workshop me\n2) Accident hua hai\n3) Vehicle chal rahi hai\n"
        "4) GPS device damaged\n5) GPS device removed"
    ),

    "ASK_EXPECTED_DATE": "Vehicle kab tak wapas running condition me aa jayegi? (koi bhi date/format chalega)",
    "SAVE_DATE_CLOSE": "Dhanyawaad! Humne {date} note kar liya hai. Jab vehicle ready ho, hume batayein. Case close kar rahe hain.",

    "ASK_CURRENT_LOCATION": "Vehicle abhi kis location par hai?",
    "ASK_SERVICE_LOCATION": "Service kis location par chahiye?",
    "ASK_SERVICE_DATE": "Service kab schedule karni hai?",
    "ASK_SERVICE_TIME": "Kis time par engineer visit kare?",
    "ASK_CONTACT_PERSON": "Site par engineer kis contact person se baat kare?",
    "ASK_CONTACT_NUMBER": "Contact person ka mobile number share kijiye.",
    "INVALID_NUMBER": "Yeh number sahi nahi lag raha. Kripya 10-digit valid mobile number bhejein.",

    "BOOKING_SUMMARY": (
        "Booking details:\n"
        "Vehicle Location: {current_location}\n"
        "Service Location: {service_location}\n"
        "Date: {service_date}\n"
        "Time: {service_time}\n"
        "Contact: {contact_person} ({contact_number})\n\n"
        "Kya yeh sahi hai? Reply YES ya NO."
    ),
    "BOOKING_CONFIRMED": (
        "Ticket ban gaya hai! Ticket ID: {ticket_id}\n"
        "Engineer {engineer_name} ({engineer_phone}) assign kiya gaya hai.\n"
        "Dhanyawaad!"
    ),
    "BOOKING_REDO": "Thik hai, kripya sahi detail dobara bhejein (location/date/time/contact).",

    "FALLBACK": "Maaf kijiye, samajh nahi paaya. Kya aap thoda aur detail me bata sakte hain?",
}


def render(key: str, **kwargs) -> str:
    return T[key].format(**kwargs)