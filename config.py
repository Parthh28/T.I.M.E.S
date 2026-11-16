"""
Configuration module for the Telegram Calendar Assistant.

Loads environment variables and provides application-wide constants.
"""

import os
import sys
import logging
from pathlib import Path

from dotenv import load_dotenv
import pytz

# Load .env file from the project root
load_dotenv(dotenv_path=Path(__file__).parent / ".env")


def _require_env(key: str) -> str:
    """Return an environment variable or exit with a clear error."""
    value = os.getenv(key)
    if not value:
        print(f"ERROR: Required environment variable '{key}' is not set.")
        print(f"       Copy .env.example to .env and fill in the values.")
        sys.exit(1)
    return value


# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = _require_env("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID: int = int(_require_env("AUTHORIZED_USER_ID"))

# --- Gemini ---
GEMINI_API_KEY: str = _require_env("GEMINI_API_KEY")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

# --- Google Calendar OAuth ---
GOOGLE_CREDENTIALS_FILE: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE: str = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
CALENDAR_SCOPES: list[str] = ["https://www.googleapis.com/auth/calendar"]

# --- Notion API ---
NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID: str = os.getenv("NOTION_DATABASE_ID", "")

# --- Database ---
DATABASE_FILE: str = os.getenv("DATABASE_FILE", "calendar_bot.db")

# --- Timezone ---
TIMEZONE_NAME: str = os.getenv("TIMEZONE", "Asia/Kolkata")
TIMEZONE = pytz.timezone(TIMEZONE_NAME)

# --- Logging ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
# Suppress noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

logger = logging.getLogger("calendar_bot")

# --- Default Reminder Options (minutes before event) ---
DEFAULT_REMINDER_TIERS: list[int] = [1440, 60, 15, 0]  # 1 day, 1 hour, 15 mins, and at event start (spoken alert)

DEFAULT_REMINDER_OPTIONS: list[dict[str, int | str]] = [
    {"minutes": 0, "label": "At event start (Phone voice note)"},
    {"minutes": 15, "label": "15 minutes before"},
    {"minutes": 60, "label": "1 hour before"},
    {"minutes": 1440, "label": "1 day before"},
]

# --- Text-to-Speech (Voice Announcements) ---
TTS_PHONE_VOICE_ENABLED: bool = os.getenv("TTS_PHONE_VOICE_ENABLED", "true").lower() == "true"
TTS_COMPUTER_SPEAKER_ENABLED: bool = os.getenv("TTS_COMPUTER_SPEAKER_ENABLED", "false").lower() == "true"
TTS_VOICE_RATE: int = int(os.getenv("TTS_VOICE_RATE", "175"))

# --- Daily Morning Briefing ---
BRIEFING_TIME: str = os.getenv("BRIEFING_TIME", "08:00")  # HH:MM 24-hour format
BRIEFING_ENABLED: bool = os.getenv("BRIEFING_ENABLED", "true").lower() == "true"
