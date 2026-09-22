import json
import logging
import re
from typing import Optional, List, Any, Dict
from datetime import datetime, timedelta
from dateutil import parser
from groq import Groq

import config
from backend.models.event_schemas import UniversalEmailAnalysis, CalendarEvent

logger = logging.getLogger("AIService")


# ----------------- Configuration & Key Ingestion ----------------- #

def _get_api_keys() -> List[str]:
    """Retrieves and cleans all available Groq API keys from configuration."""
    raw_keys = getattr(config, "GROQ_API_KEYS", [])
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]

    keys = [k.strip().strip("'\"`") for k in raw_keys if isinstance(k, str) and k.strip()]
    if not keys and getattr(config, "GROQ_API_KEY", ""):
        single_key = config.GROQ_API_KEY.strip().strip("'\"`")
        if single_key:
            keys = [single_key]

    return keys


def _get_candidate_models() -> List[str]:
    """Retrieves prioritized list of active Groq model candidates."""
    models = getattr(
        config,
        "GROQ_MODEL_CANDIDATES",
        [
            getattr(config, "GROQ_MODEL", "llama-3.3-70b-versatile"),
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "llama3-70b-8192",
            "llama3-8b-8192",
            "gemma2-9b-it",
        ],
    )
    # Deduplicate while preserving priority order
    seen = set()
    ordered_models = []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            ordered_models.append(m)
    return ordered_models


# ----------------- Output Sanitization & Parsing ----------------- #

def _clean_json_output(raw_text: str) -> str:
    """Strips Markdown backticks, trailing conversational text, and isolates the JSON payload."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    text = text.strip()
    # Extract the outermost JSON object boundary if surrounding commentary exists
    json_match = re.search(r"(\{.*\})", text, re.DOTALL)
    if json_match:
        return json_match.group(1).strip()
    return text


def _sanitize_ai_dict(
    data: Any, subject: str, default_time: str, spam_threshold: float = 0.50
) -> Dict[str, Any]:
    """Guarantees strict schema integrity, ISO timestamp conformance, and enforces spam sanitization."""
    if not isinstance(data, dict):
        data = {}

    data["spam_threshold"] = spam_threshold

    # 1. Normalize spam score
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

    # 2. Enforce Spam Guard: Purge calendar entries if flagged
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

    # 3. Category Fallback
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

    # 5. Clean, format, and validate events
    raw_events = data.get("events", [])
    if isinstance(raw_events, dict):
        raw_events = [raw_events]
    elif not isinstance(raw_events, list):
        raw_events = []

    # Clean default time format (HH:MM)
    time_parts = [p.strip() for p in default_time.split(":") if p.strip()]
    formatted_default_time = f"{time_parts[0].zfill(2)}:{time_parts[1].zfill(2)}" if len(time_parts) >= 2 else "09:00"

    clean_events = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue

        ev_title = (
            ev.get("title")
            or ev.get("summary")
            or ev.get("name")
            or subject
            or "Scheduled Meeting"
        )
        ev["title"] = str(ev_title).strip()

        ev_start = ev.get("start") or ev.get("start_time") or ev.get("startTime") or ev.get("date") or ""
        if isinstance(ev_start, dict):
            ev_start = ev_start.get("dateTime") or ev_start.get("date") or ""

        clean_start_str = str(ev_start).strip()
        # If date only (YYYY-MM-DD), attach configured default time
        if len(clean_start_str) == 10 and "-" in clean_start_str and "T" not in clean_start_str:
            clean_start_str = f"{clean_start_str}T{formatted_default_time}:00"
        ev["start"] = clean_start_str

        ev_end = ev.get("end") or ev.get("end_time") or ev.get("endTime") or ""
        if isinstance(ev_end, dict):
            ev_end = ev_end.get("dateTime") or ev_end.get("date") or ""

        clean_end_str = str(ev_end).strip()
        # Default end time to start + 1 hour if unspecified
        if ev["start"] and not clean_end_str:
            try:
                dt = parser.isoparse(ev["start"].replace("Z", "+00:00"))
                ev["end"] = (dt + timedelta(hours=1)).isoformat()
            except Exception:
                ev["end"] = ev["start"]
        else:
            if len(clean_end_str) == 10 and "-" in clean_end_str and "T" not in clean_end_str:
                clean_end_str = f"{clean_end_str}T{formatted_default_time}:00"
            ev["end"] = clean_end_str

        # Harmonize attendee emails
        guests = ev.get("guests") or ev.get("attendees") or []
        if isinstance(guests, str):
            guests = [g.strip() for g in guests.split(",") if "@" in g]
        elif isinstance(guests, list):
            guests = [
                g.get("email", "").strip() if isinstance(g, dict) else str(g).strip()
                for g in guests
                if "@" in (g.get("email", "") if isinstance(g, dict) else str(g))
            ]
        ev["guests"] = guests
        ev["attendees"] = guests

        # Harmonize all-day flags
        all_day_flag = bool(ev.get("is_all_day") or ev.get("all_day", False))
        ev["all_day"] = all_day_flag
        ev["is_all_day"] = all_day_flag

        clean_events.append(ev)

    data["events"] = clean_events
    data["has_calendar_event"] = bool(clean_events or data.get("has_calendar_event", False))
    return data


# ----------------- Core AI Extraction Pipeline ----------------- #

def call_groq_with_rotation(
    messages: List[Dict[str, str]],
    temperature: float = 0.1,
    max_tokens: int = 2000,
) -> Dict[str, Any]:
    """
    Executes Groq chat completions with automatic failover across multiple API keys
    and candidate models on 400, 401, 404, or 429 errors.
    """
    keys = _get_api_keys()
    if not keys:
        raise ValueError("No Groq API keys configured. Set GROQ_API_KEYS in your .env file.")

    models = _get_candidate_models()
    last_error: Optional[Exception] = None

    for key_idx, current_key in enumerate(keys, start=1):
        masked_key = f"...{current_key[-6:]}" if len(current_key) > 6 else f"Key #{key_idx}"

        for model_name in models:
            try:
                client = Groq(api_key=current_key, timeout=25.0)
                completion = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                )
                raw_json = _clean_json_output(completion.choices[0].message.content)
                return json.loads(raw_json)

            except Exception as err:
                last_error = err
                err_str = str(err).lower()

                # Key-level rate limit or auth errors -> rotate to the next key
                if "429" in err_str or "rate_limit" in err_str:
                    logger.warning(f"Groq key #{key_idx} ({masked_key}) rate-limited (429). Rotating key...")
                    break
                if "401" in err_str or "invalid_api_key" in err_str:
                    logger.warning(f"Groq key #{key_idx} ({masked_key}) authentication failed (401). Rotating key...")
                    break

                # Model-level deprecation or missing access -> try next model in candidate list
                if "404" in err_str or "model_decommissioned" in err_str or "not_found" in err_str:
                    logger.warning(f"Model '{model_name}' unavailable on key #{key_idx}. Trying fallback model...")
                    continue

                if "context_length_exceeded" in err_str:
                    raise err

    if last_error:
        raise last_error
    raise RuntimeError("All Groq API keys and candidate models exhausted.")


def extract_events_from_email(
    subject: str,
    sender: str,
    date_received: str,
    email_body: str,
    attachment_text: str = "",
    custom_prompt: str = "",
    default_time: str = "09:00",
    spam_threshold: float = 0.50,
) -> UniversalEmailAnalysis:
    """
    Extracts events, assesses spam scores, and normalizes calendar metadata from email content.
    """
    effective_date = (
        date_received.strip()
        if date_received and date_received.strip()
        else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    threshold_pct = int(spam_threshold * 100)
    current_year = effective_date[:4] if len(effective_date) >= 4 else "2026"
    tz_name = getattr(config, "USER_TIMEZONE", "Asia/Kolkata")

    system_prompt = f"""You are an executive AI scheduling assistant. Analyze incoming emails, extract scheduled events, and output strictly JSON.
