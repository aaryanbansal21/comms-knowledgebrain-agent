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
