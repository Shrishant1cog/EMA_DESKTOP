import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dateutil import parser

import config
from backend.models.event_schemas import CalendarEvent

logger = logging.getLogger("CalendarService")


class CalendarResult(dict):
    """Dictionary subclass supporting both dot-attribute and dictionary key access."""

    def __getattr__(self, name):
        if name in self:
            return self[name]
        for k, v in self.items():
            if k.lower() == name.lower():
                return v
        return None

    def __getitem__(self, key):
        if key in self:
            return super().__getitem__(key)
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return None


# ----------------- Timezone & Datetime Utilities ----------------- #

def _get_safe_zoneinfo(tz_name: Optional[str] = None) -> ZoneInfo:
    """Returns a valid ZoneInfo instance with tiered fallback to prevent crashes."""
    candidates = [
        tz_name,
        getattr(config, "USER_TIMEZONE", "Asia/Kolkata"),
        "Asia/Kolkata",
        "UTC",
    ]
    for c in candidates:
        if c and isinstance(c, str):
            try:
                return ZoneInfo(c.strip())
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def _normalize_to_calendar_event(
    event: Union[CalendarEvent, Dict[str, Any], Any]
) -> CalendarEvent:
    """Normalizes dictionaries or generic model instances into a typed CalendarEvent."""
    if isinstance(event, CalendarEvent):
        return event

    default_tz = getattr(config, "USER_TIMEZONE", "Asia/Kolkata")

    if isinstance(event, dict):
        title = (
            event.get("title")
            or event.get("summary")
            or event.get("name")
            or "New Scheduled Event"
        )
        desc = event.get("description", "")
        start = str(event.get("start", ""))
        end = str(event.get("end") or start)
        tz = event.get("timezone") or default_tz
        loc = event.get("location")
        m_url = (
            event.get("meeting_url")
            or event.get("meeting_link")
            or event.get("url")
        )
        org = event.get("organizer")
        all_day = bool(event.get("all_day", False) or event.get("is_all_day", False))
        guests = event.get("guests", []) or event.get("attendees", [])
        return CalendarEvent(
            title=title,
            description=desc,
            start=start,
            end=end,
            timezone=tz,
            location=loc,
            meeting_url=m_url,
            organizer=org,
            all_day=all_day,
            guests=guests,
        )

    return CalendarEvent(
        title=getattr(event, "title", None)
        or getattr(event, "summary", "New Scheduled Event"),
        description=getattr(event, "description", ""),
        start=str(getattr(event, "start", "")),
        end=str(getattr(event, "end", "") or getattr(event, "start", "")),
        timezone=getattr(event, "timezone", default_tz),
        location=getattr(event, "location", None),
        meeting_url=getattr(event, "meeting_url", None)
        or getattr(event, "meeting_link", None),
        organizer=getattr(event, "organizer", None),
        all_day=bool(getattr(event, "all_day", False) or getattr(event, "is_all_day", False)),
        guests=getattr(event, "guests", []) or getattr(event, "attendees", []),
    )


def _format_datetime_for_google(
    dt_str: str, timezone_str: str = "Asia/Kolkata"
) -> Dict[str, str]:
    """Converts a datetime string to an RFC 3339 payload expected by Google Calendar."""
    clean = str(dt_str or "").strip()
    safe_zone = _get_safe_zoneinfo(timezone_str)

    # Date-only format (All-Day Event)
    if len(clean) == 10 and clean.count("-") == 2 and "T" not in clean:
        return {"date": clean}

    try:
        clean_iso = clean.replace("Z", "+00:00")
        dt = parser.isoparse(clean_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=safe_zone)
        return {"dateTime": dt.isoformat(), "timeZone": str(safe_zone.key)}
    except Exception:
        fallback = datetime.now(safe_zone) + timedelta(days=1)
        return {"dateTime": fallback.isoformat(), "timeZone": str(safe_zone.key)}


# ----------------- Duplicate Reconciliation ----------------- #

