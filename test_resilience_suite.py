import sys
import json
import sqlite3
import requests
from pathlib import Path
from datetime import datetime

# Anchor project root
BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

import config
from backend.models.event_schemas import CalendarEvent, UniversalEmailAnalysis
from backend.services.ai_service import _sanitize_ai_dict, extract_events_from_email
from backend.services.calendar_service import _format_datetime_for_google
from backend.utils.state_tracker import (
    record_email_action,
    is_email_processed,
    get_db_connection,
    get_setting
)

class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

passed = 0
failed = 0
warned = 0

def report(name: str, status: str, details: str = ""):
    global passed, failed, warned
    if status == "PASS":
        passed += 1
        print(f" {Colors.GREEN}[PASS]{Colors.RESET} {name} {details}")
    elif status == "WARN":
        warned += 1
        print(f" {Colors.YELLOW}[WARN]{Colors.RESET} {name} - {details}")
    else:
        failed += 1
        print(f" {Colors.RED}[FAIL]{Colors.RESET} {name} - {details}")

print(f"\n{Colors.BOLD}{'='*70}\n COMPREHENSIVE MULTI-CONDITION RESILIENCE TEST SUITE\n{'='*70}{Colors.RESET}\n")

# -------------------------------------------------------------
# 1. FRONTEND AVAILABILITY & STATIC CLIENT AUDIT
# -------------------------------------------------------------
print(f"{Colors.CYAN}--- 1. Testing Frontend Static Server ---{Colors.RESET}")
frontend_urls = [
    (f"{config.FRONTEND_URL}/index.html", "index.html"),
    (f"{config.FRONTEND_URL}/login.html", "login.html")
]

for url, filename in frontend_urls:
    try:
        r = requests.get(url, timeout=3)
        if r.status_code == 200:
            content = r.text
            has_backend_url = "BACKEND_URL" in content or "8000" in content
            if has_backend_url:
                report(f"Frontend Static File ({filename})", "PASS", f"[HTTP 200, Dynamic URL found]")
            else:
                report(f"Frontend Static File ({filename})", "WARN", "Served 200, but BACKEND_URL definition was not detected")
        else:
            report(f"Frontend Static File ({filename})", "FAIL", f"Returned HTTP {r.status_code}")
    except requests.exceptions.ConnectionError:
        report(f"Frontend Static File ({filename})", "FAIL", f"Could not connect to {url}. Is 'python -m http.server 5500' running?")

# -------------------------------------------------------------
# 2. BACKEND API ENDPOINT STABILITY
# -------------------------------------------------------------
print(f"\n{Colors.CYAN}--- 2. Testing Backend API Routes ---{Colors.RESET}")
api_routes = [
    ("GET", "/api/user", 200),
    ("GET", "/api/accounts", 200),
    ("GET", "/api/stats", 200),
    ("GET", "/api/settings", 200),
    ("GET", "/api/inspect/status", 200),
    ("GET", "/api/emails?category_filter=ALL", 200),
    ("GET", "/favicon.ico", [200, 204]),
]

for method, path, expected_status in api_routes:
    target = f"{config.BACKEND_URL}{path}"
    try:
        r = requests.request(method, target, timeout=3)
        valid = r.status_code in expected_status if isinstance(expected_status, list) else r.status_code == expected_status
        if valid:
            report(f"API Endpoint {path}", "PASS", f"[HTTP {r.status_code}]")
        else:
            report(f"API Endpoint {path}", "FAIL", f"Expected {expected_status}, received {r.status_code}")
    except requests.exceptions.ConnectionError:
        report(f"API Endpoint {path}", "FAIL", f"Connection refused at {target}. Is Uvicorn running on 8000?")

# -------------------------------------------------------------
# 3. SCHEMA ROBUSTNESS & MALFORMED AI OUTPUT TESTS
# -------------------------------------------------------------
print(f"\n{Colors.CYAN}--- 3. Testing Schema Edge Cases (What if AI omits fields?) ---{Colors.RESET}")

# Condition A: Completely empty dictionary
try:
    empty_sanitized = _sanitize_ai_dict({}, subject="Null Subject", default_time="09:00")
    model = UniversalEmailAnalysis(**empty_sanitized)
    assert model.category is not None
    assert model.priority in ["High", "Normal", "Low"]
    report("Condition A: AI returns completely empty JSON", "PASS", "[Defaults generated, no crash]")
except Exception as e:
    report("Condition A: AI returns completely empty JSON", "FAIL", str(e))

# Condition B: Missing priority, summary, and action_description (The bug encountered earlier)
try:
    missing_fields_input = {
        "category": "Meeting & Event",
        "has_calendar_event": True,
        "events": [{
            "title": "Strategy Sync",
            "start": "2026-09-18T10:00:00"
        }]
    }
    sanitized = _sanitize_ai_dict(missing_fields_input, subject="Strategy Call", default_time="09:00")
    res = UniversalEmailAnalysis(**sanitized)
    assert res.summary is not None
    assert res.priority == "Normal"
    assert len(res.events) == 1
    report("Condition B: Missing 'priority' & 'summary'", "PASS", f"[Auto-filled summary: '{res.summary}']")
except Exception as e:
    report("Condition B: Missing 'priority' & 'summary'", "FAIL", str(e))

