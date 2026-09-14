import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Union
from dateutil import parser
from backend.models.event_schemas import CalendarEvent
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import config

logger = logging.getLogger(__name__)

class CalendarResult(dict):
    """Dictionary subclass supporting both dot-attribute and dict access."""
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

def _normalize_to_calendar_event(event: Union[CalendarEvent, Dict[str, Any]]) -> CalendarEvent:
    if isinstance(event, CalendarEvent):
        return event
    if isinstance(event, dict):
        title = event.get("title") or event.get("summary") or "New Event"
        desc = event.get("description", "")
        start = str(event.get("start", ""))
        end = str(event.get("end") or start)
        tz = event.get("timezone", "Asia/Kolkata")
        loc = event.get("location")
        m_url = event.get("meeting_url") or event.get("meeting_link") or event.get("url")
        org = event.get("organizer")
        all_day = bool(event.get("all_day", False))
        guests = event.get("guests", [])
        return CalendarEvent(
            title=title, description=desc, start=start, end=end,
            timezone=tz, location=loc, meeting_url=m_url, organizer=org,
            all_day=all_day, guests=guests
        )
    return event

def find_duplicate_event(calendar_service, event: Union[CalendarEvent, Dict[str, Any]], calendar_id: str = "primary") -> Optional[Dict[str, Any]]:
    if not event or not calendar_service:
        return None
    try:
        ev = _normalize_to_calendar_event(event)
        start_dt = parser.isoparse(ev.start)
        
        time_min = (start_dt - timedelta(hours=1)).isoformat()
        time_max = (start_dt + timedelta(hours=1)).isoformat()

        events_result = calendar_service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        items = events_result.get("items", [])
        target_title = ev.title.strip().lower()
        target_url = (ev.meeting_url or "").strip().lower()

        for existing in items:
            existing_title = existing.get("summary", "").strip().lower()
            existing_desc = existing.get("description", "").lower()
            existing_loc = existing.get("location", "").lower()

            if target_title and (target_title == existing_title or target_title in existing_title):
                return existing

            if target_url and (target_url in existing_desc or target_url in existing_loc):
                return existing

        return None
    except Exception as e:
        logger.error(f"Error checking duplicate events in Google Calendar: {e}")
        return None

def create_or_update_event(
    service,
    event_data: CalendarEvent,
    original_sender: str = "",
    attachments: list = None,  # List of Drive attachment objects
    calendar_id: str = "primary"
) -> CalendarResult:
    # Skip creating a duplicate if a matching event already exists nearby.
    # (This duplicate check used to be defined but never actually called.)
    existing = find_duplicate_event(service, event_data, calendar_id)
    if existing:
        logger.info(
            f"Skipped duplicate event '{event_data.title}' "
            f"(matches existing event {existing.get('id')})."
        )
        return CalendarResult(
            event_id=existing.get("id"),
            html_link=existing.get("htmlLink", ""),
            status="duplicate_skipped"
        )

    # Build description and append evidence links
    description_lines = [
        event_data.description or "",
        f"\n\nSource: Auto-scheduled from email by {original_sender}"
    ]

    if event_data.organizer:
        description_lines.append(f"Organizer: {event_data.organizer}")
    if event_data.meeting_url:
        description_lines.append(f"Meeting URL: {event_data.meeting_url}")

    if attachments:
        description_lines.append("\n📎 Attached Evidence / Documents:")
        for att in attachments:
            description_lines.append(f"• {att['title']}: {att['fileUrl']}")

    event_body: Dict[str, Any] = {
        "summary": event_data.title,
        "description": "\n".join(description_lines).strip(),
        "attendees": [{"email": guest} for guest in event_data.guests if "@" in str(guest)],
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 15},  # Device popup/notification
                {"method": "email", "minutes": 30}   # Optional email reminder
            ],
        }
    }

    # All-day events must use "date", not "dateTime" - Google Calendar rejects
    # a bare YYYY-MM-DD passed as dateTime, so this branch has to run for them.
    if event_data.all_day:
        start_date = event_data.start.split("T")[0]
        end_date = event_data.end.split("T")[0] if event_data.end else start_date
        event_body["start"] = {"date": start_date}
        event_body["end"] = {"date": end_date}
    else:
        event_body["start"] = {"dateTime": event_data.start, "timeZone": event_data.timezone}
        event_body["end"] = {"dateTime": event_data.end, "timeZone": event_data.timezone}

    if event_data.location:
        event_body["location"] = event_data.location
    elif event_data.meeting_url:
        event_body["location"] = event_data.meeting_url

    # Attach Google Drive files to the calendar event
    if attachments:
        event_body["attachments"] = [
            {
                "fileUrl": att["fileUrl"],
                "title": att["title"],
                "mimeType": att["mimeType"],
                "fileId": att["fileId"]
            }
            for att in attachments
        ]

    try:
        # Google Calendar requires supportsAttachments=True when adding attachments
        created_event = service.events().insert(
            calendarId=calendar_id,
            body=event_body,
            supportsAttachments=True
        ).execute()

        logger.info(f"Created event '{event_data.title}'. Link: {created_event.get('htmlLink')}")
        return CalendarResult(
            event_id=created_event.get("id"),
            html_link=created_event.get("htmlLink", ""),
            status="created"
        )
    except Exception as e:
        logger.error(f"Failed to create Google Calendar event '{event_data.title}': {e}")
        return CalendarResult(
            event_id=None,
            html_link="",
            status="failed",
            error=str(e)
        )

