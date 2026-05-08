# Hourglass

A Claude Code plugin that pairs a self-healing **knowledge brain** with a **comms agent** that triages your inbox and drafts replies grounded in your personal knowledge base.

---

## What it does

**Knowledge Brain** — a persistent, semantic memory store backed by Pinecone (vector search) and SQLite (provenance). You can feed it documents, URLs, emails, or short notes. It retrieves the right context when you need it and self-heals on a schedule: deduplicating chunks, refreshing stale URLs, detecting contradictions between notes, and logging questions it couldn't confidently answer. When a query lands on a gap, the brain **auto-fills it** by web-searching, ingesting an authoritative source, and re-querying — citing that the source was just-pulled from the web. Anything fact-shaped you mention in chat is **auto-ingested** as a note (the dedup pipeline handles repeats); use `--tags private` to keep something out of comms drafts.

**Comms Agent** — connects to any supported messaging MCP (Gmail, Slack, Outlook, Teams, and others) to triage your inbox, score each message by urgency, and draft replies grounded in what the knowledge brain knows about you. Drafts are automatically saved to the native platform (e.g. Gmail Drafts) so you can review and edit them at any time. The agent also:

- **Auto-extracts action items** from every actionable thread (requests, commitments, deadlines) into the brain, tagged `action-item`, so you can ask "what's due this week?" later;
- **Integrates with calendars** — Google Calendar / Outlook / Apple Calendar via MCP when present, plus a built-in `.ics` parser for invite attachments and local exports — and conflict-checks any proposed meeting time before drafting an accept;
- **Auto-drafts follow-up nudges** for sent threads that have gone unanswered (default: 5–21 days old, with a 7-day cooldown between nudges);
- **Learns approved templates** — once you've sent the same template unchanged N times to a given recipient (default 3), the agent can send future matching drafts directly. This is opt-in, kill-switchable, and any edit or cancel resets the streak. See [Auto-send via approved patterns](#auto-send-via-approved-patterns).

Apart from that gated auto-send path, nothing leaves your account without your explicit approval.

**Self-healing automation** — two layers of automatic maintenance:
- A cron job runs `heal.py all` every 2 days (dedup + stale refresh + contradiction detection).
- A Claude Code `PostToolUse` hook fires the same script in the background immediately after any ingestion during a conversation.

---

## Architecture

```
                 ┌──────────────────┐         ┌──────────────────┐
   user ─────────│   Claude Code    ├────────▶│  comms-triage    │
                 │   (parent agent) │         │  sub-agent       │
                 └──────────────────┘         └────────┬─────────┘
                          │                            │
              skills auto-trigger               kb_lookup.py
                          │                            ▼
                 ┌─────────────────┐        ┌──────────────────────┐
                 │  comms-agent    │        │   knowledge-brain    │
                 │  skill          │◀───────│   skill              │
                 │  (drafts via    │        │   Pinecone + SQLite  │
                 │   Gmail MCP)    │        │   + Voyage AI embeds │
                 └────────┬────────┘        └──────────────────────┘
                          │
            connected MCPs: Gmail / Slack / Outlook / Teams
```