def find_duplicate_event(
    calendar_service,
    event: Union[CalendarEvent, Dict[str, Any], Any],
    calendar_id: str = "primary",
) -> Optional[Dict[str, Any]]:
    """
    Scans a 3-hour window (+/- 90 mins) in the target account's calendar using
    title, meeting URL, and extended property signatures to prevent duplicates.
    """
    if not event or not calendar_service:
        return None

    try:
        ev = _normalize_to_calendar_event(event)
        if not ev.start:
            return None

        tz = _get_safe_zoneinfo(ev.timezone)
        start_clean = str(ev.start).replace("Z", "+00:00")

        # Parse start into timezone-aware datetime
        if len(start_clean) == 10 and start_clean.count("-") == 2 and "T" not in start_clean:
            start_dt = datetime.fromisoformat(start_clean).replace(
                hour=9, minute=0, tzinfo=tz
            )
        else:
            start_dt = parser.isoparse(start_clean)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=tz)

        # Build strict RFC 3339 search boundaries
        time_min = (start_dt - timedelta(minutes=90)).isoformat()
        time_max = (start_dt + timedelta(minutes=90)).isoformat()

        # Request only fields needed for duplicate matching
        events_result = (
            calendar_service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=25,
                singleEvents=True,
                orderBy="startTime",
                fields="items(id,summary,description,location,htmlLink,extendedProperties)",
            )
            .execute()
        )

        items = events_result.get("items", [])
        target_title = ev.title.strip().lower()
        target_url = (ev.meeting_url or "").strip().lower()

        for existing in items:
            existing_title = existing.get("summary", "").strip().lower()
            existing_desc = existing.get("description", "").lower()
            existing_loc = existing.get("location", "").lower()

            # 1. Exact title match
            if target_title and target_title == existing_title:
                return existing

            # 2. Substring match for non-trivial titles (> 5 characters)
            if len(target_title) > 5 and (target_title in existing_title or existing_title in target_title):
                return existing

            # 3. Meeting URL match
            if target_url and (target_url in existing_desc or target_url in existing_loc):
                return existing

            # 4. Extended Property match (Events scheduled by EMA)
            priv = existing.get("extendedProperties", {}).get("private", {})
            if priv.get("source") == "EmailAutomater" and target_title == existing_title:
                return existing

        return None
    except Exception as e:
        logger.warning(f"Duplicate reconciliation skipped on {calendar_id}: {e}")
        return None


# ----------------- Event Creation Pipeline ----------------- #

