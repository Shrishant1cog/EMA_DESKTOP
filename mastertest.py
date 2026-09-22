import os
import sys
import time
import json
import sqlite3
import base64
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta
import requests

from backend.utils.state_tracker import init_db

# ----------------------------------------------------------------------
# Terminal Visual Formatting
# ----------------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
RESET = "\033[0m"

PASS_COUNT = 0
FAIL_COUNT = 0
WARN_COUNT = 0

BASE_URL = "http://127.0.0.1:8000"
ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "assistant_v2.db"
ENV_PATH = ROOT_DIR / ".env"
FRONTEND_DIR = ROOT_DIR / "frontend"


def log_test(category: str, test_name: str, passed: bool, detail: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if passed:
        PASS_COUNT += 1
        print(f"  {GREEN}✔ [PASS]{RESET} {BOLD}{category:<10}{RESET} ➔ {test_name}")
        if detail:
            print(f"             {CYAN}{detail}{RESET}")
    else:
        FAIL_COUNT += 1
        print(f"  {RED}✖ [FAIL]{RESET} {BOLD}{category:<10}{RESET} ➔ {test_name}")
        if detail:
            print(f"             {RED}Error: {detail}{RESET}")


def log_warn(category: str, test_name: str, message: str):
    global WARN_COUNT
    WARN_COUNT += 1
    print(f"  {YELLOW}▲ [WARN]{RESET} {BOLD}{category:<10}{RESET} ➔ {test_name}")
    print(f"             {YELLOW}{message}{RESET}")


# ----------------------------------------------------------------------
# 1. Environment & API Secrets Live Validation
# ----------------------------------------------------------------------
def audit_environment_and_keys():
    print(f"\n{BOLD}{MAGENTA}=== [STAGE 1/8] Environment Configuration & Live Secrets ==={RESET}")

    if not ENV_PATH.exists():
        log_test("ENV", ".env file present", False, "Missing .env in project root")
        return

    log_test("ENV", ".env file present", True, str(ENV_PATH))
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        env_content = f.read()

    env_vars = {}
    for line in env_content.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip().strip("'\"`")

    # 1. Google Client IDs
    has_gid = bool(env_vars.get("GOOGLE_CLIENT_ID"))
    has_gsec = bool(env_vars.get("GOOGLE_CLIENT_SECRET"))
    log_test("ENV", "GOOGLE_CLIENT_ID syntax", has_gid, f"{env_vars.get('GOOGLE_CLIENT_ID', '')[:16]}...")
    log_test("ENV", "GOOGLE_CLIENT_SECRET syntax", has_gsec, "Secret is populated")

    # 2. Groq Multi-Key Auditing
    raw_keys = env_vars.get("GROQ_API_KEYS") or env_vars.get("GROQ_API_KEY") or ""
    groq_keys = [k.strip().strip("'\"`") for k in raw_keys.split(",") if k.strip().strip("'\"`")]
    log_test("ENV", "Groq Key(s) Ingestion", len(groq_keys) > 0, f"Detected {len(groq_keys)} key(s)")

    # 3. Dynamic Live Model Handshake
    if groq_keys:
        try:
            from groq import Groq
            handshake_success = False
            active_model_name = ""
            response_text = ""

            for key in groq_keys:
                try:
                    client = Groq(api_key=key)
                    # Query active models dynamically
                    models_res = client.models.list()
                    available_ids = [
                        m.id for m in models_res.data
                        if not m.id.startswith("whisper") and "guard" not in m.id
                    ]
                    if not available_ids:
                        continue

                    # Prioritize standard chat models
                    preferred = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "llama3-70b-8192", "gemma2-9b-it"]
                    chosen = next((p for p in preferred if p in available_ids), available_ids[0])

                    ping_res = client.chat.completions.create(
                        model=chosen,
                        messages=[{"role": "user", "content": "ping"}],
                        max_tokens=2,
                    )
                    response_text = ping_res.choices[0].message.content.strip()
                    active_model_name = chosen
                    handshake_success = True
                    break
                except Exception:
                    continue

            log_test("GROQ", "Live Cloud LLM Handshake", handshake_success, f"Connected via '{active_model_name}' ({response_text})")
        except Exception as e:
            log_test("GROQ", "Live Cloud LLM Handshake", False, str(e))


