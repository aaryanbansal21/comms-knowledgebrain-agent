"""
kb_store.py — unified wrapper over Pinecone (vectors) and SQLite (metadata).

Design principles
-----------------
- Pinecone is the search index. SQLite is the truth-of-record.
- A "chunk" is the atomic unit: a slice of text + its embedding + provenance.
- A "source" groups chunks (e.g. one PDF, one URL, one email thread, one note).
- Every chunk has: chunk_id (uuid), source_id, content_hash, ingested_at,
  last_verified_at, and arbitrary tags.

CLI usage
---------
    python3 kb_store.py status
    python3 kb_store.py list-sources [--type TYPE] [--limit N]
    python3 kb_store.py show-source SOURCE_ID
    python3 kb_store.py delete-source SOURCE_ID --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional

# ---- config ---------------------------------------------------------------

DEFAULT_INDEX_NAME = os.environ.get("KB_INDEX_NAME", "hourglass-knowledge-brain")
DEFAULT_NAMESPACE = os.environ.get("KB_NAMESPACE", "default")
DEFAULT_EMBED_MODEL = os.environ.get("KB_EMBED_MODEL", "voyage-3-large")
DEFAULT_EMBED_DIM = int(os.environ.get("KB_EMBED_DIM", "1024"))
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "KB_DB_PATH",
        str(Path.home() / ".hourglass" / "kb.sqlite3"),
    )
)
SIMILARITY_DEDUP_THRESHOLD = float(os.environ.get("KB_DEDUP_THRESHOLD", "0.95"))
SIMILARITY_LOW_CONFIDENCE = float(os.environ.get("KB_LOW_CONF", "0.55"))


# ---- data classes ---------------------------------------------------------


@dataclass
class Source:
    source_id: str
    type: str  # file | url | email | note | dir
    title: str
    location: str  # path, url, thread_id, or "inline"
    ingested_at: float
    last_verified_at: float
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class Chunk:
    chunk_id: str
    source_id: str
    text: str
    content_hash: str
    position: int  # ordinal within the source
    ingested_at: float


# ---- sqlite helpers -------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id        TEXT PRIMARY KEY,
    type             TEXT NOT NULL,
    title            TEXT NOT NULL,
    location         TEXT NOT NULL,
    ingested_at      REAL NOT NULL,
    last_verified_at REAL NOT NULL,
    tags             TEXT NOT NULL DEFAULT '[]',
    extra            TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    text          TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    position      INTEGER NOT NULL,
    ingested_at   REAL NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash);

CREATE TABLE IF NOT EXISTS gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    logged_at   REAL NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS contradiction_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_a      TEXT NOT NULL,
    chunk_b      TEXT NOT NULL,
    note         TEXT,
    detected_at  REAL NOT NULL,
    resolution   TEXT  -- 'kept_a' | 'kept_b' | 'superseded' | 'merged' | NULL
);
"""


