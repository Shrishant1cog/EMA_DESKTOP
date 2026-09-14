import os
import sys
import json
import time
import sqlite3
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google_auth_oauthlib.flow import Flow

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

import config
from backend.utils.logger import setup_logger
from backend.utils.state_tracker import (
    DEFAULT_DB_PATH,
    init_db,
    is_email_processed,
    record_email_action,
    get_db_connection,
    upsert_google_account,
    list_all_google_accounts,
    set_account_monitoring_status,
    delete_google_account,
    get_setting,
    set_setting,
    delete_setting,
)
from backend.services.google_auth import (
    SCOPES,
    get_google_services_for_account,
    get_drive_service_for_account,
    get_monitored_accounts,
    decode_jwt_payload,
)
from backend.services.gmail_service import (
    fetch_email_details,
    get_unprocessed_message_ids,
    fetch_raw_attachments,
)
from backend.services.drive_service import upload_evidence_to_drive
from backend.services.ai_service import extract_events_from_email
from backend.services.calendar_service import create_or_update_event

LOG_FILE = getattr(config, "LOG_FILE", BASE_DIR / "calendar_assistant.log")
init_db()

logger = setup_logger("WebController")
SERVER_START_EPOCH = int(time.time()) - 10

# ----------------- Processing Pipeline ----------------- #

