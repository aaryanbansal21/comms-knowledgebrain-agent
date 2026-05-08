---
name: knowledge-brain
description: Use this skill whenever the user wants to add information to, retrieve information from, or maintain a personal/team knowledge base. Triggers on phrases like "remember that...", "what do I know about...", "ingest this PDF/URL/note", "look up...", "search my notes", "save this to my brain", "what did the doc say about X", "refresh my knowledge", "find duplicates", "is this consistent with what I already know". Also triggers when another agent (e.g. the comms-agent) explicitly asks to consult the knowledge brain. The brain stores embeddings in Pinecone and metadata in a local SQLite file. It self-heals by deduping on ingest, refreshing stale URLs, flagging contradictions, and tracking knowledge gaps from queries that returned weak results. Do NOT use for one-off web searches the user does not want stored, ephemeral chit-chat, or content the user explicitly says is private/not-to-be-saved.
---

# Knowledge Brain

You are operating the user's personal knowledge brain. It is a structured, retrievable, self-healing store of things the user wants to remember and consult later.

## How it works

The brain has two layers:

1. **Vector layer (Pinecone)** — text chunks + embeddings, used for semantic retrieval.
2. **Metadata layer (local SQLite)** — sources, ingestion timestamps, hashes, types, tags, gaps, and a contradiction ledger.

Every chunk stored in Pinecone has a stable `chunk_id` that matches a row in SQLite. Pinecone is the **search index**; SQLite is the **truth-of-record for provenance**.

Helper scripts live in `scripts/`. Always shell them out — do not reimplement chunking, embedding, or vector ops in your responses.

## Setup check (do this once per environment)

Before any operation, verify the environment is configured:

```bash
python3 scripts/kb_store.py status
```

This prints:
- whether `PINECONE_API_KEY` and `VOYAGE_API_KEY` are set,
- whether the index exists (and creates it if not),
- the SQLite db path,
- chunk count.

If keys are missing, tell the user exactly which env vars to set and stop. Do not try to invent fallback flows.

## What this skill does

### 1. Ingest

Use the unified `ingest.py` entry point.

```bash
python3 scripts/ingest.py file <path>          # PDF, docx, md, txt, html
python3 scripts/ingest.py url <url>            # fetch + extract readable text
python3 scripts/ingest.py email <path-to-json> # email thread (see schema below)
python3 scripts/ingest.py note "<text>"        # short fact / preference / decision
python3 scripts/ingest.py dir <path>           # recursively ingest a folder
```

All ingestion paths share the same pipeline: extract → chunk → hash → dedup-check → embed → upsert to Pinecone → record metadata in SQLite. Each chunk is tagged with `source`, `source_type`, `source_id`, `ingested_at`, `content_hash`, and any user-supplied `--tags`.

Email thread JSON schema (when ingesting from the comms-agent):

```json
{
  "thread_id": "gmail:abc123",
  "subject": "Q3 forecast",
  "participants": ["alice@x.com", "bob@y.com"],
  "messages": [
    {"from": "alice@x.com", "date": "2026-04-12T09:00:00Z", "body": "..."}
  ]
}
```

### 2. Retrieve

```bash
python3 scripts/query.py "<question>" [--k 8] [--type file|url|email|note] [--tag <tag>]
```

Returns a JSON blob with: matching chunks (text + score), source metadata, and a `confidence` field that is `low` if the top result's similarity is below the configured threshold (default 0.55).

### Auto-fill on low confidence

When a direct user query returns `confidence: low`, **auto-fill the gap** instead of bouncing back with "I don't know." The flow:

1. Run the query with `--log-gap` so the gap is recorded and you get a `gap_id` back:
   ```bash
   python3 scripts/query.py "<question>" --log-gap
   ```
