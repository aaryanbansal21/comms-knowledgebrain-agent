"""
voice_profile.py: build and serve a "voice profile" so drafts match the
user's tone across whichever messaging MCP is connected.

Architecture mirrors the rest of the comms-agent: this script does NOT
call MCPs itself. The agent (Claude) fetches recent sent messages from
each connected MCP and pipes them in. The script analyses tone signals
(greetings, sign-offs, sentence length, emoji rate, contractions, etc.)
and writes a JSON profile to ~/.hourglass/voice_profile.json.

`draft.py prepare` reads that profile and includes a per-recipient voice
slice in its envelope, which Claude conditions the draft on.

Subcommands
-----------
    voice_profile.py analyze
        Read a JSON array of sent messages from stdin (or --input <path>),
        compute tone signals (global plus per-recipient), and write the
        profile.

    voice_profile.py inject <recipient_email>
        Print the voice slice (global plus the recipient's own slice if
        it has 5+ samples) as JSON. Used by draft.py.

    voice_profile.py status
        Print last-built date, total samples, recipient count.

    voice_profile.py show
        Print the full profile JSON.

Input message schema (for `analyze`):
    [
      {
        "to": ["recipient@example.com"],
        "channel": "gmail" | "outlook" | "slack" | "teams",
        "sent_at": "2026-05-08T13:00:00+10:00",
        "subject": "...",
        "body": "..."
      },
      ...
    ]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROFILE_DIR = Path(os.path.expanduser("~/.hourglass"))
PROFILE_PATH = PROFILE_DIR / "voice_profile.json"
REFRESH_FLAG_PATH = PROFILE_DIR / "voice_refresh_due.flag"
PROFILE_VERSION = 1
PER_RECIPIENT_MIN_SAMPLES = 5
STALE_AFTER_DAYS = 30

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F900-\U0001F9FF"
    "\U0001FA70-\U0001FAFF"
    "☀-➿"
    "]"
)

CONTRACTION_RE = re.compile(
    r"\b("
    r"don't|won't|can't|isn't|aren't|wasn't|weren't|couldn't|shouldn't|wouldn't|"
    r"haven't|hasn't|hadn't|doesn't|didn't|mustn't|needn't|"
    r"i'm|i'll|i've|i'd|"
    r"you're|you'll|you've|you'd|"
    r"he's|she's|it's|we're|we'll|we've|we'd|they're|they'll|they've|they'd|"
    r"that's|there's|here's|what's|where's|who's|how's|let's"
    r")\b",
    re.IGNORECASE,
)

QUOTE_LINE_RE = re.compile(r"^\s*>")
SIGNATURE_HINTS = (
    "sent from my",
    "get outlook for",
    "this email and any attachments",
    "confidentiality notice",
)
FILLER_TOKENS = (
    "lol", "lmao", "haha", "btw", "tbh", "imo", "ngl", "lmk",
    "ty", "thx", "ttyl", "ofc", "fwiw", "iirc",
)


def _strip_quoted_and_signatures(body: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    for line in lines:
        if QUOTE_LINE_RE.match(line):
            break
        low = line.strip().lower()
        if low.startswith("on ") and " wrote:" in low:
            break
        if any(low.startswith(h) for h in SIGNATURE_HINTS):
            break
        out.append(line)
    return "\n".join(out).rstrip()


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _extract_greeting(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(
            r"^(hi|hey|hello|good morning|good afternoon|dear|yo|sup)\b[\s,]*([\w \-]*?)[,.!]?$",
            stripped,
            re.IGNORECASE,
        )
        if m:
            opener = m.group(1).strip().lower()
            name_part = m.group(2).strip()
            if name_part:
                return f"{opener} <name>".lower()
            return opener
        return None
    return None


def _extract_signoff(body: str) -> str | None:
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    tail = lines[-3:] if len(lines) >= 3 else lines[-2:]
    candidates = []
    for line in tail:
        low = line.strip().lower().rstrip(",.!")
        if low in {
            "cheers", "thanks", "thank you", "ty", "regards", "best",
            "best regards", "kind regards", "warmly", "talk soon",
            "cheers!", "thanks!", "appreciate it",
        }:
            candidates.append(low.replace("!", "").strip())
    if candidates:
        return candidates[0]
    return None


def _bullet_lines(body: str) -> int:
    n = 0
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("- ", "* ", "• ", "·")) or re.match(r"^\d+[.)]\s", s):
            n += 1
    return n


def _tokens(body: str) -> list[str]:
    return re.findall(r"[A-Za-z']+", body.lower())


def _round(n: float, digits: int = 3) -> float:
    return round(float(n), digits)


def _analyze_messages(messages: list[dict]) -> dict:
    total = len(messages)
    word_counts: list[int] = []
    sent_lens: list[int] = []
    emoji_counts: list[int] = []
    contraction_hits = 0
    word_total = 0
    bullet_msgs = 0
    em_dash_msgs = 0
    greetings: Counter[str] = Counter()
    signoffs: Counter[str] = Counter()
    fillers: Counter[str] = Counter()

    cleaned_bodies: list[str] = []
    for m in messages:
        body = _strip_quoted_and_signatures(m.get("body", "") or "")
        cleaned_bodies.append(body)
        words = _tokens(body)
        word_counts.append(len(words))
        word_total += len(words)
        for sent in _split_sentences(body):
            sw = _tokens(sent)
            if sw:
                sent_lens.append(len(sw))
        emoji_counts.append(len(EMOJI_RE.findall(body)))
        contraction_hits += len(CONTRACTION_RE.findall(body))
        if _bullet_lines(body) > 0:
            bullet_msgs += 1
        if "—" in body:
            em_dash_msgs += 1
        g = _extract_greeting(body)
        if g:
            greetings[g] += 1
        s = _extract_signoff(body)
        if s:
            signoffs[s] += 1
        for t in FILLER_TOKENS:
            if re.search(rf"\b{re.escape(t)}\b", body, re.IGNORECASE):
                fillers[t] += 1

    avg_words = word_total / total if total else 0
    avg_sentence_words = (sum(sent_lens) / len(sent_lens)) if sent_lens else 0
    emoji_rate = (sum(emoji_counts) / total) if total else 0
    contraction_rate = (contraction_hits / max(word_total, 1))
    bullet_rate = bullet_msgs / total if total else 0
    em_dash_observed_rate = em_dash_msgs / total if total else 0

    def _top(counter: Counter[str], k: int) -> list[list]:
        if not counter:
            return []
        s = sum(counter.values()) or 1
        return [[item, _round(count / s)] for item, count in counter.most_common(k)]

    return {
        "sample_count": total,
        "avg_words_per_message": _round(avg_words, 1),
        "avg_words_per_sentence": _round(avg_sentence_words, 1),
        "greetings": _top(greetings, 5),
        "signoffs": _top(signoffs, 5),
        "emoji_rate_per_message": _round(emoji_rate),
        "contraction_rate": _round(contraction_rate),
        "bullet_rate": _round(bullet_rate),
        "common_fillers": [t for t, _ in fillers.most_common(5)],
        "em_dash_observed_rate": _round(em_dash_observed_rate),
    }


def _split_by_recipient(messages: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in messages:
        recips = m.get("to") or []
        if isinstance(recips, str):
            recips = [recips]
        for r in recips:
            r_norm = (r or "").strip().lower()
            if not r_norm:
                continue
            out.setdefault(r_norm, []).append(m)
    return out


def cmd_analyze(input_path: str | None) -> dict:
    if input_path and input_path != "-":
        raw = Path(input_path).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        return {"ok": False, "error": "no input data"}
    try:
        messages = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"invalid JSON: {e}"}
    if not isinstance(messages, list):
        return {"ok": False, "error": "expected a JSON array of messages"}

    global_signals = _analyze_messages(messages)

    by_recipient: dict[str, dict] = {}
    for recipient, msgs in _split_by_recipient(messages).items():
        if len(msgs) >= PER_RECIPIENT_MIN_SAMPLES:
            by_recipient[recipient] = _analyze_messages(msgs)

    profile = {
        "version": PROFILE_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "channels_seen": sorted({m.get("channel", "") for m in messages if m.get("channel")}),
        "global": global_signals,
        "by_recipient": by_recipient,
        "rules": {
            "never_use_em_dashes": True,
        },
    }

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "path": str(PROFILE_PATH),
        "samples": len(messages),
        "recipients_with_clusters": len(by_recipient),
    }


def _load_profile() -> dict | None:
    if not PROFILE_PATH.exists():
        return None
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def cmd_inject(recipient: str) -> dict:
    profile = _load_profile()
    if profile is None:
        return {
            "ok": False,
            "reason": "no voice profile yet",
            "rules": {"never_use_em_dashes": True},
        }
    rec = (recipient or "").strip().lower()
    slice_ = {
        "ok": True,
        "built_at": profile.get("built_at"),
        "global": profile.get("global", {}),
        "rules": profile.get("rules", {"never_use_em_dashes": True}),
    }
    by_recipient = profile.get("by_recipient", {})
    if rec and rec in by_recipient:
        slice_["recipient"] = rec
        slice_["recipient_signals"] = by_recipient[rec]
    return slice_


def cmd_status() -> dict:
    profile = _load_profile()
    if profile is None:
        return {"ok": False, "reason": "no voice profile yet"}
    return {
        "ok": True,
        "built_at": profile.get("built_at"),
        "samples": profile.get("global", {}).get("sample_count", 0),
        "channels_seen": profile.get("channels_seen", []),
        "recipients_with_clusters": len(profile.get("by_recipient", {})),
    }


def cmd_check_stale() -> dict:
    """Touch ~/.hourglass/voice_refresh_due.flag if profile is missing or stale.

    Designed for cron. Removes the flag once the profile is fresh again so it
    doesn't stick around forever after a refresh.
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    profile = _load_profile()
    if profile is None:
        REFRESH_FLAG_PATH.write_text("missing\n", encoding="utf-8")
        return {"ok": True, "stale": True, "reason": "no profile yet", "flag": str(REFRESH_FLAG_PATH)}
    built_at = profile.get("built_at")
    try:
        built_dt = datetime.fromisoformat(built_at.replace("Z", "+00:00")) if built_at else None
    except (ValueError, AttributeError):
        built_dt = None
    if built_dt is None:
        REFRESH_FLAG_PATH.write_text("unparseable_built_at\n", encoding="utf-8")
        return {"ok": True, "stale": True, "reason": "built_at missing or unparseable"}
    if built_dt.tzinfo is None:
        built_dt = built_dt.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - built_dt).total_seconds() / 86400
    if age_days >= STALE_AFTER_DAYS:
        REFRESH_FLAG_PATH.write_text(f"age_days={age_days:.1f}\n", encoding="utf-8")
        return {"ok": True, "stale": True, "age_days": round(age_days, 1)}
    if REFRESH_FLAG_PATH.exists():
        REFRESH_FLAG_PATH.unlink()
    return {"ok": True, "stale": False, "age_days": round(age_days, 1)}


def cmd_show() -> dict:
    profile = _load_profile()
    if profile is None:
        return {"ok": False, "reason": "no voice profile yet"}
    return profile


def _cli() -> None:
    p = argparse.ArgumentParser(description="Voice profile builder for comms-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_an = sub.add_parser("analyze")
    p_an.add_argument("--input", default=None, help="path to JSON array (or omit to read stdin)")
    p_inj = sub.add_parser("inject")
    p_inj.add_argument("recipient", help="recipient email or handle")
    sub.add_parser("status")
    sub.add_parser("show")
    sub.add_parser("check_stale")
    args = p.parse_args()

    if args.cmd == "analyze":
        out = cmd_analyze(args.input)
    elif args.cmd == "inject":
        out = cmd_inject(args.recipient)
    elif args.cmd == "status":
        out = cmd_status()
    elif args.cmd == "show":
        out = cmd_show()
    elif args.cmd == "check_stale":
        out = cmd_check_stale()
    else:
        p.error("unknown command")
        return
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
