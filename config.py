"""
config.py
Loads every environment variable once, so the rest of the app just does:
    from config import settings
    settings.META_ACCESS_TOKEN
"""

import os
from dotenv import load_dotenv

load_dotenv()  # reads the .env file into environment variables


class Settings:
    # Meta WhatsApp Cloud API
    # Some example .env files use META_TOKEN instead of META_ACCESS_TOKEN.
    # Prefer META_ACCESS_TOKEN, but fall back to META_TOKEN for compatibility.
    META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", os.getenv("META_TOKEN", ""))
    META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
    META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "gps_agent_verify_123")
    META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")

    # LLM provider: "bedrock" (OpenAI-compatible endpoint), "gemini", "ollama", or "anthropic"
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "bedrock")

    BEDROCK_API_KEY = os.getenv("BEDROCK_API_KEY", "")
    BEDROCK_BASE_URL = os.getenv(
        "BEDROCK_BASE_URL",
        "https://bedrock-mantle.eu-north-1.api.aws/v1/chat/completions",
    )
    BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "openai.gpt-oss-120b")

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # CSV "database" paths
    SESSIONS_CSV = os.getenv("SESSIONS_CSV", "data/mock_sessions.csv")
    TICKETS_CSV = os.getenv("TICKETS_CSV", "data/tickets.csv")
    ENGINEERS_CSV = os.getenv("ENGINEERS_CSV", "data/engineers.csv")
    KNOWLEDGE_BASE_PATH = os.getenv("KNOWLEDGE_BASE_PATH", "data/knowledge_base.md")

    # Twilio Voice (outbound calling agent)
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
    # Public base URL Twilio can reach (ngrok tunnel in dev). Some setups
    # call this PUBLIC_URL (like outbound.js did), others TWILIO_WEBHOOK_URL
    # — accept either so nothing breaks depending on which .env you copy.
    PUBLIC_URL = (os.getenv("PUBLIC_URL", "") or os.getenv("TWILIO_WEBHOOK_URL", "")).rstrip("/")

    # Voice quality (TTS) — Google/Polly neural voices sound far more
    # natural than Twilio's default. See Twilio's <Say> voice list for
    # other options (e.g. "Google.hi-IN-Wavenet-A/B/C", "Polly.Aditi").
    TWILIO_VOICE_NAME = os.getenv("TWILIO_VOICE_NAME", "Google.hi-IN-Wavenet-D")
    TWILIO_VOICE_LANGUAGE = os.getenv("TWILIO_VOICE_LANGUAGE", "hi-IN")

    # Speech recognition tuning
    # "phone_call" is tuned for call-quality audio (vs. dictation).
    TWILIO_SPEECH_MODEL = os.getenv("TWILIO_SPEECH_MODEL", "phone_call")
    # Below this confidence (0.0-1.0), a transcript is treated as unreliable
    # and the caller is asked to repeat instead of processing bad input.
    SPEECH_CONFIDENCE_THRESHOLD = float(os.getenv("SPEECH_CONFIDENCE_THRESHOLD", "0.4"))

    # Server
    PORT = int(os.getenv("PORT", "8000"))


settings = Settings()