2. Decide if the question is **web-fillable**. Yes if it's about public/general knowledge (a tool, a concept, a product, a public person, an API, a standard, current events). **No** if it's about the user's private world (specific people they know, internal projects, private docs, "what did Alice say last week"). For non-web-fillable questions, skip to step 6 with the gap left unresolved.
3. Use `WebSearch` for the question. Pick the **single** most authoritative result (prefer official docs, established publications, recently dated for fast-moving topics).
4. Ingest that source:
   ```bash
   python3 scripts/ingest.py url "<chosen_url>" --tags "auto-fill"
   ```
5. Re-run the query. If confidence is now acceptable, mark the gap resolved:
   ```bash
   python3 scripts/heal.py resolve-gap <gap_id>
   ```
   Then answer the user, **clearly citing that the source was auto-pulled from the web just now** (so they know it's not from their existing corpus).
6. Only if the question wasn't web-fillable, or the auto-fill didn't lift confidence, tell the user the brain is uncertain and ask whether they want to widen the search or point you at a source.

**Never fabricate.** Auto-fill means *go fetch a real source and ingest it* — not invent an answer.

When the brain is consulted by the comms-agent (i.e. for grounding a draft reply), do **not** auto-fill — return low-confidence honestly so the comms-agent can ask the user. Web sources should not silently end up in outgoing messages.

### 3. Self-heal

Run on demand (e.g., when the user says "tidy up my brain") or periodically via the schedule skill:

```bash
python3 scripts/heal.py dedup            # collapse near-duplicate chunks
python3 scripts/heal.py refresh-stale    # re-fetch URLs older than --max-age-days (default 30)
python3 scripts/heal.py contradictions   # surface chunks whose claims disagree
python3 scripts/heal.py gaps             # show questions that returned weak matches
python3 scripts/heal.py reembed          # re-embed everything (use after changing embedding model)
python3 scripts/heal.py all              # run dedup + refresh-stale + contradictions
```

For `contradictions`, the script returns candidate pairs; you (Claude) must read both, decide which is right, and apply one of:

```bash
python3 scripts/heal.py resolve <chunk_id_keep> <chunk_id_drop> --reason "<why>"
python3 scripts/heal.py supersede <chunk_id_old> <chunk_id_new> --reason "<why>"
```

### 4. Inspect

```bash
python3 scripts/kb_store.py list-sources [--type ...] [--limit 50]
python3 scripts/kb_store.py show-source <source_id>
python3 scripts/kb_store.py delete-source <source_id>   # asks for confirmation flag
```

## Operating principles

- **Cite sources.** When you answer a user question using retrieved chunks, always include the source titles/URLs the chunks came from. The user must be able to verify.
- **Auto-ingest by default.** When the user shares anything fact-shaped in chat — a decision, a preference, a deadline, a person + context, a number, a name, a URL worth keeping, a "remember that..." — silently ingest it as a `note` via `scripts/ingest.py note "<text>"`. Don't ask permission; the dedup pipeline handles repeats. After ingesting, give a one-line confirmation ("saved to brain") so the user knows.
  - **Do** ingest: facts, decisions, preferences, contact details, deadlines, project context, anything reference-worthy.
  - **Don't** ingest: questions, commands, chit-chat, code blocks pasted for review, or content the user explicitly says is private/ephemeral/"don't save." For private but worth-keeping items, use `--tags private`.
  - When in doubt, ingest. Garbage notes are cheap; missing context is expensive.
- **De-duplicate on ingest, not after.** The pipeline already checks content hashes and high-similarity neighbors before upserting. Trust it; don't re-implement.
- **Never delete without confirmation.** `delete-source` requires `--yes` and you should still confirm with the user in chat first.
- **Respect privacy flags.** If a `note` includes the tag `private`, never include it in responses to anyone but the user, and never let the comms-agent surface it in drafts.
- **When called by the comms-agent**, return concise, well-cited answers — the comms-agent will fold them into a draft, so verbosity wastes its context.

## When uncertain

If the user's intent is ambiguous (ingest vs. ask vs. heal), ask one short clarifying question. If a script errors out, read its stderr, fix the input, and retry — do not silently swallow failures.

See `REFERENCE.md` for schema details, tunable thresholds, and how to swap Pinecone for another vector store.
