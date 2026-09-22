import base64
import html
import logging
import re
from typing import Any, Dict, List, Optional, Union

from extractors.text_extractor import extract_attachment_text

logger = logging.getLogger("GmailService")

SUPPORTED_SCHEDULE_EXTENSIONS = (
    ".pdf",
    ".docx",
    ".xlsx",
    ".xls",
    ".pptx",
    ".csv",
    ".ics",
    ".png",
    ".jpg",
    ".jpeg",
    ".txt",
    ".rtf",
)

MAX_ATTACHMENT_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB safety ceiling


class MessageRef(str):
    """String subclass supporting attribute and dict-like access for message IDs."""

    def __new__(cls, message_id: str, thread_id: str = ""):
        obj = str.__new__(cls, message_id)
        obj.id = message_id
        obj.message_id = message_id
        obj.thread_id = thread_id
        return obj

    def __getitem__(self, key):
        if key in ("id", "message_id"):
            return str(self)
        if key in ("threadId", "thread_id"):
            return self.thread_id
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in ("id", "message_id"):
            return str(self)
        if key in ("threadId", "thread_id"):
            return self.thread_id
        return default


class EmailData(dict):
    """Dictionary subclass supporting dot-notation attribute and dictionary access."""

    def __getattr__(self, name):
        if name in self:
            return self[name]
        for k, v in self.items():
            if k.lower() == name.lower():
                return v
        raise AttributeError(f"'EmailData' object has no attribute '{name}'")

    def __setattr__(self, name, value):
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        """Returns a clean standard Python dictionary."""
        return dict(self)


def clean_html(html_content: str) -> str:
    """
    Strips scripts, styling, and tags while converting block elements into clean
    newlines to preserve table, itinerary, and agenda formatting.
    """
    if not html_content:
        return ""

    # Remove script, style, and head containers
    text = re.sub(
        r"<(script|style|head)[^>]*>[\s\S]*?</\1>",
        "",
        html_content,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<!--[\s\S]*?-->", "", text)

    # Convert structural HTML elements into line breaks
    text = re.sub(
        r"<(p|br|div|tr|li|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE
    )
    text = re.sub(r"<[^>]+>", " ", text)

    unescaped = html.unescape(text)

    # Clean whitespace while preserving intentional paragraph breaks
    lines = [
        re.sub(r"[ \t]+", " ", line).strip() for line in unescaped.splitlines()
    ]
    cleaned = "\n".join([l for l in lines if l])
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def decode_payload_data(data_str: str) -> bytes:
    """Safe URL-safe base64 decoder handling missing padding, whitespace, and formatting variances."""
    if not data_str:
        return b""
    clean_str = (
        data_str.strip()
        .replace("-", "+")
        .replace("_", "/")
        .replace("\r", "")
        .replace("\n", "")
        .replace(" ", "")
    )
    padded = clean_str + "=" * (-len(clean_str) % 4)
    try:
        return base64.b64decode(padded)
    except Exception as e:
        logger.warning(f"Base64 payload decode error: {e}")
        return b""


def _walk_parts(parts: list) -> list:
    """Recursively flattens multi-part MIME trees."""
    all_parts = []
    for part in parts:
        all_parts.append(part)
        if "parts" in part and isinstance(part["parts"], list):
            all_parts.extend(_walk_parts(part["parts"]))
    return all_parts


def fetch_email_details(service, message_id: str) -> EmailData:
    """
    Fetches full email details, decodes bodies, downloads valid attachments,
    and extracts text for AI analysis.
    """
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=str(message_id), format="full")
        .execute()
    )
    payload = msg.get("payload", {})
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", [])
    }

    all_parts = (
        _walk_parts(payload.get("parts", []))
        if "parts" in payload
        else [payload]
    )

    plain_body_parts: List[str] = []
    html_body_parts: List[str] = []
    collected_attachments: List[Dict[str, Any]] = []

    for part in all_parts:
        mime_type = part.get("mimeType", "").lower()
        filename = part.get("filename", "").strip()
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        declared_size = int(body.get("size", 0))

        # 1. Attachment Processing
        if filename and (attachment_id or "data" in body):
            # Check size ceiling before downloading large binary payloads
            if declared_size > MAX_ATTACHMENT_SIZE_BYTES:
                logger.warning(
                    f"Attachment '{filename}' ({declared_size} bytes) exceeds 25 MB limit. Skipping download."
                )
                continue

            raw_bytes: Optional[bytes] = None
            try:
                if attachment_id:
                    att_resp = (
                        service.users()
                        .messages()
                        .attachments()
                        .get(
                            userId="me",
                            messageId=str(message_id),
                            id=attachment_id,
                        )
                        .execute()
                    )
                    raw_bytes = decode_payload_data(att_resp.get("data", ""))
                elif "data" in body:
                    raw_bytes = decode_payload_data(body.get("data", ""))

                if raw_bytes:
                    # Filter out tracking pixels and micro signature icons
                    is_image = mime_type.startswith("image/") or filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))
                    if is_image and len(raw_bytes) < 2048:
                        continue

                    extracted_text = ""
                    try:
                        extracted_text = extract_attachment_text(
                            filename, raw_bytes, mime_type
                        )
                    except Exception as parse_err:
                        logger.warning(
                            f"Could not parse document text from '{filename}': {parse_err}"
                        )

                    collected_attachments.append(
                        {
                            "filename": filename,
                            "mime_type": mime_type or "application/octet-stream",
                            "extracted_text": extracted_text,
                            "data": raw_bytes,
                            "size": len(raw_bytes),
                        }
                    )
                    if extracted_text.strip():
                        logger.info(
                            f"Extracted {len(extracted_text)} characters from attachment '{filename}'"
                        )
            except Exception as e:
                logger.error(
                    f"Failed to fetch attachment '{filename}' for email {message_id}: {e}"
                )

        # 2. Body Text Processing
        elif "data" in body and not filename:
            try:
                raw_text_data = decode_payload_data(body.get("data", "")).decode(
                    "utf-8", errors="ignore"
                )
                if mime_type == "text/plain":
                    plain_body_parts.append(raw_text_data)
                elif mime_type == "text/html":
                    html_body_parts.append(clean_html(raw_text_data))
            except Exception:
                pass

    # Prefer plain text; fall back to stripped HTML only if plain text is absent
    if plain_body_parts:
        full_body = "\n\n".join(plain_body_parts).strip()
    elif html_body_parts:
        full_body = "\n\n".join(html_body_parts).strip()
    else:
        full_body = msg.get("snippet", "")

    attachment_summaries = [
        f"--- Attachment: {att['filename']} ---\n{att['extracted_text']}"
        for att in collected_attachments
        if att.get("extracted_text")
    ]
    combined_attachment_text = "\n\n".join(attachment_summaries).strip()

    return EmailData(
        {
            "message_id": str(message_id),
            "id": str(message_id),
            "thread_id": msg.get("threadId", ""),
            "subject": headers.get("subject", "No Subject"),
            "sender": headers.get("from", "Unknown Sender"),
            "from": headers.get("from", "Unknown Sender"),
            "recipient": headers.get("to", ""),
            "to": headers.get("to", ""),
            "date_received": headers.get("date", ""),
            "date": headers.get("date", ""),
            "snippet": msg.get("snippet", ""),
            "email_body": full_body,
            "body": full_body,
            "text": full_body,
            "has_attachments": len(collected_attachments) > 0,
            "attachments": collected_attachments,
            "combined_attachment_text": combined_attachment_text,
            "attachment_text": combined_attachment_text,
        }
    )


