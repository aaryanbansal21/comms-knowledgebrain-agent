"""
calendar_helper.py — calendar utilities for the comms-agent.

Two integration points are supported:

1. **Google Calendar via MCP.** This script does NOT call Google directly.
   The agent (Claude) calls whichever calendar MCP is connected. The
   `mcp-hint` subcommand prints the tool prefixes the agent should look
   for so it knows whether a calendar MCP is wired in this environment.

2. **iCalendar (.ics) files.** Two real use-cases:
       a. Parsing a calendar invite that arrived as an email attachment
          (the agent downloads the .ics via the messaging MCP, then calls
          this script to extract the event details).
       b. Checking for conflicts against the user's exported calendar
          (e.g. a CalDAV `.ics` URL the user has saved locally, or an
          export from Apple Calendar / Outlook / Google Calendar).

Subcommands
-----------
    calendar_helper.py mcp-hint
        Print recognised calendar MCP tool prefixes.

    calendar_helper.py parse <path.ics>
        Parse an .ics file and emit events as JSON.

    calendar_helper.py from-invite <path.ics>
        Like `parse` but optimized for a single invite (typically email
        attachment). Returns the first VEVENT plus organizer/attendees.

    calendar_helper.py conflicts --start <iso> --end <iso> --against <path.ics>
                                  [--ignore-uid <uid>]
        Check whether any event in <path.ics> overlaps the given window.
        Returns a list of conflicting events. `--ignore-uid` is useful when
        re-checking an invite the user has already accepted.

Time handling
-------------
DTSTART/DTEND can be:
  * UTC: `20260508T100000Z`
  * Floating with TZID: `DTSTART;TZID=America/Los_Angeles:20260508T100000`
  * All-day (date-only): `DTSTART;VALUE=DATE:20260508`

For floating + TZID we use stdlib `zoneinfo`. If the tz isn't available
locally, we treat the time as UTC and append a `tz_warning`.

We do NOT expand RRULEs. Recurring events are returned with `rrule` set;
the agent can decide to surface that to the user when relevant.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    _HAS_ZONEINFO = True
except ImportError:  # pragma: no cover
    _HAS_ZONEINFO = False
    ZoneInfoNotFoundError = Exception  # type: ignore


# Calendar MCP tool prefixes the agent should look for at runtime.
KNOWN_MCP_PREFIXES = [
    {
        "channel": "google_calendar",
        "prefixes": [
            "mcp__google_calendar__",
            "mcp__gcal__",
            "mcp__claude_ai_Google_Calendar__",
        ],
        "ops": ["list_events", "create_event", "update_event", "delete_event",
                "respond_to_invite", "find_free_slots"],
    },
    {
        "channel": "outlook_calendar",
        "prefixes": [
            "mcp__outlook_calendar__",
            "mcp__microsoft_graph__",
        ],
        "ops": ["list_events", "create_event", "respond_to_invite"],
    },
    {
        "channel": "apple_calendar",
        "prefixes": ["mcp__apple_calendar__", "mcp__ical__"],
        "ops": ["list_events", "create_event"],
    },
]


# ---------------------------------------------------------------------------
# .ics parsing (minimal RFC 5545 subset)
# ---------------------------------------------------------------------------

def _unfold(text: str) -> str:
    """RFC 5545 line unfolding: a leading space/tab continues previous line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text)


_DT_RE = re.compile(
    r"^(?P<key>[A-Z-]+)"
    r"(?:;(?P<params>[^:]+))?"
    r":(?P<value>.*)$"
)


def _parse_params(s: str | None) -> dict[str, str]:
    if not s:
        return {}
    out: dict[str, str] = {}
    for part in s.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def _parse_ics_dt(value: str, params: dict[str, str]) -> tuple[datetime | date, str | None]:
    """Returns (datetime_or_date, tz_warning_or_None)."""
    val = value.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", val)
    if is_date:
        # All-day
        return date(int(val[0:4]), int(val[4:6]), int(val[6:8])), None

    # Datetime
    if val.endswith("Z"):
        dt = datetime.strptime(val[:-1], "%Y%m%dT%H%M%S")
        return dt.replace(tzinfo=timezone.utc), None

    # Floating, possibly with TZID
    tzid = params.get("TZID")
    naive = datetime.strptime(val, "%Y%m%dT%H%M%S")
    if not tzid:
        # Floating local; treat as UTC for comparison purposes
        return naive.replace(tzinfo=timezone.utc), "treated floating time as UTC"
    if _HAS_ZONEINFO:
        try:
            return naive.replace(tzinfo=ZoneInfo(tzid)), None
        except ZoneInfoNotFoundError:
            return naive.replace(tzinfo=timezone.utc), f"unknown TZID {tzid}; treated as UTC"
    return naive.replace(tzinfo=timezone.utc), f"no zoneinfo; {tzid} treated as UTC"


def _parse_line(line: str) -> tuple[str, dict[str, str], str] | None:
    m = _DT_RE.match(line)
    if not m:
        return None
    return m.group("key").upper(), _parse_params(m.group("params")), m.group("value")


def _to_iso(dt: datetime | date) -> str:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return dt.isoformat()