class CalendarService:
    """Class wrapper providing object-oriented access to Google Calendar operations."""
    def __init__(self, service=None, calendar_id: str = "primary", *args, **kwargs):
        if hasattr(service, 'get_calendar_service'):
            self.service = service.get_calendar_service()
        elif hasattr(service, 'calendar'):
            self.service = service.calendar
        else:
            self.service = service
        self.calendar_id = calendar_id

    def create_event_from_email(self, parsed, original_sender: str = "", *args, **kwargs) -> CalendarResult:
        """Extracts event payload from analysis and creates calendar event."""
        target_event = None

        if hasattr(parsed, "events") and parsed.events:
            target_event = parsed.events[0]
        elif isinstance(parsed, dict):
            if parsed.get("events"):
                target_event = parsed["events"][0]
            elif parsed.get("event"):
                target_event = parsed["event"]
            elif parsed.get("event_data"):
                target_event = parsed["event_data"]
            else:
                target_event = parsed
        else:
            target_event = parsed

        return self.create_or_update_event(target_event, original_sender=original_sender, *args, **kwargs)

    def find_duplicate_event(self, event, *args, **kwargs) -> Optional[Dict[str, Any]]:
        svc = kwargs.get("service") or self.service
        cal_id = kwargs.get("calendar_id") or self.calendar_id
        return find_duplicate_event(svc, event, cal_id)

    def create_or_update_event(self, event, original_sender: str = "", *args, **kwargs) -> CalendarResult:
        svc = kwargs.get("service") or self.service
        cal_id = kwargs.get("calendar_id") or self.calendar_id
        attachments = kwargs.get("attachments")
        return create_or_update_event(svc, event, original_sender, attachments, cal_id)

    def create_event(self, event, original_sender: str = "", *args, **kwargs) -> CalendarResult:
        return self.create_or_update_event(event, original_sender, *args, **kwargs)

    def add_event(self, event, original_sender: str = "", *args, **kwargs) -> CalendarResult:
        return self.create_or_update_event(event, original_sender, *args, **kwargs)

    def insert_event(self, event, original_sender: str = "", *args, **kwargs) -> CalendarResult:
        return self.create_or_update_event(event, original_sender, *args, **kwargs)

    def sync_event(self, event, original_sender: str = "", *args, **kwargs) -> CalendarResult:
        return self.create_or_update_event(event, original_sender, *args, **kwargs)



logger = logging.getLogger(__name__)

def _format_datetime_for_google(dt_str: str, timezone_str: str = "Asia/Kolkata") -> Dict[str, str]:
    """Converts raw string to valid Google Calendar start/end payload."""
    clean = dt_str.strip()
    
    # 1. Whole date (All day)
    if len(clean) == 10 and clean.count("-") == 2:
        return {"date": clean}
    
    # 2. DateTime string
    try:
        # Standardize ISO
        clean = clean.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            tz = ZoneInfo(timezone_str)
            dt = dt.replace(tzinfo=tz)
        return {"dateTime": dt.isoformat(), "timeZone": timezone_str}
    except Exception:
        # Fallback to current time + 1 day
        fallback = datetime.now(ZoneInfo(timezone_str)) + timedelta(days=1)
        return {"dateTime": fallback.isoformat(), "timeZone": timezone_str}

def create_or_update_event(
    calendar_service,
    event_data: Any,
    original_sender: str = "",
    attachments: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    tz_str = getattr(config, "USER_TIMEZONE", "Asia/Kolkata")
    
    title = getattr(event_data, "title", "Scheduled Event")
    start_raw = getattr(event_data, "start", "")
    end_raw = getattr(event_data, "end", "")
    
    start_payload = _format_datetime_for_google(start_raw, tz_str)
    end_payload = _format_datetime_for_google(end_raw, tz_str)
    
    # If end equals start and is dateTime, add 1 hour
    if "dateTime" in start_payload and "dateTime" in end_payload:
        if start_payload["dateTime"] == end_payload["dateTime"]:
            dt_start = datetime.fromisoformat(start_payload["dateTime"])
            end_payload["dateTime"] = (dt_start + timedelta(hours=1)).isoformat()

    description_parts = []
    desc = getattr(event_data, "description", "")
    if desc:
        description_parts.append(desc)
    if original_sender:
        description_parts.append(f"\nOrganizer / From: {original_sender}")
    
    meet_url = getattr(event_data, "meeting_url", None)
    if meet_url:
        description_parts.append(f"\nMeeting Link: {meet_url}")
        
    if attachments:
        description_parts.append("\n\nAttached Documents:")
        for att in attachments:
            name = att.get("title") or att.get("filename") or "Document"
            file_id = att.get("id")
            
            # Universal URL resolver with ID fallback
            url = (
                att.get("url") or 
                att.get("webViewLink") or 
                att.get("alternateLink") or 
                att.get("webContentLink") or 
                (f"https://drive.google.com/file/d/{file_id}/view?usp=sharing" if file_id else "")
            )
            
            if url:
                description_parts.append(f"• {name}: {url}")
            else:
                description_parts.append(f"• {name}")

    body = {
        "summary": title,
        "description": "\n".join(description_parts),
        "start": start_payload,
        "end": end_payload,
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 15},
                {"method": "email", "minutes": 60}
            ]
        }
    }

    loc = getattr(event_data, "location", None) or meet_url
    if loc:
        body["location"] = loc

    # Attendees
    guests = getattr(event_data, "guests", []) or getattr(event_data, "attendees", [])
    if guests:
        body["attendees"] = [{"email": g} for g in guests if "@" in str(g)]

    try:
        created_event = calendar_service.events().insert(
            calendarId="primary",
            body=body
        ).execute()
        logger.info(f"Successfully created calendar event: {created_event.get('id')}")
        return created_event
    except Exception as e:
        logger.error(f"Google Calendar API insert error: {e}")
        raise e