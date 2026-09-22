import base64
import logging
from io import BytesIO
from PIL import Image
import requests
from groq import Groq

import config

logger = logging.getLogger("CloudOCRExtractor")


def _ocr_space_cloud_fallback(image_bytes: bytes) -> str:
    """Free cloud-based OCR fallback requiring zero local CPU/RAM load."""
    try:
        b64_data = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "base64Image": f"data:image/jpeg;base64,{b64_data}",
            "language": "eng",
            "isOverlayRequired": False,
            "OCREngine": 2,
        }
        headers = {"apikey": "helloworld"}
        response = requests.post(
            "https://api.ocr.space/parse/image",
            data=payload,
            headers=headers,
            timeout=20
        )
        data = response.json()
        if data.get("ParsedResults"):
            return data["ParsedResults"][0].get("ParsedText", "").strip()
    except Exception as err:
        logger.warning(f"Cloud OCR fallback exception: {err}")
    return ""


def extract_text_from_image(image_bytes: bytes) -> str:
    """Extracts text from images entirely in the cloud with zero local machine load."""
    if not image_bytes:
        return ""

    raw_keys = getattr(config, "GROQ_API_KEYS", [])
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    keys = [k.strip() for k in raw_keys if isinstance(k, str) and k.strip()]

    if not keys and getattr(config, "GROQ_API_KEY", ""):
        keys = [config.GROQ_API_KEY.strip()]

    # Resize in-memory to limit payload size
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            max_dimension = 1200
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=80)
            clean_bytes = buffer.getvalue()
            buffer.close()
    except Exception as prep_err:
        logger.warning(f"Could not prepare image buffer: {prep_err}")
        clean_bytes = image_bytes

    # Attempt dynamic Groq Vision if an active vision model is available on Groq
    if keys:
        b64_image = base64.b64encode(clean_bytes).decode("utf-8")
        image_url = f"data:image/jpeg;base64,{b64_image}"

        for key in keys:
            try:
                client = Groq(api_key=key)
                active_models = [
                    m.id for m in client.models.list().data
                    if "vision" in m.id.lower()
                ]

                for model_name in active_models:
                    try:
                        res = client.chat.completions.create(
                            model=model_name,
                            messages=[
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Transcribe all visible text, dates, numbers, and meeting details accurately.",
                                        },
                                        {
                                            "type": "image_url",
                                            "image_url": {"url": image_url},
                                        },
                                    ],
                                }
                            ],
                            temperature=0.1,
                            max_tokens=1000,
                        )
                        text = res.choices[0].message.content.strip()
                        if text:
                            return text
                    except Exception:
                        continue
            except Exception:
                continue

    # Fallback to free Cloud OCR engine
    return _ocr_space_cloud_fallback(clean_bytes)


# Backwards compatibility alias
extract_text_from_image_cloud = extract_text_from_image