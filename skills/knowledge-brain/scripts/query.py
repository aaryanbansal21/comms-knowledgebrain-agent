"""
query.py — semantic retrieval against the knowledge brain.

    python3 query.py "<question>" [--k 8] [--type file|url|email|note]
                                  [--tag <tag>] [--log-gap]

Output is JSON with:
    question, top_score, confidence (high/medium/low), results: [...]

If --log-gap is passed and confidence is low, the question gets logged into
the gaps table for later review by `heal.py gaps`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kb_store import KBStore, db_connection  # noqa: E402


def _cli():
    parser = argparse.ArgumentParser(description="Query the knowledge brain")
    parser.add_argument("question")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--type", dest="type_filter")
    parser.add_argument("--tag", dest="tag_filter")
    parser.add_argument(
        "--log-gap",
        action="store_true",
        help="if confidence is low, log this question as a gap",
    )
    args = parser.parse_args()

    kb = KBStore()
    out = kb.query(
        args.question,
        k=args.k,
        type_filter=args.type_filter,
        tag_filter=args.tag_filter,
    )

    if args.log_gap and out["confidence"] == "low":
        with db_connection() as conn:
            cur = conn.execute(
                "INSERT INTO gaps(question, reason, logged_at) VALUES (?, ?, ?)",
                (args.question, "weak-match", time.time()),
            )
            out["gap_id"] = cur.lastrowid
        out["logged_gap"] = True

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
