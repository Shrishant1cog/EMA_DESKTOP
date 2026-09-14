import email
import io
import logging
import re
import zipfile
from typing import List

from pypdf import PdfReader
from docx import Document
import icalendar

# Optional dependency imports
try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import xlrd
except ImportError:
    xlrd = None

try:
    from pptx import Presentation
except ImportError:
    Presentation = None

try:
    from striprtf.striprtf import rtf_to_text
except ImportError:
    rtf_to_text = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import extract_msg
except ImportError:
    extract_msg = None

from extractors.ocr_extractor import extract_text_from_image

logger = logging.getLogger(__name__)


def extract_from_pdf(file_bytes: bytes) -> str:
    """
    Robust PDF text extractor with automatic decryption and OCR fallback 
    for scanned tickets, invoices, and flyers.
    """
    extracted_pages: List[str] = []
    try:
        reader = PdfReader(io.BytesIO(file_bytes))

        # Handle password / encryption flags (e.g., standard empty passwords on bank slips/tickets)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                try:
                    reader.decrypt(b"")
                except Exception as dec_err:
                    logger.warning(f"Unable to decrypt PDF with default keys: {dec_err}")

        for idx, page in enumerate(reader.pages):
            page_text = ""
            try:
                page_text = page.extract_text(extraction_mode="layout") or page.extract_text() or ""
            except Exception as read_err:
                logger.debug(f"Direct text extraction failed on page {idx}: {read_err}")

            # If digital text exists, retain it
            if page_text.strip():
                extracted_pages.append(page_text.strip())
            else:
                # Scanned page fallback: extract and OCR embedded images
                ocr_fragments: List[str] = []
                try:
                    images = getattr(page, "images", [])
                    for img in images:
                        ocr_res = extract_text_from_image(img.data)
                        if ocr_res.strip():
                            ocr_fragments.append(ocr_res.strip())
                except Exception as ocr_err:
                    logger.debug(f"OCR scanning skipped on page {idx}: {ocr_err}")

                if ocr_fragments:
                    extracted_pages.append("\n".join(ocr_fragments))

    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        # Secondary fallback: Attempt OCR on the entire raw byte payload if it's an image-wrapped PDF
        try:
            raw_ocr = extract_text_from_image(file_bytes)
            if raw_ocr.strip():
                return raw_ocr.strip()
        except Exception:
            pass

    return "\n\n".join(extracted_pages).strip()


