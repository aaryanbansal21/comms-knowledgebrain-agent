"""
followup.py — find sent messages that have gone unanswered long enough to
warrant a polite follow-up nudge.

The script does NOT compose the nudge itself. It returns a ranked list of
stale threads; the agent (Claude) then runs `draft.py prepare` for each
with `user_intent="polite follow-up — check if they have updates"` and
saves the resulting nudge to the platform's Drafts folder.

Why split it that way? Because composition needs the LLM and access to KB
context for register/voice. The scanner just picks the threads.

Usage
-----
    python3 followup.py scan <sent_items.json>
        Read a list of sent-message records from a JSON file (or stdin if
        the path is `-`) and print ranked stale threads as JSON.

    python3 followup.py scan - < /tmp/sent.json

Input shape — list of records, each:

    {
      "thread_id": "gmail:abc",
      "channel": "gmail",
      "subject": "Q3 forecast slides",
      "sent_at": 1714831200,
      "to": ["alice@x.com"],
      "last_inbound_at": null,
      "is_internal": false,
      "already_nudged_at": null
    }

Flags
-----
    --min-days N    minimum age before a thread is "stale" (default 5)
    --max-days N    don't suggest nudges older than this — too late (default 21)
    --cooldown-days N
                    if `already_nudged_at` is within N days, skip (default 7)
    --top K         keep at most K stale threads (default 25)

Output:

    {
      "now": 1714831200,
      "stale": [
        {
          "thread_id": "...",
          "channel": "...",
          "subject": "...",
          "to": ["..."],
          "age_days": 8.4,
          "sent_at": 1714000000,
          "last_inbound_at": null,
          "recommended_intent": "polite follow-up — check if they have updates",
          "score": 84
        }
      ],
      "skipped": [{"thread_id": "...", "reason": "..."}]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

DEFAULT_INTENT = "polite follow-up — check if they have updates"


def _read_input(path: str) -> list[dict]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit("input must be a JSON list of sent-message records")
    return data


def scan(items: list[dict], *, min_days: int, max_days: int,
         cooldown_days: int, top: int, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    sec_min = min_days * 86400
    sec_max = max_days * 86400
    sec_cool = cooldown_days * 86400

    stale: list[dict] = []
    skipped: list[dict] = []

    for it in items:
        tid = it.get("thread_id") or "?"
        sent_at = it.get("sent_at")
        if not sent_at:
            skipped.append({"thread_id": tid, "reason": "no sent_at"})
            continue
        age = now - float(sent_at)
        if age < sec_min:
            skipped.append({"thread_id": tid, "reason": f"too recent ({age/86400:.1f}d)"})
            continue
        if age > sec_max:
            skipped.append({"thread_id": tid, "reason": f"too old ({age/86400:.1f}d)"})
            continue
        if it.get("is_internal"):
            skipped.append({"thread_id": tid, "reason": "internal/self thread"})
            continue
        last_in = it.get("last_inbound_at")
        if last_in and float(last_in) >= float(sent_at):
            skipped.append({"thread_id": tid, "reason": "already replied"})
            continue
        nudged = it.get("already_nudged_at")
        if nudged and (now - float(nudged)) < sec_cool:
            skipped.append({"thread_id": tid,
                            "reason": f"nudged recently ({(now-float(nudged))/86400:.1f}d ago)"})
            continue

        # Score: older threads (within range) and ones with no reply at all
        # rank higher. Cap at 100.
        age_days = age / 86400
        score = int(min(100, 40 + (age_days - min_days) * 6))
        stale.append({
            "thread_id": tid,
            "channel": it.get("channel"),
            "subject": it.get("subject"),
            "to": it.get("to") or [],
            "age_days": round(age_days, 1),
            "sent_at": sent_at,
            "last_inbound_at": last_in,
            "recommended_intent": DEFAULT_INTENT,
            "score": score,
        })

    stale.sort(key=lambda x: (x["score"], x["age_days"]), reverse=True)
    return {
        "now": now,
        "stale": stale[:top],
        "skipped": skipped,
        "n_stale": len(stale),
        "n_skipped": len(skipped),
    }


def _cli():
    parser = argparse.ArgumentParser(
        description="Find stale outbound threads that need a follow-up"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("path", help="JSON file path or '-' for stdin")
    p.add_argument("--min-days", type=int, default=5)
    p.add_argument("--max-days", type=int, default=21)
    p.add_argument("--cooldown-days", type=int, default=7)
    p.add_argument("--top", type=int, default=25)
    args = parser.parse_args()

    items = _read_input(args.path)
    out = scan(
        items,
        min_days=args.min_days,
        max_days=args.max_days,
        cooldown_days=args.cooldown_days,
        top=args.top,
    )
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