# ----------------------------------------------------------------------
# 2. SQLite Concurrency, Schema & NOCASE Collation
# ----------------------------------------------------------------------
def audit_sqlite_internals():
    print(f"\n{BOLD}{MAGENTA}=== [STAGE 2/8] SQLite Storage Engine & Collation Integrity ==={RESET}")

    init_db(force=True)

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        cur = conn.cursor()

        cur.execute("PRAGMA journal_mode;")
        j_mode = cur.fetchone()[0].upper()
        log_test("SQLITE", "WAL Journal Mode Active", j_mode == "WAL", f"Current mode: {j_mode}")

        cur.execute("PRAGMA synchronous;")
        sync_mode = cur.fetchone()[0]
        log_test("SQLITE", "PRAGMA Synchronous Configuration", sync_mode in (1, 2), f"Sync Level: {sync_mode}")

        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}
        for t in ["google_accounts", "email_actions", "system_settings"]:
            log_test("SQLITE", f"Table: {t}", t in tables)

        if "google_accounts" in tables:
            test_email_lower = "audit_stress_test@example.com"
            test_email_mixed = "AuDiT_StReSs_TeSt@ExAmPlE.CoM"

            cur.execute("""
                INSERT OR REPLACE INTO google_accounts (email, name, picture, token_data, is_monitored, created_at)
                VALUES (?, 'Stress Test', '', '{}', 0, datetime('now'))
            """, (test_email_mixed,))
            conn.commit()

            cur.execute("SELECT email FROM google_accounts WHERE email = ?", (test_email_lower,))
            row = cur.fetchone()
            matched = row is not None and row[0].lower() == test_email_lower.lower()
            log_test("SQLITE", "NOCASE Collation Integrity", matched, f"Queried lowercase, matched: {row[0] if row else 'None'}")

            cur.execute("DELETE FROM google_accounts WHERE email = ?", (test_email_lower,))
            conn.commit()

        conn.close()
    except Exception as e:
        log_test("SQLITE", "Database Engine Audit", False, str(e))


# ----------------------------------------------------------------------
# 3. Live Cloud Vision OCR Integration
# ----------------------------------------------------------------------
def audit_cloud_ocr_pipeline():
    print(f"\n{BOLD}{MAGENTA}=== [STAGE 3/8] Cloud OCR & Vision API Unit Execution ==={RESET}")

    try:
        from extractors.ocr_extractor import extract_text_from_image
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (320, 80), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.text((15, 30), "PROJECT AUDIT VERIFIED 2026", fill=(0, 0, 0))

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        test_bytes = buf.getvalue()

        ocr_result = extract_text_from_image(test_bytes)
        verified = bool(ocr_result) and ("PROJECT" in ocr_result.upper() or "AUDIT" in ocr_result.upper() or "2026" in ocr_result)
        log_test("VISION", "Cloud OCR Pipeline", verified, f"Transcribed Output: '{ocr_result.strip()}'")
    except ImportError as ie:
        log_warn("VISION", "Pillow/Extractors missing", f"Could not run image test: {ie}")
    except Exception as e:
        log_test("VISION", "Cloud OCR Pipeline", False, str(e))


# ----------------------------------------------------------------------
# 4. Frontend DOM & Static Asset Integrity
# ----------------------------------------------------------------------
def audit_frontend_dom(session: requests.Session):
    print(f"\n{BOLD}{MAGENTA}=== [STAGE 4/8] Frontend DOM Architecture & Assets ==={RESET}")

    r_fav = session.get(f"{BASE_URL}/favicon.ico", timeout=5)
    log_test("STATIC", "Favicon Delivery (/favicon.ico)", r_fav.status_code in (200, 204), f"Status: {r_fav.status_code}")

    r_logo = session.get(f"{BASE_URL}/logo.png", timeout=5)
    log_test("STATIC", "Brand Asset Delivery (/logo.png)", r_logo.status_code in (200, 204), f"Status: {r_logo.status_code}")

    r_index = session.get(f"{BASE_URL}/index.html", timeout=5)
    if r_index.status_code != 200:
        log_test("DOM", "Index Document Retrieval", False, f"HTTP {r_index.status_code}")
        return

    html = r_index.text
    log_test("DOM", "Index HTTP 200 OK", True)

    has_bridge = "assistant_session_id" in html and "X-Session-ID" in html
    log_test("DOM", "Session Bridge Script Injection", has_bridge)

    # Log Terminal DOM Presence
    has_terminal = any(term in html.lower() for term in [
        'terminal', 'log', 'eventsource', '/api/logs/stream'
    ])
    log_test("DOM", "Container: Live Terminal Component", has_terminal)


