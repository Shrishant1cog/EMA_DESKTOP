import json
import logging
import re
from typing import Optional, List, Any, Dict
from datetime import datetime, timedelta
from groq import Groq
import config
from backend.models.event_schemas import UniversalEmailAnalysis, CalendarEvent

logger = logging.getLogger(__name__)


def _get_api_keys() -> List[str]:
    keys = getattr(config, "GROQ_API_KEYS", [])
    if not keys and getattr(config, "GROQ_API_KEY", None):
        keys = [config.GROQ_API_KEY]
    return [k.strip() for k in keys if k and k.strip()]


def _clean_json_output(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _sanitize_ai_dict(data: Any, subject: str, default_time: str, spam_threshold: float = 0.50) -> Dict[str, Any]:
    """Guarantees valid structure and purges events whenever spam threshold criteria are met."""
    if not isinstance(data, dict):
        data = {}

    data["spam_threshold"] = spam_threshold

    # 1. Parse and normalize spam score
    raw_spam = data.get("spam_score", 0.0)
    try:
        if isinstance(raw_spam, str):
            spam_score = float(raw_spam.replace("%", "").strip())
            if spam_score > 1.0:
                spam_score /= 100.0
        else:
            spam_score = float(raw_spam / 100.0) if raw_spam > 1.0 else float(raw_spam)
    except Exception:
        spam_score = 0.0

    is_spam = bool(
        data.get("is_spam_or_scam")
        or spam_score >= spam_threshold
        or data.get("category") == "Spam / Scam"
    )

    # 2. Strict Spam Guard: purge scheduling data and set low priority
    if is_spam:
        data["is_spam_or_scam"] = True
        data["spam_score"] = spam_score if spam_score > 0 else max(spam_threshold, 0.55)
        data["category"] = "Spam / Scam"
        data["priority"] = "Low"
        data["events"] = []
        data["has_calendar_event"] = False
        pct = int(data["spam_score"] * 100)
        data["action_description"] = f"Ignored (Detected as Spam/Scam - {pct}% probability)"
        data["summary"] = data.get("summary") or subject or "Spam email detected and filtered"
        return data

    # 3. Category Fallback for legitimate emails
    cat = data.get("category")
    if not cat:
        if data.get("events") or data.get("has_calendar_event"):
            cat = "Meeting & Event"
        else:
            cat = "General / Marketing"
    data["category"] = cat
    data["priority"] = data.get("priority") or "Normal"

    # 4. Summary & Action Fallbacks
    summ = data.get("summary") or data.get("reasoning") or data.get("action_description")
    data["summary"] = str(summ).strip() if summ else (subject if subject else "Inbox message")
    data["action_description"] = data.get("action_description") or "Processed email"

    # 5. Clean legitimate events
    raw_events = data.get("events", [])
    if isinstance(raw_events, dict):
        raw_events = [raw_events]
    elif not isinstance(raw_events, list):
        raw_events = []

    clean_events = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue

        ev_title = ev.get("title") or ev.get("summary") or ev.get("name") or subject or "Scheduled Meeting"
        ev["title"] = ev_title

        ev_start = ev.get("start") or ev.get("start_time") or ev.get("startTime") or ev.get("date") or ""
        if isinstance(ev_start, dict):
            ev_start = ev_start.get("dateTime") or ev_start.get("date") or ""
        
        # If date only (YYYY-MM-DD), attach configured default time
        if len(str(ev_start).strip()) == 10 and "-" in str(ev_start) and "T" not in str(ev_start):
            ev_start = f"{str(ev_start).strip()}T{default_time}:00"
        ev["start"] = str(ev_start).strip()

        ev_end = ev.get("end") or ev.get("end_time") or ev.get("endTime") or ""
        if isinstance(ev_end, dict):
            ev_end = ev_end.get("dateTime") or ev_end.get("date") or ""

        # Default end time to start + 1 hour if unspecified
        if ev["start"] and not ev_end:
            try:
                dt = datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
                ev["end"] = (dt + timedelta(hours=1)).isoformat()
            except Exception:
                ev["end"] = ev["start"]
        else:
            ev["end"] = str(ev_end).strip()

        clean_events.append(ev)

    data["events"] = clean_events
    data["has_calendar_event"] = bool(clean_events or data.get("has_calendar_event", False))
    return data


def extract_events_from_email(
    subject: str,
    sender: str,
    date_received: str,
    email_body: str,
    attachment_text: str = "",
    custom_prompt: str = "",
    default_time: str = "09:00",
    spam_threshold: float = 0.50
) -> UniversalEmailAnalysis:
    effective_date = (
        date_received.strip()
        if date_received and date_received.strip()
        else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    threshold_pct = int(spam_threshold * 100)

    system_prompt = f"""You are an executive AI scheduling assistant. Read incoming emails, extract meeting details, and output strictly JSON.
Reference Date: {effective_date} | Timezone: {config.USER_TIMEZONE} | Year: 2026

REQUIRED JSON FIELDS:
{{
  "category": "Meeting & Event" | "Task & Deadline" | "Important Update" | "General / Marketing" | "Spam / Scam",
  "priority": "High" | "Normal" | "Low",
  "summary": "1-sentence summary of the email.",
  "action_description": "Description of action (e.g., 'Scheduled 1 event to Calendar')",
  "has_calendar_event": true | false,
  "is_attachment_evidence": false,
  "is_spam_or_scam": false,
  "spam_score": 0.0 to 1.0,
  "events": [
    {{
      "title": "Meeting Title",
      "start": "2026-MM-DDTHH:MM:SS",
      "end": "2026-MM-DDTHH:MM:SS",
      "location": "Room or video URL",
      "description": "Details",
      "attendees": ["guest@domain.com"],
      "is_all_day": false
    }}
  ]
}}

TIMING RULES:
- Default start time when only a day is provided: {default_time}.
- Default duration: 1 hour if end time is unspecified.
- Output pure JSON only.

SECURITY & SPAM RULES:
- User Spam Threshold is: {threshold_pct}%.
- If spam_score >= {spam_threshold}: category MUST be 'Spam / Scam', priority MUST be 'Low', has_calendar_event MUST be false, and events MUST be []."""

    if custom_prompt and custom_prompt.strip():
        system_prompt += f"\n\nCustom Instructions:\n{custom_prompt.strip()}"

    user_content = f"Subject: {subject}\nFrom: {sender}\nReceived: {effective_date}\n\nBody:\n{email_body}\n\nAttachments Text:\n{attachment_text}"

    keys = _get_api_keys()
    if not keys:
        logger.error("No Groq API keys configured.")
        return _fallback_response(subject, spam_threshold)

    for index, current_key in enumerate(keys, start=1):
        masked_key = f"...{current_key[-6:]}" if len(current_key) > 6 else f"Key #{index}"

        # 1. API Call Phase
        try:
            client = Groq(api_key=current_key)
            completion = client.chat.completions.create(
                model=config.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            raw_json = _clean_json_output(completion.choices[0].message.content)
            parsed_data = json.loads(raw_json)
        except Exception as api_err:
            logger.warning(f"Groq API error on key #{index} ({masked_key}): {api_err}. Trying next key...")
            continue

        # 2. Schema Normalization & Validation Phase
        try:
            sanitized_data = _sanitize_ai_dict(parsed_data, subject, default_time, spam_threshold)
            return UniversalEmailAnalysis(**sanitized_data)
        except Exception as parse_err:
            logger.error(f"Error normalizing JSON from key #{index}: {parse_err}")
            return UniversalEmailAnalysis(
                category="Meeting & Event" if parsed_data.get("events") else "General / Marketing",
                priority="Normal",
                summary=subject or "Meeting notice",
                action_description="Processed email",
                has_calendar_event=bool(parsed_data.get("events")),
                events=[]
            )

    logger.error("All Groq keys failed due to API connectivity/quota errors.")
    return _fallback_response(subject, spam_threshold)


def _fallback_response(subject: str, spam_threshold: float = 0.50) -> UniversalEmailAnalysis:
    return UniversalEmailAnalysis(
        category="General / Marketing",
        priority="Normal",
        summary=subject if subject else "General correspondence",
        action_description="Processed email (AI fallback)",
        has_calendar_event=False,
        is_attachment_evidence=False,
        is_spam_or_scam=False,
        spam_score=0.0,
        events=[]
    )