Skills auto-trigger in Claude Code based on what you say. The two sub-agents (`comms-triage`, `knowledge-brain`) handle context-heavy tasks — bulk ingestion, full inbox sweeps — in an isolated context so the parent conversation stays clean.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS or Linux | Windows untested |
| Python 3.9+ | `python3 --version` |
| [Claude Code CLI](https://docs.anthropic.com/claude-code) | `npm install -g @anthropic-ai/claude-code` |
| [Pinecone account](https://pinecone.io) | Free tier is sufficient |
| [Voyage AI account](https://voyageai.com) | Free tier is sufficient |
| Google account | For Gmail integration |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/your-username/hourglass.git
cd hourglass
```

### 2. Install Python dependencies

```bash
pip install -r skills/knowledge-brain/scripts/requirements.txt
```

### 3. Get your API keys

**Pinecone:**
1. Sign up at [pinecone.io](https://pinecone.io)
2. Go to **API Keys** in the Pinecone console
3. Copy your key

**Voyage AI:**
1. Sign up at [voyageai.com](https://voyageai.com)
2. Go to **API Keys** in the dashboard
3. Copy your key

### 4. Create your local environment file

Hourglass reads secrets and the repo path from `~/.hourglass/env.sh`. This file stays on your machine and is never committed.

```bash
mkdir -p ~/.hourglass
cat > ~/.hourglass/env.sh << 'EOF'
export PINECONE_API_KEY=your_pinecone_api_key_here
export VOYAGE_API_KEY=your_voyage_api_key_here
export HOURGLASS_DIR="$HOME/path/to/hourglass"   # absolute path to this repo
EOF
```

Then add those variables to your shell profile so they're available in every session:

```bash
echo 'source ~/.hourglass/env.sh' >> ~/.zshrc
source ~/.zshrc
```

### 5. Verify the knowledge brain is ready

```bash
cd skills/knowledge-brain
python3 scripts/kb_store.py status
```

Expected output:
```json
{
  "env": { "PINECONE_API_KEY": true, "VOYAGE_API_KEY": true },
  "index_ok": true,
  "n_chunks": 0
}
```

The Pinecone index (`hourglass-knowledge-brain`) is created automatically on first run.

### 6. Install the Claude Code plugin

Open a Claude Code session in any directory and run:

```
/plugin install /path/to/hourglass
```

This registers the `hourglass:knowledge-brain` and `hourglass:comms-agent` skills so they auto-trigger in conversation.

### 7. Connect messaging and calendar MCPs

The comms-agent works with any supported messaging MCP. Connect whichever platforms you use:

```
/mcp add gmail              # Google Gmail
/mcp add slack              # Slack
/mcp add outlook            # Microsoft Outlook / M365
/mcp add teams              # Microsoft Teams
/mcp add google_calendar    # Google Calendar (optional, for conflict-checking)
/mcp add outlook_calendar   # Outlook Calendar (optional)
```

Follow the OAuth prompts for each. The agent detects whichever MCPs are present at runtime and tells you if a requested channel isn't connected. To see which calendar MCP prefixes the agent recognises:

```bash
python3 skills/comms-agent/scripts/calendar_helper.py mcp-hint
```

If you don't want to install a calendar MCP, the agent can still parse `.ics` invite attachments and conflict-check against a local export from Google Calendar / Apple Calendar / Outlook — point it at the exported `.ics` file when the question comes up.

> **Gmail is used as the example throughout this README**, but the architecture is the same for any connector. Drafts are saved to the native platform's draft store (Gmail Drafts, Outlook Drafts, etc.) where supported — for platforms without a native draft concept (Slack, Teams), drafts are presented in chat only and posted on your approval.

#### Pre-approved tool permissions

The repo ships a `.claude/settings.json` that pre-allows the read-only / obviously-safe Gmail and Google Calendar MCP tools the agent uses (`search_emails`, `read_email`, `search-events`, `list-events`, etc.) plus draft/modify operations. This means Claude Code won't prompt you for permission every time the agent reads your inbox or checks your calendar. Anything that **sends, deletes, or forwards** still requires explicit approval — that's intentional. To customise, edit `.claude/settings.json` (committed) or add per-machine entries to `.claude/settings.local.json` (gitignored).

### 8. Set up self-healing automation

**Step A — cron job (runs every 2 days):**

```bash
# Make the script executable
chmod +x skills/knowledge-brain/heal_cron.sh

# Add to crontab (runs at 9am in your local timezone — adjust UTC offset as needed)
# Example for Australia/Sydney (UTC+10): 9am AEST = 11pm UTC previous day
(crontab -l 2>/dev/null; echo "0 23 */2 * * $HOURGLASS_DIR/skills/knowledge-brain/heal_cron.sh") | crontab -
```

Logs are written to `/tmp/kb_heal.log`.

**Step B — PostToolUse hook (fires after every in-conversation ingest):**

Add this block to `~/.claude/settings.json`. If the file doesn't exist, create it.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "input=$(cat); echo \"$input\" | jq -r '.tool_input.command // \"\"' | grep -q 'ingest\\.py' && source ~/.hourglass/env.sh && python3 \"$HOURGLASS_DIR/skills/knowledge-brain/scripts/heal.py\" all >> /tmp/kb_heal.log 2>&1 || true",
            "async": true,
            "statusMessage": "Self-healing knowledge brain..."
          }
        ]
      }
    ]
  }
}
```

> **Merge carefully** if `~/.claude/settings.json` already has content — add the `PostToolUse` block inside the existing `"hooks"` object rather than replacing the file.

### 9. (Optional) Configure triage preferences

Create `~/.hourglass/comms_config.json` to tune the triage scorer:

```json
{
  "high_signal_senders": ["your-boss@company.com", "important-client@example.com"],
  "low_signal_senders": ["newsletter@", "noreply@", "no-reply@"],
  "high_signal_tags": ["@me", "blocker", "urgent"],
  "auto_archive_subjects": ["^\\[no-?reply\\]", "weekly digest"],
  "respond_today_window_hours": 24
}
```

Without this file the triage scorer uses sensible defaults, but adding your actual high-signal senders makes a significant difference.

---

## Usage

### Knowledge Brain

```
# Store a short fact or decision
> remember that our team standup moved to 9:30am on Tuesdays

# Ingest a file, folder, or URL
> ingest ~/Documents/team-handbook.pdf and tag it "handbook"
> ingest the PDFs in ~/Desktop/research --tags research,ai

# Ask a question — the brain retrieves relevant chunks and cites sources
> what do I know about our Q3 migration plan?

# Tidy up the brain manually
> tidy up my brain
```

### Comms Agent

```
# Triage your inbox
> check my email over the last 24 hours and tell me what needs attention

# Draft a reply (brain context is pulled automatically)
> draft a reply to the invoice email from Stripe

# Full inbox sweep across all connected channels
> sweep my inboxes and tell me what needs me today

# Action items extracted from every triaged thread are searchable in the brain
> what action items do I have tagged for this week?
> what did I commit to Alice this month?

# Calendar conflict check before agreeing to a meeting time
> is 3pm Thursday clear?  (uses your calendar MCP if connected, or local .ics)

# Stale thread sweep — auto-drafts nudges for unanswered sent items
> follow up on anything I sent 5+ days ago that hasn't gotten a reply
```

For actionable items, drafts are **automatically saved to your Gmail Drafts** (or equivalent platform draft store) so you can open them, review, and edit directly in your email client. Nothing is sent until you explicitly say "send it" — *except* for drafts that match an approved template (see below), in which case the agent sends and tells you it did.

### Auto-send via approved patterns

Once you've approved the same draft template unchanged a few times, the agent can send future matching drafts directly without asking.

```bash
# Turn the global kill switch on (default: off)
python3 skills/comms-agent/scripts/patterns.py enable

# Tune the unchanged-approval threshold (default: 3)
python3 skills/comms-agent/scripts/patterns.py threshold 3

# See what's been learned
python3 skills/comms-agent/scripts/patterns.py list

# Audit every check / record / state-change
python3 skills/comms-agent/scripts/patterns.py audit --limit 50

# Reset one pattern's unchanged-streak
python3 skills/comms-agent/scripts/patterns.py reset <pattern_id>

# Off again
python3 skills/comms-agent/scripts/patterns.py disable
```

**The hard gates an outgoing draft must pass to auto-send:**

1. Global auto-send is enabled,
2. The draft body matches a stored template at jaccard similarity ≥ 0.85 (or exact normalized fingerprint),
3. That template's `approved_unchanged_count` ≥ threshold,
4. The recipient is in the pattern's `recipients_seen` list (no auto-send to brand-new addresses),
5. The channel matches,
6. The draft has zero `warnings` (any sensitive-info flag from the drafter blocks auto-send).

Any **edit** or **cancel** zeros the unchanged-streak — the agent treats a single edit as evidence the template wasn't quite right and starts learning from scratch. Every auto-send is announced loudly in chat with the matched pattern id and approval count, so you can use Gmail's undo-send if something looked off.

The pattern store lives at `~/.hourglass/comms_patterns.json` (override with `COMMS_PATTERNS_PATH`).

### Self-healing

The brain heals itself automatically. To trigger manually:

```
> tidy up my brain          # runs dedup + refresh stale + contradiction detection
> what gaps does my brain have?   # shows questions that returned weak matches
```

---

## Project structure

```
hourglass/
├── .claude-plugin/
│   └── plugin.json                  # Claude Code plugin manifest
├── skills/
│   ├── knowledge-brain/
│   │   ├── SKILL.md                 # skill trigger rules (loaded by Claude Code)
│   │   ├── REFERENCE.md             # data model, tunables, self-heal cookbook
│   │   ├── heal_cron.sh             # cron entry point (sources ~/.hourglass/env.sh)
│   │   └── scripts/
│   │       ├── requirements.txt
│   │       ├── kb_store.py          # Pinecone + SQLite wrapper
│   │       ├── extract.py           # PDF / docx / html / URL → plain text
│   │       ├── chunking.py          # semantic chunker + content hash for dedup
│   │       ├── ingest.py            # unified ingest: file / dir / url / email / note
│   │       ├── query.py             # semantic retrieval + confidence scoring
│   │       └── heal.py              # dedup, refresh-stale, contradictions, gaps
│   └── comms-agent/
│       ├── SKILL.md                 # skill trigger rules
│       ├── REFERENCE.md             # message schema, triage scoring table
│       └── scripts/
│           ├── triage.py            # score & categorise messages
│           ├── draft.py             # bundle thread + KB context for a draft
│           ├── kb_lookup.py         # comms ↔ knowledge brain bridge
│           ├── action_items.py     # extract requests/commitments/deadlines → brain
│           ├── calendar_helper.py  # parse .ics, conflict-check, MCP hints
│           ├── followup.py         # find stale outbound threads to nudge
│           └── patterns.py         # template-learning auto-send store
├── agents/
│   ├── knowledge-brain.md           # sub-agent for bulk KB tasks
│   └── comms-triage.md              # sub-agent for multi-channel inbox sweeps
├── examples/
│   └── sample_email_thread.json     # example input shape for email ingest
├── .gitignore
└── README.md
```

---

## Configuration reference

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `PINECONE_API_KEY` | Yes | — | Pinecone API key |
| `VOYAGE_API_KEY` | Yes | — | Voyage AI API key |
| `HOURGLASS_DIR` | Yes (for cron) | — | Absolute path to this repo |
| `KB_INDEX_NAME` | No | `hourglass-knowledge-brain` | Pinecone index name |
| `KB_NAMESPACE` | No | `default` | Pinecone namespace |
| `KB_EMBED_MODEL` | No | `voyage-3-large` | Voyage embedding model |
| `KB_EMBED_DIM` | No | `1024` | Must match the embed model |
| `KB_DB_PATH` | No | `~/.hourglass/kb.sqlite3` | SQLite metadata path |
| `KB_DEDUP_THRESHOLD` | No | `0.95` | Similarity above which two chunks are duplicates |
| `KB_LOW_CONF` | No | `0.55` | Score below which a query is flagged low-confidence |
| `COMMS_CONFIG_PATH` | No | `~/.hourglass/comms_config.json` | Triage config |
| `COMMS_PATTERNS_PATH` | No | `~/.hourglass/comms_patterns.json` | Auto-send pattern store |

### Triage scoring

| Signal | Score delta |
|---|---|
| Sender in `high_signal_senders` | +25 |
| Sender in `low_signal_senders` | −20 |
| Direct message | +10 |
| Label in `high_signal_tags` | +15 |
| Urgent phrase in subject/body | +20 |
| Question mark in snippet | +10 |
| Matches `auto_archive_subjects` | −30 |
| Received < 2h ago | +5 |
| Received > 48h ago | −10 |
| Baseline | 30 |

| Total | Category |
|---|---|
| ≥ 70 | `respond_now` |
| ≥ 50 | `respond_today` |
| ≥ 30 | `read_only` |
| < 30 | `archive` |

---

## Extending

**Swap the vector database** — only `kb_store.py` talks to Pinecone. Replace the four `index.*` calls (`upsert`, `query`, `delete`, `describe_index_stats`) to use Weaviate, Qdrant, or any other store.

**Change the embedding model** — update `KB_EMBED_MODEL` and `KB_EMBED_DIM`, then run:
```bash
python3 skills/knowledge-brain/scripts/heal.py reembed
```
If the dimension changed, delete and recreate the Pinecone index first.

**Add more channels** — connect any supported MCP (`/mcp add slack`) and the comms-agent will detect and use it automatically.

**Tune triage weights** — edit the score deltas at the top of `skills/comms-agent/scripts/triage.py` or override per-user via `~/.hourglass/comms_config.json`.

---

## Safety contract

- The comms agent **automatically saves drafts** to the platform's native draft store (e.g. Gmail Drafts). This is safe — drafts are invisible to recipients and can be discarded at any time.
- The comms agent **never sends, archives, forwards, or deletes** without an explicit per-action approval from the user in chat — *with one exception:* sends that pass every gate of the approved-pattern store (global enable, template match ≥ 0.85, ≥ threshold unchanged approvals, recipient on the pattern's allow-list, no draft warnings). Auto-send is opt-in, off by default, kill-switchable in one command, and any edit/cancel resets the streak. Every auto-send is announced in chat with the matched pattern id.
- Past general approvals do not carry forward outside the pattern store. "Send all my replies today" is *not* a thing the agent will agree to.
- The knowledge brain **never deletes a source** without `--yes` and a chat confirmation.
- The knowledge brain **auto-fills knowledge gaps** by web-searching and ingesting a single authoritative source, then citing it as just-fetched. This is disabled when the brain is consulted by the comms agent (so web sources never silently land in outgoing replies).
- Notes tagged `private` are filtered from all comms drafts — they are visible only in direct knowledge brain queries.
- No financial or credential data is entered into any external form.