# ----------------------------------------------------------------------
# 5. REST API Endpoints & State Services
# ----------------------------------------------------------------------
def audit_rest_api(session: requests.Session):
    print(f"\n{BOLD}{MAGENTA}=== [STAGE 5/8] REST API Layer Contracts ==={RESET}")

    endpoints = [
        ("GET", "/api/user", 200, ["authenticated"]),
        ("GET", "/api/accounts", 200, ["accounts"]),
        ("GET", "/api/settings", 200, ["spam_threshold", "only_remind_with_files"]),
        ("GET", "/api/stats", 200, ["summary", "worker"]),
        ("GET", "/api/emails", 200, ["emails"]),
        ("GET", "/api/calendar/upcoming", 200, ["events"]),
        ("GET", "/api/inspect/status", 200, ["is_running", "target_count"]),
    ]

    for method, path, expected_code, expected_keys in endpoints:
        try:
            res = session.request(method, f"{BASE_URL}{path}", timeout=5)
            status_ok = res.status_code == expected_code
            data = res.json() if status_ok else {}
            keys_ok = all(k in data for k in expected_keys)
            log_test("API", f"{method} {path}", status_ok and keys_ok, f"Keys: {list(data.keys())[:4]}")
        except Exception as e:
            log_test("API", f"{method} {path}", False, str(e))


# ----------------------------------------------------------------------
# 6. Settings Mutation & Persistence Round-Trip
# ----------------------------------------------------------------------
def audit_settings_lifecycle(session: requests.Session):
    print(f"\n{BOLD}{MAGENTA}=== [STAGE 6/8] Settings Mutation & Persistence Round-Trip ==={RESET}")

    try:
        get_res = session.get(f"{BASE_URL}/api/settings", timeout=5)
        initial_val = get_res.json().get("spam_threshold", 50)

        test_val = 67 if initial_val != 67 else 72
        post_res = session.post(f"{BASE_URL}/api/settings", json={"spam_threshold": test_val}, timeout=5)

        if post_res.status_code == 400 and "No active accounts" in post_res.text:
            log_test("SETTINGS", "Mutation Account Guard", True, "Safely rejected mutation when zero accounts authenticated.")
            return

        verify_res = session.get(f"{BASE_URL}/api/settings", timeout=5)
        persisted_val = verify_res.json().get("spam_threshold")
        persisted = persisted_val == test_val
        log_test("SETTINGS", "State Mutation Round-Trip", persisted, f"Target: {test_val}%, Persisted: {persisted_val}%")

        session.post(f"{BASE_URL}/api/settings", json={"spam_threshold": initial_val}, timeout=5)
    except Exception as e:
        log_test("SETTINGS", "Settings Lifecycle", False, str(e))


# ----------------------------------------------------------------------
# 7. Background Worker & Inspect Daemon Lifecycle
# ----------------------------------------------------------------------
def audit_daemon_threads(session: requests.Session):
    print(f"\n{BOLD}{MAGENTA}=== [STAGE 7/8] Background Workers & Inspect Lifecycle ==={RESET}")

    try:
        r_start = session.post(f"{BASE_URL}/api/worker/start", timeout=5)
        log_test("DAEMON", "Worker Start Command", r_start.json().get("status") in ("started", "already_running"))

        time.sleep(0.4)
        r_stats = session.get(f"{BASE_URL}/api/stats", timeout=5)
        is_live = r_stats.json().get("worker", {}).get("is_running", False)
        log_test("DAEMON", "Worker State Concurrency", is_live, f"Status: {r_stats.json().get('worker', {}).get('status')}")

        r_stop = session.post(f"{BASE_URL}/api/worker/stop", timeout=5)
        log_test("DAEMON", "Worker Stop Command", r_stop.json().get("status") in ("stopping", "not_running"))
    except Exception as e:
        log_test("DAEMON", "Worker Thread Lifecycle", False, str(e))

    try:
        r_insp_stop = session.post(f"{BASE_URL}/api/inspect/stop", timeout=5)
        log_test("DAEMON", "Inspect Stop Guard", r_insp_stop.status_code == 200)
    except Exception as e:
        log_test("DAEMON", "Inspect Stop Guard", False, str(e))