@contextmanager
def db_connection(path: Path = DEFAULT_DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---- pinecone + voyage clients (lazy, optional imports) ------------------


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(
            f"[kb_store] Missing env var {name}. Set it and retry.",
            file=sys.stderr,
        )
        sys.exit(2)
    return val


def get_pinecone_index(create_if_missing: bool = True):
    """Returns a pinecone Index handle, creating the index if needed."""
    try:
        from pinecone import Pinecone, ServerlessSpec  # type: ignore
    except ImportError:
        print(
            "[kb_store] pinecone package not installed. Run: "
            "pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(2)

    api_key = _require_env("PINECONE_API_KEY")
    pc = Pinecone(api_key=api_key)

    existing = {ix["name"] for ix in pc.list_indexes()}
    if DEFAULT_INDEX_NAME not in existing:
        if not create_if_missing:
            return None
        cloud = os.environ.get("PINECONE_CLOUD", "aws")
        region = os.environ.get("PINECONE_REGION", "us-east-1")
        pc.create_index(
            name=DEFAULT_INDEX_NAME,
            dimension=DEFAULT_EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud=cloud, region=region),
        )
        # Wait until ready (best-effort; Pinecone serverless is usually <10s).
        for _ in range(30):
            desc = pc.describe_index(DEFAULT_INDEX_NAME)
            if desc.status.get("ready"):
                break
            time.sleep(1)

    return pc.Index(DEFAULT_INDEX_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with Voyage. Returns one vector per input."""
    try:
        import voyageai  # type: ignore
    except ImportError:
        print(
            "[kb_store] voyageai package not installed. Run: "
            "pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(2)

    _require_env("VOYAGE_API_KEY")
    client = voyageai.Client()
    # Voyage caps batch sizes; 128 is comfortably under all current limits.
    out: list[list[float]] = []
    for i in range(0, len(texts), 128):
        batch = texts[i : i + 128]
        result = client.embed(
            batch, model=DEFAULT_EMBED_MODEL, input_type="document"
        )
        out.extend(result.embeddings)
    return out


def embed_query(text: str) -> list[float]:
    try:
        import voyageai  # type: ignore
    except ImportError:
        print("[kb_store] voyageai not installed.", file=sys.stderr)
        sys.exit(2)
    _require_env("VOYAGE_API_KEY")
    client = voyageai.Client()
    return client.embed(
        [text], model=DEFAULT_EMBED_MODEL, input_type="query"
    ).embeddings[0]


# ---- core ops -------------------------------------------------------------


class KBStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._index = None

    @property
    def index(self):
        if self._index is None:
            self._index = get_pinecone_index()
        return self._index

    # ---- write paths -----------------------------------------------------

    def upsert_source(self, source: Source, chunks: list[Chunk]) -> dict:
        """
        Insert a source and its chunks. Skips chunks whose content_hash
        already exists anywhere in the brain (cross-source dedup).
        """
        if not chunks:
            return {"upserted": 0, "skipped": 0, "source_id": source.source_id}

        with db_connection(self.db_path) as conn:
            # Check existing hashes
            placeholders = ",".join("?" for _ in chunks)
            existing = {
                row["content_hash"]
                for row in conn.execute(
                    f"SELECT content_hash FROM chunks WHERE content_hash IN ({placeholders})",
                    [c.content_hash for c in chunks],
                )
            }
            new_chunks = [c for c in chunks if c.content_hash not in existing]
            skipped = len(chunks) - len(new_chunks)

            if not new_chunks:
                # Even if all chunks are duplicates, record the source so the
                # user knows we saw this file. last_verified_at gets bumped.
                conn.execute(
                    """
                    INSERT INTO sources(source_id, type, title, location,
                        ingested_at, last_verified_at, tags, extra)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        last_verified_at = excluded.last_verified_at
                    """,
                    (
                        source.source_id,
                        source.type,
                        source.title,
                        source.location,
                        source.ingested_at,
                        source.last_verified_at,
                        json.dumps(source.tags),
                        json.dumps(source.extra),
                    ),
                )
                return {
                    "upserted": 0,
                    "skipped": skipped,
                    "source_id": source.source_id,
                    "note": "all chunks already in brain",
                }

            # Embed new chunks
            vectors = embed_texts([c.text for c in new_chunks])

            # Upsert source row
            conn.execute(
                """
                INSERT INTO sources(source_id, type, title, location,
                    ingested_at, last_verified_at, tags, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    title = excluded.title,
                    location = excluded.location,
                    last_verified_at = excluded.last_verified_at,
                    tags = excluded.tags,
                    extra = excluded.extra
                """,
                (
                    source.source_id,
                    source.type,
                    source.title,
                    source.location,
                    source.ingested_at,
                    source.last_verified_at,
                    json.dumps(source.tags),
                    json.dumps(source.extra),
                ),
            )
            # Insert chunks
            conn.executemany(
                """
                INSERT INTO chunks(chunk_id, source_id, text, content_hash,
                    position, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        c.chunk_id,
                        c.source_id,
                        c.text,
                        c.content_hash,
                        c.position,
                        c.ingested_at,
                    )
                    for c in new_chunks
                ],
            )

        # Push vectors to Pinecone (outside the SQLite transaction).
        self.index.upsert(
            vectors=[
                {
                    "id": c.chunk_id,
                    "values": v,
                    "metadata": {
                        "source_id": source.source_id,
                        "source_type": source.type,
                        "title": source.title,
                        "location": source.location,
                        "tags": source.tags,
                        "text_preview": c.text[:300],
                    },
                }
                for c, v in zip(new_chunks, vectors)
            ],
            namespace=DEFAULT_NAMESPACE,
        )

        return {
            "upserted": len(new_chunks),
            "skipped": skipped,
            "source_id": source.source_id,
        }

    def delete_source(self, source_id: str) -> int:
        with db_connection(self.db_path) as conn:
            chunk_ids = [
                row["chunk_id"]
                for row in conn.execute(
                    "SELECT chunk_id FROM chunks WHERE source_id = ?",
                    (source_id,),
                )
            ]
            if not chunk_ids:
                return 0
            conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
            # chunks are removed via CASCADE
        # Pinecone delete in batches of 1000
        for i in range(0, len(chunk_ids), 1000):
            self.index.delete(
                ids=chunk_ids[i : i + 1000], namespace=DEFAULT_NAMESPACE
            )
        return len(chunk_ids)

    # ---- read paths ------------------------------------------------------

    def query(
        self,
        question: str,
        k: int = 8,
        type_filter: Optional[str] = None,
        tag_filter: Optional[str] = None,
    ) -> dict:
        qvec = embed_query(question)
        flt: dict = {}
        if type_filter:
            flt["source_type"] = {"$eq": type_filter}
        if tag_filter:
            flt["tags"] = {"$in": [tag_filter]}

        res = self.index.query(
            vector=qvec,
            top_k=k,
            include_metadata=True,
            filter=flt or None,
            namespace=DEFAULT_NAMESPACE,
        )
        matches = res.get("matches", []) if isinstance(res, dict) else res.matches

        # Fetch full chunk text from SQLite (Pinecone metadata is preview-only)
        ids = [m["id"] if isinstance(m, dict) else m.id for m in matches]
        full_texts: dict[str, dict] = {}
        if ids:
            placeholders = ",".join("?" for _ in ids)
            with db_connection(self.db_path) as conn:
                for row in conn.execute(
                    f"""
                    SELECT c.chunk_id, c.text, c.source_id, c.position,
                           s.type, s.title, s.location, s.tags
                    FROM chunks c
                    JOIN sources s ON c.source_id = s.source_id
                    WHERE c.chunk_id IN ({placeholders})
                    """,
                    ids,
                ):
                    full_texts[row["chunk_id"]] = {
                        "text": row["text"],
                        "source_id": row["source_id"],
                        "position": row["position"],
                        "source_type": row["type"],
                        "title": row["title"],
                        "location": row["location"],
                        "tags": json.loads(row["tags"]),
                    }

        results = []
        top_score = 0.0
        for m in matches:
            cid = m["id"] if isinstance(m, dict) else m.id
            score = m["score"] if isinstance(m, dict) else m.score
            top_score = max(top_score, score)
            meta = full_texts.get(cid, {})
            results.append(
                {
                    "chunk_id": cid,
                    "score": score,
                    "text": meta.get("text", ""),
                    "title": meta.get("title", ""),
                    "location": meta.get("location", ""),
                    "source_type": meta.get("source_type", ""),
                    "source_id": meta.get("source_id", ""),
                    "tags": meta.get("tags", []),
                }
            )

        confidence = "high" if top_score >= 0.75 else (
            "medium" if top_score >= SIMILARITY_LOW_CONFIDENCE else "low"
        )

        return {
            "question": question,
            "top_score": top_score,
            "confidence": confidence,
            "results": results,
        }

    def neighbors(self, vector: list[float], k: int = 5) -> list[dict]:
        """Used during ingest to detect near-duplicates by similarity."""
        res = self.index.query(
            vector=vector,
            top_k=k,
            include_metadata=True,
            namespace=DEFAULT_NAMESPACE,
        )
        out = []
        matches = res.get("matches", []) if isinstance(res, dict) else res.matches
        for m in matches:
            out.append(
                {
                    "chunk_id": m["id"] if isinstance(m, dict) else m.id,
                    "score": m["score"] if isinstance(m, dict) else m.score,
                }
            )
        return out

    # ---- inspect ---------------------------------------------------------

    def list_sources(
        self, type_filter: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        with db_connection(self.db_path) as conn:
            if type_filter:
                rows = conn.execute(
                    """
                    SELECT s.*, COUNT(c.chunk_id) AS n_chunks
                    FROM sources s
                    LEFT JOIN chunks c ON s.source_id = c.source_id
                    WHERE s.type = ?
                    GROUP BY s.source_id
                    ORDER BY s.last_verified_at DESC
                    LIMIT ?
                    """,
                    (type_filter, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT s.*, COUNT(c.chunk_id) AS n_chunks
                    FROM sources s
                    LEFT JOIN chunks c ON s.source_id = c.source_id
                    GROUP BY s.source_id
                    ORDER BY s.last_verified_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [
            {
                "source_id": r["source_id"],
                "type": r["type"],
                "title": r["title"],
                "location": r["location"],
                "ingested_at": r["ingested_at"],
                "last_verified_at": r["last_verified_at"],
                "tags": json.loads(r["tags"]),
                "n_chunks": r["n_chunks"],
            }
            for r in rows
        ]

    def show_source(self, source_id: str) -> Optional[dict]:
        with db_connection(self.db_path) as conn:
            srow = conn.execute(
                "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if not srow:
                return None
            chunks = conn.execute(
                "SELECT chunk_id, position, text FROM chunks "
                "WHERE source_id = ? ORDER BY position",
                (source_id,),
            ).fetchall()
        return {
            "source": {
                "source_id": srow["source_id"],
                "type": srow["type"],
                "title": srow["title"],
                "location": srow["location"],
                "ingested_at": srow["ingested_at"],
                "last_verified_at": srow["last_verified_at"],
                "tags": json.loads(srow["tags"]),
                "extra": json.loads(srow["extra"]),
            },
            "chunks": [
                {
                    "chunk_id": c["chunk_id"],
                    "position": c["position"],
                    "text": c["text"],
                }
                for c in chunks
            ],
        }

    def status(self) -> dict:
        with db_connection(self.db_path) as conn:
            n_sources = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
            n_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            n_gaps = conn.execute(
                "SELECT COUNT(*) AS n FROM gaps WHERE resolved = 0"
            ).fetchone()["n"]
        env_ok = {
            "PINECONE_API_KEY": bool(os.environ.get("PINECONE_API_KEY")),
            "VOYAGE_API_KEY": bool(os.environ.get("VOYAGE_API_KEY")),
        }
        # Touching the index will create it if missing
        index_ok = False
        try:
            ix = get_pinecone_index()
            stats = ix.describe_index_stats()
            index_ok = True
            try:
                pinecone_count = stats.total_vector_count  # type: ignore
            except AttributeError:
                pinecone_count = stats.get("total_vector_count")  # type: ignore
        except SystemExit:
            pinecone_count = None
        except Exception as e:
            pinecone_count = f"error: {e}"

        return {
            "db_path": str(self.db_path),
            "index_name": DEFAULT_INDEX_NAME,
            "namespace": DEFAULT_NAMESPACE,
            "embed_model": DEFAULT_EMBED_MODEL,
            "embed_dim": DEFAULT_EMBED_DIM,
            "env": env_ok,
            "index_ok": index_ok,
            "n_sources": n_sources,
            "n_chunks": n_chunks,
            "n_open_gaps": n_gaps,
            "pinecone_vector_count": pinecone_count,
        }


# ---- CLI ------------------------------------------------------------------


def _cli():
    parser = argparse.ArgumentParser(description="Knowledge brain store inspector")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    p_list = sub.add_parser("list-sources")
    p_list.add_argument("--type")
    p_list.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show-source")
    p_show.add_argument("source_id")

    p_del = sub.add_parser("delete-source")
    p_del.add_argument("source_id")
    p_del.add_argument("--yes", action="store_true",
                       help="confirm destructive action")

    args = parser.parse_args()
    kb = KBStore()

    if args.cmd == "status":
        print(json.dumps(kb.status(), indent=2, default=str))
    elif args.cmd == "list-sources":
        print(json.dumps(
            kb.list_sources(type_filter=args.type, limit=args.limit),
            indent=2, default=str,
        ))
    elif args.cmd == "show-source":
        out = kb.show_source(args.source_id)
        print(json.dumps(out, indent=2, default=str)
              if out else f"no source {args.source_id}")
    elif args.cmd == "delete-source":
        if not args.yes:
            print("refusing to delete without --yes", file=sys.stderr)
            sys.exit(1)
        n = kb.delete_source(args.source_id)
        print(json.dumps({"deleted_chunks": n, "source_id": args.source_id}))


if __name__ == "__main__":
    _cli()
