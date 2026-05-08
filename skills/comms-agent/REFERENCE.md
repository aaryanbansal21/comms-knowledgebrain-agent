# Comms Agent — Reference

## Normalized message schema

The triage script and any code paths that hand messages between MCPs and the agent expect this shape:

```json
{
  "id": "<MCP-native message id>",
  "channel": "gmail | outlook | slack | teams",
  "from": "Display Name <addr>",
  "from_id": "stable sender id",
  "to": ["..."],
  "subject": "(emails only)",
  "snippet": "first ~300 chars of body",
  "received_at": 1714831200,
  "thread_id": "...",
  "is_dm": true,
  "labels": ["@me", "starred"]
}
```

When the agent reads from a connector MCP, it should map the connector's response shape into this normalized form before piping into `triage.py`. Concrete shape adapters are not in the script — the agent does the mapping inline because each MCP is slightly different.

## Thread schema (input to `draft.py prepare`)

```json
{
  "id": "thread-id",
  "channel": "gmail",
  "subject": "...",
  "participants": ["alice@x.com", "bob@y.com"],
  "messages": [
    {"from": "alice@x.com", "date": "2026-04-30T10:00:00Z", "body": "..."}
  ],
  "user_intent": "decline politely, offer next week"
}
```

`user_intent` is what the user told you in chat about how to respond. It gets used to retrieve the right KB chunks.

## Outcome schema (input to `draft.py log` and `kb_lookup.py log-outcome`)

```json
{
  "channel": "gmail",
  "thread_id": "...",
  "action": "sent | archived | skipped | forwarded",
  "summary": "one-sentence what was decided/said",
  "participants": ["..."]
}
```

Outcome notes are stored in the brain with tags `comms-outcome`, the channel, and `with:<participant>` per person — useful for "what did I last tell Bob?" lookups.

## comms_config.json

Lives at `~/.hourglass/comms_config.json`. All keys optional.

```json
{
  "high_signal_senders": ["boss@company.com", "@my-cofounder"],
  "low_signal_senders": ["newsletter@", "noreply@"],
  "high_signal_tags": ["@me", "blocker", "urgent"],
  "auto_archive_subjects": ["^\\[no-?reply\\]", "weekly digest"],
  "respond_today_window_hours": 24
}
```

`auto_archive_subjects` are regex patterns matched case-insensitively against the subject. The triage scorer uses them only to penalize the score; nothing is auto-archived without user approval.

## Triage scoring

| signal | delta |
|---|---|
| sender in `high_signal_senders` | +25 |
| sender in `low_signal_senders` | -20 |
| `is_dm` | +10 |
| label in `high_signal_tags` | +15 |
| urgent phrase in subject/snippet | +20 |
| question mark in snippet | +10 |
| matches `auto_archive_subjects` | -30 |
| received <2h ago | +5 |
| received >48h ago | -10 |
| baseline | 30 |

| total score → category |
|---|
| ≥70 → respond_now |
| ≥50 → respond_today |
| ≥30 → read_only |
| <30 → archive |

## Confirmation rules (recap)

- Every send / forward / archive / delete is gated on an explicit user OK in chat.
- Past general approvals do not carry over.
- If a message contains credentials/financial info, the draft helper appends a warning the agent must surface.

## Privacy bridge to the knowledge-brain

`kb_lookup.py` filters chunks tagged `private`. The agent must additionally avoid quoting raw KB content in outgoing messages unless the user explicitly says "include the citation". Internal-to-the-user citations (shown in chat for verification) are fine.

## Action item extraction (`action_items.py`)

Input: a thread JSON in the same shape `draft.py prepare` consumes. Output:

```json
{
  "thread_id": "...",
  "subject": "...",
  "channel": "gmail",
  "items": [
    {
      "text": "send Q3 forecast slides by Friday",
      "owner": "user | sender | unknown",
      "deadline_phrase": "Friday | null",
      "source_from": "alice@x.com",
      "source_date": "2026-05-07T10:00:00Z",
      "match_kind": "request | commitment | direct_question"
    }
  ],
  "ingested": ["note:abc123…"]
}
```