Reference Received Date: {effective_date} | Local Timezone: {tz_name} | Reference Year: {current_year}

REQUIRED JSON SCHEMA:
{{
  "category": "Meeting & Event" | "Task & Deadline" | "Important Update" | "General / Marketing" | "Spam / Scam",
  "priority": "High" | "Normal" | "Low",
  "summary": "1-sentence summary of the email.",
  "action_description": "Clear action taken (e.g., 'Scheduled 1 event to Calendar')",
  "has_calendar_event": true | false,
  "is_attachment_evidence": false,
  "is_spam_or_scam": false,
  "spam_score": 0.0 to 1.0,
  "events": [
    {{
      "title": "Concise Event Title",
      "start": "YYYY-MM-DDTHH:MM:SS",
      "end": "YYYY-MM-DDTHH:MM:SS",
      "location": "Location or meeting URL",
      "description": "Short summary description",
      "attendees": ["email@example.com"],
      "is_all_day": false
    }}
  ]
}}

TIMING & SCHEDULING RULES:
- If only a date is provided without a time, assign default start time: {default_time}.
- If start time is specified but end time is omitted, default duration is exactly 1 hour.
- Interpret relative days ('tomorrow', 'this Friday') anchored strictly to Reference Received Date ({effective_date}).
- Output valid JSON only.

SECURITY & SPAM PROTOCOL:
- User Spam Sensitivity Threshold: {threshold_pct}%.
- If spam_score >= {spam_threshold}:
    category MUST be 'Spam / Scam'
    priority MUST be 'Low'
    has_calendar_event MUST be false
    events MUST be []"""

    if custom_prompt and custom_prompt.strip():
        system_prompt += f"\n\nCustom User Rules:\n{custom_prompt.strip()}"

    # Protect against context window overflow
    safe_body = (email_body or "")[:18000]
    safe_attachments = (attachment_text or "")[:10000]

    user_content = (
        f"Subject: {subject}\n"
        f"From: {sender}\n"
        f"Date: {effective_date}\n\n"
        f"Email Body:\n{safe_body}\n\n"
        f"Extracted Attachment Text:\n{safe_attachments}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    try:
        parsed_data = call_groq_with_rotation(messages=messages, temperature=0.1)
        sanitized_data = _sanitize_ai_dict(
            parsed_data,
            subject=subject,
            default_time=default_time,
            spam_threshold=spam_threshold,
        )
        return UniversalEmailAnalysis(**sanitized_data)

    except Exception as exc:
        logger.error(f"AI extraction failed across all keys and models: {exc}. Activating fallback parser.")
        return _fallback_response(subject, spam_threshold)


def _fallback_response(subject: str, spam_threshold: float = 0.50) -> UniversalEmailAnalysis:
    """Safe, schema-compliant fallback when AI services are offline."""
    return UniversalEmailAnalysis(
        category="General / Marketing",
        priority="Normal",
        summary=subject if subject else "General correspondence",
        action_description="Processed email (AI service offline)",
        has_calendar_event=False,
        is_attachment_evidence=False,
        is_spam_or_scam=False,
        spam_score=0.0,
        events=[],
    )