# ----------------------------------------------------------------------
# 8. OAuth2 Flow, Security Callbacks & SSE Stream
# ----------------------------------------------------------------------
def audit_security_and_streaming(session: requests.Session):
    print(f"\n{BOLD}{MAGENTA}=== [STAGE 8/8] OAuth Security, Handshakes & Live Telemetry ==={RESET}")

    try:
        r_login = session.get(f"{BASE_URL}/auth/login?browser=1", allow_redirects=False, timeout=5)
        loc = r_login.headers.get("location", "")
        is_oauth = r_login.status_code in (302, 307) and "accounts.google.com" in loc and "response_type=code" in loc
        log_test("OAUTH", "Google Consent Redirect Construction", is_oauth, f"Target: {loc[:60]}...")
    except Exception as e:
        log_test("OAUTH", "Google Consent Redirect Construction", False, str(e))

    try:
        r_bad = session.get(f"{BASE_URL}/auth/callback?error=access_denied", timeout=5)
        caught = r_bad.status_code == 400 and "Authentication Failed" in r_bad.text
        log_test("OAUTH", "Invalid Callback Defense (HTTP 400)", caught)
    except Exception as e:
        log_test("OAUTH", "Invalid Callback Defense", False, str(e))

    try:
        r_sse = session.get(f"{BASE_URL}/api/logs/stream", stream=True, timeout=5)
        valid_stream = r_sse.status_code == 200 and "text/event-stream" in r_sse.headers.get("content-type", "")

        first_messages = []
        for line in r_sse.iter_lines(decode_unicode=True):
            if line:
                first_messages.append(line)
                if len(first_messages) >= 2:
                    break

        has_data = len(first_messages) > 0 and any("data:" in m for m in first_messages)
        log_test("SSE", "Real-Time Telemetry Stream", valid_stream and has_data, f"Ingested: '{first_messages[0][:50]}...'")
    except Exception as e:
        log_test("SSE", "Real-Time Telemetry Stream", False, str(e))


# ----------------------------------------------------------------------
# Main Execution Runner
# ----------------------------------------------------------------------
def main():
    print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
    print(f"{BOLD}{CYAN}      EMA ASSISTANT — EXHAUSTIVE DEEP-SYSTEM DIAGNOSTIC AUDIT         {RESET}")
    print(f"{BOLD}{CYAN}======================================================================{RESET}")

    try:
        requests.get(f"{BASE_URL}/api/user", timeout=2)
    except Exception:
        print(f"\n{RED}{BOLD}[!] FATAL: Application backend is offline on {BASE_URL}.{RESET}")
        print(f"{YELLOW}Launch the app first in another terminal:{RESET}")
        print(f"  python desktop_app.py")
        print(f"Then re-run this audit.\n")
        sys.exit(1)

    session = requests.Session()

    start_time = time.time()
    audit_environment_and_keys()
    audit_sqlite_internals()
    audit_cloud_ocr_pipeline()
    audit_frontend_dom(session)
    audit_rest_api(session)
    audit_settings_lifecycle(session)
    audit_daemon_threads(session)
    audit_security_and_streaming(session)
    duration = time.time() - start_time

    print(f"\n{BOLD}{MAGENTA}======================================================================{RESET}")
    print(f"{BOLD}                        AUDIT SUMMARY MATRIX                          {RESET}")
    print(f"{BOLD}{MAGENTA}======================================================================{RESET}")
    print(f"  {GREEN}Total Passed Checks: {PASS_COUNT}{RESET}")
    print(f"  {RED}Total Failed Checks: {FAIL_COUNT}{RESET}")
    print(f"  {YELLOW}Active Warnings:     {WARN_COUNT}{RESET}")
    print(f"  Audit Execution Time: {duration:.2f} seconds")

    if FAIL_COUNT == 0:
        print(f"\n{GREEN}{BOLD}[✔] SYSTEM OPERATIONAL: ALL SUBSYSTEMS, APIS, SCHEMAS & THREADS PASS 100%!{RESET}\n")
    else:
        print(f"\n{RED}{BOLD}[✖] AUDIT FAILED WITH {FAIL_COUNT} ERROR(S). REVIEW TRACES ABOVE.{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()