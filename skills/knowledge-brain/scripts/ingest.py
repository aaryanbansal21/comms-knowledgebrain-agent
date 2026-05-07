"""
ingest.py — unified entry point for adding things to the knowledge brain.

Subcommands:
    file <path>           ingest a single PDF/docx/md/txt/html file
    dir  <path>           recursively ingest a folder
    url  <url>            fetch + extract a web page
    email <path-to-json>  ingest an email thread (schema in SKILL.md)
    note "<text>"         ingest a short fact / preference / decision

Common flags:
    --tags a,b,c          comma-separated tags applied to the source
    --title "..."         override the auto-detected title
    --quiet               only emit JSON

Exit codes:
    0   success
    2   missing dependency or env var
    3   nothing ingested (e.g. URL returned empty body)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

# Make sibling scripts importable when run directly
sys.path.insert(0, str(Path(__file__).parent))

from chunking import chunk_text, content_hash  # noqa: E402
from extract import (  # noqa: E402
    extract_email_thread,
    extract_file,
    extract_url,
)
from kb_store import Chunk, KBStore, Source  # noqa: E402

SUPPORTED_FILE_SUFFIXES = {
    ".pdf", ".docx", ".md", ".markdown", ".txt", ".rst", ".html", ".htm",
}


def _build_chunks(source_id: str, text: str, now: float) -> list[Chunk]:
    chunks: list[Chunk] = []
    for i, ct in enumerate(chunk_text(text)):
        chunks.append(
            Chunk(
                chunk_id=str(uuid.uuid4()),
                source_id=source_id,
                text=ct,
                content_hash=content_hash(ct),
                position=i,
                ingested_at=now,
            )
        )
    return chunks


def _parse_tags(s: str | None) -> list[str]:
    if not s:
        return []
    return [t.strip() for t in s.split(",") if t.strip()]


def ingest_file(path: str, tags: list[str], title: str | None) -> dict:
    p = Path(path).expanduser().resolve()
    detected_title, text = extract_file(p)
    if not text.strip():
        return {"ok": False, "reason": "empty extraction", "path": str(p)}

    now = time.time()
    source = Source(
        source_id=f"file:{hashlib.sha256(str(p).encode()).hexdigest()[:16]}",
        type="file",
        title=title or detected_title,
        location=str(p),
        ingested_at=now,
        last_verified_at=now,
        tags=tags,
        extra={"size_bytes": p.stat().st_size, "suffix": p.suffix.lower()},
    )
    chunks = _build_chunks(source.source_id, text, now)
    return KBStore().upsert_source(source, chunks)


def ingest_dir(path: str, tags: list[str]) -> list[dict]:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return [{"ok": False, "reason": "not a directory", "path": str(p)}]
    results: list[dict] = []
    for f in sorted(p.rglob("*")):
        if f.is_file() and f.suffix.lower() in SUPPORTED_FILE_SUFFIXES:
            try:
                results.append({"path": str(f), **ingest_file(str(f), tags, None)})
            except Exception as e:
                results.append({"ok": False, "path": str(f), "error": str(e)})
    return results


def ingest_url(url: str, tags: list[str], title: str | None) -> dict:
    detected_title, text = extract_url(url)
    if not text.strip():
        return {"ok": False, "reason": "empty extraction", "url": url}

    now = time.time()
    source = Source(
        source_id=f"url:{hashlib.sha256(url.encode()).hexdigest()[:16]}",
        type="url",
        title=title or detected_title,
        location=url,
        ingested_at=now,
        last_verified_at=now,
        tags=tags,
        extra={"fetched_at": now},
    )
    chunks = _build_chunks(source.source_id, text, now)
    return KBStore().upsert_source(source, chunks)


def ingest_email(json_path: str, tags: list[str], title: str | None) -> dict:
    p = Path(json_path).expanduser().resolve()
    data = json.loads(p.read_text(encoding="utf-8"))
    detected_title, text = extract_email_thread(data)

    now = time.time()
    thread_id = data.get("thread_id") or f"email:{p.stem}"
    source = Source(
        source_id=thread_id if thread_id.startswith("email:")
                  else f"email:{thread_id}",
        type="email",
        title=title or detected_title,
        location=thread_id,
        ingested_at=now,
        last_verified_at=now,
        tags=tags + ["email"],
        extra={
            "participants": data.get("participants", []),
            "n_messages": len(data.get("messages", [])),
        },
    )
    chunks = _build_chunks(source.source_id, text, now)
    return KBStore().upsert_source(source, chunks)


def ingest_note(text: str, tags: list[str], title: str | None) -> dict:
    text = text.strip()
    if not text:
        return {"ok": False, "reason": "empty note"}
    now = time.time()
    note_id = f"note:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    source = Source(
        source_id=note_id,
        type="note",
        title=title or text[:60].replace("\n", " "),
        location="inline",
        ingested_at=now,
        last_verified_at=now,
        tags=tags,
        extra={},
    )
    chunks = _build_chunks(source.source_id, text, now)
    return KBStore().upsert_source(source, chunks)


def _cli():
    parser = argparse.ArgumentParser(description="Ingest into the knowledge brain")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tags", default="")
    common.add_argument("--title", default=None)
    common.add_argument("--quiet", action="store_true")

    p_file = sub.add_parser("file", parents=[common])
    p_file.add_argument("path")

    p_dir = sub.add_parser("dir", parents=[common])
    p_dir.add_argument("path")

    p_url = sub.add_parser("url", parents=[common])
    p_url.add_argument("url")

    p_em = sub.add_parser("email", parents=[common])
    p_em.add_argument("json_path")

    p_note = sub.add_parser("note", parents=[common])
    p_note.add_argument("text")

    args = parser.parse_args()
    tags = _parse_tags(args.tags)
    title = args.title

    if args.cmd == "file":
        out = ingest_file(args.path, tags, title)
    elif args.cmd == "dir":
        out = ingest_dir(args.path, tags)
    elif args.cmd == "url":
        out = ingest_url(args.url, tags, title)
    elif args.cmd == "email":
        out = ingest_email(args.json_path, tags, title)
    elif args.cmd == "note":
        out = ingest_note(args.text, tags, title)
    else:
        parser.error("unknown command")
        return

    print(json.dumps(out, indent=2 if not args.quiet else None, default=str))

    if isinstance(out, dict) and out.get("ok") is False:
        sys.exit(3)


if __name__ == "__main__":
    _cli()
