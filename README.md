# Hourglass

A Claude Code plugin that pairs a self-healing **knowledge brain** with a **comms agent** that triages your inbox and drafts replies grounded in your personal knowledge base.

---

## What it does

**Knowledge Brain** — a persistent, semantic memory store backed by Pinecone (vector search) and SQLite (provenance). You can feed it documents, URLs, emails, or short notes. It retrieves the right context when you need it and self-heals on a schedule: deduplicating chunks, refreshing stale URLs, detecting contradictions between notes, and logging questions it couldn't confidently answer.

**Comms Agent** — connects to any supported messaging MCP (Gmail, Slack, Outlook, Teams, and others) to triage your inbox, score each message by urgency, and draft replies grounded in what the knowledge brain knows about you. Drafts are automatically saved to the native platform (e.g. Gmail Drafts) so you can review and edit them at any time. Nothing is sent without your explicit approval.

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

### 7. Connect messaging MCPs

The comms-agent works with any supported messaging MCP. Connect whichever platforms you use:

```
/mcp add gmail      # Google Gmail
/mcp add slack      # Slack
/mcp add outlook    # Microsoft Outlook / M365
/mcp add teams      # Microsoft Teams
```

Follow the OAuth prompts for each. The agent detects whichever MCPs are present at runtime and tells you if a requested channel isn't connected.

> **Gmail is used as the example throughout this README**, but the architecture is the same for any connector. Drafts are saved to the native platform's draft store (Gmail Drafts, Outlook Drafts, etc.) where supported — for platforms without a native draft concept (Slack, Teams), drafts are presented in chat only and posted on your approval.

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
```

For actionable items, drafts are **automatically saved to your Gmail Drafts** (or equivalent platform draft store) so you can open them, review, and edit directly in your email client. Nothing is sent until you explicitly say "send it" in the conversation.

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
│           └── kb_lookup.py         # comms ↔ knowledge brain bridge
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
- The comms agent **never sends, archives, forwards, or deletes** without an explicit per-action approval from the user in chat. Past general approvals do not carry forward.
- The knowledge brain **never deletes a source** without `--yes` and a chat confirmation.
- Notes tagged `private` are filtered from all comms drafts — they are visible only in direct knowledge brain queries.
- No financial or credential data is entered into any external form.