def process_single_email(gmail_service, calendar_service, drive_email: str, mid: str, account_email: str) -> bool:
    try:
        email_data = fetch_email_details(gmail_service, mid)
        spam_threshold_pct = int(get_setting("spam_threshold", "50"))
        spam_threshold_ratio = spam_threshold_pct / 100.0
        design_enabled = get_setting("design_events_enabled", "false").lower() == "true"
        user_prompt = get_setting("design_events_prompt", "") if design_enabled else ""
        default_time = get_setting("default_timing", "09:00")

        analysis = extract_events_from_email(
            subject=email_data.get("subject", ""),
            sender=email_data.get("sender", ""),
            date_received=email_data.get("date_received", ""),
            email_body=email_data.get("email_body", ""),
            attachment_text=email_data.get("combined_attachment_text", ""),
            custom_prompt=user_prompt,
            default_time=default_time,
            spam_threshold=spam_threshold_ratio,
        )

        action_taken = analysis.action_description
        status = "PROCESSED"
        category = analysis.category

        spam_protection_enabled = get_setting("spam_protection_enabled", "true").lower() == "true"
        is_spam = (
            getattr(analysis, "is_spam_or_scam", False)
            or getattr(analysis, "spam_score", 0.0) >= spam_threshold_ratio
            or analysis.category == "Spam / Scam"
        )

        if spam_protection_enabled and is_spam:
            score = getattr(analysis, "spam_score", 0.8)
            pct = int(score * 100) if score > 0 else 80
            category = "Spam / Scam"
            status = "FILTERED"
            action_taken = f"Ignored - Detected as Spam/Scam ({pct}% probability)"
            logger.info(f"Filtered spam email '{email_data.get('subject')}' (Score: {score})")
        elif analysis.has_calendar_event and analysis.events:
            has_files = (
                bool(email_data.get("has_attachments"))
                or len(email_data.get("attachments", [])) > 0
                or bool(email_data.get("combined_attachment_text"))
            )
            only_files_required = get_setting("only_remind_with_files", "false").lower() == "true"

            if only_files_required and not has_files:
                action_taken = "Calendar reminder skipped (No file attached per settings)"
                status = "FILTERED"
            else:
                uploaded_attachments = []
                raw_attachments = email_data.get("attachments", [])

                if raw_attachments and (has_files or getattr(analysis, "is_attachment_evidence", False)):
                    try:
                        drive_service = get_drive_service_for_account(drive_email)
                        for att in raw_attachments:
                            if att.get("data"):
                                drive_meta = upload_evidence_to_drive(
                                    drive_service,
                                    filename=att["filename"],
                                    file_bytes=att["data"],
                                    mime_type=att.get("mime_type", "application/octet-stream"),
                                )
                                if drive_meta:
                                    uploaded_attachments.append(drive_meta)
                    except Exception as drive_err:
                        logger.warning(f"Drive upload failed for {drive_email}: {drive_err}")
                elif not raw_attachments and (has_files or getattr(analysis, "is_attachment_evidence", False)):
                    downloaded = fetch_raw_attachments(gmail_service, mid)
                    if downloaded:
                        try:
                            drive_service = get_drive_service_for_account(drive_email)
                            for rf in downloaded:
                                drive_meta = upload_evidence_to_drive(
                                    drive_service,
                                    filename=rf["filename"],
                                    file_bytes=rf["data"],
                                    mime_type=rf["mimeType"],
                                )
                                if drive_meta:
                                    uploaded_attachments.append(drive_meta)
                        except Exception as drive_err:
                            logger.warning(f"Drive fallback upload failed: {drive_err}")

                for ev in analysis.events:
                    create_or_update_event(
                        calendar_service,
                        ev,
                        original_sender=email_data.get("sender", ""),
                        attachments=uploaded_attachments,
                    )

                evidence_note = f" with {len(uploaded_attachments)} file(s) attached" if uploaded_attachments else ""
                action_taken = f"Scheduled {len(analysis.events)} event(s) to Calendar{evidence_note}"
                status = "ACTIONED"
        elif analysis.category == "General / Marketing":
            status = "FILTERED"
        else:
            status = "PROCESSED"

        record_email_action(
            email_id=mid,
            account_email=account_email,
            sender=email_data.get("sender", ""),
            subject=email_data.get("subject", "No Subject"),
            category=category,
            priority=analysis.priority,
            summary=analysis.summary,
            action_taken=action_taken,
            status=status,
        )

        try:
            gmail_service.users().messages().modify(
                userId="me", id=str(mid), body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except Exception:
            pass

        return True
    except Exception as e:
        logger.error(f"Error processing email {mid} for {account_email}: {e}")
        return False

# ----------------- Background Worker ----------------- #

WORKER_STATE = {
    "is_running": False,
    "last_sync": None,
    "current_status": "Idle",
    "stop_event": threading.Event(),
}
WORKER_THREAD: Optional[threading.Thread] = None

def background_worker_loop():
    logger.info("Multi-account email monitor thread started.")
    WORKER_STATE["is_running"] = True
    WORKER_STATE["current_status"] = "Live"

    while not WORKER_STATE["stop_event"].is_set():
        accounts = get_monitored_accounts()
        if not accounts:
            WORKER_STATE["current_status"] = "Idle"
            time.sleep(3)
            continue

        WORKER_STATE["current_status"] = "Live"
        WORKER_STATE["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for acc in accounts:
            if WORKER_STATE["stop_event"].is_set():
                break

            acc_email = acc["email"]
            try:
                gmail_service, calendar_service = get_google_services_for_account(acc_email)
                query = f"is:unread label:INBOX -label:spam -label:trash after:{SERVER_START_EPOCH}"
                msg_ids = get_unprocessed_message_ids(gmail_service, query=query, max_results=5)

                for mid in msg_ids:
                    if WORKER_STATE["stop_event"].is_set():
                        break
                    if is_email_processed(mid, account_email=acc_email):
                        continue

                    process_single_email(gmail_service, calendar_service, acc_email, mid, acc_email)
            except Exception as acc_err:
                logger.error(f"Error scanning inbox for {acc_email}: {acc_err}")

        interval = getattr(config.AppConfig(), "poll_interval", 15)
        for _ in range(interval):
            if WORKER_STATE["stop_event"].is_set():
                break
            time.sleep(1)

    WORKER_STATE["is_running"] = False
    WORKER_STATE["current_status"] = "Idle"

def start_worker_internal():
    global WORKER_THREAD
    if WORKER_STATE["is_running"]:
        return
    WORKER_STATE["stop_event"].clear()
    WORKER_THREAD = threading.Thread(target=background_worker_loop, daemon=True)
    WORKER_THREAD.start()
    set_setting("worker_enabled", "true")

def stop_worker_internal():
    WORKER_STATE["stop_event"].set()
    set_setting("worker_enabled", "false")

# ----------------- Inspector Engine ----------------- #

INSPECT_STATE = {
    "is_running": False,
    "current_count": 0,
    "target_count": 10,
    "status_message": "Ready to inspect",
    "stop_event": threading.Event(),
}
INSPECT_THREAD: Optional[threading.Thread] = None

def inspect_previous_loop(max_target: int):
    clamped_target = min(max(10, max_target), 40)
    INSPECT_STATE["is_running"] = True
    INSPECT_STATE["target_count"] = clamped_target
    INSPECT_STATE["current_count"] = 0
    INSPECT_STATE["status_message"] = f"Scanning {clamped_target} unprocessed emails..."

    try:
        accounts = get_monitored_accounts()
        if not accounts:
            INSPECT_STATE["status_message"] = "No accounts enabled for monitoring."
            INSPECT_STATE["is_running"] = False
            return

        found_unprocessed = 0

        for acc in accounts:
            if INSPECT_STATE["stop_event"].is_set() or found_unprocessed >= clamped_target:
                break

            acc_email = acc["email"]
            try:
                gmail_service, calendar_service = get_google_services_for_account(acc_email)
                results = gmail_service.users().messages().list(
                    userId="me",
                    q="label:INBOX -label:spam -label:trash",
                    maxResults=25,
                ).execute()

                for m in results.get("messages", []):
                    if INSPECT_STATE["stop_event"].is_set() or found_unprocessed >= clamped_target:
                        break

                    mid = m["id"]
                    if is_email_processed(mid, account_email=acc_email):
                        continue

                    INSPECT_STATE["status_message"] = f"Analyzing [{acc_email}] email {found_unprocessed + 1} of {clamped_target}..."
                    process_single_email(gmail_service, calendar_service, acc_email, mid, acc_email)
                    found_unprocessed += 1
                    INSPECT_STATE["current_count"] = found_unprocessed
            except Exception as e:
                logger.error(f"Inspection error on {acc_email}: {e}")

        if INSPECT_STATE["stop_event"].is_set():
            INSPECT_STATE["status_message"] = f"Stopped. Processed {found_unprocessed} email(s)."
        elif found_unprocessed == 0:
            INSPECT_STATE["status_message"] = "All inbox emails have already been processed."
        else:
            INSPECT_STATE["status_message"] = f"Complete! Processed {found_unprocessed} email(s)."
    finally:
        INSPECT_STATE["is_running"] = False

# ----------------- App Lifecycle & FastAPI Instance ----------------- #

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    if get_setting("worker_enabled", "true") == "true" and get_monitored_accounts():
        start_worker_internal()
    yield
    stop_worker_internal()

app = FastAPI(title="Smart Universal Email Assistant API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Routes & Endpoints ----------------- #

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/")
def root():
    return RedirectResponse(url=f"{config.FRONTEND_URL}/index.html", status_code=303)

def build_oauth_flow(state: Optional[str] = None) -> Flow:
    client_config = {
        "web": {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [config.REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=config.REDIRECT_URI, state=state)

@app.get("/auth/login")
def auth_login():
    flow = build_oauth_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="select_account consent",
        include_granted_scopes="true",
    )
    if flow.code_verifier:
        set_setting(f"verifier_{state}", flow.code_verifier)
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
def auth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        return RedirectResponse(f"{config.FRONTEND_URL}/index.html?auth_error=" + (error or "missing_code"))

    try:
        flow = build_oauth_flow(state=state)
        code_verifier = get_setting(f"verifier_{state}", "")
        flow.fetch_token(code=code, code_verifier=code_verifier or None)
        delete_setting(f"verifier_{state}")

        creds = flow.credentials
        creds_dict = json.loads(creds.to_json())

        user_email = "default"
        user_name = "User"
        user_picture = ""

        if hasattr(creds, "id_token") and creds.id_token:
            payload = decode_jwt_payload(creds.id_token)
            user_email = payload.get("email", user_email)
            user_name = payload.get("name", user_email)
            user_picture = payload.get("picture", "")

        upsert_google_account(
            email=user_email,
            name=user_name,
            picture=user_picture,
            token_data=json.dumps(creds_dict),
            is_monitored=1,
        )

        stop_worker_internal()
        time.sleep(0.5)
        start_worker_internal()

        return RedirectResponse(url=f"{config.FRONTEND_URL}/index.html", status_code=303)
    except Exception as e:
        logger.error(f"Failed to authenticate account: {e}")
        return RedirectResponse(f"{config.FRONTEND_URL}/index.html?auth_error={e}")

@app.get("/auth/logout")
def auth_logout():
    stop_worker_internal()
    return RedirectResponse(url=f"{config.FRONTEND_URL}/index.html", status_code=303)

@app.get("/api/accounts")
def list_accounts():
    return {"accounts": list_all_google_accounts()}

class AccountTogglePayload(BaseModel):
    email: str
    is_monitored: bool

@app.post("/api/accounts/toggle")
def toggle_account_monitoring(payload: AccountTogglePayload):
    set_account_monitoring_status(payload.email, payload.is_monitored)
    return {"status": "success", "email": payload.email, "is_monitored": payload.is_monitored}

class AccountDeletePayload(BaseModel):
    email: str

@app.post("/api/accounts/delete")
def disconnect_account(payload: AccountDeletePayload):
    delete_google_account(payload.email)
    remaining = list_all_google_accounts()
    if not remaining:
        stop_worker_internal()
    return {
        "status": "success",
        "email": payload.email,
        "remaining": len(remaining),
    }

@app.get("/api/user")
def get_user():
    accs = list_all_google_accounts()
    if accs:
        primary = accs[0]
        return {
            "authenticated": True,
            "email": primary["email"],
            "name": primary.get("name") or primary["email"],
            "picture": primary.get("picture"),
            "total_accounts": len(accs),
        }
    return {"authenticated": False}

class SettingsPayload(BaseModel):
    only_remind_with_files: Optional[bool] = None
    design_events_enabled: Optional[bool] = None
    design_events_prompt: Optional[str] = None
    spam_protection_enabled: Optional[bool] = None
    spam_threshold: Optional[int] = None
    default_timing: Optional[str] = None

@app.get("/api/settings")
def get_app_settings():
    return {
        "only_remind_with_files": get_setting("only_remind_with_files", "false").lower() == "true",
        "design_events_enabled": get_setting("design_events_enabled", "false").lower() == "true",
        "design_events_prompt": get_setting("design_events_prompt", ""),
        "spam_protection_enabled": get_setting("spam_protection_enabled", "true").lower() == "true",
        "spam_threshold": int(get_setting("spam_threshold", "50")),
        "default_timing": get_setting("default_timing", "09:00"),
    }

@app.post("/api/settings")
def update_app_settings(payload: SettingsPayload):
    if payload.only_remind_with_files is not None:
        set_setting("only_remind_with_files", str(payload.only_remind_with_files).lower())
    if payload.design_events_enabled is not None:
        set_setting("design_events_enabled", str(payload.design_events_enabled).lower())
    if payload.design_events_prompt is not None:
        set_setting("design_events_prompt", payload.design_events_prompt.strip())
    if payload.spam_protection_enabled is not None:
        set_setting("spam_protection_enabled", str(payload.spam_protection_enabled).lower())
    if payload.spam_threshold is not None:
        clamped = max(10, min(95, payload.spam_threshold))
        set_setting("spam_threshold", str(clamped))
    if payload.default_timing is not None:
        clean_time = payload.default_timing.strip()
        if len(clean_time) == 5 and ":" in clean_time:
            set_setting("default_timing", clean_time)
    return {"status": "success"}

class InspectRequest(BaseModel):
    count: int

@app.post("/api/inspect/start")
def start_inspect(req: InspectRequest):
    global INSPECT_THREAD
    if not get_monitored_accounts():
        raise HTTPException(status_code=400, detail="No accounts enabled for monitoring.")

    if INSPECT_STATE["is_running"]:
        return {"status": "already_running"}

    count = min(max(10, req.count), 40)
    INSPECT_STATE["stop_event"].clear()
    INSPECT_THREAD = threading.Thread(target=inspect_previous_loop, args=(count,), daemon=True)
    INSPECT_THREAD.start()
    return {"status": "started", "target_count": count}

@app.post("/api/inspect/stop")
def stop_inspect():
    if not INSPECT_STATE["is_running"]:
        return {"status": "not_running"}
    INSPECT_STATE["stop_event"].set()
    return {"status": "stopping"}

@app.get("/api/inspect/status")
def get_inspect_status():
    return {
        "is_running": INSPECT_STATE["is_running"],
        "current_count": INSPECT_STATE["current_count"],
        "target_count": INSPECT_STATE["target_count"],
        "status_message": INSPECT_STATE["status_message"],
    }

@app.get("/api/stats")
def get_stats():
    conn = get_db_connection()
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM email_actions").fetchone()[0]
    actioned = cur.execute("SELECT COUNT(*) FROM email_actions WHERE status = 'ACTIONED'").fetchone()[0]
    processed = cur.execute("SELECT COUNT(*) FROM email_actions WHERE status = 'PROCESSED'").fetchone()[0]
    filtered = cur.execute("SELECT COUNT(*) FROM email_actions WHERE status = 'FILTERED'").fetchone()[0]
    conn.close()

    return {
        "summary": {
            "total": total,
            "actioned": actioned,
            "processed": processed,
            "filtered": filtered,
            "time_saved": (total * 2),
        },
        "worker": {
            "is_running": WORKER_STATE["is_running"],
            "status": WORKER_STATE["current_status"],
            "last_sync": WORKER_STATE["last_sync"],
        },
    }

@app.get("/api/emails")
def list_emails(limit: int = 50, offset: int = 0, category_filter: str = "ALL"):
    conn = get_db_connection()
    cur = conn.cursor()

    if category_filter != "ALL":
        cur.execute(
            "SELECT * FROM email_actions WHERE category = ? ORDER BY processed_at DESC LIMIT ? OFFSET ?",
            (category_filter, limit, offset),
        )
    else:
        cur.execute(
            "SELECT * FROM email_actions ORDER BY processed_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"emails": rows}

@app.post("/api/worker/start")
def start_worker():
    start_worker_internal()
    return {"status": "started"}

@app.post("/api/worker/stop")
def stop_worker():
    stop_worker_internal()
    return {"status": "stopping"}

@app.get("/api/calendar/upcoming")
def get_upcoming_events(max_events: int = 10):
    accounts = get_monitored_accounts()
    if not accounts:
        return {"events": []}

    all_events = []
    now = datetime.now(ZoneInfo(config.USER_TIMEZONE)).isoformat()

    for acc in accounts:
        try:
            _, calendar_service = get_google_services_for_account(acc["email"])
            events_result = calendar_service.events().list(
                calendarId="primary",
                timeMin=now,
                maxResults=5,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            for ev in events_result.get("items", []):
                ev["_account_email"] = acc["email"]
                all_events.append(ev)
        except Exception as e:
            logger.warning(f"Could not load calendar for {acc['email']}: {e}")

    return {"events": all_events[:max_events]}

@app.get("/api/logs/stream")
def stream_logs():
    def log_generator():
        log_path = Path(LOG_FILE)
        if not log_path.exists():
            log_path.touch()
            yield "data: [Log monitor connected]\n\n"

        with open(log_path, "r", encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    yield f"data: {line.strip()}\n\n"
                else:
                    time.sleep(0.5)

    return StreamingResponse(log_generator(), media_type="text/event-stream")