def fetch_raw_attachments(
    gmail_service, message_or_id: Union[str, Dict[str, Any], EmailData]
) -> List[Dict[str, Any]]:
    """
    Returns binary payloads for schedule-relevant attachments.
    Reuses existing EmailData in memory if available to avoid duplicate network calls.
    """
    attachments = []

    try:
        if isinstance(message_or_id, (dict, EmailData)) and "attachments" in message_or_id:
            email_data = message_or_id
        else:
            email_data = fetch_email_details(gmail_service, str(message_or_id))

        for att in email_data.get("attachments", []):
            filename = att.get("filename", "")
            raw_bytes = att.get("data")
            if not raw_bytes:
                continue

            # Skip tracking pixels and micro-icons unless matching document extensions
            if len(raw_bytes) > 2048 or filename.lower().endswith(SUPPORTED_SCHEDULE_EXTENSIONS):
                attachments.append(
                    {
                        "filename": filename,
                        "mimeType": att.get("mime_type", "application/octet-stream"),
                        "data": raw_bytes,
                    }
                )
    except Exception as e:
        logger.error(f"Error retrieving raw attachments: {e}")

    return attachments


def get_unprocessed_message_ids(
    service, query: str = "label:INBOX", max_results: int = 10
) -> List[MessageRef]:
    """Queries Gmail inbox for recent matching messages."""
    try:
        results = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        messages = results.get("messages", [])
        return [MessageRef(m["id"], m.get("threadId", "")) for m in messages]
    except Exception as e:
        logger.error(f"Failed to query Gmail messages: {e}")
        return []


def mark_email_as_read(service, message_id: str):
    """Marks an email as read by removing the UNREAD label."""
    try:
        service.users().messages().modify(
            userId="me",
            id=str(message_id),
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except Exception as e:
        logger.warning(f"Could not remove UNREAD label from message {message_id}: {e}")


class GmailService:
    """OOP wrapper for Gmail operations."""

    def __init__(self, service=None, *args, **kwargs):
        if hasattr(service, "get_gmail_service"):
            self.service = service.get_gmail_service()
        elif hasattr(service, "gmail"):
            self.service = service.gmail
        elif hasattr(service, "gmail_service"):
            self.service = service.gmail_service
        else:
            self.service = service

    def list_recent_messages(
        self, query: str = "label:INBOX", max_results: int = 10
    ) -> List[MessageRef]:
        return get_unprocessed_message_ids(
            self.service, query=query, max_results=max_results
        )

    def get_unprocessed_message_ids(
        self, query: str = "label:INBOX", max_results: int = 10
    ) -> List[MessageRef]:
        return get_unprocessed_message_ids(
            self.service, query=query, max_results=max_results
        )

    def get_message(self, message_id: str) -> EmailData:
        return fetch_email_details(self.service, str(message_id))

    def fetch_email_details(self, message_id: str) -> EmailData:
        return fetch_email_details(self.service, str(message_id))

    def fetch_message(self, message_id: str) -> EmailData:
        return fetch_email_details(self.service, str(message_id))

    def mark_as_read(self, message_id: str):
        return mark_email_as_read(self.service, str(message_id))

    def mark_email_as_read(self, message_id: str):
        return mark_email_as_read(self.service, str(message_id))