# Condition C: Malformed event timings (start without end, string dates, missing title)
try:
    bad_event = {
        "events": [{
            "start": "2026-10-05",  # Date only, no hours
            "location": "Room 402"
        }]
    }
    sanitized_event = _sanitize_ai_dict(bad_event, subject="Room Reservation", default_time="14:00")
    res_event = UniversalEmailAnalysis(**sanitized_event)
    ev = res_event.events[0]
    assert "14:00:00" in ev.start
    assert ev.end != ""
    report("Condition C: Ambiguous date-only event", "PASS", f"[Resolved start: {ev.start} -> end: {ev.end}]")
except Exception as e:
    report("Condition C: Ambiguous date-only event", "FAIL", str(e))

# Condition D: High-probability spam test
try:
    spam_input = {
        "spam_score": 0.95,
        "is_spam_or_scam": True,
        "events": [{"title": "Fake Winner Meeting", "start": "2026-09-19T12:00:00"}]
    }
    sanitized_spam = _sanitize_ai_dict(spam_input, subject="YOU WON BITCOIN", default_time="09:00")
    res_spam = UniversalEmailAnalysis(**sanitized_spam)
    if res_spam.category == "Spam / Scam" and len(res_spam.events) == 0:
        report("Condition D: Malicious Spam Injection", "PASS", "[Calendar creation blocked, events wiped]")
    else:
        report("Condition D: Malicious Spam Injection", "FAIL", f"Spam events were not cleared: {res_spam.events}")
except Exception as e:
    report("Condition D: Malicious Spam Injection", "FAIL", str(e))

# -------------------------------------------------------------
# 4. CALENDAR DATE PARSER TESTS
# -------------------------------------------------------------
print(f"\n{Colors.CYAN}--- 4. Testing Google Calendar Date Edge Cases ---{Colors.RESET}")

date_test_cases = [
    ("2026-09-18", "Whole-day date string", lambda r: "date" in r),
    ("2026-09-18T16:00:00", "Naive ISO without TZ", lambda r: "dateTime" in r and "timeZone" in r),
    ("2026-09-18T16:00:00Z", "UTC Zulu formatted ISO", lambda r: "dateTime" in r),
    ("NOT-A-REAL-DATE", "Invalid garbage date string", lambda r: "dateTime" in r)  # Tests graceful fallback
]

for date_str, desc, validator in date_test_cases:
    try:
        formatted = _format_datetime_for_google(date_str, timezone_str="Asia/Kolkata")
        if validator(formatted):
            report(f"Calendar Date: {desc}", "PASS", f"-> {formatted}")
        else:
            report(f"Calendar Date: {desc}", "FAIL", f"Malformed result: {formatted}")
    except Exception as e:
        report(f"Calendar Date: {desc}", "FAIL", f"Threw exception: {e}")

# -------------------------------------------------------------
# 5. DATABASE INTEGRITY, SPECIAL CHARACTERS & BOUNDS
# -------------------------------------------------------------
print(f"\n{Colors.CYAN}--- 5. Testing Database Boundary Limits & SQL Safety ---{Colors.RESET}")

dummy_id = f"test_resilience_{int(datetime.now().timestamp())}"
test_strings = [
    ("Unicode & Emojis", "Meeting with team 🚀📅 — urgent!"),
    ("Quotes & SQL injection strings", "Robert'); DROP TABLE email_actions;--"),
    ("Extremely long payload", "A" * 1500)
]

for label, payload in test_strings:
    sub_id = f"{dummy_id}_{label[:4]}"
    try:
        record_email_action(
            email_id=sub_id,
            account_email="test@domain.com",
            sender="tester@domain.com",
            subject=payload,
            category="Important Update",
            priority="Normal",
            summary=payload,
            action_taken="Diagnostic verification",
            status="PROCESSED"
        )
        assert is_email_processed(sub_id, account_email="test@domain.com") == True
        report(f"Database Boundary: {label}", "PASS", "[Safely stored and retrieved]")
    except Exception as e:
        report(f"Database Boundary: {label}", "FAIL", str(e))

# Clean up inserted diagnostic rows
try:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM email_actions WHERE email_id LIKE 'test_resilience_%'")
    conn.commit()
    conn.close()
    report("Database Cleanup", "PASS", "[Removed temporary diagnostic records]")
except Exception as e:
    report("Database Cleanup", "WARN", f"Could not clear temporary test rows: {e}")

# -------------------------------------------------------------
# FINAL VERDICT
# -------------------------------------------------------------
print(f"\n{Colors.BOLD}{'='*70}")
print(f" RESULTS: {Colors.GREEN}{passed} Passed{Colors.RESET} | {Colors.YELLOW}{warned} Warnings{Colors.RESET} | {Colors.RED}{failed} Failures{Colors.RESET}")
print(f"{'='*70}{Colors.RESET}\n")

if failed == 0:
    print(f"{Colors.GREEN}✅ SYSTEM FULLY OPERATIONAL. All backend and frontend interfaces handle errors gracefully.{Colors.RESET}\n")
else:
    print(f"{Colors.RED}❌ RESOLUTION REQUIRED: Review failed test cases above before deploying.{Colors.RESET}\n")
    sys.exit(1)