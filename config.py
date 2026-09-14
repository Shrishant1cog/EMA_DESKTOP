import os
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# Project Root Resolution (handles execution from root or backend/)
BASE_DIR = Path(__file__).resolve().parent
if BASE_DIR.name == "backend":
    BASE_DIR = BASE_DIR.parent

# Load environment variables (.env in project root)
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE, override=True)

# Application URLs (ensures no trailing slash collisions)
BACKEND_URL: str = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").strip().rstrip("/")
FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5500").strip().rstrip("/")
REDIRECT_URI: str = f"{BACKEND_URL}/auth/callback"

# Groq API Keys & Model Configuration
def _parse_groq_keys() -> List[str]:
    raw_keys = os.getenv("GROQ_API_KEYS", "").strip()
    if raw_keys:
        parsed = [
            k.strip().strip("'\"`")
            for k in raw_keys.split(",")
            if k.strip().strip("'\"`")
        ]
        if parsed:
            return parsed

    single_key = os.getenv("GROQ_API_KEY", "").strip().strip("'\"`")
    return [single_key] if single_key else []

GROQ_API_KEYS: List[str] = _parse_groq_keys()
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
USER_TIMEZONE: str = os.getenv("USER_TIMEZONE", "Asia/Kolkata").strip()

# Google OAuth Credentials
GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

# System Paths & Files
TESSERACT_CMD: str = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe").strip()
LOG_FILE: Path = BASE_DIR / "calendar_assistant.log"
DB_PATH: Path = BASE_DIR / "database" / "assistant_v2.db"

# Runtime Application Settings
class AppConfig:
    try:
        poll_interval: int = int(os.getenv("POLL_INTERVAL", "15"))
    except ValueError:
        poll_interval: int = 15

    try:
        confidence_threshold: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.70"))
    except ValueError:
        confidence_threshold: float = 0.70