import base64
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from backend.utils.state_tracker import (
    delete_google_account,
    get_google_account,
    list_all_google_accounts,
    set_account_monitoring_status,
    upsert_google_account,
)
import config

logger = logging.getLogger("GoogleAuth")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
TOKEN_PATH = BASE_DIR / "token.json"

_AUTH_LOCK = threading.Lock()
REAUTH_NEEDED_ACCOUNTS: Set[str] = set()

# In-memory persistent client cache to eliminate repeated build() calls
_SERVICE_CLIENT_CACHE: Dict[str, Dict[str, Any]] = {}


def invalidate_service_cache(email: str):
    """Evicts cached Google API clients when credentials change or disconnect."""
    clean_email = email.strip().lower()
    with _AUTH_LOCK:
        _SERVICE_CLIENT_CACHE.pop(clean_email, None)


# ----------------- Re-Authentication Registry ----------------- #


def mark_account_needs_reauth(email: str):
    """Flags an account requiring user re-authentication and evicts cached clients."""
    clean_email = email.strip().lower()
    with _AUTH_LOCK:
        REAUTH_NEEDED_ACCOUNTS.add(clean_email)
        _SERVICE_CLIENT_CACHE.pop(clean_email, None)


def clear_account_reauth(email: str):
    """Clears the re-authentication flag for an account."""
    clean_email = email.strip().lower()
    with _AUTH_LOCK:
        REAUTH_NEEDED_ACCOUNTS.discard(clean_email)


def is_reauth_needed(email: str) -> bool:
    """Checks whether an account requires re-authentication."""
    clean_email = email.strip().lower()
    with _AUTH_LOCK:
        return clean_email in REAUTH_NEEDED_ACCOUNTS


SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.appdata",
]


def decode_jwt_payload(jwt_str: str) -> Dict[str, Any]:
    """Safely decodes an unverified JWT token payload with proper base64 URL padding."""
    if not jwt_str or not isinstance(jwt_str, str):
        return {}
    try:
        parts = jwt_str.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            decoded_bytes = base64.urlsafe_b64decode(padded.encode("utf-8"))
            return json.loads(decoded_bytes.decode("utf-8", errors="ignore"))
    except Exception:
        pass
    return {}


def auto_migrate_legacy_token():
    """Migrates an existing legacy single-account token.json into the multi-account database once."""
    if not TOKEN_PATH.exists():
        return

    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        email = None
        name = "User"
        picture = None

        if "id_token" in data:
            payload = decode_jwt_payload(data["id_token"])
            email = payload.get("email")
            name = payload.get("name") or name
            picture = payload.get("picture")

        if email:
            clean_email = email.strip().lower()
            existing = get_google_account(clean_email)
            if not existing:
                upsert_google_account(
                    email=clean_email,
                    name=name,
                    picture=picture or "",
                    token_data=json.dumps(data),
                    is_monitored=1,
                )
                logger.info(
                    f"Migrated legacy token for {clean_email} into database."
                )
    except Exception as e:
        logger.warning(f"Could not auto-migrate legacy token: {e}")


auto_migrate_legacy_token()