def extract_from_docx(file_bytes: bytes) -> str:
    try:
        doc = Document(io.BytesIO(file_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        tables_text = []
        for table in doc.tables:
            for row in table.rows:
                row_str = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_str:
                    tables_text.append(row_str)
        return "\n".join(paragraphs + tables_text).strip()
    except Exception as e:
        logger.error(f"DOCX extraction error: {e}")
        return ""


def extract_from_excel(file_bytes: bytes, filename: str) -> str:
    rows_text: List[str] = []
    lower = filename.lower()

    if (lower.endswith(".xlsx") or lower.endswith(".xlsm")) and openpyxl:
        try:
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            for sheet in wb.worksheets:
                rows_text.append(f"[Sheet: {sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    filtered = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                    if filtered:
                        rows_text.append(" | ".join(filtered))
            return "\n".join(rows_text).strip()
        except Exception as e:
            logger.error(f"OpenPyXL extraction error: {e}")

    elif lower.endswith(".xls") and xlrd:
        try:
            wb = xlrd.open_workbook(file_contents=file_bytes)
            for sheet in wb.sheets():
                rows_text.append(f"[Sheet: {sheet.name}]")
                for row_idx in range(sheet.nrows):
                    row_vals = [str(sheet.cell_value(row_idx, c)).strip() for c in range(sheet.ncols)]
                    filtered = [v for v in row_vals if v]
                    if filtered:
                        rows_text.append(" | ".join(filtered))
            return "\n".join(rows_text).strip()
        except Exception as e:
            logger.error(f"XLRD extraction error: {e}")

    return ""


def extract_from_pptx(file_bytes: bytes) -> str:
    if not Presentation:
        return ""
    try:
        prs = Presentation(io.BytesIO(file_bytes))
        slides_text: List[str] = []
        for idx, slide in enumerate(prs.slides, start=1):
            slide_content = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        if paragraph.text.strip():
                            slide_content.append(paragraph.text.strip())
            if slide_content:
                slides_text.append(f"[Slide {idx}]\n" + "\n".join(slide_content))
        return "\n\n".join(slides_text).strip()
    except Exception as e:
        logger.error(f"PPTX extraction error: {e}")
        return ""


def extract_from_rtf(file_bytes: bytes) -> str:
    if not rtf_to_text:
        return ""
    try:
        raw = file_bytes.decode("utf-8", errors="ignore")
        return rtf_to_text(raw).strip()
    except Exception as e:
        logger.error(f"RTF extraction error: {e}")
        return ""


def extract_from_html(file_bytes: bytes) -> str:
    if not BeautifulSoup:
        clean = re.sub(r'<[^>]+>', ' ', file_bytes.decode("utf-8", errors="ignore"))
        return re.sub(r'\s+', ' ', clean).strip()
    try:
        soup = BeautifulSoup(file_bytes, "html.parser")
        for tag in soup(["script", "style", "meta", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except Exception as e:
        logger.error(f"HTML extraction error: {e}")
        return ""


def extract_from_ics(file_bytes: bytes) -> str:
    try:
        cal = icalendar.Calendar.from_ical(file_bytes)
        details = []
        for component in cal.walk():
            if component.name == "VEVENT":
                summary = component.get('summary', '')
                description = component.get('description', '')
                start = component.get('dtstart')
                end = component.get('dtend')
                location = component.get('location', '')
                details.append(
                    f"Event: {summary}\nStart: {start.dt if start else ''}\n"
                    f"End: {end.dt if end else ''}\nLocation: {location}\nDetails: {description}"
                )
        return "\n---\n".join(details)
    except Exception as e:
        logger.error(f"ICS extraction error: {e}")
        return ""


def extract_from_msg(file_bytes: bytes) -> str:
    if not extract_msg:
        return ""
    try:
        msg = extract_msg.Message(io.BytesIO(file_bytes))
        return f"Subject: {msg.subject}\nFrom: {msg.sender}\nDate: {msg.date}\n\n{msg.body}".strip()
    except Exception as e:
        logger.error(f"MSG extraction error: {e}")
        return ""


def extract_from_eml(file_bytes: bytes) -> str:
    try:
        msg = email.message_from_bytes(file_bytes)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore") + "\n"
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        return f"Subject: {msg.get('subject')}\nDate: {msg.get('date')}\n\n{body}".strip()
    except Exception as e:
        logger.error(f"EML extraction error: {e}")
        return ""


def extract_from_zip(file_bytes: bytes, max_depth: int = 5) -> str:
    extracted_docs = []
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            for fname in z.namelist()[:max_depth]:
                if fname.startswith("__MACOSX") or fname.endswith("/"):
                    continue
                nested_bytes = z.read(fname)
                nested_text = extract_attachment_text(fname, nested_bytes)
                if nested_text.strip():
                    extracted_docs.append(f"--- File inside archive: {fname} ---\n{nested_text.strip()}")
        return "\n\n".join(extracted_docs)
    except Exception as e:
        logger.error(f"ZIP extraction error: {e}")
        return ""


def extract_attachment_text(filename: str, file_bytes: bytes, mime_type: str = "") -> str:
    """Universal attachment router with raw byte fallbacks."""
    if not filename or not file_bytes:
        return ""

    lower = filename.lower()

    if lower.endswith(".pdf"):
        return extract_from_pdf(file_bytes)
    elif lower.endswith(".docx"):
        return extract_from_docx(file_bytes)
    elif lower.endswith(".rtf"):
        return extract_from_rtf(file_bytes)
    elif lower.endswith((".xlsx", ".xlsm", ".xls")):
        return extract_from_excel(file_bytes, filename)
    elif lower.endswith((".pptx", ".ppt")):
        return extract_from_pptx(file_bytes)
    elif lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif")):
        return extract_text_from_image(file_bytes)
    elif lower.endswith(".ics"):
        return extract_from_ics(file_bytes)
    elif lower.endswith(".msg"):
        return extract_from_msg(file_bytes)
    elif lower.endswith(".eml"):
        return extract_from_eml(file_bytes)
    elif lower.endswith((".html", ".htm", ".xhtml", ".xml")):
        return extract_from_html(file_bytes)
    elif lower.endswith((".zip", ".jar")):
        return extract_from_zip(file_bytes)
    elif lower.endswith((".txt", ".csv", ".tsv", ".json", ".md", ".yaml", ".yml", ".log")):
        try:
            return file_bytes.decode("utf-8", errors="ignore").strip()
        except Exception:
            return file_bytes.decode("latin-1", errors="ignore").strip()

    # Plain text byte inspection fallback
    if b"\x00" not in file_bytes[:1024]:
        try:
            decoded = file_bytes.decode("utf-8", errors="ignore").strip()
            if len(decoded) > 10:
                return decoded
        except Exception:
            pass

    return ""