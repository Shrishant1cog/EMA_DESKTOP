import sys
from pathlib import Path
from datetime import datetime

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

import config
from backend.utils.state_tracker import get_setting, list_all_google_accounts
from backend.services.ai_service import extract_events_from_email
from backend.services.google_auth import get_google_services_for_account
from backend.services.calendar_service import create_or_update_event

print("\n" + "="*60)
print(" 🚀 RUNNING INSTANT EMAIL & CALENDAR DIAGNOSTIC")
print("="*60)

# 1. Check Settings
only_files = get_setting("only_remind_with_files", "false").lower() == "true"
default_time = get_setting("default_timing", "09:00")
print(f"[*] Setting 'only_remind_with_files': {only_files}")
print(f"[*] Setting 'default_timing': {default_time}")

if only_files:
    print("⚠️ WARNING: 'Only remind when files are available' is ENABLED.")
    print("   Emails without attachments will NEVER create calendar reminders!")

# 2. Test Accounts
accounts = list_all_google_accounts()
if not accounts:
    print("❌ ERROR: No Google accounts found in the database. Sign in first!")
    sys.exit(1)

test_account = accounts[0]["email"]
print(f"[*] Testing with account: {test_account}")

# 3. Simulate Test Email to Groq
print("\n[*] Sending sample meeting email to AI...")
test_subject = "Urgent Project Review Meeting"
test_sender = "manager@company.com"
test_body = f"Hi Nikshith, let's meet tomorrow at 3:00 PM for the project review. Join link: https://meet.google.com/abc-defg-hij"

analysis = extract_events_from_email(
    subject=test_subject,
    sender=test_sender,
    date_received=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    email_body=test_body,
    attachment_text="",
    default_time=default_time
)

print("\n--- AI Extraction Output ---")
print(f"Category:           {analysis.category}")
print(f"Priority:           {analysis.priority}")
print(f"Has Calendar Event: {analysis.has_calendar_event}")
print(f"Spam Score:         {analysis.spam_score}")
print(f"Events Count:       {len(analysis.events)}")
print(f"Action Description: {analysis.action_description}")

if not analysis.has_calendar_event or not analysis.events:
    print("❌ FAILED AT AI: AI did not identify a calendar event in this email.")
    sys.exit(1)

# 4. Test Google Calendar Insertion
print("\n[*] Testing Google Calendar API insertion...")
try:
    _, calendar_service = get_google_services_for_account(test_account)
    event_data = analysis.events[0]
    print(f"[*] Attempting to schedule event: '{event_data.title}' at start: {event_data.start}")

    created = create_or_update_event(
        calendar_service=calendar_service,
        event_data=event_data,
        original_sender=test_sender,
        attachments=[]
    )
    print(f"\n✅ SUCCESS! Event created on Google Calendar.")
    print(f"Event Link: {created.get('htmlLink') if isinstance(created, dict) else created}")
except Exception as e:
    print(f"\n❌ FAILED AT GOOGLE CALENDAR INSERT: {e}")
    import traceback
    traceback.print_exc()

print("="*60 + "\n")