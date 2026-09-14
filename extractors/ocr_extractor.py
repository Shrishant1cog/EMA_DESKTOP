import io
import logging
import os
from PIL import Image
import pytesseract
from config import TESSERACT_CMD

logger = logging.getLogger(__name__)

# Configure Tesseract binary path if set in .env
if TESSERACT_CMD and os.path.exists(TESSERACT_CMD):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

def extract_text_from_image(image_bytes: bytes) -> str:
    """Extracts raw text from image bytes using OCR."""
    if not image_bytes:
        return ""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # Convert to RGB if palette or RGBA
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        text = pytesseract.image_to_string(img)
        return text.strip()
    except Exception as e:
        logger.warning(f"OCR extraction failed: {e}")
        return ""