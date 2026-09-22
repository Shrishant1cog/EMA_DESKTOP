import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Resolve project root log path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_FILE = BASE_DIR / "calendar_assistant.log"

# Define custom log levels for clean terminal UI badges
SUCCESS_LEVEL_NUM = 25
FILTER_LEVEL_NUM = 22

logging.addLevelName(SUCCESS_LEVEL_NUM, "SUCCESS")
logging.addLevelName(FILTER_LEVEL_NUM, "FILTER")

def success(self, message, *args, **kws):
    if self.isEnabledFor(SUCCESS_LEVEL_NUM):
        self._log(SUCCESS_LEVEL_NUM, message, args, **kws)

def filter_event(self, message, *args, **kws):
    """Logs an email filtered by AI without overriding Python's internal Logger.filter method."""
    if self.isEnabledFor(FILTER_LEVEL_NUM):
        self._log(FILTER_LEVEL_NUM, message, args, **kws)

# Attach custom levels safely without breaking Python's internal logger
logging.Logger.success = success
logging.Logger.filter_event = filter_event

def humanize_error(error: Exception) -> str:
    """Translates technical Python and network exceptions into direct, plain-English statements."""
    if not error:
        return "Unknown error occurred."

    err_str = str(error)
    err_type = type(error).__name__

    # 1. DNS & Network Connection Issues
    if "11001" in err_str or "getaddrinfo failed" in err_str or "NameResolutionError" in err_str:
        return "Internet/DNS lookup failed: Could not resolve server address. Check your internet connection."
    if "Connection refused" in err_str or "ConnectionRefusedError" in err_type:
        return "Connection failed: The local server is unreachable or port is blocked."
    if "timed out" in err_str.lower() or "TimeoutError" in err_type:
        return "Connection timed out: The remote service took too long to respond."

    # 2. Google OAuth & API Authentication
    if "invalid_grant" in err_str or "Token has been expired or revoked" in err_str:
        return "Google authorization expired: Please reconnect your Google account."
    if "insufficientPermissions" in err_str or "403" in err_str:
        return "Permission denied: Google rejected the action due to insufficient OAuth permissions."
    if "404" in err_str:
        return "Resource not found: The requested email or calendar event no longer exists on Google servers."

    # 3. Groq AI & Rate Limits
    if "429" in err_str or "rate_limit_exceeded" in err_str:
        return "Groq AI rate limit reached: Switching to backup key or waiting for quota reset."
    if "invalid_api_key" in err_str:
        return "Invalid Groq API key: Please verify the GROQ_API_KEYS configured in your .env file."

    # 4. JSON / Data Parsing
    if "JSONDecodeError" in err_str:
        return "Data format error: AI response could not be parsed as valid JSON event data."

    # General Fallback
    return f"{err_type}: {err_str}"

class AutoFlushingRotatingFileHandler(RotatingFileHandler):
    """Ensures every single log record is written and flushed immediately to disk for SSE streams."""
    def emit(self, record):
        super().emit(record)
        self.flush()

class CleanTerminalFormatter(logging.Formatter):
    """Produces clean, standardized timestamps and formats exceptions into plain English."""
    def format(self, record):
        timestamp = self.formatTime(record, "%H:%M:%S")
        level = record.levelname.upper()
        msg = record.getMessage()

        # Handle exception records cleanly without spewing multi-line code traces
        if record.exc_info and record.exc_info[1]:
            english_err = humanize_error(record.exc_info[1])
            msg = f"{msg} | Reason: {english_err}" if msg else english_err

        return f"[{timestamp}] [{level}] {msg}"

def setup_logger(name: str = "AssistantEngine", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    # Prevent duplicate handlers on re-import
    if not logger.handlers:
        formatter = CleanTerminalFormatter()

        # 1. Console stream (stdout)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # 2. Rotating file handler (flushed instantly for SSE stream)
        try:
            file_handler = AutoFlushingRotatingFileHandler(
                LOG_FILE,
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=3,
                encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception:
            pass

    return logger

get_logger = setup_logger