"""
action_items.py — extract action items, commitments, and deadlines from a
message thread and (optionally) auto-ingest each into the knowledge brain.

Usage
-----
    python3 action_items.py extract <thread.json>
        Print the extracted action items as JSON. Does NOT ingest.

    python3 action_items.py extract <thread.json> --ingest
        Extract AND ingest each item into the brain as a `note` with tags
        action-item, channel:<channel>, from:<sender>, with:<participant>...
        and (when a deadline phrase is detected) deadline:<phrase>.

Thread JSON shape: same as draft.py prepare's input (see comms-agent
REFERENCE.md). Minimum useful fields: id, channel, subject, messages[].

Output JSON shape:

    {
      "thread_id": "...",
      "subject": "...",
      "items": [
        {
          "text": "send Q3 forecast slides",
          "owner": "user" | "sender" | "unknown",
          "deadline_phrase": "Friday" | null,
          "source_from": "alice@x.com",
          "source_date": "2026-05-07T14:00:00Z",
          "match_kind": "request" | "commitment" | "deadline" | "direct_question"
        }
      ],
      "ingested": [<source_id>, ...]   // present if --ingest passed
    }

Design notes
------------
The extractor is regex-driven on purpose. We do NOT want this script to
make LLM calls — Claude is the LLM. The script's job is to surface
candidates that Claude can confirm/expand when needed. Conservative beats
chatty: we'd rather miss a soft "we should think about X" than ingest a
hundred non-actionable phrases.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

THIS = Path(__file__).resolve().parent
KB_SCRIPTS = (THIS.parent.parent / "knowledge-brain" / "scripts").resolve()


# Patterns where the *sender* is asking the *recipient* (typically the user)
# to do something. Each pattern captures the verb-phrase that follows.
REQUEST_PATTERNS = [
    re.compile(r"\b(?:could|can|would|will)\s+you\s+([^?.!\n]{4,200})", re.I),
    re.compile(r"\bplease\s+([a-z][^.!?\n]{3,200})", re.I),
    re.compile(r"\b(?:do you mind|any chance you could|are you able to)\s+([^?.!\n]{4,200})", re.I),
    re.compile(r"\bwe (?:need|have) to\s+([^.!?\n]{4,200})", re.I),
    re.compile(r"\blet'?s\s+([a-z][^.!?\n]{3,200})", re.I),
]

# Patterns where the *sender* is committing to do something themselves.
COMMITMENT_PATTERNS = [
    re.compile(r"\bi(?:'ll| will| am going to| plan to| intend to)\s+([^.!?\n]{4,200})", re.I),
    re.compile(r"\blet me\s+([a-z][^.!?\n]{3,200})", re.I),
]

# Deadline language. Captured separately and stitched onto a nearby item.
DEADLINE_PATTERNS = [
    re.compile(
        r"\b(?:by|before|due|no later than)\s+"
        r"("
        r"today|tomorrow|tonight|"
        r"(?:next |this )?(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|"
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?|"
        r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
        r"\d{4}-\d{2}-\d{2}|"
        r"end of (?:day|week|month|quarter)|"
        r"eo[dwmqy]|"
        r"next week"
        r")",
        re.I,
    ),
]

# Strong stand-alone deadline tokens worth surfacing even without a verb.
STANDALONE_DEADLINE = re.compile(
    r"\b(?:deadline|due date|cut[- ]?off)\b[^.!?\n]{0,80}", re.I
)

# Phrases that almost always mark a thread as non-actionable and we should
# skip even if a regex above hit. Keep this list tight.
NEGATION_NEAR = (
    "no need to", "you don't have to", "we don't need to", "ignore this",
    "this is just fyi", "no action required", "nothing required from you",
)


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _is_user_recipient(thread: dict, msg: dict) -> bool:
    """
    Best-effort: returns True if the message is addressed to the user (not
    by the user). The schema doesn't always include `to`, so when in doubt
    we assume the user is a recipient — Claude can override.
    """
    sender = (msg.get("from") or "").lower()
    user_marker = (thread.get("user_email") or "").lower()
    if user_marker and user_marker in sender:
        return False
    return True


def _find_deadline_near(line: str) -> str | None:
    for pat in DEADLINE_PATTERNS:
        m = pat.search(line)
        if m:
            return _strip(m.group(1))
    m = STANDALONE_DEADLINE.search(line)
    if m:
        return _strip(m.group(0))
    return None


def _split_sentences(body: str) -> list[str]:
    body = body or ""
    body = re.sub(r"\r", "", body)
    parts = re.split(r"(?<=[.!?])\s+|\n+", body)
    return [_strip(p) for p in parts if _strip(p)]


def _has_negation(line: str) -> bool:
    low = line.lower()
    return any(n in low for n in NEGATION_NEAR)


def _classify_question(line: str, addressed_to_user: bool) -> dict | None:
    if "?" not in line:
        return None
    if any(line.lower().startswith(s) for s in (
        "fyi ", "btw ", "cool ", "great ", "thanks ", "thank you")):
        return None
    return {
        "text": line.rstrip("?"),
        "owner": "user" if addressed_to_user else "sender",
        "match_kind": "direct_question",
    }


def extract_from_message(msg: dict, thread: dict) -> list[dict]:
    body = msg.get("body") or ""
    if not body.strip():
        return []
    addressed_to_user = _is_user_recipient(thread, msg)
    out: list[dict] = []
    seen_texts: set[str] = set()

    for line in _split_sentences(body):
        if _has_negation(line):
            continue

        deadline = _find_deadline_near(line)
        line_matched = False

        for pat in REQUEST_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            text = _strip(m.group(1))
            if not text or text.lower() in seen_texts:
                continue
            seen_texts.add(text.lower())
            line_matched = True
            out.append({
                "text": text,
                "owner": "user" if addressed_to_user else "sender",
                "deadline_phrase": deadline,
                "source_from": msg.get("from"),
                "source_date": msg.get("date"),
                "match_kind": "request",
            })

        for pat in COMMITMENT_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            text = _strip(m.group(1))
            if not text or text.lower() in seen_texts:
                continue
            seen_texts.add(text.lower())
            line_matched = True
            out.append({
                "text": text,
                "owner": "sender" if addressed_to_user else "user",
                "deadline_phrase": deadline,
                "source_from": msg.get("from"),
                "source_date": msg.get("date"),
                "match_kind": "commitment",
            })

        if line_matched:
            continue
        q = _classify_question(line, addressed_to_user)
        if q and q["text"].lower() not in seen_texts:
            seen_texts.add(q["text"].lower())
            q.update({
                "deadline_phrase": deadline,
                "source_from": msg.get("from"),
                "source_date": msg.get("date"),
            })
            out.append(q)

    return out


def extract(thread: dict) -> dict:
    items: list[dict] = []
    for msg in thread.get("messages", []) or []:
        items.extend(extract_from_message(msg, thread))
    return {
        "thread_id": thread.get("id"),
        "subject": thread.get("subject"),
        "channel": thread.get("channel"),
        "items": items,
    }


def _ingest(items: Iterable[dict], thread: dict) -> list[str]:
    """Ingest each item as a knowledge-brain note. Returns list of source_ids."""
    source_ids: list[str] = []
    channel = thread.get("channel") or "unknown"
    thread_id = thread.get("id") or "unknown"
    subject = thread.get("subject") or ""
    participants = thread.get("participants") or []

    for it in items:
        deadline = it.get("deadline_phrase")
        text_parts = [
            "[action-item]",
            f"OWNER:{it.get('owner','unknown')}",
            f"KIND:{it.get('match_kind','?')}",
            f"TASK: {it['text']}",
        ]
        if deadline:
            text_parts.append(f"DEADLINE: {deadline}")
        text_parts.append(
            f"SOURCE: {channel}/{thread_id}"
            + (f" — {subject}" if subject else "")
        )
        if it.get("source_from"):
            text_parts.append(f"FROM: {it['source_from']}")
        note_text = " | ".join(text_parts)

        tags = ["action-item", f"channel:{channel}"]
        if it.get("source_from"):
            tags.append(f"from:{it['source_from']}")
        for p in participants:
            tags.append(f"with:{p}")
        if deadline:
            tags.append(f"deadline:{deadline}")
        if it.get("owner"):
            tags.append(f"owner:{it['owner']}")

        proc = subprocess.run(
            [
                sys.executable, str(KB_SCRIPTS / "ingest.py"),
                "note", note_text,
                "--tags", ",".join(t for t in tags if t),
                "--quiet",
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            continue
        try:
            data = json.loads(proc.stdout)
            sid = data.get("source_id") or data.get("ingest", {}).get("source_id")
            if sid:
                source_ids.append(sid)
        except json.JSONDecodeError:
            pass
    return source_ids


def _cli():
    parser = argparse.ArgumentParser(
        description="Extract action items from a thread and (optionally) ingest them"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ext = sub.add_parser("extract")
    p_ext.add_argument("thread_path", help="path to thread JSON")
    p_ext.add_argument(
        "--ingest", action="store_true",
        help="ingest extracted items into the knowledge brain",
    )
    args = parser.parse_args()

    thread = json.loads(Path(args.thread_path).read_text(encoding="utf-8"))
    out = extract(thread)
    if args.ingest and out["items"]:
        out["ingested"] = _ingest(out["items"], thread)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
