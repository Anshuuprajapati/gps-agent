"""
whatsapp/sender.py

One function every other part of the app calls to actually deliver a
WhatsApp message: send_message(phone_number, text).
"""

import requests
from config import settings


def send_message(to_phone: str, text: str = "", interactive: dict | None = None) -> dict:
    url = f"https://graph.facebook.com/{settings.META_API_VERSION}/{settings.META_PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {settings.META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    if interactive is not None:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "interactive",
            "interactive": interactive,
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "text",
            "text": {"body": text},
        }

    response = requests.post(url, headers=headers, json=payload, timeout=15)

    if response.status_code >= 400:
        # A failed send is otherwise invisible to the customer — the bot
        # just goes quiet from their side. This at least surfaces it
        # loudly in the server logs; wire up real alerting (Sentry, a
        # Slack webhook, etc.) on this branch if you need to know sooner.
        print(f"[sender] ALERT: failed to send to {to_phone}: {response.status_code} {response.text}")

    return {"status_code": response.status_code, "body": response.text}