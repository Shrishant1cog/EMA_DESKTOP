import os
import sys
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# Resolve project root whether running from root, backend/, or as frozen executable
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
    if BASE_DIR.name == "backend":
        BASE_DIR = BASE_DIR.parent

# Candidate paths for .env resolution
CANDIDATE_ENV_PATHS = [
    BASE_DIR / ".env",
    Path.cwd() / ".env",
    Path(os.path.expandvars(r"%LocalAppData%\Programs\EmailAutomater\.env")),
    Path(os.path.expandvars(r"%LocalAppData%\EmailAutomater\.env")),
    BASE_DIR.parent / ".env",
]

ENV_LOADED = False
for env_path in CANDIDATE_ENV_PATHS:
    if env_path.is_file():
        load_dotenv(env_path, override=True)
        ENV_LOADED = True
        break

if not ENV_LOADED:
    load_dotenv(override=True)


def _clean_env_val(key: str, default: str = "") -> str:
    """Retrieves an environment variable and strips whitespace and surrounding quotes."""
    val = os.getenv(key, default)
    if val is None:
        return default
    return val.strip().strip("'\"`")


# Application URLs
BACKEND_URL: str = _clean_env_val("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
FRONTEND_URL: str = _clean_env_val("FRONTEND_URL", "http://localhost:5500").rstrip("/")
REDIRECT_URI: str = _clean_env_val("REDIRECT_URI", f"{BACKEND_URL}/auth/callback")


# Groq API Keys & Model Configuration
def _parse_groq_keys() -> List[str]:
    raw_keys = _clean_env_val("GROQ_API_KEYS")
    if raw_keys:
        parsed = [
            k.strip().strip("'\"`")
            for k in raw_keys.split(",")
            if k.strip().strip("'\"`")
        ]
        if parsed:
            return parsed

    single_key = _clean_env_val("GROQ_API_KEY")
    return [single_key] if single_key else []


GROQ_API_KEYS: List[str] = _parse_groq_keys()
GROQ_API_KEY: str = GROQ_API_KEYS[0] if GROQ_API_KEYS else ""

# Production Supported Models
GROQ_MODEL: str = _clean_env_val("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_MODEL_CANDIDATES: List[str] = [
    GROQ_MODEL,
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama3-70b-8192",
    "llama3-8b-8192",
    "gemma2-9b-it",
]

USER_TIMEZONE: str = _clean_env_val("USER_TIMEZONE", "Asia/Kolkata")

# Google OAuth Credentials
GOOGLE_CLIENT_ID: str = (
    _clean_env_val("GOOGLE_CLIENT_ID")
    or _clean_env_val("CLIENT_ID")
    or _clean_env_val("GOOGLE_OAUTH_CLIENT_ID")
)
GOOGLE_CLIENT_SECRET: str = (
    _clean_env_val("GOOGLE_CLIENT_SECRET")
    or _clean_env_val("CLIENT_SECRET")
    or _clean_env_val("GOOGLE_OAUTH_CLIENT_SECRET")
)

# System Paths & Files
LOG_FILE: Path = BASE_DIR / "calendar_assistant.log"
DB_PATH: Path = BASE_DIR / "assistant_v2.db"


class AppConfig:
    try:
        poll_interval: int = int(_clean_env_val("POLL_INTERVAL", "15"))
    except ValueError:
        poll_interval: int = 15

    try:
        confidence_threshold: float = float(
            _clean_env_val("CONFIDENCE_THRESHOLD", "0.70")
        )
    except ValueError:
        confidence_threshold: float = 0.70