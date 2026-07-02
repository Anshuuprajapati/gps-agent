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

    # LLM provider: "groq" (free, recommended), "gemini" (free), "ollama" (free/local), or "anthropic" (paid)
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")

    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

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

    # Server
    PORT = int(os.getenv("PORT", "8000"))


settings = Settings()
