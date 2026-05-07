"""
triage.py — score and categorize a batch of incoming messages.

Reads JSON from a file or stdin. Each item is a normalized message:

    {
      "id": "<mcp-message-id>",
      "channel": "gmail" | "outlook" | "slack" | "teams",
      "from": "<display name or address>",
      "from_id": "<unique sender id>",
      "to": ["..."],
      "subject": "...",            // emails only
      "snippet": "...",            // first ~300 chars
      "received_at": 1714831200,   // epoch seconds
      "thread_id": "...",
      "is_dm": true | false,       // Slack/Teams only
      "has_question": null,        // optional, pre-classified by caller
      "labels": ["..."]            // any pre-existing labels/tags
    }

Output is the same JSON with `triage` added to each item:

    "triage": {
        "category": "respond_now" | "respond_today" | "read_only" |
                    "delegate" | "archive",
        "score": 0..100,
        "reasons": ["..."]
    }

The agent (Claude) is expected to read this output and present it to the user
in a ranked, human-friendly summary. Final decisions about what to act on
remain the user's.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get(
        "COMMS_CONFIG_PATH",
        str(Path.home() / ".hourglass" / "comms_config.json"),
    )
)

DEFAULT_CONFIG = {
    "high_signal_senders": [],   # exact match on from_id or from
    "low_signal_senders": [],    # newsletters, automated alerts
    "high_signal_tags": ["@me", "blocker", "urgent"],
    "auto_archive_subjects": [
        r"^\[no-?reply\]",
        r"newsletter",
        r"weekly digest",
        r"^automatic reply",
    ],
    "respond_today_window_hours": 24,
}

URGENT_PHRASES = (
    "urgent", "asap", "blocker", "blocking", "by eod", "by end of day",
    "today", "right now", "deadline", "as soon as possible",
)
QUESTION_RE = re.compile(r"\?")


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def score_message(msg: dict, cfg: dict, now: float) -> dict:
    score = 30
    reasons: list[str] = []
    sender = (msg.get("from_id") or msg.get("from") or "").lower()
    subject = (msg.get("subject") or "").lower()
    snippet = (msg.get("snippet") or "").lower()
    body_blob = subject + "\n" + snippet
    age_hours = max(0.0, (now - float(msg.get("received_at") or now)) / 3600)

    # Sender signal
    if any(s.lower() in sender for s in cfg["high_signal_senders"]):
        score += 25
        reasons.append("high-signal sender")
    if any(s.lower() in sender for s in cfg["low_signal_senders"]):
        score -= 20
        reasons.append("low-signal sender")

    # DMs always weigh a bit higher than channel posts
    if msg.get("is_dm"):
        score += 10
        reasons.append("direct message")

    # Direct addressing of the user
    labels = [l.lower() for l in (msg.get("labels") or [])]
    if any(t.lower() in labels for t in cfg["high_signal_tags"]):
        score += 15
        reasons.append("tagged as high signal")

    # Urgency language
    if any(p in body_blob for p in URGENT_PHRASES):
        score += 20
        reasons.append("urgency language")

    # Has a question mark
    if QUESTION_RE.search(snippet):
        score += 10
        reasons.append("contains a question")

    # Auto-archive candidates
    if _matches_any(subject, cfg["auto_archive_subjects"]):
        score -= 30
        reasons.append("matches auto-archive pattern")

    # Recency
    if age_hours < 2:
        score += 5
    elif age_hours > 48:
        score -= 10

    # Categorize
    if score >= 70:
        category = "respond_now"
    elif score >= 50:
        category = "respond_today"
    elif score >= 30:
        category = "read_only"
    elif score >= 15:
        category = "archive"
    else:
        category = "archive"

    # If sender is explicitly high-signal AND the snippet has a direct ask,
    # bump even low-scoring items to respond_today.
    if "high-signal sender" in reasons and "contains a question" in reasons:
        category = max(
            category,
            "respond_today",
            key=lambda c: ["archive", "read_only", "respond_today",
                           "delegate", "respond_now"].index(c),
        )

    return {
        "category": category,
        "score": max(0, min(100, score)),
        "reasons": reasons,
    }


def triage(items: list[dict]) -> list[dict]:
    cfg = _load_config()
    now = time.time()
    out: list[dict] = []
    for m in items:
        m = dict(m)
        m["triage"] = score_message(m, cfg, now)
        out.append(m)
    out.sort(key=lambda m: m["triage"]["score"], reverse=True)
    return out


def _cli():
    parser = argparse.ArgumentParser(description="Triage a batch of messages")
    parser.add_argument(
        "input",
        nargs="?",
        help="path to JSON file (a list of messages); reads stdin if omitted",
    )
    args = parser.parse_args()

    if args.input:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        data = json.loads(sys.stdin.read())

    if not isinstance(data, list):
        print("input must be a JSON list of messages", file=sys.stderr)
        sys.exit(2)

    out = triage(data)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
