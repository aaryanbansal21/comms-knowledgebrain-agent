"""
kb_lookup.py — thin wrapper around the knowledge-brain query.py for the
comms-agent. Strips any chunks tagged `private` and returns a compact JSON
shape optimized for drafting.

Usage
-----
    python3 kb_lookup.py "<question>" [--k 5]
    python3 kb_lookup.py log-outcome --channel gmail --thread "<id>" \
        --action sent --summary "<one-sentence>"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

THIS = Path(__file__).resolve().parent
KB_SCRIPTS = (THIS.parent.parent / "knowledge-brain" / "scripts").resolve()


def lookup(question: str, k: int = 5) -> dict:
    proc = subprocess.run(
        [sys.executable, str(KB_SCRIPTS / "query.py"), question, "--k", str(k)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()}
    data = json.loads(proc.stdout)
    data["results"] = [
        r for r in data.get("results", [])
        if "private" not in (r.get("tags") or [])
    ]
    # Compact for drafting
    data["results"] = [
        {
            "title": r.get("title"),
            "location": r.get("location"),
            "text": r.get("text"),
            "score": r.get("score"),
            "source_id": r.get("source_id"),
        }
        for r in data["results"]
    ]
    data["ok"] = True
    return data


def log_outcome(channel: str, thread: str, action: str, summary: str,
                participants: list[str] | None) -> dict:
    note = (
        f"[comms-outcome] {channel} thread={thread} action={action}: {summary}"
    )
    tags = ["comms-outcome", channel]
    for p in (participants or []):
        tags.append(f"with:{p}")
    proc = subprocess.run(
        [
            sys.executable, str(KB_SCRIPTS / "ingest.py"),
            "note", note, "--tags", ",".join(t for t in tags if t), "--quiet",
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
    parser = argparse.ArgumentParser(description="KB lookup for comms-agent")
    sub = parser.add_subparsers(dest="cmd")

    # default verb is "query": `kb_lookup.py "question"`
    parser.add_argument("question", nargs="?")
    parser.add_argument("--k", type=int, default=5)

    p_log = sub.add_parser("log-outcome")
    p_log.add_argument("--channel", required=True)
    p_log.add_argument("--thread", required=True)
    p_log.add_argument("--action", required=True)
    p_log.add_argument("--summary", required=True)
    p_log.add_argument("--participants", default="",
                       help="comma-separated list")

    args = parser.parse_args()

    if args.cmd == "log-outcome":
        parts = [p.strip() for p in args.participants.split(",") if p.strip()]
        out = log_outcome(args.channel, args.thread, args.action,
                          args.summary, parts)
    else:
        if not args.question:
            parser.error("provide a question or use a subcommand")
            return
        out = lookup(args.question, k=args.k)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
