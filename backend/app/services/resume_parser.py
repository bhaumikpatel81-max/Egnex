"""
Resume text extraction.

Supported now:  PDF (.pdf), Word (.docx / .doc)
Scaffolded:     Image OCR (.jpg/.jpeg/.png) — hook is here, implementation deferred
Stubbed:        Job-board import (Naukri/LinkedIn) — needs paid API access
"""
import io
import os


def extract_text_from_pdf(file_bytes: bytes) -> str:
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    return "\n".join(
        page.extract_text() or "" for page in reader.pages
    ).strip()


def extract_text_from_docx(file_bytes: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(file_bytes))
    return "\n".join(
        para.text for para in doc.paragraphs if para.text.strip()
    )


def extract_text_from_image(file_bytes: bytes) -> str:
    # OCR hook — deferred to a later sub-step.
    # Requires pytesseract + Tesseract binary, or GCP Vision API.
    raise NotImplementedError(
        "Image OCR is not yet implemented. "
        "Upload a PDF or Word document instead."
    )


def import_from_jobboard(source: str, profile_url: str) -> dict:
    # Naukri / LinkedIn import stub.
    # Needs a paid API agreement or partnership with the job board.
    raise NotImplementedError(
        f"Import from '{source}' is not yet supported. "
        "This requires a paid API agreement with the job board."
    )


def extract_contact_info(text: str) -> dict:
    """
    Heuristic extraction of name, email, and phone from raw resume text.
    Used to pre-fill the submit-application form.
    """
    import re

    # Email — reliable
    email_match = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}', text)
    email = email_match.group().lower() if email_match else None

    # Phone — 10-digit Indian mobile or international with country code
    phone_match = re.search(
        r'(?:\+91[\s\-]?)?[6-9]\d{9}|(?:\+\d{1,3}[\s\-]?)?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}',
        text,
    )
    phone = re.sub(r'\s+', '', phone_match.group()) if phone_match else None

    # Name — look for the first short line (2-5 words, all letters/spaces, no numbers)
    name = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 60:
            continue
        # Skip lines that look like section headers or contain numbers/special chars
        if re.search(r'[\d@#/\\|<>{}]', stripped):
            continue
        words = stripped.split()
        if 2 <= len(words) <= 5 and all(re.match(r"[A-Za-z'\-\.]+$", w) for w in words):
            name = stripped.title()
            break

    return {"full_name": name, "email": email, "phone": phone}


def extract_text(file_bytes: bytes, filename: str) -> tuple:
    """
    Extract resume text from an uploaded file.
    Returns (text: str, warning: str).
    `warning` is empty on clean success; non-empty if extraction was partial or
    the file type fell through to a stub.
    """
    suffix = os.path.splitext(filename)[1].lower()

    if suffix == ".pdf":
        try:
            text = extract_text_from_pdf(file_bytes)
            if not text:
                return "", "PDF parsed but no text found — it may be a scanned/image-only PDF."
            return text, ""
        except Exception as exc:
            return "", f"PDF parsing error: {exc}"

    if suffix in (".docx", ".doc"):
        try:
            text = extract_text_from_docx(file_bytes)
            if not text:
                return "", "Word document parsed but contained no readable text."
            return text, ""
        except Exception as exc:
            return "", f"Word document parsing error: {exc}"

    if suffix in (".jpg", ".jpeg", ".png"):
        # File is saved for future OCR processing; screening runs with empty text.
        return "", (
            "Image resumes are not yet OCR-processed. "
            "Application submitted — resume text will be empty until OCR is implemented."
        )

    return "", f"Unsupported file type '{suffix}'."
