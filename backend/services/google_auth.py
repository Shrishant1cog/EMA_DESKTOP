import os
import json
import base64
import logging
from pathlib import Path
from typing import Tuple, Optional, Dict, Any, List

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import config
from backend.utils.state_tracker import (
    upsert_google_account,
    get_google_account,
    list_all_google_accounts,
    delete_google_account,
    set_account_monitoring_status
)

logger = logging.getLogger(__name__)

TOKEN_PATH = Path(__file__).resolve().parent.parent / "token.json"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file"
]

def decode_jwt_payload(jwt_str: str) -> Dict[str, Any]:
    try:
        parts = jwt_str.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
    except Exception:
        pass
    return {}

def auto_migrate_legacy_token():
    """Migrates an existing single token.json into the multi-account database if found."""
    if TOKEN_PATH.exists():
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
                upsert_google_account(
                    email=email,
                    name=name,
                    picture=picture or "",
                    token_data=json.dumps(data),
                    is_monitored=1
                )
                logger.info(f"Migrated legacy token for {email} to database.")
        except Exception as e:
            logger.warning(f"Could not auto-migrate legacy token: {e}")

# Run legacy migration on startup
auto_migrate_legacy_token()

def get_credentials_for_account(email: str) -> Optional[Credentials]:
    """Retrieves and refreshes credentials for a specific account email."""
    acc = get_google_account(email)
    if not acc or not acc.get("token_data"):
        return None

    try:
        data = json.loads(acc["token_data"])
        token = data.get("token") or data.get("access_token")
        refresh_token = data.get("refresh_token")
        token_uri = data.get("token_uri", "https://oauth2.googleapis.com/token")
        client_id = data.get("client_id") or getattr(config, "GOOGLE_CLIENT_ID", None)
        client_secret = data.get("client_secret") or getattr(config, "GOOGLE_CLIENT_SECRET", None)
        scopes = data.get("scopes") or SCOPES

        creds = Credentials(
            token=token,
            refresh_token=refresh_token,
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes
        )

        if (creds.expired or not creds.valid) and creds.refresh_token:
            creds.refresh(Request())
            updated_dict = json.loads(creds.to_json())
            # Ensure refresh_token is preserved
            if not updated_dict.get("refresh_token") and refresh_token:
                updated_dict["refresh_token"] = refresh_token
            upsert_google_account(
                email=email,
                name=acc.get("name") or email,
                picture=acc.get("picture") or "",
                token_data=json.dumps(updated_dict),
                is_monitored=acc.get("is_monitored", 1)
            )

        return creds if creds.valid else None
    except Exception as e:
        logger.error(f"Error loading credentials for account {email}: {e}")
        return None

def get_google_services_for_account(email: str) -> Tuple[Any, Any]:
    creds = get_credentials_for_account(email)
    if not creds:
        raise RuntimeError(f"No valid credentials found for account: {email}")
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return gmail, calendar

def get_drive_service_for_account(email: str) -> Any:
    creds = get_credentials_for_account(email)
    if not creds:
        raise RuntimeError(f"No valid credentials found for account: {email}")
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def get_monitored_accounts() -> List[Dict[str, Any]]:
    """Returns all accounts that have monitoring toggled ON."""
    all_accs = list_all_google_accounts()
    return [a for a in all_accs if a.get("is_monitored") == 1]