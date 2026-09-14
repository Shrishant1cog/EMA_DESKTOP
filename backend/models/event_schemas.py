from typing import List, Optional, Any, Dict
from datetime import datetime, timedelta
from pydantic import BaseModel, Field, model_validator, field_validator


class CalendarEvent(BaseModel):
    title: str = Field(default="Scheduled Event", description="Title or summary of the event")
    description: str = Field(default="", description="Detailed description or agenda")
    start: str = Field(default="", description="ISO 8601 formatted start datetime (YYYY-MM-DDTHH:MM:SS) or YYYY-MM-DD")
    end: str = Field(default="", description="ISO 8601 formatted end datetime (YYYY-MM-DDTHH:MM:SS) or YYYY-MM-DD")
    timezone: str = Field(default="Asia/Kolkata", description="Timezone of the event")
    location: Optional[str] = Field(default=None, description="Physical location or meeting room")
    meeting_url: Optional[str] = Field(default=None, description="Direct meeting URL (Meet, Zoom, Teams)")
    organizer: Optional[str] = Field(default=None, description="Name or email of organizer")
    all_day: bool = Field(default=False, description="True if this is an all-day event")
    is_all_day: bool = Field(default=False, description="Alias for all_day")
    guests: List[str] = Field(default_factory=list, description="List of attendee email addresses")
    attendees: List[str] = Field(default_factory=list, description="Alias for guests")

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        # Title normalization
        if not data.get("title"):
            data["title"] = data.get("summary") or data.get("name") or data.get("subject") or "Scheduled Event"

        # Start time normalization
        start_val = data.get("start") or data.get("start_time") or data.get("startTime") or data.get("date") or ""
        if isinstance(start_val, dict):
            start_val = start_val.get("dateTime") or start_val.get("date") or ""
        data["start"] = str(start_val).strip()

        # End time normalization
        end_val = data.get("end") or data.get("end_time") or data.get("endTime") or ""
        if isinstance(end_val, dict):
            end_val = end_val.get("dateTime") or end_val.get("date") or ""
        data["end"] = str(end_val).strip()

        # Auto-compute 1-hour end time if missing
        if data["start"] and not data["end"]:
            try:
                clean_start = data["start"].replace("Z", "+00:00")
                if "T" in clean_start:
                    dt = datetime.fromisoformat(clean_start)
                    data["end"] = (dt + timedelta(hours=1)).isoformat()
                else:
                    data["end"] = data["start"]
                    data["all_day"] = True
                    data["is_all_day"] = True
            except Exception:
                data["end"] = data["start"]

        # Attendees / Guests synchronization
        raw_guests = data.get("guests") or data.get("attendees") or []
        clean_guests: List[str] = []
        if isinstance(raw_guests, str):
            clean_guests = [g.strip() for g in raw_guests.split(",") if g.strip()]
        elif isinstance(raw_guests, list):
            for item in raw_guests:
                if isinstance(item, dict) and item.get("email"):
                    clean_guests.append(str(item["email"]).strip())
                elif isinstance(item, str) and item.strip():
                    clean_guests.append(item.strip())
        data["guests"] = clean_guests
        data["attendees"] = clean_guests

        # Flag synchronization
        all_day_flag = bool(data.get("all_day") or data.get("is_all_day", False))
        data["all_day"] = all_day_flag
        data["is_all_day"] = all_day_flag

        return data

    def __getitem__(self, key: str) -> Any:
        val = self.get(key)
        if val is None and key not in self.__dict__:
            raise KeyError(key)
        return val

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("summary", "title"):
            return self.title
        if key in ("link", "meeting_link", "meeting_url"):
            return self.meeting_url
        if key in ("attendees", "guests"):
            return self.guests
        if key in ("all_day", "is_all_day"):
            return self.all_day
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        return default

    def __contains__(self, key: str) -> bool:
        return key in self.__dict__ or key in ("summary", "title", "meeting_link", "meeting_url", "attendees")

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class UniversalEmailAnalysis(BaseModel):
    category: str = Field(default="General / Marketing")
    priority: str = Field(default="Normal")
    summary: str = Field(default="General correspondence")
    action_description: str = Field(default="Processed email")
    has_calendar_event: bool = Field(default=False)
    is_attachment_evidence: bool = Field(default=False)
    is_spam_or_scam: bool = Field(default=False)
    spam_score: float = Field(default=0.0)
    spam_threshold: float = Field(default=0.50)
    is_event: bool = Field(default=False)
    confidence_score: float = Field(default=0.0)
    reasoning: str = Field(default="")
    events: List[CalendarEvent] = Field(default_factory=list)

    @field_validator("spam_score", mode="before")
    @classmethod
    def parse_spam(cls, v: Any) -> float:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v / 100.0) if v > 1.0 else float(v)
        if isinstance(v, str):
            clean = v.replace("%", "").strip()
            try:
                val = float(clean)
                return val / 100.0 if val > 1.0 else val
            except ValueError:
                return 0.0
        return 0.0

    @model_validator(mode="before")
    @classmethod
    def ensure_valid_structure(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        # 1. Parse spam score safely
        raw_score = data.get("spam_score", 0.0)
        try:
            score_f = float(str(raw_score).replace("%", "").strip()) if raw_score is not None else 0.0
            if score_f > 1.0:
                score_f /= 100.0
        except Exception:
            score_f = 0.0

        # 2. Parse dynamic user threshold (defaults to 0.50 / 50%)
        threshold = float(data.get("spam_threshold", 0.50))
        if threshold > 1.0:
            threshold /= 100.0

        is_spam = bool(
            data.get("is_spam_or_scam") or
            score_f >= threshold or
            data.get("category") == "Spam / Scam"
        )

        # 3. Strict Spam Guard: Purge calendar events immediately
        if is_spam:
            data["is_spam_or_scam"] = True
            data["spam_score"] = score_f if score_f > 0 else max(threshold, 0.55)
            data["category"] = "Spam / Scam"
            data["priority"] = "Low"
            data["has_calendar_event"] = False
            data["is_event"] = False
            data["events"] = []
            data["action_description"] = f"Ignored (Detected as Spam/Scam - {int(data['spam_score']*100)}% probability)"
            if not data.get("summary"):
                data["summary"] = data.get("subject") or "Spam email filtered"
            return data

        # 4. Defaults for legitimate emails
        if not data.get("category"):
            data["category"] = "General / Marketing"
        if not data.get("priority"):
            data["priority"] = "Normal"
        if not data.get("summary"):
            data["summary"] = data.get("subject") or data.get("action_description") or "Automated inbox notice"
        if not data.get("action_description"):
            data["action_description"] = "Processed correspondence"

        # Compatibility flags
        is_evt = bool(data.get("has_calendar_event") or data.get("is_event") or len(data.get("events", [])) > 0)
        data["has_calendar_event"] = is_evt
        data["is_event"] = is_evt

        if not data.get("confidence_score"):
            data["confidence_score"] = 0.9 if is_evt else 0.1
        if not data.get("reasoning"):
            data["reasoning"] = data.get("summary")

        return data

    @property
    def should_create_event(self) -> bool:
        return bool(self.has_calendar_event and len(self.events) > 0 and not self.is_spam_or_scam)

    def __getitem__(self, key: str) -> Any:
        val = self.get(key)
        if val is None and key not in self.__dict__:
            raise KeyError(key)
        return val

    def get(self, key: str, default: Any = None) -> Any:
        if key == "should_create_event":
            return self.should_create_event
        if key in ("event", "event_data"):
            return self.events[0] if self.events else None
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        return default

    def __contains__(self, key: str) -> bool:
        return key in self.__dict__ or key in ("should_create_event", "event", "event_data")

    def to_dict(self) -> Dict[str, Any]:
        data = self.model_dump()
        data["should_create_event"] = self.should_create_event
        data["event_data"] = self.events[0].model_dump() if self.events else None
        data["event"] = data["event_data"]
        return data


# Backward-compatible alias
EmailEventAnalysis = UniversalEmailAnalysis