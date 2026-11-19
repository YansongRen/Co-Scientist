"""Session-serialized hypothesis query + publication; FAISS is a repairable cache.

Generation, Evolution and Proximity share the same cross-process file lock.
SQLite/artifacts remain authoritative. No network call changes scientific evidence.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import numpy as np

from .. import ids
from ..logging import get_logger
from ..models import Hypothesis
from ..storage.artifacts import entity_lock, write_json
from ..storage.repos import embeddings, events, hypotheses
from .embedder import HashEmbedder
from .store import FaissStore

log = get_logger("vectors.admission")
POOL_LOCK = "hypothesis-pool"


def embedding_text(h):
    return h.title + "\n\n" + h.summary


def checked_vectors(value, n, dim):
    a = np.asarray(value, dtype="float32")
    if a.shape != (n, dim) or not np.isfinite(a).all():
        raise ValueError("Embedding shape or finite-value check failed")
    norms = np.linalg.norm(a, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Zero embedding has no cosine similarity")
    return a / norms


async def note(deps, sid, event, **payload):
    await events.emit(deps.db, session_id=sid, task_id=None, agent="proximity", event=event, payload=payload)


async def embed_with_status(deps, sid, embedder, texts):
    """Bounded service call, explicit local lexical fallback on failure."""
    reason = None
    try:
        a = checked_vectors(await asyncio.wait_for(embedder.embed(texts),
            deps.cfg.vectors.embedding_timeout_seconds), len(texts), embedder.dim)
    except Exception as exc:
        reason = type(exc).__name__  # provider messages may contain secrets
        log.warning("embedding_service_failed_degraded_mode", session_id=sid, error_type=reason,
                    backend=getattr(embedder, "model", "unavailable"))
        embedder = HashEmbedder(deps.cfg)
        a = checked_vectors(await embedder.embed(texts), len(texts), embedder.dim)
    degraded = embedder.model == "hash-fallback"
    if degraded:
        log.warning("embedding_degraded_mode", session_id=sid, backend=embedder.model,
                    limitation="lexical overlap only; paraphrases are not reliably detected")
    await note(deps, sid, "embedding_mode", backend=embedder.model, dim=embedder.dim,
               hash_fallback=degraded, degraded=degraded, reason=reason)
    return embedder, a


async def synced_store(deps, sid, embedder):
    """Caller holds POOL_LOCK. Repair DB-only publications and offset metadata.

    A crash after DB commit and before FAISS commit cannot make the next query
    silently ignore that hypothesis. Model-specific indexes prevent space mixing.
    """
    store = FaissStore(deps.cfg, sid, dim=embedder.dim, model=embedder.model)
    try:
        await store.load_or_create()
    except (OSError, ValueError, RuntimeError) as exc:
        log.warning("embedding_cache_rebuilding", session_id=sid, error_type=type(exc).__name__)
        # Recreate only the derived cache in memory; no hypothesis is removed.
        import faiss
        store.index = faiss.IndexFlatIP(embedder.dim)
        store._ordered_ids, store._offset_by_id = [], {}
        # A corrupt on-disk pair cannot be merged by add(). Preserve it for audit.
        for p in (store._index_path, store._meta_path):
            if p.exists():
                p.rename(p.with_name(p.name + ".invalid-" + ids.task_id()))
        await note(deps, sid, "embedding_cache_repaired", error_type=type(exc).__name__)
    pool = await hypotheses.list_for_session(deps.db, sid)
    missing = [h for h in pool if store.offset_of(h.id) is None]
    for start in range(0, len(missing), 64):
        batch = missing[start:start+64]
        values = checked_vectors(await asyncio.wait_for(embedder.embed([embedding_text(h) for h in batch]),
            deps.cfg.vectors.embedding_timeout_seconds), len(batch), embedder.dim)
        for h, vec in zip(batch, values, strict=True):
            await store.add(h.id, vec)
    metadata = {r["hypothesis_id"]: r for r in await embeddings.list_for_session(deps.db, sid)
                if r["model"] == embedder.model}
    for h in pool:
        offset = store.offset_of(h.id)
        previous = metadata.get(h.id)
        text_hash = ids.text_hash(embedding_text(h))
        if previous and (previous["dim"], previous["faiss_offset"], previous["text_hash"]) == (embedder.dim, offset, text_hash):
            continue
        await embeddings.upsert(deps.db, id_=ids.embedding_id(h.id, embedder.model), session_id=sid,
            hypothesis_id=h.id, model=embedder.model, dim=embedder.dim, faiss_offset=offset,
            text_hash=ids.text_hash(embedding_text(h)))
    return store, len(missing)


async def query(deps, sid, text, factory):
    try:
        emb = factory(deps.cfg)
    except (ValueError, RuntimeError) as exc:
        log.warning("embedding_resolver_degraded_mode", error_type=type(exc).__name__, session_id=sid)
        emb = HashEmbedder(deps.cfg)
    emb, vecs = await embed_with_status(deps, sid, emb, [text])
    try:
        store, _ = await synced_store(deps, sid, emb)
    except Exception as exc:
        # Existing rows must also be embedded in the fallback space.
        if isinstance(emb, HashEmbedder):
            raise  # cache/DB failures are not a 'no duplicate' verdict
        log.warning("embedding_repair_degraded_mode", session_id=sid, error_type=type(exc).__name__)
        emb, vecs = await embed_with_status(deps, sid, HashEmbedder(deps.cfg), [text])
        store, _ = await synced_store(deps, sid, emb)
    nearest = await store.search(vecs[0], k=1)
    payload = {"vector": vecs[0], "model": emb.model, "dim": emb.dim, "text_hash": ids.text_hash(text)}
    duplicate = nearest[0][0] if nearest and nearest[0][1] >= deps.cfg.vectors.dedup_cosine_threshold else None
    return duplicate, payload


async def commit(deps, sid, hid, payload):
    store = FaissStore(deps.cfg, sid, dim=payload["dim"], model=payload["model"])
    await store.load_or_create()
    offset = await store.add(hid, payload["vector"])
    await embeddings.upsert(deps.db, id_=ids.embedding_id(hid, payload["model"]), session_id=sid,
        hypothesis_id=hid, model=payload["model"], dim=payload["dim"], faiss_offset=offset,
        text_hash=payload["text_hash"])


async def publish(agent, sid, record, strategy, render_md, citations):
    """One lock spans fresh query, capacity check, DB insert and vector commit.

    Separate workers/processes on the same filesystem obey this lock. A shared
    NFS/multi-host deployment needs a distributed admission service instead.
    """
    origin = agent.name
    statement = record.get("statement") or record.get("title") or ""
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("record_hypothesis: missing statement")
    hid = ids.hypothesis_id(sid, f"{origin}/{strategy}", statement)
    safety_state = await _hypothesis_safety(agent, sid, statement)
    if safety_state == "block":
        return None, False
    async with entity_lock(agent.deps.cfg, sid, POOL_LOCK):
        if await hypotheses.fetch(agent.deps.db, hid):
            return hid, False
        async with agent.deps.db.execute("SELECT COUNT(*) FROM hypotheses WHERE session_id=?", (sid,)) as cur:
            size = (await cur.fetchone())[0]
        if size >= agent.deps.cfg.run.max_ideas:
            await note(agent.deps, sid, "hypothesis_capacity_blocked", candidate_id=hid, origin=origin)
            return None, False
        h = Hypothesis(id=hid, session_id=sid, created_at=datetime.now(UTC), created_by=origin,
            strategy=strategy, parent_ids=record.get("parent_ids") or [], title=record.get("title", "")[:300],
            summary=statement[:1000], full_text=render_md(record), citations=citations,
            artifact_path="pending", state="quarantined" if safety_state == "quarantine" else "draft")
        duplicate, payload = await agent._dedup_query(sid, embedding_text(h))
        if duplicate and duplicate != hid and agent.deps.cfg.vectors.mark_duplicates:
            await note(agent.deps, sid, "hypothesis_dedup_blocked", candidate_id=hid,
                       duplicate_of=duplicate, origin=origin, strategy=strategy)
            return duplicate, False
        if duplicate:
            await note(agent.deps, sid, "hypothesis_similarity_flagged", candidate_id=hid, duplicate_of=duplicate)
        h.artifact_path = await write_json(agent.deps.cfg, sid, "hypotheses", hid,
            {"strategy": strategy, "record": record,
             "embedding": {k: v for k, v in (payload or {}).items() if k != "vector"}})
        inserted = await hypotheses.insert(agent.deps.db, h)
        if inserted and payload is not None:
            try:
                await agent._dedup_commit(sid, hid, payload)
            except Exception as exc:
                log.warning("embedding_commit_degraded_mode", session_id=sid, hypothesis_id=hid,
                            error_type=type(exc).__name__)
                await note(agent.deps, sid, "embedding_commit_failed", hypothesis_id=hid,
                           error_type=type(exc).__name__, repair_on_next_query=True)
        return hid, inserted


async def _hypothesis_safety(agent, sid, statement) -> str:
    """`allow`, `warn`, `quarantine`, or `block`. Fail-open when disabled."""
    if not agent.deps.cfg.safety.enable_classifier:
        return "allow"
    from ..safety.classifier import SafetyClassifier

    result = await SafetyClassifier(agent.deps.cfg).classify(
        statement, label="hypothesis", llm=getattr(agent.deps, "llm", None),
        session_id=sid,
    )
    action = result.action(agent.deps.cfg)
    await note(agent.deps, sid, "safety_assessment", label="hypothesis",
               safety_action=action, categories=result.categories,
               confidence=result.confidence)
    if action == "block":
        await note(agent.deps, sid, "hypothesis_safety_blocked",
                   categories=result.categories, origin=agent.name)
    return action