def get_credentials_for_account(email: str) -> Optional[Credentials]:
    """Retrieves, checks, and refreshes OAuth credentials strictly isolated to the specified email."""
    if not email:
        return None

    clean_email = email.strip().lower()

    with _AUTH_LOCK:
        acc = get_google_account(clean_email)
        if not acc or not acc.get("token_data"):
            logger.error(
                f"No account record or token_data found for: {clean_email}"
            )
            return None

        db_email = str(acc.get("email", "")).strip().lower()
        if db_email != clean_email:
            logger.critical(
                f"Account cross-contamination detected! Requested: '{clean_email}', DB returned: '{db_email}'."
            )
            return None

        try:
            data = json.loads(acc["token_data"])
            token = data.get("token") or data.get("access_token")
            refresh_token = data.get("refresh_token")
            token_uri = data.get(
                "token_uri", "https://oauth2.googleapis.com/token"
            )
            client_id = data.get("client_id") or getattr(
                config, "GOOGLE_CLIENT_ID", None
            )
            client_secret = data.get("client_secret") or getattr(
                config, "GOOGLE_CLIENT_SECRET", None
            )
            scopes = data.get("scopes") or SCOPES

            creds = Credentials(
                token=token,
                refresh_token=refresh_token,
                token_uri=token_uri,
                client_id=client_id,
                client_secret=client_secret,
                scopes=scopes,
            )

            # Refresh token if expired or invalid
            if (creds.expired or not creds.valid) and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    clear_account_reauth(clean_email)
                    _SERVICE_CLIENT_CACHE.pop(clean_email, None)
                except RefreshError as refresh_err:
                    logger.error(
                        f"Google refresh token expired or revoked for {clean_email}: {refresh_err}"
                    )
                    mark_account_needs_reauth(clean_email)
                    return None

                updated_dict = json.loads(creds.to_json())
                if not updated_dict.get("refresh_token") and refresh_token:
                    updated_dict["refresh_token"] = refresh_token

                upsert_google_account(
                    email=clean_email,
                    name=acc.get("name") or clean_email,
                    picture=acc.get("picture") or "",
                    token_data=json.dumps(updated_dict),
                    session_id=acc.get("session_id"),
                    is_monitored=acc.get("is_monitored", 1),
                )

            return creds if creds.valid else None

        except Exception as e:
            logger.error(
                f"Error loading credentials for account {clean_email}: {e}"
            )
            return None


def get_google_services_for_account(email: str) -> Tuple[Any, Any]:
    """Returns persistent, cached Gmail and Calendar service clients strictly authorized for this email."""
    clean_email = email.strip().lower()

    # Reuse cached instances if token remains valid
    with _AUTH_LOCK:
        if clean_email in _SERVICE_CLIENT_CACHE and not is_reauth_needed(
            clean_email
        ):
            cached = _SERVICE_CLIENT_CACHE[clean_email]
            if "gmail" in cached and "calendar" in cached:
                return cached["gmail"], cached["calendar"]

    creds = get_credentials_for_account(clean_email)
    if not creds:
        raise RuntimeError(
            f"No valid credentials found for account: {clean_email}. Re-authentication required."
        )

    # Build once with cache_discovery=False to prevent disk/memory leaks
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)

    with _AUTH_LOCK:
        if clean_email not in _SERVICE_CLIENT_CACHE:
            _SERVICE_CLIENT_CACHE[clean_email] = {}
        _SERVICE_CLIENT_CACHE[clean_email]["gmail"] = gmail
        _SERVICE_CLIENT_CACHE[clean_email]["calendar"] = calendar

    return gmail, calendar


def get_drive_service_for_account(email: str) -> Any:
    """Returns a persistent, cached Google Drive service client authorized for this email."""
    clean_email = email.strip().lower()

    with _AUTH_LOCK:
        if clean_email in _SERVICE_CLIENT_CACHE and not is_reauth_needed(
            clean_email
        ):
            cached = _SERVICE_CLIENT_CACHE[clean_email]
            if "drive" in cached:
                return cached["drive"]

    creds = get_credentials_for_account(clean_email)
    if not creds:
        raise RuntimeError(
            f"No valid credentials found for account: {clean_email}. Re-authentication required."
        )

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    with _AUTH_LOCK:
        if clean_email not in _SERVICE_CLIENT_CACHE:
            _SERVICE_CLIENT_CACHE[clean_email] = {}
        _SERVICE_CLIENT_CACHE[clean_email]["drive"] = drive

    return drive


def get_monitored_accounts() -> List[Dict[str, Any]]:
    """Returns all accounts configured with active monitoring with normalized emails."""
    all_accs = list_all_google_accounts()
    monitored = []
    for a in all_accs:
        if a.get("is_monitored") == 1:
            a["email"] = a["email"].strip().lower()
            monitored.append(a)
    return monitored