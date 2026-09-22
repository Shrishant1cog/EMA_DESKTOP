import io
import json
import copy
import logging
import threading
from typing import Dict, Any, List, Optional, Set

from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

from backend.utils.state_tracker import get_setting, set_setting
from backend.services.google_auth import (
    get_drive_service_for_account,
    mark_account_needs_reauth,
    clear_account_reauth,
    is_reauth_needed
)

logger = logging.getLogger("DriveStateAdapter")

STATE_FILENAME = "assistant_state.json"

DEFAULT_STATE_TEMPLATE: Dict[str, Any] = {
    "version": 1,
    "settings": {
        "only_remind_with_files": "false",
        "design_events_enabled": "false",
        "design_events_prompt": "",
        "spam_protection_enabled": "true",
        "spam_threshold": "50",
        "default_timing": "09:00",
        "worker_enabled": "true",
    },
    "email_actions": []
}

def _create_fresh_default_state() -> Dict[str, Any]:
    """Generates an isolated deep copy of the default state template."""
    return copy.deepcopy(DEFAULT_STATE_TEMPLATE)

# ----------------- Thread-Safe Memory Caches ----------------- #

_CACHE_LOCK = threading.Lock()
_STATE_CACHE: Dict[str, Dict[str, Any]] = {}
_PROCESSED_CACHE: Dict[str, Set[str]] = {}
_FILE_ID_CACHE: Dict[str, str] = {}

# ----------------- Drive AppData Operations ----------------- #

def _get_or_create_state_file_id(drive_service, email: str) -> str:
    """Finds assistant_state.json in appDataFolder or creates an initial state file."""
    with _CACHE_LOCK:
        if email in _FILE_ID_CACHE:
            return _FILE_ID_CACHE[email]

    try:
        response = drive_service.files().list(
            spaces="appDataFolder",
            q=f"name = '{STATE_FILENAME}' and trashed = false",
            fields="files(id, name)",
            pageSize=1
        ).execute()
        files = response.get("files", [])

        if files:
            file_id = files[0]["id"]
            with _CACHE_LOCK:
                _FILE_ID_CACHE[email] = file_id
            return file_id

        # Create initial state file if not found
        file_metadata = {
            "name": STATE_FILENAME,
            "parents": ["appDataFolder"]
        }
        initial_payload = _create_fresh_default_state()
        raw_data = json.dumps(initial_payload, separators=(',', ':')).encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(raw_data), mimetype="application/json", resumable=True)

        created_file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()

        file_id = created_file.get("id")
        with _CACHE_LOCK:
            _FILE_ID_CACHE[email] = file_id
        logger.info(f"Initialized new {STATE_FILENAME} in Drive appDataFolder for {email}")
        return file_id

    except HttpError as http_err:
        status = http_err.resp.status if hasattr(http_err, 'resp') else 0
        if status in (401, 403):
            mark_account_needs_reauth(email)
        raise http_err

