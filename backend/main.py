import os
import sys
import gc
import json
import time
import uuid
import html
import threading
import asyncio
import subprocess
import webbrowser
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google_auth_oauthlib.flow import Flow

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# Clear accidental proxies that cause DNS lookup failures
for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"]:
    os.environ.pop(proxy_var, None)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
sys.path.append(str(BASE_DIR))

# Install resilient DNS resolver
try:
    from backend.utils.dns_resolver import install_resilient_dns
    install_resilient_dns()
except Exception:
    pass

import config
from backend.utils.logger import setup_logger
from backend.utils.state_tracker import (
    init_db,
    upsert_google_account,
    list_all_google_accounts,
    set_account_monitoring_status,
    delete_google_account,
)
from backend.services.drive_state_adapter import (
    is_email_processed_drive,
    record_email_action_drive,
    get_drive_stats,
    list_drive_emails,
    get_drive_setting,
    set_drive_setting,
    is_reauth_needed,
    clear_account_reauth,
)
from backend.services.google_auth import (
    SCOPES,
    get_credentials_for_account,
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

_INDEX_HTML_CACHE: Optional[str] = None
_LOGIN_HTML_CACHE: Optional[str] = None

_AUTH_LOCK = threading.Lock()
_AUTH_FLOWS: Dict[str, Dict[str, Any]] = {}

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def emit_user_log(level: str, message: str):
    """Passes messages directly to logger; formatting and file output are handled by logger handlers."""
    lvl = level.upper()
    if lvl in ("ERROR", "FAIL"):
        logger.error(message)
    elif lvl in ("WARN", "WARNING", "FILTER"):
        logger.warning(message)
    else:
        logger.info(message)


def set_auth_flow(sid: str, **kwargs):
    """Updates auth flow state thread-safely and prunes entries older than 15 minutes."""
    with _AUTH_LOCK:
        now = time.time()
        expired = [k for k, v in _AUTH_FLOWS.items() if now - v.get("timestamp", 0) > 900]
        for k in expired:
            _AUTH_FLOWS.pop(k, None)

        if sid not in _AUTH_FLOWS:
            _AUTH_FLOWS[sid] = {
                "timestamp": now,
                "status": "pending",
                "error": None,
                "email": None,
                "code_verifier": "",
                "redirect_uri": "",
            }
        _AUTH_FLOWS[sid].update(kwargs)
        _AUTH_FLOWS[sid]["timestamp"] = now


def get_auth_flow(sid: str) -> Optional[Dict[str, Any]]:
    """Fetches flow state safely without destroying verifier credentials."""
    with _AUTH_LOCK:
        return _AUTH_FLOWS.get(sid)


def open_in_chrome_or_default(url: str) -> bool:
    """Launches Google Chrome (or Edge/Default browser) on Windows silently without command windows."""
    if sys.platform == "win32":
        browser_candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
        for exe_path in browser_candidates:
            if os.path.isfile(exe_path):
                try:
                    subprocess.Popen(
                        [exe_path, url],
                        creationflags=CREATE_NO_WINDOW,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return True
                except Exception:
                    pass

        try:
            os.startfile(url)
            return True
        except Exception:
            pass

    try:
        webbrowser.open(url, new=2)
        return True
    except Exception as e:
        logger.warning(f"Browser launch failed: {e}")
        return False


def focus_desktop_window():
    """Brings the native desktop app window back to the front silently."""
    if sys.platform == "win32":
        try:
            ps_script = (
                '$wshell = New-Object -ComObject WScript.Shell; '
                '$wshell.AppActivate("EMA"); '
                '$wshell.AppActivate("Smart Universal Email Assistant"); '
                '$wshell.AppActivate("EmailAutomater")'
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            pass


# ----------------- App Lifecycle & Middleware ----------------- #


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    emit_user_log("INFO", "EMA Assistant service started successfully.")
    yield
    stop_worker_internal()
    if INSPECT_STATE["is_running"]:
        INSPECT_STATE["stop_event"].set()
    emit_user_log("INFO", "EMA Assistant service stopped.")


app = FastAPI(title="EMA - Smart Universal Email Assistant API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_request_session_id(request: Request) -> Optional[str]:
    sid = (
        request.headers.get("x-session-id")
        or request.cookies.get("assistant_session_id")
        or request.query_params.get("session_id")
        or request.query_params.get("state")
        or getattr(request.state, "session_id", None)
    )
    if sid in (None, "", "undefined", "null"):
        return None
    return sid


def get_primary_email_for_session(sid: Optional[str]) -> Optional[str]:
    all_accs = list_all_google_accounts()
    if not all_accs:
        return None
    if sid:
        scoped = [a for a in all_accs if a.get("session_id") == sid]
        if scoped:
            return scoped[0]["email"]
    return all_accs[0]["email"]


def get_dynamic_redirect_uri(request: Request) -> str:
    configured = getattr(config, "REDIRECT_URI", "").strip()
    if configured:
        return configured
    # Standardize to 127.0.0.1 to avoid redirect_uri_mismatch or session collisions
    return "http://127.0.0.1:8000/auth/callback"


# ----------------- Processing Pipeline ----------------- #


def process_single_email(
    gmail_service, calendar_service, drive_email: str, mid: str, account_email: str
) -> bool:
    clean_acc_email = account_email.strip().lower()
    subject = "No Subject"
    sender = "Unknown"
    try:
        email_data = fetch_email_details(gmail_service, mid)
        subject = email_data.get("subject", "No Subject")
        sender = email_data.get("sender", "Unknown")

        emit_user_log("INFO", f"Analyzing email from {sender} — '{subject}' [{clean_acc_email}]")

        spam_threshold_pct = int(get_drive_setting(clean_acc_email, "spam_threshold", "50"))
        spam_threshold_ratio = spam_threshold_pct / 100.0
        design_enabled = get_drive_setting(clean_acc_email, "design_events_enabled", "false").lower() == "true"
        user_prompt = get_drive_setting(clean_acc_email, "design_events_prompt", "") if design_enabled else ""
        default_time = get_drive_setting(clean_acc_email, "default_timing", "09:00")

        analysis = extract_events_from_email(
            subject=subject,
            sender=sender,
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

        spam_protection_enabled = get_drive_setting(clean_acc_email, "spam_protection_enabled", "true").lower() == "true"
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
            action_taken = f"Ignored - Detected as Spam/Scam ({pct}% confidence)"
            emit_user_log("FILTER", f"Filtered spam email '{subject}' for {clean_acc_email} ({pct}% confidence).")
        elif analysis.has_calendar_event and analysis.events:
            has_files = (
                bool(email_data.get("has_attachments"))
                or len(email_data.get("attachments", [])) > 0
                or bool(email_data.get("combined_attachment_text"))
            )
            only_files_required = get_drive_setting(clean_acc_email, "only_remind_with_files", "false").lower() == "true"

            if only_files_required and not has_files:
                action_taken = "Calendar reminder skipped (No file attached per settings)"
                status = "FILTERED"
                emit_user_log("INFO", f"Skipped calendar for '{subject}': No attachment attached.")
            else:
                uploaded_attachments = []
                raw_attachments = email_data.get("attachments", [])

                if raw_attachments and (has_files or getattr(analysis, "is_attachment_evidence", False)):
                    try:
                        drive_service = get_drive_service_for_account(clean_acc_email)
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
                                att["data"] = None  # Free byte buffer immediately
                    except Exception as drive_err:
                        emit_user_log("WARN", f"Could not save attachments to Drive for {clean_acc_email}: {drive_err}")

                scheduled_names = []
                for ev in analysis.events:
                    create_or_update_event(
                        calendar_service=calendar_service,
                        event_data=ev,
                        original_sender=sender,
                        attachments=uploaded_attachments,
                        calendar_id=clean_acc_email,
                        target_account_email=clean_acc_email,
                    )
                    scheduled_names.append(getattr(ev, "title", "Event"))

                evidence_note = f" with {len(uploaded_attachments)} file(s) attached" if uploaded_attachments else ""
                action_taken = f"Scheduled {len(analysis.events)} event(s) to Calendar{evidence_note}"
                status = "ACTIONED"
                emit_user_log("SUCCESS", f"[{clean_acc_email}] Scheduled: {', '.join(scheduled_names)}{evidence_note}")
        elif analysis.category == "General / Marketing":
            status = "FILTERED"
            emit_user_log("FILTER", f"Organized newsletter/marketing email: '{subject}'")
        else:
            status = "PROCESSED"
            emit_user_log("INFO", f"Processed email '{subject}' for {clean_acc_email} — No calendar action needed.")

        record_email_action_drive(
            account_email=clean_acc_email,
            action_data={
                "email_id": str(mid),
                "account_email": clean_acc_email,
                "sender": sender,
                "subject": subject,
                "category": category,
                "priority": analysis.priority,
                "summary": analysis.summary,
                "action_taken": action_taken,
                "status": status,
                "processed_at": datetime.now().isoformat(),
            },
        )

        try:
            gmail_service.users().messages().modify(
                userId="me", id=str(mid), body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except Exception:
            pass

        email_data.clear()
        return True

    except Exception as e:
        emit_user_log("ERROR", f"Failed to process email '{subject}' for {clean_acc_email}: {str(e)}")
        record_email_action_drive(
            account_email=clean_acc_email,
            action_data={
                "email_id": str(mid),
                "account_email": clean_acc_email,
                "sender": sender,
                "subject": subject,
                "category": "Failed",
                "priority": "LOW",
                "summary": f"Error: {str(e)}",
                "action_taken": "Failed processing",
                "status": "FAILED",
                "processed_at": datetime.now().isoformat(),
            },
        )
        return False
    finally:
        gc.collect()


# ----------------- Background Worker ----------------- #

WORKER_STATE = {
    "is_running": False,
    "last_sync": None,
    "current_status": "Idle",
    "stop_event": threading.Event(),
}
WORKER_THREAD: Optional[threading.Thread] = None


def background_worker_loop():
    emit_user_log("INFO", "Multi-account cloud-optimized background monitor active.")
    WORKER_STATE["is_running"] = True
    WORKER_STATE["current_status"] = "Live"

    IDLE_SLEEP = 60
    ACTIVE_SLEEP = 15

    while not WORKER_STATE["stop_event"].is_set():
        accounts = get_monitored_accounts()
        if not accounts:
            WORKER_STATE["current_status"] = "Idle"
            if WORKER_STATE["stop_event"].wait(timeout=5):
                break
            continue

        WORKER_STATE["current_status"] = "Live"
        WORKER_STATE["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_new_emails = 0

        for acc in accounts:
            if WORKER_STATE["stop_event"].is_set():
                break

            acc_email = acc["email"].strip().lower()
            try:
                gmail_service, calendar_service = get_google_services_for_account(acc_email)
                query = f"is:unread label:INBOX -label:spam -label:trash after:{SERVER_START_EPOCH}"
                msg_ids = get_unprocessed_message_ids(gmail_service, query=query, max_results=5)

                if msg_ids:
                    total_new_emails += len(msg_ids)
                    emit_user_log("INFO", f"Found {len(msg_ids)} new unread email(s) in {acc_email}.")

                for mid in msg_ids:
                    if WORKER_STATE["stop_event"].is_set():
                        break
                    if is_email_processed_drive(acc_email, mid):
                        continue

                    process_single_email(gmail_service, calendar_service, acc_email, mid, acc_email)

            except Exception as acc_err:
                emit_user_log("ERROR", f"Error checking inbox for {acc_email}: {str(acc_err)}")

        sleep_duration = ACTIVE_SLEEP if total_new_emails > 0 else IDLE_SLEEP
        if WORKER_STATE["stop_event"].wait(timeout=sleep_duration):
            break

    WORKER_STATE["is_running"] = False
    WORKER_STATE["current_status"] = "Idle"
    emit_user_log("INFO", "Background monitor paused.")


def start_worker_internal():
    global WORKER_THREAD
    if WORKER_STATE["is_running"]:
        return
    WORKER_STATE["stop_event"].clear()
    WORKER_THREAD = threading.Thread(target=background_worker_loop, daemon=True)
    WORKER_THREAD.start()


def stop_worker_internal():
    WORKER_STATE["stop_event"].set()


# ----------------- Inspector Engine ----------------- #

INSPECT_STATE = {
    "is_running": False,
    "current_count": 0,
    "target_count": 10,
    "status_message": "Ready to inspect",
    "stop_event": threading.Event(),
}
INSPECT_THREAD: Optional[threading.Thread] = None


def inspect_previous_loop(max_target: int, scoped_accounts: List[Dict[str, Any]]):
    clamped_target = min(max(10, max_target), 40)
    INSPECT_STATE["is_running"] = True
    INSPECT_STATE["target_count"] = clamped_target
    INSPECT_STATE["current_count"] = 0
    INSPECT_STATE["status_message"] = f"Scanning {clamped_target} unprocessed emails..."
    emit_user_log("INFO", f"Starting historical email scan (target: {clamped_target} emails).")

    try:
        if not scoped_accounts:
            INSPECT_STATE["status_message"] = "No accounts enabled for monitoring."
            return

        found_unprocessed = 0

        for acc in scoped_accounts:
            if INSPECT_STATE["stop_event"].is_set() or found_unprocessed >= clamped_target:
                break

            acc_email = acc["email"].strip().lower()
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
                    if is_email_processed_drive(acc_email, mid):
                        continue

                    INSPECT_STATE["status_message"] = f"Analyzing [{acc_email}] email {found_unprocessed + 1} of {clamped_target}..."
                    process_single_email(gmail_service, calendar_service, acc_email, mid, acc_email)
                    found_unprocessed += 1
                    INSPECT_STATE["current_count"] = found_unprocessed
            except Exception as e:
                emit_user_log("ERROR", f"Historical scan error on {acc_email}: {str(e)}")

        if INSPECT_STATE["stop_event"].is_set():
            INSPECT_STATE["status_message"] = f"Stopped. Processed {found_unprocessed} email(s)."
        elif found_unprocessed == 0:
            INSPECT_STATE["status_message"] = "All inbox emails have already been processed."
            emit_user_log("INFO", "Historical scan completed: No unprocessed emails found.")
        else:
            INSPECT_STATE["status_message"] = f"Complete! Processed {found_unprocessed} email(s)."
            emit_user_log("SUCCESS", f"Historical scan finished. Processed {found_unprocessed} email(s).")
    finally:
        INSPECT_STATE["is_running"] = False


# ----------------- Template Handlers ----------------- #


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    fav_path = FRONTEND_DIR / "favicon.ico"
    if fav_path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(fav_path)
    return Response(status_code=204)


@app.get("/logo.png", include_in_schema=False)
def logo():
    logo_path = FRONTEND_DIR / "logo.png"
    if logo_path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(logo_path)
    return Response(status_code=204)


@app.get("/")
def root():
    return RedirectResponse(url="/index.html", status_code=303)


@app.get("/index.html", response_class=HTMLResponse)
def serve_index_page(request: Request, session_id: Optional[str] = None):
    global _INDEX_HTML_CACHE
    html_path = FRONTEND_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html not found")
    with open(html_path, "r", encoding="utf-8") as f:
        _INDEX_HTML_CACHE = f.read()

    sid = session_id or get_request_session_id(request) or ""

    bridge_script = f"""
    <script>
    (function() {{
        let activeSid = "{sid}";
        if (!activeSid) {{
            activeSid = localStorage.getItem("assistant_session_id") || "";
        }}
        if (activeSid) {{
            localStorage.setItem("assistant_session_id", activeSid);
            document.cookie = "assistant_session_id=" + activeSid + "; path=/; max-age=2592000; SameSite=Lax";
        }}
        const _origFetch = window.fetch;
        window.fetch = function(input, init) {{
            init = init || {{}};
            init.headers = init.headers || {{}};
            const storedSid = localStorage.getItem("assistant_session_id") || activeSid;
            if (storedSid) {{
                if (init.headers instanceof Headers) {{
                    init.headers.set("X-Session-ID", storedSid);
                }} else {{
                    init.headers["X-Session-ID"] = storedSid;
                }}
            }}
            init.credentials = "include";
            return _origFetch(input, init);
        }};
    }})();
    </script>
    """

    content = _INDEX_HTML_CACHE
    content = content.replace("<head>", f"<head>\n{bridge_script}", 1) if "<head>" in content else bridge_script + content

    response = HTMLResponse(content=content)
    if sid:
        response.set_cookie(
            key="assistant_session_id",
            value=sid,
            httponly=False,
            samesite="lax",
            path="/",
            max_age=30 * 24 * 3600,
        )
    return response


@app.get("/login.html", response_class=HTMLResponse)
def serve_login_page(request: Request):
    global _LOGIN_HTML_CACHE
    html_path = FRONTEND_DIR / "login.html"
    if not html_path.exists():
        return RedirectResponse(url="/index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        _LOGIN_HTML_CACHE = f.read()

    bridge_script = """
    <script>
    (function() {
        const _origFetch = window.fetch;
        window.fetch = function(input, init) {
            init = init || {};
            init.headers = init.headers || {};
            const storedSid = localStorage.getItem("assistant_session_id");
            if (storedSid) {
                if (init.headers instanceof Headers) {
                    init.headers.set("X-Session-ID", storedSid);
                } else {
                    init.headers["X-Session-ID"] = storedSid;
                }
            }
            init.credentials = "include";
            return _origFetch(input, init);
        };
    })();
    </script>
    """
    content = _LOGIN_HTML_CACHE
    content = content.replace("<head>", f"<head>\n{bridge_script}", 1) if "<head>" in content else bridge_script + content
    return HTMLResponse(content=content)


# ----------------- OAuth Handlers & Redirects ----------------- #


def build_oauth_flow(request: Request, state: Optional[str] = None, redirect_uri: Optional[str] = None) -> Flow:
    target_redirect = redirect_uri or get_dynamic_redirect_uri(request)
    client_config = {
        "web": {
            "client_id": config.GOOGLE_CLIENT_ID.strip(),
            "client_secret": config.GOOGLE_CLIENT_SECRET.strip(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [
                target_redirect,
                "http://127.0.0.1:8000/auth/callback",
                "http://localhost:8000/auth/callback",
                "http://127.0.0.1:8000/api/auth/callback",
            ],
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=target_redirect, state=state)


@app.post("/api/auth/launch-browser")
def launch_browser_auth(request: Request, email: Optional[str] = None):
    """Generates OAuth URL and opens Google Chrome/external browser."""
    sid = get_request_session_id(request) or str(uuid.uuid4())
    hint_param = f"&email={email.strip().lower()}" if email else ""
    chrome_target = f"http://127.0.0.1:8000/auth/login?session_id={sid}&browser=1{hint_param}"
    open_in_chrome_or_default(chrome_target)

    response = JSONResponse({"status": "opened", "session_id": sid, "auth_url": chrome_target})
    response.set_cookie(key="assistant_session_id", value=sid, httponly=False, samesite="lax", path="/", max_age=30 * 24 * 3600)
    return response


@app.get("/auth/login")
@app.get("/auth/google")
@app.get("/api/auth/google")
def auth_login(
    request: Request,
    session_id: Optional[str] = None,
    browser: Optional[str] = None,
    email: Optional[str] = None,
):
    sid = session_id or get_request_session_id(request) or str(uuid.uuid4())
    locked_redirect_uri = get_dynamic_redirect_uri(request)

    flow = build_oauth_flow(request=request, state=sid, redirect_uri=locked_redirect_uri)

    auth_kwargs: Dict[str, Any] = {
        "state": sid,
        "access_type": "offline",
        "prompt": "select_account",
        "include_granted_scopes": "false",
    }
    if email:
        auth_kwargs["login_hint"] = email.strip().lower()

    auth_url, _ = flow.authorization_url(**auth_kwargs)

    # Persist flow verifier and exact redirect_uri
    set_auth_flow(
        sid,
        status="pending",
        redirect_uri=locked_redirect_uri,
        code_verifier=flow.code_verifier or "",
        error=None,
    )

    if browser != "1":
        hint_param = f"&email={email.strip().lower()}" if email else ""
        chrome_target = f"http://127.0.0.1:8000/auth/login?session_id={sid}&browser=1{hint_param}"
        open_in_chrome_or_default(chrome_target)

        waiting_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="UTF-8">
          <title>Authenticating in Browser...</title>
          <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-[#060911] text-slate-200 min-h-screen flex items-center justify-center p-4 font-sans">
          <div class="max-w-md w-full p-8 rounded-3xl bg-slate-900 border border-blue-500/30 text-center space-y-4 shadow-2xl">
            <div id="spinner" class="w-12 h-12 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin mx-auto"></div>
            <h2 id="heading" class="text-base font-bold text-white">Opened in Browser</h2>
            <p id="subtext" class="text-xs text-slate-400">Complete authentication in the opened browser window.</p>
            <div id="error-box" class="hidden p-3 bg-rose-950/80 border border-rose-500/30 rounded-2xl text-xs text-rose-300"></div>
            <button onclick="window.location.replace('/login.html')" class="mt-4 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 rounded-xl transition">
              Cancel and Return
            </button>
          </div>
          <script>
            const timer = setInterval(async () => {{
              try {{
                const res = await fetch("/api/auth/status?session_id={sid}");
                const data = await res.json();
                if (data.status === "completed" || data.authenticated) {{
                  clearInterval(timer);
                  window.location.replace("/index.html?session_id={sid}");
                }} else if (data.status === "failed") {{
                  clearInterval(timer);
                  document.getElementById("spinner").classList.add("hidden");
                  document.getElementById("heading").innerText = "Sign-In Failed";
                  document.getElementById("subtext").innerText = "An error occurred during authentication.";
                  const errBox = document.getElementById("error-box");
                  errBox.innerText = data.error || "Authentication denied";
                  errBox.classList.remove("hidden");
                }}
              }} catch (e) {{}}
            }}, 1200);
          </script>
        </body>
        </html>
        """
        response = HTMLResponse(content=waiting_html)
        response.set_cookie(key="assistant_session_id", value=sid, httponly=False, samesite="lax", path="/", max_age=30 * 24 * 3600)
        return response

    response = RedirectResponse(auth_url)
    response.set_cookie(key="assistant_session_id", value=sid, httponly=False, samesite="lax", path="/", max_age=30 * 24 * 3600)
    return response


@app.get("/auth/callback")
@app.get("/api/auth/callback")
def auth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    session_id = state or get_request_session_id(request) or ""
    flow_record = get_auth_flow(session_id) if session_id else None

    if error or not code:
        raw_err = error or "missing_code"
        safe_err = html.escape(str(raw_err))
        emit_user_log("ERROR", f"Google authentication failed: {safe_err}")
        if session_id:
            set_auth_flow(session_id, status="failed", error=safe_err)

        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html lang="en">
            <head><meta charset="utf-8"><title>Authentication Failed</title><script src="https://cdn.tailwindcss.com"></script></head>
            <body class="bg-[#060911] text-slate-100 min-h-screen flex items-center justify-center p-4 font-sans">
              <div class="max-w-md w-full p-8 rounded-3xl bg-slate-900 border border-rose-500/30 text-center space-y-4 shadow-2xl">
                <div class="w-12 h-12 rounded-full bg-rose-500/10 text-rose-400 flex items-center justify-center mx-auto text-xl font-bold">✕</div>
                <h2 class="text-base font-bold text-white">Sign-in Unsuccessful</h2>
                <p class="text-xs text-slate-400">{safe_err}</p>
                <a href="/auth/login?browser=1" class="inline-block mt-3 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-xs font-semibold text-white rounded-xl transition">Try Again</a>
              </div>
            </body>
            </html>
            """,
            status_code=400,
        )

    try:
        redirect_uri = flow_record.get("redirect_uri") if flow_record else get_dynamic_redirect_uri(request)
        code_verifier = flow_record.get("code_verifier") if flow_record else None

        flow = build_oauth_flow(request=request, state=session_id, redirect_uri=redirect_uri)
        # Ensure code_verifier is preserved for PKCE verification
        if code_verifier:
            flow.code_verifier = code_verifier
        flow.fetch_token(code=code, code_verifier=code_verifier or None)

        creds = flow.credentials
        creds_dict = json.loads(creds.to_json())

        user_email = ""
        user_name = ""
        user_picture = ""

        if hasattr(creds, "id_token") and creds.id_token:
            try:
                payload = decode_jwt_payload(creds.id_token)
                user_email = payload.get("email", "")
                user_name = payload.get("name", "")
                user_picture = payload.get("picture", "")
            except Exception:
                pass

        if not user_email:
            try:
                from googleapiclient.discovery import build
                service = build("oauth2", "v2", credentials=creds)
                ui = service.userinfo().get().execute()
                user_email = ui.get("email", "")
                user_name = ui.get("name", user_email)
                user_picture = ui.get("picture", "")
            except Exception:
                user_email = f"user_{session_id[:6]}@gmail.com"
                user_name = "User"

        clean_user_email = user_email.strip().lower()

        upsert_google_account(
            email=clean_user_email,
            name=user_name or clean_user_email,
            picture=user_picture,
            token_data=json.dumps(creds_dict),
            session_id=session_id,
            is_monitored=1,
        )

        clear_account_reauth(clean_user_email)
        if session_id:
            set_auth_flow(session_id, status="completed", email=clean_user_email, error=None)

        emit_user_log("SUCCESS", f"Connected account {clean_user_email} to monitoring.")
        focus_desktop_window()

        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <title>Authentication Successful</title>
          <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-[#060911] text-slate-200 min-h-screen flex items-center justify-center p-4 font-sans">
          <div class="max-w-md w-full p-8 rounded-3xl bg-slate-900 border border-emerald-500/30 text-center space-y-4 shadow-2xl">
            <div class="w-14 h-14 rounded-2xl bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 flex items-center justify-center mx-auto text-2xl font-bold">
              ✓
            </div>
            <div>
              <h2 class="text-base font-bold text-white tracking-tight">Account Connected!</h2>
              <p class="text-xs text-slate-400 mt-1">Authenticated <span class="text-blue-300 font-mono">{html.escape(clean_user_email)}</span></p>
            </div>
            <div class="p-3 bg-slate-950/80 border border-white/5 rounded-2xl text-xs text-emerald-400 font-medium">
              You can now close this browser window and return to EMA.
            </div>
          </div>
          <script>
            setTimeout(() => {{
              try {{ window.close(); }} catch (e) {{}}
              window.location.replace("/index.html?session_id={session_id}");
            }}, 2000);
          </script>
        </body>
        </html>
        """
        response = HTMLResponse(content=html_content)
        if session_id:
            response.set_cookie(
                key="assistant_session_id",
                value=session_id,
                httponly=False,
                samesite="lax",
                path="/",
                max_age=30 * 24 * 3600,
            )
        return response
    except Exception as e:
        err_msg = str(e)
        emit_user_log("ERROR", f"Account authentication failed: {err_msg}")
        if session_id:
            set_auth_flow(session_id, status="failed", error=err_msg)
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html lang="en">
            <head><meta charset="utf-8"><title>Authentication Failed</title><script src="https://cdn.tailwindcss.com"></script></head>
            <body class="bg-[#060911] text-slate-100 min-h-screen flex items-center justify-center p-4 font-sans">
              <div class="max-w-md w-full p-8 rounded-3xl bg-slate-900 border border-rose-500/30 text-center space-y-4 shadow-2xl">
                <div class="w-12 h-12 rounded-full bg-rose-500/10 text-rose-400 flex items-center justify-center mx-auto text-xl font-bold">✕</div>
                <h2 class="text-base font-bold text-white">Token Exchange Failed</h2>
                <p class="text-xs text-slate-400">{html.escape(err_msg)}</p>
                <a href="/auth/login?browser=1" class="inline-block mt-3 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-xs font-semibold text-white rounded-xl transition">Try Again</a>
              </div>
            </body>
            </html>
            """,
            status_code=400,
        )


@app.get("/api/auth/status")
def get_auth_status(request: Request, session_id: Optional[str] = None):
    sid = session_id or get_request_session_id(request)
    flow = get_auth_flow(sid) if sid else None

    if flow:
        return {
            "status": flow.get("status", "pending"),
            "error": flow.get("error"),
            "email": flow.get("email"),
            "authenticated": flow.get("status") == "completed",
        }

    # Fallback to database check if no flow record in memory
    all_accs = list_all_google_accounts()
    if sid:
        scoped = [a for a in all_accs if a.get("session_id") == sid]
        if scoped:
            return {"status": "completed", "authenticated": True, "email": scoped[0]["email"], "error": None}
    elif all_accs:
        return {"status": "completed", "authenticated": True, "email": all_accs[0]["email"], "error": None}

    return {"status": "idle", "authenticated": False, "error": None}


@app.get("/auth/logout")
@app.post("/auth/logout")
@app.get("/api/auth/logout")
@app.post("/api/auth/logout")
@app.get("/api/logout")
@app.post("/api/logout")
def auth_logout(request: Request):
    """Universal logout handler: cleans DB accounts, stops workers, evicts caches, and clears cookies."""
    sid = get_request_session_id(request)
    accounts = list_all_google_accounts()

    to_delete = [a for a in accounts if a.get("session_id") == sid] if sid else accounts
    if not to_delete:
        to_delete = accounts

    for acc in to_delete:
        clean_email = acc.get("email", "").strip().lower()
        if clean_email:
            delete_google_account(clean_email)
            emit_user_log("INFO", f"Disconnected account: {clean_email}")

    # Evict drive state memory cache
    try:
        from backend.services.drive_state_adapter import _CACHE_LOCK, _STATE_CACHE, _PROCESSED_CACHE, _FILE_ID_CACHE
        with _CACHE_LOCK:
            _STATE_CACHE.clear()
            _PROCESSED_CACHE.clear()
            _FILE_ID_CACHE.clear()
    except Exception:
        pass

    remaining = list_all_google_accounts()
    if not remaining:
        stop_worker_internal()

    is_ajax = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
        or request.method == "POST"
    )

    if is_ajax:
        response = JSONResponse({
            "status": "success",
            "authenticated": False,
            "remaining": len(remaining),
            "redirect": "/login.html"
        })
    else:
        response = RedirectResponse(url="/login.html", status_code=303)

    response.delete_cookie(key="assistant_session_id", path="/")
    response.delete_cookie(key="assistant_session_id", path="/auth")
    response.delete_cookie(key="assistant_session_id", path="/api")
    return response


# ----------------- Drive-Backed API Routes ----------------- #


@app.get("/api/user")
def get_user(request: Request):
    sid = get_request_session_id(request)
    all_accs = list_all_google_accounts()
    if not all_accs:
        return {"authenticated": False}

    # Session-isolated lookup with desktop single-user fallback
    scoped_accs = [a for a in all_accs if a.get("session_id") == sid] if sid else []
    primary = scoped_accs[0] if scoped_accs else all_accs[0]
    email = primary["email"]
    creds = get_credentials_for_account(email)

    if creds is None or is_reauth_needed(email):
        return {
            "authenticated": False,
            "needs_reauth": True,
            "email": email,
            "message": "Google credentials expired. Please re-authenticate.",
        }

    return {
        "authenticated": True,
        "email": email,
        "name": primary.get("name") or email,
        "picture": primary.get("picture"),
        "total_accounts": len(scoped_accs) if scoped_accs else len(all_accs),
    }


@app.get("/api/accounts")
def list_accounts(request: Request):
    sid = get_request_session_id(request)
    all_accs = list_all_google_accounts()
    accounts = [a for a in all_accs if a.get("session_id") == sid] if sid else all_accs
    if not accounts and all_accs:
        accounts = all_accs

    for a in accounts:
        acc_email = a["email"]
        a["needs_reauth"] = is_reauth_needed(acc_email) or (get_credentials_for_account(acc_email) is None)
    return {"accounts": accounts}


class AccountTogglePayload(BaseModel):
    email: str
    is_monitored: bool


@app.post("/api/accounts/toggle")
def toggle_account_monitoring(payload: AccountTogglePayload):
    clean_email = payload.email.strip().lower()
    set_account_monitoring_status(clean_email, payload.is_monitored)
    status_label = "enabled" if payload.is_monitored else "paused"
    emit_user_log("INFO", f"Monitoring {status_label} for {clean_email}")
    return {"status": "success", "email": clean_email, "is_monitored": payload.is_monitored}


class AccountDeletePayload(BaseModel):
    email: str


@app.post("/api/accounts/delete")
def disconnect_account(payload: AccountDeletePayload):
    clean_email = payload.email.strip().lower()
    delete_google_account(clean_email)
    emit_user_log("INFO", f"Removed account: {clean_email}")
    remaining = list_all_google_accounts()
    if not remaining:
        stop_worker_internal()
    return {"status": "success", "email": clean_email, "remaining": len(remaining)}


class SettingsPayload(BaseModel):
    only_remind_with_files: Optional[bool] = None
    design_events_enabled: Optional[bool] = None
    design_events_prompt: Optional[str] = None
    spam_protection_enabled: Optional[bool] = None
    spam_threshold: Optional[int] = None
    default_timing: Optional[str] = None


@app.get("/api/settings")
def get_app_settings(request: Request):
    sid = get_request_session_id(request)
    email = get_primary_email_for_session(sid)
    if not email:
        return {
            "only_remind_with_files": False,
            "design_events_enabled": False,
            "design_events_prompt": "",
            "spam_protection_enabled": True,
            "spam_threshold": 50,
            "default_timing": "09:00",
        }

    return {
        "only_remind_with_files": get_drive_setting(email, "only_remind_with_files", "false").lower() == "true",
        "design_events_enabled": get_drive_setting(email, "design_events_enabled", "false").lower() == "true",
        "design_events_prompt": get_drive_setting(email, "design_events_prompt", ""),
        "spam_protection_enabled": get_drive_setting(email, "spam_protection_enabled", "true").lower() == "true",
        "spam_threshold": int(get_drive_setting(email, "spam_threshold", "50")),
        "default_timing": get_drive_setting(email, "default_timing", "09:00"),
    }


@app.post("/api/settings")
def update_app_settings(payload: SettingsPayload, request: Request):
    sid = get_request_session_id(request)
    all_accs = list_all_google_accounts()
    session_accs = [a for a in all_accs if a.get("session_id") == sid] if sid else all_accs
    if not session_accs and all_accs:
        session_accs = all_accs

    if not session_accs:
        raise HTTPException(status_code=400, detail="No active accounts to configure.")

    for acc in session_accs:
        acc_email = acc["email"].strip().lower()
        if payload.only_remind_with_files is not None:
            set_drive_setting(acc_email, "only_remind_with_files", str(payload.only_remind_with_files).lower())
        if payload.design_events_enabled is not None:
            set_drive_setting(acc_email, "design_events_enabled", str(payload.design_events_enabled).lower())
        if payload.design_events_prompt is not None:
            set_drive_setting(acc_email, "design_events_prompt", payload.design_events_prompt.strip())
        if payload.spam_protection_enabled is not None:
            set_drive_setting(acc_email, "spam_protection_enabled", str(payload.spam_protection_enabled).lower())
        if payload.spam_threshold is not None:
            clamped = max(10, min(95, payload.spam_threshold))
            set_drive_setting(acc_email, "spam_threshold", str(clamped))
        if payload.default_timing is not None:
            clean_time = payload.default_timing.strip()
            if len(clean_time) == 5 and ":" in clean_time:
                set_drive_setting(acc_email, "default_timing", clean_time)

    emit_user_log("INFO", "Settings updated and saved to Google Drive.")
    return {"status": "success"}


class InspectRequest(BaseModel):
    count: int


@app.post("/api/inspect/start")
def start_inspect(req: InspectRequest, request: Request):
    global INSPECT_THREAD
    sid = get_request_session_id(request)
    all_accs = list_all_google_accounts()
    scoped = [a for a in all_accs if a.get("session_id") == sid] if sid else all_accs
    session_accs = [a for a in (scoped if scoped else all_accs) if a.get("is_monitored")]

    if not session_accs:
        raise HTTPException(status_code=400, detail="No accounts enabled for monitoring.")

    if INSPECT_STATE["is_running"]:
        return {"status": "already_running"}

    count = min(max(10, req.count), 40)
    INSPECT_STATE["stop_event"].clear()
    INSPECT_THREAD = threading.Thread(target=inspect_previous_loop, args=(count, session_accs), daemon=True)
    INSPECT_THREAD.start()
    return {"status": "started", "target_count": count}


@app.post("/api/inspect/stop")
def stop_inspect():
    if not INSPECT_STATE["is_running"]:
        return {"status": "not_running"}
    INSPECT_STATE["stop_event"].set()
    emit_user_log("INFO", "Email inspection canceled by user.")
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
def get_stats(request: Request):
    sid = get_request_session_id(request)
    all_accs = list_all_google_accounts()
    scoped = [a for a in all_accs if a.get("session_id") == sid] if sid else all_accs
    session_accs = scoped if scoped else all_accs

    total, actioned, processed, filtered = 0, 0, 0, 0
    for acc in session_accs:
        st = get_drive_stats(acc["email"])
        total += st["total"]
        actioned += st["actioned"]
        processed += st["processed"]
        filtered += st["filtered"]

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
def list_emails(request: Request, limit: int = 50, offset: int = 0, category_filter: str = "ALL"):
    sid = get_request_session_id(request)
    all_accs = list_all_google_accounts()
    scoped = [a for a in all_accs if a.get("session_id") == sid] if sid else all_accs
    session_accs = scoped if scoped else all_accs

    all_emails = []
    for acc in session_accs:
        all_emails.extend(list_drive_emails(acc["email"], category_filter=category_filter, limit=100, offset=0))

    all_emails.sort(key=lambda x: x.get("processed_at", ""), reverse=True)
    return {"emails": all_emails[offset:offset + limit]}


@app.post("/api/worker/start")
def start_worker():
    start_worker_internal()
    return {"status": "started"}


@app.post("/api/worker/stop")
def stop_worker():
    stop_worker_internal()
    return {"status": "stopping"}


@app.get("/api/calendar/upcoming")
def get_upcoming_events(request: Request, max_events: int = 15, include_all: bool = False):
    sid = get_request_session_id(request)
    all_accs = list_all_google_accounts()
    scoped = [a for a in all_accs if a.get("session_id") == sid] if sid else all_accs
    accounts = [a for a in (scoped if scoped else all_accs) if a.get("is_monitored")]

    if not accounts:
        return {"events": []}

    all_events = []
    tz_name = getattr(config, "USER_TIMEZONE", "UTC")
    now = datetime.now(ZoneInfo(tz_name)).isoformat()

    for acc in accounts:
        acc_email = acc["email"].strip().lower()
        try:
            _, calendar_service = get_google_services_for_account(acc_email)
            events_result = calendar_service.events().list(
                calendarId=acc_email,
                timeMin=now,
                maxResults=20,
                singleEvents=True,
                orderBy="startTime",
                fields="items(id,summary,description,location,start,end,htmlLink,extendedProperties)",
            ).execute()

            for ev in events_result.get("items", []):
                ev["_account_email"] = acc_email
                desc = ev.get("description", "") or ""
                summary = ev.get("summary", "") or ""

                is_assistant_event = (
                    "Smart Universal Email Assistant" in desc
                    or "Scheduled by" in desc
                    or "[AI Assistant]" in summary
                    or ev.get("extendedProperties", {}).get("private", {}).get("source") == "EmailAutomater"
                )
                ev["_is_assistant_created"] = is_assistant_event

                if include_all or is_assistant_event:
                    all_events.append(ev)
        except Exception as e:
            emit_user_log("ERROR", f"Could not fetch calendar events for {acc_email}: {str(e)}")

    all_events.sort(key=lambda x: x.get("start", {}).get("dateTime") or x.get("start", {}).get("date") or "")
    return {"events": all_events[:max_events]}


@app.get("/api/logs/stream")
async def stream_logs(request: Request):
    """Real-time SSE logger streaming plain-English logs directly to UI."""
    async def log_generator():
        log_path = Path(LOG_FILE)
        if not log_path.exists():
            log_path.touch()

        yield "data: [Connected] Assistant log monitor is live.\n\n"

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in lines[-30:]:
                    cleaned = line.strip()
                    if cleaned:
                        yield f"data: {cleaned}\n\n"
        except Exception:
            pass

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                f.seek(0, os.SEEK_END)
                while not await request.is_disconnected():
                    line = f.readline()
                    if line:
                        yield f"data: {line.strip()}\n\n"
                    else:
                        await asyncio.sleep(0.5)
        except (asyncio.CancelledError, GeneratorExit):
            pass

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=False), name="frontend")