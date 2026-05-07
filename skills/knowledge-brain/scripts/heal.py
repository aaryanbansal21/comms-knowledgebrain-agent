"""
heal.py — self-healing routines for the knowledge brain.

Subcommands:
    dedup                  collapse near-duplicate chunks (across sources)
    refresh-stale          re-fetch URL sources older than --max-age-days
    contradictions         surface chunks whose claims look contradictory
    gaps                   list questions that returned weak matches
    log-gap "<question>"   manually log a knowledge gap
    resolve A B            keep chunk A, drop chunk B (after a contradiction call)
    supersede OLD NEW      mark OLD as superseded by NEW (drop OLD, keep NEW)
    reembed                re-embed every chunk (for embed-model swaps)
    all                    dedup + refresh-stale + contradictions

Most subcommands print JSON. Destructive ops require --yes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kb_store import (  # noqa: E402
    DEFAULT_NAMESPACE,
    SIMILARITY_DEDUP_THRESHOLD,
    KBStore,
    db_connection,
    embed_query,
    embed_texts,
    get_pinecone_index,
)


# ---- dedup ---------------------------------------------------------------


def dedup(yes: bool = False) -> dict:
    """
    Find near-duplicate chunk pairs (similarity >= SIMILARITY_DEDUP_THRESHOLD)
    and, when confirmed with --yes, drop the newer of each pair.
    """
    kb = KBStore()
    index = kb.index
    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    with db_connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text, ingested_at FROM chunks ORDER BY ingested_at"
        ).fetchall()

    # Walk newest → oldest, query Pinecone for each chunk's vector neighbors.
    # We keep the older chunk as canonical.
    for row in reversed(rows):
        cid = row["chunk_id"]
        # We don't store vectors locally; re-embed the text to get its vector.
        # (Cheaper than fetching from Pinecone given Voyage's batching.)
        vec = embed_query(row["text"])
        res = index.query(
            vector=vec, top_k=5, include_metadata=False,
            namespace=DEFAULT_NAMESPACE,
        )
        matches = res.get("matches", []) if isinstance(res, dict) else res.matches
        for m in matches:
            mid = m["id"] if isinstance(m, dict) else m.id
            score = m["score"] if isinstance(m, dict) else m.score
            if mid == cid or score < SIMILARITY_DEDUP_THRESHOLD:
                continue
            pair = tuple(sorted((cid, mid)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            candidates.append({"keep": pair[0], "drop": cid if cid != pair[0] else mid,
                               "score": score})

    if not yes:
        return {"action": "dedup", "candidates": candidates,
                "note": "rerun with --yes to drop the duplicates"}

    dropped = []
    if candidates:
        ids = [c["drop"] for c in candidates]
        with db_connection() as conn:
            conn.executemany(
                "DELETE FROM chunks WHERE chunk_id = ?",
                [(i,) for i in ids],
            )
        for i in range(0, len(ids), 1000):
            index.delete(ids=ids[i : i + 1000], namespace=DEFAULT_NAMESPACE)
        dropped = ids
    return {"action": "dedup", "dropped_chunk_ids": dropped, "n_dropped": len(dropped)}


# ---- refresh stale URLs --------------------------------------------------


def refresh_stale(max_age_days: int = 30) -> dict:
    cutoff = time.time() - (max_age_days * 86400)
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT source_id, location FROM sources "
            "WHERE type = 'url' AND last_verified_at < ?",
            (cutoff,),
        ).fetchall()
    if not rows:
        return {"action": "refresh-stale", "stale": [], "note": "nothing stale"}

    # Re-ingest each stale URL by shelling out to ingest.py logic in-process
    from ingest import ingest_url  # local import to avoid cycle at import time

    refreshed: list[dict] = []
    for row in rows:
        url = row["location"]
        try:
            res = ingest_url(url, tags=[], title=None)
            refreshed.append({"url": url, "result": res})
        except Exception as e:
            refreshed.append({"url": url, "error": str(e)})
    return {"action": "refresh-stale", "refreshed": refreshed}


# ---- contradictions ------------------------------------------------------


def find_contradictions(limit: int = 20) -> dict:
    """
    Heuristic: pull pairs of chunks with high similarity (likely about the
    same topic) but with negation markers in opposite directions, OR with
    explicitly conflicting numbers/dates.

    This is a triage tool — Claude must read both and decide. The output is a
    candidate list; resolution happens via `resolve` or `supersede`.
    """
    kb = KBStore()
    index = kb.index
    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()

    NEG_TOKENS = (" not ", " no ", " never ", " false", "isn't", "aren't",
                  "don't", "doesn't", "won't", "cannot", "can't")

    with db_connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text, source_id FROM chunks"
        ).fetchall()
    text_by_id = {r["chunk_id"]: r["text"] for r in rows}
    src_by_id = {r["chunk_id"]: r["source_id"] for r in rows}

    for r in rows[:limit]:
        cid = r["chunk_id"]
        vec = embed_query(r["text"])
        res = index.query(vector=vec, top_k=5, include_metadata=False,
                          namespace=DEFAULT_NAMESPACE)
        matches = res.get("matches", []) if isinstance(res, dict) else res.matches
        for m in matches:
            mid = m["id"] if isinstance(m, dict) else m.id
            score = m["score"] if isinstance(m, dict) else m.score
            if mid == cid or score < 0.78:
                continue
            if src_by_id.get(mid) == src_by_id.get(cid):
                continue  # same source — not a contradiction, just redundancy
            pair = tuple(sorted((cid, mid)))
            if pair in seen:
                continue
            seen.add(pair)
            a = r["text"].lower()
            b = (text_by_id.get(mid) or "").lower()
            a_has_neg = any(t in a for t in NEG_TOKENS)
            b_has_neg = any(t in b for t in NEG_TOKENS)
            if a_has_neg ^ b_has_neg:
                pairs.append({
                    "chunk_a": cid, "chunk_b": mid, "score": score,
                    "preview_a": r["text"][:240],
                    "preview_b": (text_by_id.get(mid) or "")[:240],
                    "reason": "negation-asymmetry",
                })

    # Persist detected pairs for the audit trail
    if pairs:
        with db_connection() as conn:
            for p in pairs:
                conn.execute(
                    "INSERT INTO contradiction_log(chunk_a, chunk_b, note, "
                    "detected_at) VALUES (?, ?, ?, ?)",
                    (p["chunk_a"], p["chunk_b"], p["reason"], time.time()),
                )
    return {"action": "contradictions", "pairs": pairs}


def resolve(keep: str, drop: str, reason: str) -> dict:
    return _resolve_pair(keep=keep, drop=drop, reason=reason, mode="kept")


def supersede(old: str, new: str, reason: str) -> dict:
    return _resolve_pair(keep=new, drop=old, reason=reason, mode="superseded")


def _resolve_pair(keep: str, drop: str, reason: str, mode: str) -> dict:
    kb = KBStore()
    with db_connection() as conn:
        existing = conn.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id = ?", (drop,)
        ).fetchone()
        if not existing:
            return {"action": "resolve", "ok": False, "reason": f"chunk {drop} not found"}
        conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (drop,))
        conn.execute(
            "UPDATE contradiction_log SET resolution = ? "
            "WHERE (chunk_a = ? AND chunk_b = ?) OR (chunk_a = ? AND chunk_b = ?)",
            (mode, keep, drop, drop, keep),
        )
    kb.index.delete(ids=[drop], namespace=DEFAULT_NAMESPACE)
    return {"action": "resolve", "ok": True, "kept": keep, "dropped": drop,
            "mode": mode, "reason": reason}


# ---- gaps ----------------------------------------------------------------


def list_gaps(only_open: bool = True, limit: int = 50) -> dict:
    with db_connection() as conn:
        if only_open:
            rows = conn.execute(
                "SELECT id, question, reason, logged_at, resolved FROM gaps "
                "WHERE resolved = 0 ORDER BY logged_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, question, reason, logged_at, resolved FROM gaps "
                "ORDER BY logged_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {
        "gaps": [
            {
                "id": r["id"], "question": r["question"],
                "reason": r["reason"], "logged_at": r["logged_at"],
                "resolved": bool(r["resolved"]),
            }
            for r in rows
        ]
    }


def log_gap(question: str, reason: str = "manual") -> dict:
    with db_connection() as conn:
        cur = conn.execute(
            "INSERT INTO gaps(question, reason, logged_at) VALUES (?, ?, ?)",
            (question, reason, time.time()),
        )
        gid = cur.lastrowid
    return {"action": "log-gap", "id": gid, "question": question, "reason": reason}


def resolve_gap(gap_id: int) -> dict:
    with db_connection() as conn:
        conn.execute("UPDATE gaps SET resolved = 1 WHERE id = ?", (gap_id,))
    return {"action": "resolve-gap", "id": gap_id}


# ---- reembed -------------------------------------------------------------


def reembed(batch_size: int = 100) -> dict:
    kb = KBStore()
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text, source_id FROM chunks"
        ).fetchall()
    if not rows:
        return {"action": "reembed", "n": 0}
    n = 0
    # Fetch source metadata once
    with db_connection() as conn:
        src_rows = conn.execute(
            "SELECT source_id, type, title, location, tags FROM sources"
        ).fetchall()
    src_by_id = {
        r["source_id"]: {
            "type": r["type"],
            "title": r["title"],
            "location": r["location"],
            "tags": json.loads(r["tags"]),
        }
        for r in src_rows
    }
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        vecs = embed_texts([r["text"] for r in batch])
        kb.index.upsert(
            vectors=[
                {
                    "id": r["chunk_id"],
                    "values": v,
                    "metadata": {
                        **src_by_id.get(r["source_id"], {}),
                        "source_id": r["source_id"],
                        "text_preview": r["text"][:300],
                    },
                }
                for r, v in zip(batch, vecs)
            ],
            namespace=DEFAULT_NAMESPACE,
        )
        n += len(batch)
    return {"action": "reembed", "n": n}


# ---- CLI -----------------------------------------------------------------


def _cli():
    parser = argparse.ArgumentParser(description="Self-heal the knowledge brain")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dedup = sub.add_parser("dedup")
    p_dedup.add_argument("--yes", action="store_true")

    p_ref = sub.add_parser("refresh-stale")
    p_ref.add_argument("--max-age-days", type=int, default=30)

    p_con = sub.add_parser("contradictions")
    p_con.add_argument("--limit", type=int, default=20)

    p_gaps = sub.add_parser("gaps")
    p_gaps.add_argument("--all", action="store_true",
                        help="include resolved gaps")
    p_gaps.add_argument("--limit", type=int, default=50)

    p_log = sub.add_parser("log-gap")
    p_log.add_argument("question")
    p_log.add_argument("--reason", default="manual")

    p_resolve_gap = sub.add_parser("resolve-gap")
    p_resolve_gap.add_argument("gap_id", type=int)

    p_res = sub.add_parser("resolve")
    p_res.add_argument("keep")
    p_res.add_argument("drop")
    p_res.add_argument("--reason", default="")

    p_sup = sub.add_parser("supersede")
    p_sup.add_argument("old")
    p_sup.add_argument("new")
    p_sup.add_argument("--reason", default="")

    sub.add_parser("reembed")

    p_all = sub.add_parser("all")
    p_all.add_argument("--yes", action="store_true",
                       help="apply dedup drops (otherwise dry-run)")
    p_all.add_argument("--max-age-days", type=int, default=30)

    args = parser.parse_args()

    if args.cmd == "dedup":
        out = dedup(yes=args.yes)
    elif args.cmd == "refresh-stale":
        out = refresh_stale(max_age_days=args.max_age_days)
    elif args.cmd == "contradictions":
        out = find_contradictions(limit=args.limit)
    elif args.cmd == "gaps":
        out = list_gaps(only_open=not args.all, limit=args.limit)
    elif args.cmd == "log-gap":
        out = log_gap(args.question, args.reason)
    elif args.cmd == "resolve-gap":
        out = resolve_gap(args.gap_id)
    elif args.cmd == "resolve":
        out = resolve(keep=args.keep, drop=args.drop, reason=args.reason)
    elif args.cmd == "supersede":
        out = supersede(old=args.old, new=args.new, reason=args.reason)
    elif args.cmd == "reembed":
        out = reembed()
    elif args.cmd == "all":
        out = {
            "dedup": dedup(yes=args.yes),
            "refresh_stale": refresh_stale(max_age_days=args.max_age_days),
            "contradictions": find_contradictions(),
        }
    else:
        parser.error("unknown command")
        return

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
