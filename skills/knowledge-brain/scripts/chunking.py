"""
chunking.py — split long text into semantically coherent, embedding-sized chunks.

Strategy: a simple recursive splitter targeting ~800 chars / ~200 tokens with
~120 char overlap. Splits on paragraph → sentence → word boundaries.
Good enough for most prose; specialized chunkers can replace this later.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

DEFAULT_CHUNK_CHARS = 1200
DEFAULT_OVERLAP = 150
MIN_CHUNK_CHARS = 80


_PARA_BREAK = re.compile(r"\n\s*\n+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def chunk_text(
    text: str,
    target_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= target_chars:
        return [text]

    paras = [p.strip() for p in _PARA_BREAK.split(text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        if len(para) > target_chars:
            # split a giant paragraph by sentences
            for piece in _split_paragraph(para, target_chars):
                if len(buf) + len(piece) + 2 <= target_chars:
                    buf = (buf + "\n\n" + piece).strip() if buf else piece
                else:
                    if buf:
                        chunks.append(buf)
                    buf = piece
            continue

        if len(buf) + len(para) + 2 <= target_chars:
            buf = (buf + "\n\n" + para).strip() if buf else para
        else:
            if buf:
                chunks.append(buf)
            buf = para
    if buf:
        chunks.append(buf)

    # Add overlap between chunks for context continuity
    if overlap > 0 and len(chunks) > 1:
        out = [chunks[0]]
        for prev, cur in zip(chunks, chunks[1:]):
            tail = prev[-overlap:]
            out.append((tail + "\n\n" + cur).strip())
        chunks = out

    # Drop micro-chunks (rare, but happens at the end of files)
    chunks = [c for c in chunks if len(c) >= MIN_CHUNK_CHARS] or chunks
    return chunks


def _split_paragraph(text: str, target_chars: int) -> list[str]:
    sents = _SENTENCE.split(text)
    out: list[str] = []
    buf = ""
    for s in sents:
        if len(buf) + len(s) + 1 <= target_chars:
            buf = (buf + " " + s).strip() if buf else s
        else:
            if buf:
                out.append(buf)
            if len(s) > target_chars:
                # Hard split very long "sentences" (e.g. unbroken text dumps)
                for i in range(0, len(s), target_chars):
                    out.append(s[i : i + target_chars])
                buf = ""
            else:
                buf = s
    if buf:
        out.append(buf)
    return out


def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]
