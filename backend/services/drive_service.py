import io
import logging
import re
import threading
from typing import Optional, Dict, Any
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

logger = logging.getLogger("DriveService")

FOLDER_NAME = "Calendar Evidence Attachments"

_CACHE_LOCK = threading.Lock()
_FOLDER_CACHE: Dict[str, str] = {}


def _sanitize_drive_query_string(value: str) -> str:
    """Escapes backslashes and single quotes to prevent Drive search query syntax errors."""
    if not value:
        return ""
    clean = value.replace("\\", "\\\\").replace("'", "\\'")
    # Strip non-printable or newline characters
    return re.sub(r"[\r\n\t]+", " ", clean).strip()


def get_or_create_evidence_folder(drive_service) -> Optional[str]:
    """
    Finds or creates a dedicated evidence folder in Drive.
    Caches the folder ID thread-safely on the service instance and in-memory registry.
    """
    # 1. Check instance-level attribute cache first
    cached_instance_id = getattr(drive_service, "_evidence_folder_id", None)
    if cached_instance_id:
        return cached_instance_id

    # 2. Check service key registry
    service_key = str(getattr(drive_service, "_account_email", "")) or str(id(drive_service))
    with _CACHE_LOCK:
        if service_key in _FOLDER_CACHE:
            return _FOLDER_CACHE[service_key]

    try:
        # Search for existing folder
        query = (
            f"name = '{FOLDER_NAME}' and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )
        results = (
            drive_service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=1,
            )
            .execute()
        )
        files = results.get("files", [])

        if files:
            folder_id = files[0]["id"]
            with _CACHE_LOCK:
                _FOLDER_CACHE[service_key] = folder_id
            setattr(drive_service, "_evidence_folder_id", folder_id)
            return folder_id

        # Create new folder if absent
        folder_metadata = {
            "name": FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = (
            drive_service.files()
            .create(
                body=folder_metadata,
                fields="id, name",
            )
            .execute()
        )

        folder_id = folder.get("id")
        if folder_id:
            with _CACHE_LOCK:
                _FOLDER_CACHE[service_key] = folder_id
            setattr(drive_service, "_evidence_folder_id", folder_id)
            logger.info(f"Created dedicated evidence folder in Google Drive: '{FOLDER_NAME}' (ID: {folder_id})")
            return folder_id

        return None

    except HttpError as e:
        status = e.resp.status if hasattr(e, "resp") else 0
        if status in (401, 403):
            logger.warning(f"Google Drive folder access denied or token expired ({status}). Uploading to root.")
        else:
            logger.warning(f"Drive folder lookup error: {e}. Defaulting to root Drive directory.")
        return None
    except Exception as e:
        logger.warning(f"Could not initialize folder in Drive: {e}. Defaulting to root.")
        return None


def upload_evidence_to_drive(
    drive_service,
    filename: str,
    file_bytes: bytes,
    mime_type: str = "application/octet-stream",
) -> Optional[Dict[str, Any]]:
    """
    Uploads file bytes to Drive, avoids duplicate uploads, configures view permissions,
    and returns standardized metadata compatible with CalendarService and the UI.
    """
    if not file_bytes:
        return None

    clean_filename = filename.strip() if filename else "Evidence_Document"
    safe_query_name = _sanitize_drive_query_string(clean_filename)
    stream_buffer: Optional[io.BytesIO] = None

    try:
        folder_id = get_or_create_evidence_folder(drive_service)

        # 1. Deduplication: Check if identical file already exists in evidence folder
        if folder_id:
            try:
                dup_query = (
                    f"name = '{safe_query_name}' and "
                    f"'{folder_id}' in parents and "
                    "trashed = false"
                )
                dup_check = (
                    drive_service.files()
                    .list(
                        q=dup_query,
                        spaces="drive",
                        fields="files(id, name, mimeType, webViewLink, webContentLink)",
                        pageSize=1,
                    )
                    .execute()
                )
                existing_files = dup_check.get("files", [])
                if existing_files:
                    existing = existing_files[0]
                    fid = existing["id"]
                    web_link = (
                        existing.get("webViewLink")
                        or existing.get("webContentLink")
                        or f"https://drive.google.com/file/d/{fid}/view?usp=sharing"
                    )
                    logger.info(f"Reusing previously uploaded Drive evidence: '{clean_filename}' ({fid})")
                    return {
                        "id": fid,
                        "fileId": fid,
                        "name": existing.get("name", clean_filename),
                        "title": existing.get("name", clean_filename),
                        "filename": clean_filename,
                        "mime_type": existing.get("mimeType", mime_type),
                        "mimeType": existing.get("mimeType", mime_type),
                        "url": web_link,
                        "fileUrl": web_link,
                        "webViewLink": web_link,
                        "alternateLink": web_link,
                        "size": len(file_bytes),
                    }
            except Exception as dup_err:
                logger.debug(f"Deduplication check skipped for '{clean_filename}': {dup_err}")

        # 2. Build upload metadata
        file_metadata: Dict[str, Any] = {"name": clean_filename}
        if folder_id:
            file_metadata["parents"] = [folder_id]

        file_size = len(file_bytes)
        stream_buffer = io.BytesIO(file_bytes)

        # Optimize upload strategy: direct multipart for small files (<5MB), resumable for large files
        use_resumable = file_size >= (5 * 1024 * 1024)
        media = MediaIoBaseUpload(
            stream_buffer,
            mimetype=mime_type or "application/octet-stream",
            resumable=use_resumable,
            chunksize=1024 * 1024 if use_resumable else -1,
        )

        uploaded = (
            drive_service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, mimeType, webViewLink, webContentLink",
            )
            .execute()
        )

        file_id = uploaded.get("id")
        if not file_id:
            return None

        # Direct view URL
        direct_url = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
        web_link = uploaded.get("webViewLink") or uploaded.get("webContentLink") or direct_url

        # 3. Configure view permissions so calendar attendees can read the document
        try:
            drive_service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                fields="id",
            ).execute()
        except Exception as perm_err:
            # Workspace administrative policy may disallow public link sharing
            logger.debug(f"Notice: Could not set public link permission on {clean_filename}: {perm_err}")

        logger.info(f"Uploaded attachment '{clean_filename}' to Drive: {web_link}")

        return {
            "id": file_id,
            "fileId": file_id,
            "name": uploaded.get("name", clean_filename),
            "title": uploaded.get("name", clean_filename),
            "filename": clean_filename,
            "mime_type": uploaded.get("mimeType", mime_type),
            "mimeType": uploaded.get("mimeType", mime_type),
            "url": web_link,
            "fileUrl": web_link,
            "webViewLink": web_link,
            "alternateLink": web_link,
            "size": file_size,
        }

    except HttpError as e:
        status_code = e.resp.status if hasattr(e, "resp") else 0
        if status_code == 403:
            logger.error(f"Google Drive storage quota exceeded or access denied for '{clean_filename}'.")
        elif status_code == 401:
            logger.error(f"Google Drive token expired while uploading '{clean_filename}'. Re-authentication required.")
        elif status_code == 404 and folder_id:
            # Evidence folder was moved or trashed during runtime; evict cache
            service_key = str(getattr(drive_service, "_account_email", "")) or str(id(drive_service))
            with _CACHE_LOCK:
                _FOLDER_CACHE.pop(service_key, None)
            setattr(drive_service, "_evidence_folder_id", None)
            logger.warning("Evidence folder was invalid. Evicted from cache.")
        else:
            logger.error(f"Google Drive API error ({status_code}) uploading '{clean_filename}': {e}")
        return None

    except Exception as e:
        logger.error(f"Unexpected exception uploading '{clean_filename}' to Drive: {e}")
        return None

    finally:
        # Guarantee byte buffer deallocation
        if stream_buffer:
            try:
                stream_buffer.close()
            except Exception:
                pass
            del stream_buffer