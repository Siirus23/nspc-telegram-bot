import os
import re
from PIL import Image
from io import BytesIO

# Default OFF on Render. Turn ON only when you have Tesseract installed.
OCR_ENABLED = os.getenv("OCR_ENABLED", "0") == "1"

# Only import pytesseract when OCR is enabled (prevents Render crash)
if OCR_ENABLED:
    try:
        import pytesseract
    except ImportError:
        pytesseract = None
else:
    pytesseract = None


async def extract_text_from_photo(bot, message):
    """
    Downloads the photo sent by admin and runs OCR on it.
    If OCR is disabled/unavailable, returns empty string safely.
    """
    if not OCR_ENABLED:
        return ""

    if pytesseract is None:
        # pytesseract not installed in this environment
        return ""

    try:
        # Get the highest resolution photo
        photo = message.photo[-1]

        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)

        image = Image.open(BytesIO(file_bytes.getvalue()))

        # Convert to grayscale for better OCR
        image = image.convert("L")

        # Increase contrast
        image = image.point(lambda x: 0 if x < 140 else 255)

        text = pytesseract.image_to_string(image)
        return text

    except Exception as e:
        print("OCR Error:", e)
        return ""


def extract_tracking_number(text: str):
    """
    Extracts SingPost tracking number safely from OCR text
    with validation and normalization
    """
    if not text:
        return None

    # Normalize OCR common mistakes
    clean = text.upper()
    clean = clean.replace(" ", "")
    clean = clean.replace("O", "0")   # letter O -> zero
    clean = clean.replace("I", "1")   # letter I -> one
    clean = clean.replace("S", "5")   # S -> 5 (sometimes)

    pattern = re.compile(r"[A-Z]{2}\d{9}SG")
    match = pattern.search(clean)

    if not match:
        return None

    tracking = match.group(0)

    # Extra safety check
    if len(tracking) != 13:
        return None

    return tracking