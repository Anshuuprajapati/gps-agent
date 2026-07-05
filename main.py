"""
main.py
Starts the FastAPI server and mounts the WhatsApp webhook.

Run:
    uvicorn main:app --reload --port 8000

Then point ngrok at it:
    ngrok http 8000
(your current tunnel: https://eupotamic-bryce-oversensibly.ngrok-free.dev)
"""

from fastapi import FastAPI
from whatsapp.webhook import router as whatsapp_router
from voice.webhook import router as voice_router

app = FastAPI(title="GPS AI Support Agent")

app.include_router(whatsapp_router)
app.include_router(voice_router)

@app.get("/")
def health_check():
    return {"status": "running", "service": "gps-agent"}
