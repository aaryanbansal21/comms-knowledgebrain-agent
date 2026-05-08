"""
patterns.py — template-learning auto-send for the comms-agent.

Tracks which draft templates the user has approved unchanged. Once a
template has been approved unchanged N times AND the user has enabled
global auto-send, the comms-agent may send future drafts that match the
same template to the same recipient WITHOUT a per-message confirmation.

Any edit, cancel, or recipient-mismatch resets the unchanged-streak.

Storage
-------
JSON file at $COMMS_PATTERNS_PATH or ~/.hourglass/comms_patterns.json.

Subcommands
-----------
    patterns.py record <draft.json> --result {sent_unchanged|sent_with_edit|cancelled}
        Record an outcome for a draft.

    patterns.py check <draft.json>
        Returns {auto_send: bool, ...}. The agent uses this BEFORE sending
        to decide whether it can skip the per-message confirmation.

    patterns.py list
        Print stored patterns and their counts.

    patterns.py enable
    patterns.py disable
        Toggle the global auto-send kill switch (default: disabled).

    patterns.py threshold <N>
        Set the unchanged-approval count required for eligibility (default 3).

    patterns.py reset <pattern_id>
        Zero the approved_unchanged_count for one pattern (use after a
        scare).

    patterns.py audit [--limit N]
        Print the audit log (every record/check/auto_send/state-change).

Draft JSON shape (input to record/check):

    {
      "channel": "gmail",
      "thread_id": "gmail:abc",
      "recipient": "alice@x.com",     // single primary recipient
      "subject": "Re: Q3 forecast",
      "body": "Thanks — confirming Tuesday at 3pm works.",
      "warnings": []                  // optional; if non-empty, never auto-send
    }
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

STORE_PATH = Path(
    os.environ.get(
        "COMMS_PATTERNS_PATH",
        str(Path.home() / ".hourglass" / "comms_patterns.json"),
    )
)

DEFAULT_THRESHOLD = 3
DEFAULT_MIN_SIM = 0.85
AUDIT_LIMIT = 1000  # cap audit log to avoid unbounded growth


# ---------------------------------------------------------------------------
# Normalization + similarity
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
URL_RE = re.compile(r"https?://\S+")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
SHORT_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b\d{2}:\d{2}\b", re.I)
LONG_NUM_RE = re.compile(r"\b\d{4,}\b")
DAY_RE = re.compile(
    r"\b(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?\b", re.I)
MONTH_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", re.I)
PUNCT_STRIP_RE = re.compile(r"[^\w\s<>]")
WS_RE = re.compile(r"\s+")


def normalize(body: str) -> str:
    s = body or ""
    s = EMAIL_RE.sub("<EMAIL>", s)
    s = URL_RE.sub("<URL>", s)
    s = ISO_DATE_RE.sub("<DATE>", s)
    s = SHORT_DATE_RE.sub("<DATE>", s)
    s = TIME_RE.sub("<TIME>", s)
    s = LONG_NUM_RE.sub("<NUM>", s)
    s = DAY_RE.sub("<DAY>", s)
    s = MONTH_RE.sub("<MONTH>", s)
    s = s.lower()
    s = PUNCT_STRIP_RE.sub(" ", s)
    s = WS_RE.sub(" ", s).strip()
    return s


def fingerprint(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _bigrams(s: str) -> set[tuple[str, str]]:
    toks = s.split()
    return set(zip(toks, toks[1:])) if len(toks) >= 2 else {(t, "") for t in toks}


def jaccard(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _empty_store() -> dict:
    return {
        "version": 1,
        "global_auto_send_enabled": False,
        "threshold": DEFAULT_THRESHOLD,
        "min_similarity": DEFAULT_MIN_SIM,
        "patterns": [],
        "audit_log": [],
    }


def load_store() -> dict:
    if not STORE_PATH.exists():
        return _empty_store()
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_store()


def save_store(store: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Trim audit log
    if len(store.get("audit_log", [])) > AUDIT_LIMIT:
        store["audit_log"] = store["audit_log"][-AUDIT_LIMIT:]
    STORE_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")


def _audit(store: dict, **kw) -> None:
    entry = {"at": time.time(), **kw}
    store.setdefault("audit_log", []).append(entry)


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------

def _find_match(store: dict, normalized: str, fp: str) -> tuple[dict | None, float]:
    """Return (pattern, similarity) or (None, 0.0)."""
    min_sim = store.get("min_similarity", DEFAULT_MIN_SIM)
    # Exact fingerprint match wins.
    for p in store["patterns"]:
        if p["fingerprint"] == fp:
            return p, 1.0
    # Otherwise pick the highest-similarity pattern above threshold.
    best, best_sim = None, 0.0
    for p in store["patterns"]:
        sim = jaccard(normalized, p["template"])
        if sim > best_sim:
            best, best_sim = p, sim
    if best and best_sim >= min_sim:
        return best, best_sim
    return None, best_sim


def _new_pattern(normalized: str, fp: str, draft: dict) -> dict:
    return {
        "id": f"p_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        "fingerprint": fp,
        "template": normalized,
        "exemplars": [draft.get("body", "")][:1],
        "recipients_seen": [draft["recipient"]] if draft.get("recipient") else [],
        "channel": draft.get("channel"),
        "approved_unchanged_count": 0,
        "approved_with_edit_count": 0,
        "cancelled_count": 0,
        "first_seen_at": time.time(),
        "last_used_at": time.time(),
        "auto_send_eligible": False,
    }


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _refresh_eligibility(p: dict, threshold: int) -> None:
    p["auto_send_eligible"] = p["approved_unchanged_count"] >= threshold


def record(draft: dict, result: str) -> dict:
    if result not in {"sent_unchanged", "sent_with_edit", "cancelled"}:
        raise SystemExit(f"unknown result '{result}'")
    body = draft.get("body") or ""
    norm = normalize(body)
    fp = fingerprint(norm)
    store = load_store()
    threshold = store.get("threshold", DEFAULT_THRESHOLD)

    p, sim = _find_match(store, norm, fp)
    if p is None:
        p = _new_pattern(norm, fp, draft)
        store["patterns"].append(p)

    rcp = draft.get("recipient")
    if rcp and rcp not in p["recipients_seen"]:
        p["recipients_seen"].append(rcp)
    p["last_used_at"] = time.time()

    if result == "sent_unchanged":
        p["approved_unchanged_count"] += 1
        if draft.get("body") and len(p["exemplars"]) < 5:
            p["exemplars"].append(draft["body"])
    elif result == "sent_with_edit":
        p["approved_with_edit_count"] += 1
        # An edit invalidates the prior unchanged streak. Reset.
        p["approved_unchanged_count"] = 0
    elif result == "cancelled":
        p["cancelled_count"] += 1
        p["approved_unchanged_count"] = 0

    _refresh_eligibility(p, threshold)
    _audit(
        store, event="record", result=result,
        pattern_id=p["id"], similarity=round(sim, 3),
        recipient=rcp, channel=draft.get("channel"),
        thread_id=draft.get("thread_id"),
        approved_unchanged_count=p["approved_unchanged_count"],
        auto_send_eligible=p["auto_send_eligible"],
    )
    save_store(store)
    return {
        "ok": True,
        "pattern_id": p["id"],
        "approved_unchanged_count": p["approved_unchanged_count"],
        "approved_with_edit_count": p["approved_with_edit_count"],
        "cancelled_count": p["cancelled_count"],
        "auto_send_eligible": p["auto_send_eligible"],
        "matched_similarity": round(sim, 3),
    }


def check(draft: dict) -> dict:
    body = draft.get("body") or ""
    norm = normalize(body)
    fp = fingerprint(norm)
    store = load_store()
    threshold = store.get("threshold", DEFAULT_THRESHOLD)
    p, sim = _find_match(store, norm, fp)

    reasons: list[str] = []
    auto_send = True

    if not store.get("global_auto_send_enabled"):
        auto_send = False
        reasons.append("auto-send disabled globally")
    if draft.get("warnings"):
        auto_send = False
        reasons.append(f"draft has {len(draft['warnings'])} warning(s)")
    if p is None:
        auto_send = False
        reasons.append(f"no matching pattern (best similarity {sim:.2f})")
    else:
        if p["approved_unchanged_count"] < threshold:
            auto_send = False
            reasons.append(
                f"pattern only approved {p['approved_unchanged_count']}/{threshold} times"
            )
        rcp = draft.get("recipient")
        if rcp and rcp not in p["recipients_seen"]:
            auto_send = False
            reasons.append(f"recipient {rcp} not in approved list for this pattern")
        if p["channel"] and draft.get("channel") and p["channel"] != draft.get("channel"):
            auto_send = False
            reasons.append(f"channel mismatch ({draft.get('channel')} vs {p['channel']})")

    out = {
        "auto_send": auto_send,
        "matched_pattern_id": p["id"] if p else None,
        "matched_similarity": round(sim, 3),
        "approved_unchanged_count": p["approved_unchanged_count"] if p else 0,
        "threshold": threshold,
        "reasons": reasons,
    }
    _audit(
        store, event="check",
        pattern_id=p["id"] if p else None, similarity=round(sim, 3),
        auto_send=auto_send, recipient=draft.get("recipient"),
        thread_id=draft.get("thread_id"),
    )
    save_store(store)
    return out


def list_patterns() -> dict:
    store = load_store()
    return {
        "global_auto_send_enabled": store.get("global_auto_send_enabled", False),
        "threshold": store.get("threshold", DEFAULT_THRESHOLD),
        "min_similarity": store.get("min_similarity", DEFAULT_MIN_SIM),
        "n_patterns": len(store["patterns"]),
        "patterns": [
            {
                "id": p["id"],
                "approved_unchanged_count": p["approved_unchanged_count"],
                "approved_with_edit_count": p["approved_with_edit_count"],
                "cancelled_count": p["cancelled_count"],
                "auto_send_eligible": p["auto_send_eligible"],
                "recipients_seen": p["recipients_seen"],
                "channel": p["channel"],
                "first_seen_at": p["first_seen_at"],
                "last_used_at": p["last_used_at"],
                "preview": (p["exemplars"][0][:120] + "…") if p["exemplars"] else "",
            }
            for p in store["patterns"]
        ],
    }


def set_enabled(enabled: bool) -> dict:
    store = load_store()
    store["global_auto_send_enabled"] = enabled
    _audit(store, event="set_enabled", value=enabled)
    save_store(store)
    return {"ok": True, "global_auto_send_enabled": enabled}


def set_threshold(n: int) -> dict:
    if n < 1:
        raise SystemExit("threshold must be >= 1")
    store = load_store()
    store["threshold"] = n
    for p in store["patterns"]:
        _refresh_eligibility(p, n)
    _audit(store, event="set_threshold", value=n)
    save_store(store)
    return {"ok": True, "threshold": n}


def reset(pattern_id: str) -> dict:
    store = load_store()
    threshold = store.get("threshold", DEFAULT_THRESHOLD)
    for p in store["patterns"]:
        if p["id"] == pattern_id:
            p["approved_unchanged_count"] = 0
            _refresh_eligibility(p, threshold)
            _audit(store, event="reset", pattern_id=pattern_id)
            save_store(store)
            return {"ok": True, "pattern_id": pattern_id}
    return {"ok": False, "reason": "not found"}


def audit(limit: int) -> dict:
    store = load_store()
    log = store.get("audit_log", [])
    return {"audit_log": log[-limit:]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_draft(path: str) -> dict:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def _cli():
    parser = argparse.ArgumentParser(
        description="Template-learning auto-send for the comms-agent"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record")
    p_rec.add_argument("draft_path")
    p_rec.add_argument(
        "--result", required=True,
        choices=["sent_unchanged", "sent_with_edit", "cancelled"],
    )

    p_chk = sub.add_parser("check")
    p_chk.add_argument("draft_path")

    sub.add_parser("list")
    sub.add_parser("enable")
    sub.add_parser("disable")

    p_thr = sub.add_parser("threshold")
    p_thr.add_argument("n", type=int)

    p_rst = sub.add_parser("reset")
    p_rst.add_argument("pattern_id")

    p_aud = sub.add_parser("audit")
    p_aud.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()

    if args.cmd == "record":
        out = record(_read_draft(args.draft_path), args.result)
    elif args.cmd == "check":
        out = check(_read_draft(args.draft_path))
    elif args.cmd == "list":
        out = list_patterns()
    elif args.cmd == "enable":
        out = set_enabled(True)
    elif args.cmd == "disable":
        out = set_enabled(False)
    elif args.cmd == "threshold":
        out = set_threshold(args.n)
    elif args.cmd == "reset":
        out = reset(args.pattern_id)
    elif args.cmd == "audit":
        out = audit(args.limit)
    else:
        parser.error("unknown command")
        return
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
