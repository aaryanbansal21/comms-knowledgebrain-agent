"""
draft.py — produce a structured draft proposal for a single message thread.

This script does NOT call an LLM directly. Its job is to gather the right
context and emit a structured "draft request" JSON that the agent (Claude)
then turns into prose. Keeping the LLM call inside the agent loop means
the user sees and approves the final body in chat.

Usage
-----
    python3 draft.py prepare <thread_json_path>
        Reads a thread JSON, runs KB lookups for the most likely topics,
        and prints a JSON envelope with everything needed to write the reply.

    python3 draft.py log <outcome_json_path>
        Logs the executed action back to the knowledge brain via ingest.py
        (called as a subprocess) with structured tags.

Thread JSON schema (input to `prepare`):

    {
      "id": "<thread_id>",
      "channel": "gmail" | "outlook" | "slack" | "teams",
      "subject": "...",
      "participants": ["..."],
      "messages": [
        {"from": "...", "date": "...", "body": "..."}
      ],
      "user_intent": "free-text description of what the user wants the reply to do"
    }

Output of `prepare`:

    {
      "channel": "...",
      "thread_id": "...",
      "subject": "...",
      "participants": [...],
      "context_messages": [last N messages],
      "kb_context": [
         {"title": "...", "location": "...", "text": "...", "score": 0.81}
      ],
      "route_recommendation": "reply" | "reply_all" | "forward" | "archive" | "delegate",
      "reply_to": "<email/handle>",
      "warnings": ["..."]
    }
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

THIS = Path(__file__).resolve().parent
KB_SCRIPTS = (THIS.parent.parent / "knowledge-brain" / "scripts").resolve()


def _kb_query(question: str, k: int = 5) -> dict:
    """Call the knowledge-brain query.py via subprocess; filter `private`."""
    proc = subprocess.run(
        [
            sys.executable,
            str(KB_SCRIPTS / "query.py"),
            question,
            "--k",
            str(k),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"results": [], "error": proc.stderr.strip(), "confidence": "low"}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"results": [], "error": "kb returned non-JSON", "confidence": "low"}
    # filter private
    data["results"] = [
        r for r in data.get("results", [])
        if "private" not in (r.get("tags") or [])
    ]
    return data


def _last_n_messages(thread: dict, n: int = 6) -> list[dict]:
    msgs = thread.get("messages", [])
    return msgs[-n:]


def _topics_from_thread(thread: dict) -> list[str]:
    """Pick a few candidate KB queries from the thread."""
    subject = thread.get("subject") or ""
    intent = thread.get("user_intent") or ""
    # Take last message body as another query (often the most concrete ask).
    msgs = thread.get("messages") or []
    last_body = msgs[-1]["body"] if msgs else ""
    last_body = re.sub(r"\s+", " ", last_body).strip()[:300]

    topics = []
    if subject:
        topics.append(subject)
    if intent:
        topics.append(intent)
    if last_body:
        topics.append(last_body)
    # de-dupe while preserving order
    seen = set()
    uniq = []
    for t in topics:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq[:3]


def _detect_route(thread: dict) -> tuple[str, str]:
    """
    Returns (route, reply_to). Defaults to a single reply to the last sender.
    """
    msgs = thread.get("messages") or []
    if not msgs:
        return "reply", ""
    last = msgs[-1]
    sender = last.get("from", "")
    participants = thread.get("participants") or []
    # very loose heuristic: 3+ participants and the user not directly asked → reply_all
    if len(participants) >= 3 and "?" in (last.get("body") or ""):
        return "reply_all", sender
    return "reply", sender


def _warnings(thread: dict, kb_results: list[dict]) -> list[str]:
    warns: list[str] = []
    last_body = (thread.get("messages") or [{}])[-1].get("body", "").lower()
    if any(w in last_body for w in (
        "wire", "iban", "ssn", "social security", "credit card",
        "password", "api key", "secret",
    )):
        warns.append(
            "thread mentions sensitive info — do not include any "
            "credentials/financial data in the reply"
        )
    if not kb_results:
        warns.append("no KB context found; do not invent facts")
    return warns


def prepare(thread_path: str) -> dict:
    thread = json.loads(Path(thread_path).read_text(encoding="utf-8"))
    topics = _topics_from_thread(thread)
    kb_context: list[dict] = []
    for t in topics:
        res = _kb_query(t, k=4)
        for r in res.get("results", []):
            if r["score"] < 0.55:
                continue
            kb_context.append(
                {
                    "title": r.get("title"),
                    "location": r.get("location"),
                    "text": r.get("text"),
                    "score": r.get("score"),
                    "source_type": r.get("source_type"),
                    "source_id": r.get("source_id"),
                    "topic": t,
                }
            )
    # de-duplicate kb results by chunk text
    seen = set()
    deduped = []
    for r in kb_context:
        key = (r.get("source_id"), r.get("text", "")[:120])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    route, reply_to = _detect_route(thread)
    return {
        "channel": thread.get("channel"),
        "thread_id": thread.get("id"),
        "subject": thread.get("subject"),
        "participants": thread.get("participants"),
        "user_intent": thread.get("user_intent"),
        "context_messages": _last_n_messages(thread, n=6),
        "kb_context": deduped,
        "route_recommendation": route,
        "reply_to": reply_to,
        "warnings": _warnings(thread, deduped),
    }


def log_outcome(outcome_path: str) -> dict:
    """
    outcome JSON shape:
      {
        "channel": "gmail",
        "thread_id": "...",
        "action": "sent" | "archived" | "skipped" | "forwarded",
        "summary": "<one sentence about what was decided>",
        "participants": ["..."]
      }
    """
    outcome = json.loads(Path(outcome_path).read_text(encoding="utf-8"))
    summary = outcome.get("summary") or ""
    if not summary:
        return {"ok": False, "reason": "summary required"}
    note = (
        f"[comms-outcome] {outcome.get('channel','?')} "
        f"thread={outcome.get('thread_id','?')} "
        f"action={outcome.get('action','?')}: {summary}"
    )
    tags = ["comms-outcome", outcome.get("channel", "")]
    for p in outcome.get("participants", []):
        tags.append(f"with:{p}")

    proc = subprocess.run(
        [
            sys.executable,
            str(KB_SCRIPTS / "ingest.py"),
            "note", note,
            "--tags", ",".join(t for t in tags if t),
            "--quiet",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()}
    try:
        return {"ok": True, "ingest": json.loads(proc.stdout)}
    except json.JSONDecodeError:
        return {"ok": True, "stdout": proc.stdout.strip()}


def _cli():
    parser = argparse.ArgumentParser(description="Comms-agent draft helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prep = sub.add_parser("prepare")
    p_prep.add_argument("thread_path")
    p_log = sub.add_parser("log")
    p_log.add_argument("outcome_path")
    args = parser.parse_args()

    if args.cmd == "prepare":
        out = prepare(args.thread_path)
    elif args.cmd == "log":
        out = log_outcome(args.outcome_path)
    else:
        parser.error("unknown command")
        return
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
