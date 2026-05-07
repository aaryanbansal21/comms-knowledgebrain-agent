"""
extract.py — pull readable text out of files and URLs.

Supports: pdf, docx, md, txt, html, eml-as-text. URL fetching uses
trafilatura (preferred) and falls back to BeautifulSoup readability.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


def extract_file(path: str | Path) -> tuple[str, str]:
    """Returns (title, text). Title defaults to the file stem."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)

    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return p.stem, _extract_pdf(p)
    if suffix == ".docx":
        return p.stem, _extract_docx(p)
    if suffix in {".md", ".markdown", ".txt", ".rst"}:
        return p.stem, p.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        return p.stem, _extract_html(p.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".json":
        # Treat as plain text. Email JSONs go through ingest_email instead.
        return p.stem, p.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Unsupported file type: {suffix} (path: {p})")


def extract_url(url: str) -> tuple[str, str]:
    """Returns (title, text)."""
    try:
        import trafilatura  # type: ignore
    except ImportError:
        return _extract_url_fallback(url)

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise RuntimeError(f"Could not fetch URL: {url}")
    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    ) or ""
    meta = trafilatura.extract_metadata(downloaded)
    title = (meta.title if meta and meta.title else url) if meta else url
    return title, text


def _extract_pdf(path: Path) -> str:
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        # Fall back to pypdf
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            print("[extract] install pdfplumber or pypdf for PDF support",
                  file=sys.stderr)
            raise
        reader = PdfReader(str(path))
        return "\n\n".join(
            (page.extract_text() or "") for page in reader.pages
        )
    parts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
    return "\n\n".join(parts)


def _extract_docx(path: Path) -> str:
    try:
        import docx  # type: ignore  # python-docx
    except ImportError:
        print("[extract] install python-docx for .docx support",
              file=sys.stderr)
        raise
    d = docx.Document(str(path))
    return "\n\n".join(p.text for p in d.paragraphs if p.text.strip())


def _extract_html(html: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _extract_url_fallback(url: str) -> tuple[str, str]:
    try:
        import urllib.request
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        raise RuntimeError(
            "Need either trafilatura, or beautifulsoup4 + urllib, for URL extraction."
        )
    req = urllib.request.Request(url, headers={"User-Agent": "hourglass-kb/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    return title, soup.get_text(separator="\n", strip=True)


def extract_email_thread(email_json: dict) -> tuple[str, str]:
    """
    Render an email thread JSON into a single text blob plus a title.
    Schema: {thread_id, subject, participants[], messages: [{from, date, body}]}
    """
    subject = email_json.get("subject", "(no subject)")
    parts = [f"Subject: {subject}"]
    participants = email_json.get("participants", [])
    if participants:
        parts.append("Participants: " + ", ".join(participants))
    parts.append("")
    for m in email_json.get("messages", []):
        parts.append(f"From: {m.get('from','?')}")
        parts.append(f"Date: {m.get('date','?')}")
        parts.append("")
        parts.append(m.get("body", "").strip())
        parts.append("\n---\n")
    return subject, "\n".join(parts).strip()