def create_or_update_event(
    calendar_service,
    event_data: Union[CalendarEvent, Dict[str, Any], Any],
    original_sender: str = "",
    attachments: Optional[List[Dict[str, Any]]] = None,
    calendar_id: str = "primary",
    target_account_email: Optional[str] = None,
) -> CalendarResult:
    """
    Creates a calendar event strictly in the target account's calendar.
    Includes an automatic fallback that strips Google Drive attachments if
    the Calendar API rejects the file attachments with a 400/403 error.
    """
    ev = _normalize_to_calendar_event(event_data)
    tz_str = getattr(config, "USER_TIMEZONE", ev.timezone or "Asia/Kolkata")
    resolved_calendar_id = (target_account_email or calendar_id).strip().lower()

    # 1. Skip creating duplicate if matching event exists
    existing = find_duplicate_event(calendar_service, ev, resolved_calendar_id)
    if existing:
        logger.info(
            f"[{resolved_calendar_id}] Skipped duplicate event '{ev.title}' (ID: {existing.get('id')})."
        )
        return CalendarResult(
            event_id=existing.get("id"),
            html_link=existing.get("htmlLink", ""),
            status="duplicate_skipped",
            summary=existing.get("summary", ev.title),
        )

    # 2. Build start and end payloads
    if ev.all_day:
        start_date = str(ev.start).split("T")[0]
        end_date = str(ev.end).split("T")[0] if ev.end else start_date
        # For multi-day all-day events, Google expects end date to be exclusive
        if start_date == end_date:
            try:
                dt_s = datetime.fromisoformat(start_date)
                end_date = (dt_s + timedelta(days=1)).strftime("%Y-%m-%d")
            except Exception:
                pass
        start_payload = {"date": start_date}
        end_payload = {"date": end_date}
    else:
        start_payload = _format_datetime_for_google(ev.start, tz_str)
        end_payload = _format_datetime_for_google(ev.end or ev.start, tz_str)

        # Ensure end time is strictly after start time
        if "dateTime" in start_payload and "dateTime" in end_payload:
            try:
                dt_start = parser.isoparse(start_payload["dateTime"])
                dt_end = parser.isoparse(end_payload["dateTime"])
                if dt_end <= dt_start:
                    end_payload["dateTime"] = (dt_start + timedelta(hours=1)).isoformat()
            except Exception:
                pass

    # 3. Assemble Description & Source Attributions
    description_lines = []
    if ev.description:
        description_lines.append(ev.description.strip())

    source_label = original_sender if original_sender else "Smart Universal Email Assistant"
    description_lines.append(
        f"\n---\nAuto-scheduled by Smart Universal Email Assistant\nSource: {source_label}\nAccount: {resolved_calendar_id}"
    )

    if ev.organizer:
        description_lines.append(f"Organizer: {ev.organizer}")
    if ev.meeting_url:
        description_lines.append(f"Meeting URL: {ev.meeting_url}")

    # 4. Format Attachments
    drive_attachments = []
    if attachments:
        description_lines.append("\n📎 Attached Evidence / Documents:")
        for att in attachments:
            name = att.get("name") or att.get("title") or att.get("filename") or "Document"
            file_id = att.get("id") or att.get("fileId")
            url = (
                att.get("webViewLink")
                or att.get("fileUrl")
                or att.get("url")
                or att.get("alternateLink")
                or att.get("webContentLink")
                or (f"https://drive.google.com/file/d/{file_id}/view" if file_id else "")
            )
            mime_type = att.get("mimeType") or att.get("mime_type") or "application/octet-stream"

            if url:
                description_lines.append(f"• {name}: {url}")
            else:
                description_lines.append(f"• {name}")

            if file_id and url:
                drive_attachments.append(
                    {
                        "fileUrl": url,
                        "title": name,
                        "mimeType": mime_type,
                        "fileId": file_id,
                    }
                )

    loc = ev.location or ev.meeting_url

    event_body: Dict[str, Any] = {
        "summary": ev.title,
        "description": "\n".join(description_lines).strip(),
        "start": start_payload,
        "end": end_payload,
        "extendedProperties": {
            "private": {
                "source": "EmailAutomater",
                "scheduled_by": "SmartUniversalEmailAssistant",
                "account_email": resolved_calendar_id,
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 15},
                {"method": "email", "minutes": 30},
            ],
        },
    }

    if loc:
        event_body["location"] = loc

    # Filter out self-invites and duplicates from attendees list
    if ev.guests:
        clean_attendees = set()
        for g in ev.guests:
            clean_g = str(g).strip().lower()
            if "@" in clean_g and clean_g != resolved_calendar_id:
                clean_attendees.add(clean_g)
        if clean_attendees:
            event_body["attendees"] = [{"email": email} for email in sorted(clean_attendees)]

    if drive_attachments:
        event_body["attachments"] = drive_attachments

    # 5. Insert Event with Automatic Attachment Fallback
    try:
        created_event = (
            calendar_service.events()
            .insert(
                calendarId=resolved_calendar_id,
                body=event_body,
                supportsAttachments=bool(drive_attachments),
            )
            .execute()
        )

        logger.info(
            f"[{resolved_calendar_id}] Created event '{ev.title}'. Link: {created_event.get('htmlLink')}"
        )
        return CalendarResult(
            event_id=created_event.get("id"),
            html_link=created_event.get("htmlLink", ""),
            status="created",
            summary=created_event.get("summary"),
        )

    except Exception as primary_err:
        err_str = str(primary_err)
        # If rejected due to attachment permissions, retry without body["attachments"]
        if drive_attachments and ("attachments" in err_str.lower() or "400" in err_str or "403" in err_str):
            logger.warning(
                f"[{resolved_calendar_id}] Attachment insertion failed ({err_str[:80]}). Retrying event without attachment payload..."
            )
            try:
                event_body.pop("attachments", None)
                retry_event = (
                    calendar_service.events()
                    .insert(
                        calendarId=resolved_calendar_id,
                        body=event_body,
                        supportsAttachments=False,
                    )
                    .execute()
                )
                logger.info(
                    f"[{resolved_calendar_id}] Created event '{ev.title}' without direct attachment payload. Link: {retry_event.get('htmlLink')}"
                )
                return CalendarResult(
                    event_id=retry_event.get("id"),
                    html_link=retry_event.get("htmlLink", ""),
                    status="created",
                    summary=retry_event.get("summary"),
                )
            except Exception as retry_err:
                primary_err = retry_err

        logger.error(f"[{resolved_calendar_id}] Failed to create event '{ev.title}': {primary_err}")
        return CalendarResult(
            event_id=None,
            html_link="",
            status="failed",
            error=str(primary_err),
        )