def load_drive_state(email: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Reads state from local memory cache, falling back to Google Drive and local SQLite cache."""
    clean_email = email.strip().lower()
    with _CACHE_LOCK:
        if not force_refresh and clean_email in _STATE_CACHE:
            return _STATE_CACHE[clean_email]

    try:
        drive_service = get_drive_service_for_account(clean_email)
        file_id = _get_or_create_state_file_id(drive_service, clean_email)

        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        fh.seek(0)
        content = fh.read().decode("utf-8")
        state = json.loads(content) if content.strip() else _create_fresh_default_state()

        with _CACHE_LOCK:
            _STATE_CACHE[clean_email] = state
            _PROCESSED_CACHE[clean_email] = {
                str(a["email_id"]) for a in state.get("email_actions", [])
                if isinstance(a, dict) and "email_id" in a and a["email_id"]
            }

        clear_account_reauth(clean_email)
        return state

    except Exception as e:
        logger.debug(f"Drive state sync skipped for {clean_email} ({e}). Using local fallback.")
        with _CACHE_LOCK:
            if clean_email not in _STATE_CACHE:
                _STATE_CACHE[clean_email] = _create_fresh_default_state()
            return _STATE_CACHE[clean_email]

def commit_drive_state(email: str) -> bool:
    """Flushes cached in-memory state to Google Drive safely."""
    clean_email = email.strip().lower()
    with _CACHE_LOCK:
        state = _STATE_CACHE.get(clean_email)
        if not state:
            return False
        try:
            raw_data = json.dumps(state, separators=(',', ':')).encode("utf-8")
        except Exception as ser_err:
            logger.error(f"State serialization failure for {clean_email}: {ser_err}")
            return False

    try:
        drive_service = get_drive_service_for_account(clean_email)
        file_id = _get_or_create_state_file_id(drive_service, clean_email)

        media = MediaIoBaseUpload(io.BytesIO(raw_data), mimetype="application/json")
        drive_service.files().update(
            fileId=file_id,
            media_body=media
        ).execute()
        return True

    except Exception as e:
        logger.debug(f"Could not commit state to Drive for {clean_email}: {e}")
        return False

# ---------------- State Helper Interfaces ---------------- #

def is_email_processed_drive(email: str, email_id: str) -> bool:
    """Performs an O(1) in-memory lookup to check if an email has already been actioned."""
    clean_email = email.strip().lower()
    str_mid = str(email_id)
    with _CACHE_LOCK:
        if clean_email in _PROCESSED_CACHE:
            return str_mid in _PROCESSED_CACHE[clean_email]

    load_drive_state(clean_email)
    with _CACHE_LOCK:
        return str_mid in _PROCESSED_CACHE.get(clean_email, set())

def record_email_action_drive(account_email: str, action_data: Dict[str, Any]):
    """Appends an email processing action to local cache and syncs it to Google Drive."""
    clean_email = account_email.strip().lower()
    state = load_drive_state(clean_email)
    mid = str(action_data.get("email_id", ""))

    with _CACHE_LOCK:
        actions = state.setdefault("email_actions", [])
        existing_idx = next((i for i, a in enumerate(actions) if str(a.get("email_id")) == mid), None)
        if existing_idx is not None:
            actions[existing_idx] = action_data
        else:
            actions.insert(0, action_data)

        state["email_actions"] = actions[:500]

        if clean_email not in _PROCESSED_CACHE:
            _PROCESSED_CACHE[clean_email] = set()
        if mid:
            _PROCESSED_CACHE[clean_email].add(mid)

    commit_drive_state(clean_email)

def get_drive_stats(email: str) -> Dict[str, int]:
    """Computes summary KPI statistics for an account."""
    clean_email = email.strip().lower()
    state = load_drive_state(clean_email)
    with _CACHE_LOCK:
        actions = state.get("email_actions", [])
        total = len(actions)
        actioned = sum(1 for a in actions if isinstance(a, dict) and a.get("status") == "ACTIONED")
        processed = sum(1 for a in actions if isinstance(a, dict) and a.get("status") == "PROCESSED")
        filtered = sum(1 for a in actions if isinstance(a, dict) and a.get("status") == "FILTERED")

    return {"total": total, "actioned": actioned, "processed": processed, "filtered": filtered}

def list_drive_emails(email: str, category_filter: str = "ALL", limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Returns paginated email action history matching the category filter."""
    clean_email = email.strip().lower()
    state = load_drive_state(clean_email)
    with _CACHE_LOCK:
        actions = state.get("email_actions", [])
        if category_filter and category_filter != "ALL":
            filtered = [a for a in actions if isinstance(a, dict) and a.get("category") == category_filter]
        else:
            filtered = [a for a in actions if isinstance(a, dict)]
        return filtered[offset:offset + limit]

def get_drive_setting(email: str, key: str, default: str = "") -> str:
    """Reads a configuration preference from local storage with Drive state fallback."""
    clean_email = email.strip().lower() if email else "global"
    
    # Dual-layer: Check local persistent SQLite store first
    local_val = get_setting(f"setting:{clean_email}:{key}") or get_setting(f"setting:global:{key}")
    if local_val:
        return local_val

    state = load_drive_state(clean_email)
    with _CACHE_LOCK:
        val = str(state.get("settings", {}).get(key, ""))
        return val if val else default

def set_drive_setting(email: str, key: str, value: str):
    """Updates a configuration preference into SQLite immediately and mirrors to Google Drive."""
    clean_email = email.strip().lower() if email else "global"

    # Dual-layer: Write to local persistent SQLite immediately
    set_setting(f"setting:{clean_email}:{key}", str(value))
    set_setting(f"setting:global:{key}", str(value))

    state = load_drive_state(clean_email)
    with _CACHE_LOCK:
        state.setdefault("settings", {})[key] = str(value)
    commit_drive_state(clean_email)