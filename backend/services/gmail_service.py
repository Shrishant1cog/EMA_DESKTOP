import base64
import html
import logging
import re
from typing import Any, Dict, List, Optional

from extractors.text_extractor import extract_attachment_text

logger = logging.getLogger(__name__)


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
    """Dictionary subclass supporting dot-notation attribute access."""
    def __getattr__(self, name):
        if name in self:
            return self[name]
        for k, v in self.items():
            if k.lower() == name.lower():
                return v
        raise AttributeError(f"'EmailData' object has no attribute '{name}'")

    def __setattr__(self, name, value):
        self[name] = value


def clean_html(html_content: str) -> str:
    """Strips scripts, styling, tags, and unescapes HTML entities."""
    no_scripts = re.sub(r'<(script|style)[^>]*>[\s\S]*?</\1>', '', html_content, flags=re.IGNORECASE)
    clean_text = re.sub(r'<[^>]+>', ' ', no_scripts)
    unescaped = html.unescape(clean_text)
    return re.sub(r'\s+', ' ', unescaped).strip()


def decode_payload_data(data_str: str) -> bytes:
    """Safe URL-safe base64 decoder handling missing padding."""
    padded = data_str + "=" * (-len(data_str) % 4)
    return base64.urlsafe_b64decode(padded.encode('utf-8'))


def _walk_parts(parts: list) -> list:
    """Recursively flattens multi-part MIME trees."""
    all_parts = []
    for part in parts:
        all_parts.append(part)
        if 'parts' in part:
            all_parts.extend(_walk_parts(part['parts']))
    return all_parts


def fetch_email_details(service, message_id: str) -> EmailData:
    """
    Fetches full email details, decodes bodies, downloads attachments,
    and extracts text for AI analysis.
    """
    msg = service.users().messages().get(userId='me', id=str(message_id), format='full').execute()
    payload = msg.get('payload', {})
    headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}

    all_parts = _walk_parts(payload.get('parts', [])) if 'parts' in payload else [payload]

    collected_body: List[str] = []
    collected_attachments: List[Dict[str, Any]] = []

    for part in all_parts:
        mime_type = part.get('mimeType', '')
        filename = part.get('filename', '')
        body = part.get('body', {})
        attachment_id = body.get('attachmentId')

        # 1. Attachment Processing
        if filename and (attachment_id or 'data' in body):
            raw_bytes: Optional[bytes] = None
            try:
                if attachment_id:
                    att_resp = service.users().messages().attachments().get(
                        userId='me', messageId=str(message_id), id=attachment_id
                    ).execute()
                    raw_bytes = decode_payload_data(att_resp['data'])
                elif 'data' in body:
                    raw_bytes = decode_payload_data(body['data'])

                if raw_bytes:
                    extracted_text = extract_attachment_text(filename, raw_bytes, mime_type)
                    collected_attachments.append({
                        "filename": filename,
                        "mime_type": mime_type,
                        "extracted_text": extracted_text,
                        "data": raw_bytes,
                        "size": len(raw_bytes)
                    })
                    if extracted_text.strip():
                        logger.info(f"Extracted {len(extracted_text)} chars from attachment '{filename}'")
            except Exception as e:
                logger.error(f"Failed to fetch/parse attachment '{filename}': {e}")

        # 2. Body Text Processing
        elif mime_type == 'text/plain' and 'data' in body and not filename:
            try:
                collected_body.append(decode_payload_data(body['data']).decode('utf-8', errors='ignore'))
            except Exception:
                pass
        elif mime_type == 'text/html' and 'data' in body and not filename:
            try:
                raw_html = decode_payload_data(body['data']).decode('utf-8', errors='ignore')
                collected_body.append(clean_html(raw_html))
            except Exception:
                pass

    full_body = "\n\n".join(collected_body).strip()
    attachment_summaries = [
        f"--- Attachment: {att['filename']} ---\n{att['extracted_text']}"
        for att in collected_attachments if att.get("extracted_text")
    ]
    combined_attachment_text = "\n\n".join(attachment_summaries).strip()

    return EmailData({
        "message_id": str(message_id),
        "id": str(message_id),
        "thread_id": msg.get('threadId'),
        "subject": headers.get('subject', 'No Subject'),
        "sender": headers.get('from', 'Unknown Sender'),
        "from": headers.get('from', 'Unknown Sender'),
        "recipient": headers.get('to', ''),
        "to": headers.get('to', ''),
        "date_received": headers.get('date', ''),
        "date": headers.get('date', ''),
        "snippet": msg.get('snippet', ''),
        "email_body": full_body,
        "body": full_body,
        "text": full_body,
        "has_attachments": len(collected_attachments) > 0,
        "attachments": collected_attachments,
        "combined_attachment_text": combined_attachment_text,
        "attachment_text": combined_attachment_text
    })


def fetch_raw_attachments(gmail_service, message_id: str) -> List[Dict[str, Any]]:
    """
    Returns binary payloads for schedule-relevant attachments.
    Avoids duplicate downloads if called through an existing EmailData structure.
    """
    valid_exts = ('.pdf', '.docx', '.xlsx', '.png', '.jpg', '.jpeg', '.ics', '.csv', '.txt')
    attachments = []
    try:
        email_data = fetch_email_details(gmail_service, str(message_id))
        for att in email_data.get("attachments", []):
            filename = att["filename"]
            raw_bytes = att.get("data")
            if not raw_bytes:
                continue

            # Skip tracking pixels / micro-icons unless they match document formats
            if len(raw_bytes) > 2000 or filename.lower().endswith(valid_exts):
                attachments.append({
                    'filename': filename,
                    'mimeType': att.get('mime_type', 'application/octet-stream'),
                    'data': raw_bytes
                })
    except Exception as e:
        logger.error(f"Error retrieving raw attachments for email {message_id}: {e}")

    return attachments


def get_unprocessed_message_ids(service, query: str = "label:INBOX", max_results: int = 10) -> List[MessageRef]:
    try:
        results = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
        messages = results.get('messages', [])
        return [MessageRef(m['id'], m.get('threadId', '')) for m in messages]
    except Exception as e:
        logger.error(f"Failed to query Gmail messages: {e}")
        return []


def mark_email_as_read(service, message_id: str):
    try:
        service.users().messages().modify(
            userId='me',
            id=str(message_id),
            body={'removeLabelIds': ['UNREAD']}
        ).execute()
    except Exception as e:
        logger.warning(f"Could not remove UNREAD label from message {message_id}: {e}")


class GmailService:
    """OOP wrapper for Gmail operations."""
    def __init__(self, service=None, *args, **kwargs):
        if hasattr(service, 'get_gmail_service'):
            self.service = service.get_gmail_service()
        elif hasattr(service, 'gmail'):
            self.service = service.gmail
        elif hasattr(service, 'gmail_service'):
            self.service = service.gmail_service
        else:
            self.service = service

    def list_recent_messages(self, query: str = "label:INBOX", max_results: int = 10) -> List[MessageRef]:
        return get_unprocessed_message_ids(self.service, query=query, max_results=max_results)

    def get_unprocessed_message_ids(self, query: str = "label:INBOX", max_results: int = 10) -> List[MessageRef]:
        return get_unprocessed_message_ids(self.service, query=query, max_results=max_results)

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