import os
import sys
import time
import json
import sqlite3
from io import BytesIO
from pathlib import Path
import requests
from PIL import Image, ImageDraw

# ----------------------------------------------------------------------
# Terminal Colors & Counters
# ----------------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

PASS_COUNT = 0
FAIL_COUNT = 0

BASE_URL = "http://127.0.0.1:8000"
ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "assistant_v2.db"


def log_test(module: str, test_name: str, passed: bool, detail: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if passed:
        PASS_COUNT += 1
        print(f"  {GREEN}✔ [PASS]{RESET} {BOLD}{module:<12}{RESET} ➔ {test_name}")
        if detail:
            print(f"               {CYAN}{detail}{RESET}")
    else:
        FAIL_COUNT += 1
        print(f"  {RED}✖ [FAIL]{RESET} {BOLD}{module:<12}{RESET} ➔ {test_name}")
        if detail:
            print(f"               {RED}Error: {detail}{RESET}")


# ----------------------------------------------------------------------
# 1. Frontend Web & Asset Delivery Tests
# ----------------------------------------------------------------------
def test_frontend(session: requests.Session):
    print(f"\n{BOLD}=== [TEST 1/5] Frontend GUI & Static Assets ==={RESET}")

    # Root redirect
    try:
        r = session.get(f"{BASE_URL}/", allow_redirects=False, timeout=5)
        passed = r.status_code in (301, 302, 303, 307) and "/index.html" in r.headers.get("location", "")
        log_test("FRONTEND", "Root Navigation (/ -> /index.html)", passed)
    except Exception as e:
        log_test("FRONTEND", "Root Navigation", False, str(e))

    # Index HTML & Session script
    try:
        r = session.get(f"{BASE_URL}/index.html", timeout=5)
        has_content = r.status_code == 200 and "EMA" in r.text and "assistant_session_id" in r.text
        log_test("FRONTEND", "Dashboard Template (/index.html)", has_content)
    except Exception as e:
        log_test("FRONTEND", "Dashboard Template", False, str(e))

    # Login HTML
    try:
        r = session.get(f"{BASE_URL}/login.html", timeout=5)
        has_login = r.status_code == 200 and "Google" in r.text
        log_test("FRONTEND", "Login Portal (/login.html)", has_login)
    except Exception as e:
        log_test("FRONTEND", "Login Portal", False, str(e))

    # Branding Assets
    for asset in ["favicon.ico", "logo.png"]:
        try:
            r = session.get(f"{BASE_URL}/{asset}", timeout=5)
            log_test("FRONTEND", f"Asset Delivery (/{asset})", r.status_code in (200, 204))
        except Exception as e:
            log_test("FRONTEND", f"Asset Delivery (/{asset})", False, str(e))


# ----------------------------------------------------------------------
# 2. Live Cloud AI & Spam Filter Ingestion Tests
# ----------------------------------------------------------------------
def test_ai_pipeline():
    print(f"\n{BOLD}=== [TEST 2/5] Groq AI Ingestion & Classification Pipeline ==={RESET}")

    from backend.services.ai_service import extract_events_from_email

    # Test Case A: Valid Meeting Email
    meeting_subject = "Urgent: Sprint Planning Sync Tomorrow at 11 AM"
    meeting_sender = "techlead@company.com"
    meeting_body = (
        "Hi team, let us meet tomorrow at 11:00 AM IST for 45 minutes on Google Meet "
        "to finalize the production sprint roadmap. Meeting link: https://meet.google.com/abc-defg-hij"
    )

    try:
        analysis = extract_events_from_email(
            subject=meeting_subject,
            sender=meeting_sender,
            date_received="2026-09-21T08:00:00Z",
            email_body=meeting_body,
            attachment_text="",
            custom_prompt="",
            default_time="09:00",
            spam_threshold=0.50
        )
        has_event = analysis.has_calendar_event and len(analysis.events) > 0
        event_title = analysis.events[0].title if has_event else "None"
        log_test("AI_PIPELINE", "Meeting Extraction", has_event, f"Detected Event: '{event_title}'")
    except Exception as e:
        log_test("AI_PIPELINE", "Meeting Extraction", False, str(e))

    # Test Case B: Promotional Spam Email
    spam_subject = "CONGRATULATIONS! You won $5,000,000 lottery cash prize claim now!"
    spam_sender = "claims-center@freeprizerewards.xyz"
    spam_body = "Click here immediately to claim your wire transfer reward before your access code expires."

    try:
        spam_analysis = extract_events_from_email(
            subject=spam_subject,
            sender=spam_sender,
            date_received="2026-09-21T08:00:00Z",
            email_body=spam_body,
            attachment_text="",
            custom_prompt="",
            default_time="09:00",
            spam_threshold=0.50
        )
        is_filtered = spam_analysis.category == "Spam / Scam" or getattr(spam_analysis, "is_spam_or_scam", False) or spam_analysis.spam_score >= 0.50
        log_test("AI_PIPELINE", "Spam & Phishing Shield", is_filtered, f"Score: {getattr(spam_analysis, 'spam_score', 1.0)*100:.0f}% (Category: {spam_analysis.category})")
    except Exception as e:
        log_test("AI_PIPELINE", "Spam & Phishing Shield", False, str(e))


# ----------------------------------------------------------------------
# 3. Live Cloud OCR Engine Test
# ----------------------------------------------------------------------
def test_ocr_cloud():
    print(f"\n{BOLD}=== [TEST 3/5] Zero-Load Cloud Vision OCR Engine ==={RESET}")

    from extractors.ocr_extractor import extract_text_from_image

    img = Image.new("RGB", (360, 90), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((20, 35), "EMA SYSTEM TEST OCR 2026", fill=(0, 0, 0))

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    test_bytes = buf.getvalue()

    try:
        transcribed = extract_text_from_image(test_bytes)
        verified = bool(transcribed) and any(w in transcribed.upper() for w in ["EMA", "SYSTEM", "TEST", "OCR", "2026"])
        log_test("CLOUD_OCR", "Zero-Load Image Transcription", verified, f"Output: '{transcribed.strip()}'")
    except Exception as e:
        log_test("CLOUD_OCR", "Zero-Load Image Transcription", False, str(e))


# ----------------------------------------------------------------------
# 4. Storage & Collation Isolation
# ----------------------------------------------------------------------
def test_storage():
    print(f"\n{BOLD}=== [TEST 4/5] Multi-Account SQLite Storage & Isolation ==={RESET}")

    from backend.utils.state_tracker import (
        upsert_google_account,
        get_google_account,
        delete_google_account,
        record_email_action,
        is_email_processed
    )

    test_acc = "demo_tester@example.com"
    try:
        # Upsert account
        upsert_google_account(test_acc, "Tester", "", '{"token": "xyz"}', session_id="test_sess", is_monitored=1)
        acc_data = get_google_account(test_acc.upper()) # Tests NOCASE collation
        log_test("DATABASE", "Case-Insensitive Account Lookup", acc_data is not None and acc_data["email"] == test_acc)

        # Record email action
        record_email_action("msg_999", test_acc, "boss@company.com", "Project Status", "Meeting & Event", "HIGH", "Meeting summary", "Scheduled to Calendar", "ACTIONED")
        processed = is_email_processed("msg_999", test_acc)
        log_test("DATABASE", "Email Deduplication & Tracking", processed)

        # Cleanup
        delete_google_account(test_acc)
    except Exception as e:
        log_test("DATABASE", "Storage Transactions", False, str(e))


# ----------------------------------------------------------------------
# 5. Background Daemons, Controls & Telemetry Stream
# ----------------------------------------------------------------------
def test_daemons_and_stream(session: requests.Session):
    print(f"\n{BOLD}=== [TEST 5/5] Background Workers & Live SSE Telemetry ==={RESET}")

    # Worker controls
    try:
        r_start = session.post(f"{BASE_URL}/api/worker/start", timeout=5)
        log_test("DAEMONS", "Worker Start Signal", r_start.status_code == 200)

        r_stop = session.post(f"{BASE_URL}/api/worker/stop", timeout=5)
        log_test("DAEMONS", "Worker Stop Signal", r_stop.status_code == 200)
    except Exception as e:
        log_test("DAEMONS", "Worker Signals", False, str(e))

    # Real-Time SSE stream
    try:
        r_sse = session.get(f"{BASE_URL}/api/logs/stream", stream=True, timeout=5)
        is_sse = r_sse.status_code == 200 and "text/event-stream" in r_sse.headers.get("content-type", "")
        log_test("TELEMETRY", "Live SSE Log Broadcast", is_sse)
    except Exception as e:
        log_test("TELEMETRY", "Live SSE Log Broadcast", False, str(e))


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------
def main():
    print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
    print(f"{BOLD}{CYAN}         EMA ASSISTANT — COMPLETE SYSTEM & PIPELINE TEST              {RESET}")
    print(f"{BOLD}{CYAN}======================================================================{RESET}")

    try:
        requests.get(f"{BASE_URL}/api/user", timeout=2)
    except Exception:
        print(f"\n{RED}{BOLD}[!] Backend server is offline on {BASE_URL}.{RESET}")
        print(f"{YELLOW}Please launch your server in another terminal first:{RESET}")
        print(f"   python desktop_app.py\n")
        sys.exit(1)

    session = requests.Session()
    test_frontend(session)
    test_ai_pipeline()
    test_ocr_cloud()
    test_storage()
    test_daemons_and_stream(session)

    print(f"\n{BOLD}======================================================================{RESET}")
    print(f"  {GREEN}Passed Checks: {PASS_COUNT}{RESET}")
    print(f"  {RED}Failed Checks: {FAIL_COUNT}{RESET}")
    print(f"{BOLD}======================================================================{RESET}")

    if FAIL_COUNT == 0:
        print(f"\n{GREEN}{BOLD}[✔] ALL FRONTEND ASSETS, AI EXTRACTION, OCR, AND BACKEND ROUTING PASSED 100%!{RESET}\n")
    else:
        print(f"\n{RED}{BOLD}[✖] {FAIL_COUNT} test(s) failed. Check logs above.{RESET}\n")


if __name__ == "__main__":
    main()