def parse_ics(text: str) -> list[dict]:
    text = _unfold(text)
    events: list[dict] = []
    in_event = False
    cur: dict = {}
    warnings: list[str] = []

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {"attendees": [], "warnings": []}
            continue
        if line == "END:VEVENT":
            in_event = False
            events.append(cur)
            continue
        if not in_event:
            continue
        parsed = _parse_line(line)
        if not parsed:
            continue
        key, params, val = parsed
        if key == "UID":
            cur["uid"] = val
        elif key == "SUMMARY":
            cur["summary"] = val
        elif key == "DESCRIPTION":
            cur["description"] = val.replace("\\n", "\n").replace("\\,", ",")
        elif key == "LOCATION":
            cur["location"] = val
        elif key == "DTSTART":
            dt, w = _parse_ics_dt(val, params)
            cur["start"] = _to_iso(dt)
            cur["_start_dt"] = dt
            if w:
                cur.setdefault("warnings", []).append(f"DTSTART: {w}")
        elif key == "DTEND":
            dt, w = _parse_ics_dt(val, params)
            cur["end"] = _to_iso(dt)
            cur["_end_dt"] = dt
            if w:
                cur.setdefault("warnings", []).append(f"DTEND: {w}")
        elif key == "DURATION":
            cur["duration_raw"] = val
        elif key == "ORGANIZER":
            cur["organizer"] = val.replace("mailto:", "")
        elif key == "ATTENDEE":
            cur["attendees"].append(val.replace("mailto:", ""))
        elif key == "STATUS":
            cur["status"] = val
        elif key == "RRULE":
            cur["rrule"] = val
        elif key == "SEQUENCE":
            cur["sequence"] = val

    return events


def _strip_internal(events: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in events:
        e2 = {k: v for k, v in e.items() if not k.startswith("_")}
        out.append(e2)
    return out


# ---------------------------------------------------------------------------
# Conflict checking
# ---------------------------------------------------------------------------

def _to_aware_utc(s: str | datetime | date) -> datetime:
    """Coerce to a UTC-aware datetime for range comparison."""
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if isinstance(s, date):
        return datetime.combine(s, time.min, tzinfo=timezone.utc)
    # ISO string
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        # Bare date
        return datetime.combine(date.fromisoformat(s), time.min, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def conflicts(start: str, end: str, against_path: str,
              ignore_uid: str | None = None) -> dict:
    text = Path(against_path).read_text(encoding="utf-8", errors="replace")
    events = parse_ics(text)
    s = _to_aware_utc(start)
    e = _to_aware_utc(end)
    if e <= s:
        return {"ok": False, "error": "end must be after start"}

    hits: list[dict] = []
    for ev in events:
        if ignore_uid and ev.get("uid") == ignore_uid:
            continue
        ev_start = ev.get("_start_dt")
        ev_end = ev.get("_end_dt") or ev_start
        if ev_start is None:
            continue
        ev_s = _to_aware_utc(ev_start)
        ev_e = _to_aware_utc(ev_end) if ev_end else ev_s + timedelta(minutes=30)
        # Half-open overlap: [s, e) ∩ [ev_s, ev_e)
        if ev_s < e and ev_e > s:
            hits.append({k: v for k, v in ev.items() if not k.startswith("_")})

    return {
        "ok": True,
        "candidate": {"start": _to_aware_utc(start).isoformat(),
                       "end": _to_aware_utc(end).isoformat()},
        "conflicts": hits,
        "n_conflicts": len(hits),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(description="Calendar helper for the comms-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("mcp-hint", help="print recognised calendar MCP prefixes")

    p_parse = sub.add_parser("parse", help="parse an .ics file")
    p_parse.add_argument("path")

    p_inv = sub.add_parser("from-invite", help="parse a single-event invite")
    p_inv.add_argument("path")

    p_conf = sub.add_parser("conflicts", help="check window against an .ics")
    p_conf.add_argument("--start", required=True, help="ISO datetime")
    p_conf.add_argument("--end", required=True, help="ISO datetime")
    p_conf.add_argument("--against", required=True, help="path to .ics")
    p_conf.add_argument("--ignore-uid", default=None)

    args = parser.parse_args()

    if args.cmd == "mcp-hint":
        print(json.dumps({"calendars": KNOWN_MCP_PREFIXES}, indent=2))
        return
    if args.cmd == "parse":
        text = Path(args.path).read_text(encoding="utf-8", errors="replace")
        events = _strip_internal(parse_ics(text))
        print(json.dumps({"path": args.path, "events": events,
                          "n": len(events)}, indent=2))
        return
    if args.cmd == "from-invite":
        text = Path(args.path).read_text(encoding="utf-8", errors="replace")
        events = _strip_internal(parse_ics(text))
        if not events:
            print(json.dumps({"ok": False, "reason": "no VEVENT found"}, indent=2))
            sys.exit(3)
        print(json.dumps({"ok": True, "event": events[0],
                          "extra_events": events[1:]}, indent=2))
        return
    if args.cmd == "conflicts":
        out = conflicts(
            start=args.start, end=args.end, against_path=args.against,
            ignore_uid=args.ignore_uid,
        )
        print(json.dumps(out, indent=2, default=str))
        return


if __name__ == "__main__":
    _cli()
