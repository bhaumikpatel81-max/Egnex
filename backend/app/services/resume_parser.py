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