`--ingest` writes each item to the knowledge brain as a `note` with tags:

- `action-item` (always)
- `channel:<channel>`
- `from:<sender>` (when present)
- `with:<participant>` for each thread participant
- `owner:<user|sender|unknown>`
- `deadline:<phrase>` (when a deadline phrase was found)

The text format:

```
[action-item] OWNER:user | KIND:request | TASK: send Q3 forecast slides | DEADLINE: Friday | SOURCE: gmail/<thread_id> — Q3 forecast | FROM: alice@x.com
```

This shape is intentional — `kb_lookup.py "action-item deadline:today"` becomes a useful retrieval.

## Calendar helper (`calendar_helper.py`)

Subcommands and inputs:

| Subcommand | Input | Output |
|---|---|---|
| `mcp-hint` | — | recognised calendar-MCP tool prefixes |
| `parse <path.ics>` | iCalendar file | list of events with `start`, `end`, `summary`, `attendees`, `rrule` |
| `from-invite <path.ics>` | invite attachment (single VEVENT) | `{ok: true, event: {...}}` |
| `conflicts --start <iso> --end <iso> --against <path.ics> [--ignore-uid <uid>]` | candidate window + a calendar | overlapping events |

All datetimes round-trip through UTC. `TZID=…` values use stdlib `zoneinfo` when available; otherwise the script falls back to UTC and emits a `warnings: ["..."]` field on that event so the agent knows to flag it.

## Follow-up scanner (`followup.py`)

Sent-item record shape (input is a JSON list of these):

```json
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
```

A thread is "stale" when `min-days <= age <= max-days`, no inbound reply since `sent_at`, not internal, and `now - already_nudged_at >= cooldown-days`.

Defaults: `--min-days 5 --max-days 21 --cooldown-days 7 --top 25`.

The scanner's job is *which* threads to nudge. Composing the nudge body uses the normal `draft.py prepare` flow with `user_intent: "polite follow-up — check if they have updates"`.

## Pattern store (`patterns.py`)

Storage path: `$COMMS_PATTERNS_PATH` or `~/.hourglass/comms_patterns.json`.

Draft JSON shape (input to `record` and `check`):

```json
{
  "channel": "gmail",
  "thread_id": "gmail:abc",
  "recipient": "alice@x.com",
  "subject": "Re: Q3 forecast",
  "body": "Thanks — confirming Tuesday at 3pm works.",
  "warnings": []
}
```

`check` returns:

```json
{
  "auto_send": true,
  "matched_pattern_id": "p_1714831200_a8f9",
  "matched_similarity": 0.94,
  "approved_unchanged_count": 4,
  "threshold": 3,
  "reasons": []
}
```

`record --result {sent_unchanged|sent_with_edit|cancelled}` updates counters. `sent_with_edit` and `cancelled` reset `approved_unchanged_count` to 0 (a single edit invalidates the prior streak).

**Hard gates that block auto-send (any one of these → `auto_send: false`):**
- `global_auto_send_enabled` is false (default),
- best similarity < `min_similarity` (default 0.85),
- `approved_unchanged_count` < `threshold` (default 3),
- recipient is not in the pattern's `recipients_seen` list,
- draft channel ≠ pattern channel,
- draft has any non-empty `warnings`.

Normalization replaces emails, URLs, dates, times, day-of-week names, month names, long numbers, and punctuation with placeholders before fingerprinting and similarity. Two drafts that say "Confirming Tuesday at 3pm" and "Confirming Friday at 11am" hash identically.

Audit log: every `record`, `check`, and state change is appended to `audit_log` (capped at 1000 entries). View with `patterns.py audit --limit N`.
