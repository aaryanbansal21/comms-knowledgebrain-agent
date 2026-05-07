# Knowledge Brain — Reference

## Architecture

```
                ┌──────────────────────────┐
                │        ingest.py         │
                │  (file/url/email/note)   │
                └────────────┬─────────────┘
                             │
                  ┌──────────┴──────────┐
                  │                     │
            extract.py             chunking.py
            (text out of           (semantic split
             PDFs, URLs,           + content_hash
             docx, html)            for dedup)
                  │                     │
                  └──────────┬──────────┘
                             │
                ┌────────────▼────────────┐
                │       kb_store.py       │
                │ ┌─────────┐ ┌─────────┐ │
                │ │Pinecone │ │ SQLite  │ │
                │ │(vectors)│ │(metadata│ │
                │ │         │ │+ gaps)  │ │
                │ └─────────┘ └─────────┘ │
                └────────────┬────────────┘
                             │
                  ┌──────────┴───────────┐
                  │                      │
              query.py                 heal.py
            (semantic +             (dedup, refresh,
            confidence              contradictions,
            scoring)                gaps, reembed)
```

## Data model

**sources** (one row per ingested artifact)
| col | meaning |
|-----|---------|
| source_id | stable hash-based id, e.g. `file:abc123…`, `url:…`, `email:…`, `note:…` |
| type | `file` / `url` / `email` / `note` |
| title | display title |
| location | path / url / thread_id / `inline` |
| ingested_at | first time seen (epoch seconds) |
| last_verified_at | last time we re-ingested or confirmed |
| tags | JSON array of strings |
| extra | JSON object: type-specific fields (size, participants, etc.) |

**chunks** (one row per text chunk; FK to sources, ON DELETE CASCADE)
| col | meaning |
|-----|---------|
| chunk_id | UUID, also the Pinecone vector id |
| source_id | parent source |
| text | full chunk text |
| content_hash | normalized SHA-256 prefix; used for cross-source dedup |
| position | ordinal within the source |
| ingested_at | epoch seconds |

**gaps** — questions whose top retrieval score was below threshold; reviewed via `heal.py gaps`.

**contradiction_log** — pairs flagged by `heal.py contradictions`, with eventual resolution mode.

## Tunables (env vars)

| var | default | meaning |
|-----|---------|---------|
| `KB_INDEX_NAME` | `hourglass-knowledge-brain` | Pinecone index name |
| `KB_NAMESPACE` | `default` | Pinecone namespace (use per-user/team for isolation) |
| `KB_EMBED_MODEL` | `voyage-3-large` | Voyage embedding model |
| `KB_EMBED_DIM` | `1024` | must match the embed model |
| `KB_DB_PATH` | `~/.hourglass/kb.sqlite3` | SQLite metadata path |
| `KB_DEDUP_THRESHOLD` | `0.95` | similarity above which two chunks are "duplicates" |
| `KB_LOW_CONF` | `0.55` | top-score below which a query is flagged low-confidence |
| `PINECONE_CLOUD` | `aws` | only used when creating the index for the first time |
| `PINECONE_REGION` | `us-east-1` | only used when creating the index for the first time |

## Swapping the vector store

`kb_store.py` is the only file that touches Pinecone. To swap to e.g. Weaviate or Qdrant, replace these methods on `KBStore` (and the helper `get_pinecone_index`):
- `index.upsert(...)` in `upsert_source`
- `index.query(...)` in `query` and `neighbors`
- `index.delete(...)` in `delete_source` and `heal._resolve_pair`
- `describe_index_stats()` call in `status`

## Self-heal cookbook

| symptom | command |
|---------|---------|
| "I've been ingesting the same docs from different folders." | `python3 scripts/heal.py dedup` then rerun with `--yes` |
| "My web sources are stale." | `python3 scripts/heal.py refresh-stale --max-age-days 14` |
| "I think one note contradicts another." | `python3 scripts/heal.py contradictions`, then `resolve` or `supersede` |
| "I asked something and got a weak answer." | `python3 scripts/query.py "..." --log-gap` |
| "What questions has the brain failed on?" | `python3 scripts/heal.py gaps` |
| "I changed embedding models." | `python3 scripts/heal.py reembed` (also requires re-creating the index if the dim changed) |

## Privacy

Use `--tags private` on any note that should never leak into comms drafts. The comms-agent has explicit instructions to skip `private`-tagged retrievals when assembling drafts.
