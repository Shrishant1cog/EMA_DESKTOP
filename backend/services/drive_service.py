import io
import logging
from typing import Optional, Dict, Any
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

FOLDER_NAME = "Calendar Evidence Attachments"

def get_or_create_evidence_folder(drive_service) -> Optional[str]:
    """Finds or creates a dedicated evidence folder in Drive."""
    try:
        query = f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = results.get('files', [])
        if files:
            return files[0]['id']

        folder_metadata = {
            'name': FOLDER_NAME,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')
    except Exception as e:
        logger.warning(f"Could not initialize folder in Drive: {e}. Defaulting to root.")
        return None

def upload_evidence_to_drive(
    drive_service,
    filename: str,
    file_bytes: bytes,
    mime_type: str = "application/octet-stream"
) -> Optional[Dict[str, Any]]:
    """Uploads file bytes to Drive, grants read access, and returns a verified URL."""
    try:
        folder_id = get_or_create_evidence_folder(drive_service)

        file_metadata: Dict[str, Any] = {'name': filename}
        if folder_id:
            file_metadata['parents'] = [folder_id]

        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
        uploaded = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name, mimeType, webViewLink, webContentLink'
        ).execute()

        file_id = uploaded.get('id')

        # Generate universal Drive preview URL
        direct_url = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
        web_link = uploaded.get('webViewLink') or uploaded.get('webContentLink') or direct_url

        # Make file accessible to anyone with the link
        try:
            drive_service.permissions().create(
                fileId=file_id,
                body={'type': 'anyone', 'role': 'reader'},
                fields='id'
            ).execute()
        except Exception as perm_err:
            logger.warning(f"Could not set public permission on {filename}: {perm_err}")

        logger.info(f"Uploaded attachment '{filename}' to Drive: {web_link}")
        return {
            "id": file_id,
            "title": uploaded.get("name", filename),
            "filename": filename,
            "mime_type": uploaded.get("mimeType", mime_type),
            "url": web_link,
            "webViewLink": web_link,
            "alternateLink": web_link
        }
    except HttpError as e:
        logger.error(f"Google Drive upload error for '{filename}': {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error uploading '{filename}': {e}")
        return None