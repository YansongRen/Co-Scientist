"""Proximity agent — embeds + clusters hypotheses. No LLM call.

The dedup-on-save path is handled inline by Generation/Evolution agents (they
need the FAISS read to skip duplicates before persisting). This agent owns the
*batch* recluster: it scans hypotheses that don't yet have an embedding,
embeds them, refreshes the index on disk, then runs agglomerative clustering
on the cosine distance matrix to refresh `hypotheses.dedup_cluster`.
"""

from __future__ import annotations

import numpy as np

from ..logging import get_logger
from ..models import Task, TaskResult
from ..storage.repos import hypotheses as hyp_repo
from ..vectors.embedder import make_embedder
from ..vectors.store import FaissStore
from .base import BaseAgent

log = get_logger("proximity")


class ProximityAgent(BaseAgent):
    name = "proximity"

    async def execute(self, task: Task) -> TaskResult:
        # Batch repair/clustering uses the same session admission lock as saves.
        from ..storage.artifacts import entity_lock
        from ..vectors import admission
        async with entity_lock(self.deps.cfg, task.session_id, admission.POOL_LOCK):
            return await self._refresh(task)

    async def _refresh(self, task):
        from ..vectors import admission
        sid = task.session_id
        embedder = make_embedder(self.deps.cfg)
        embedder, _ = await admission.embed_with_status(self.deps, sid, embedder, [])
        try:
            store, n_new = await admission.synced_store(self.deps, sid, embedder)
        except Exception as exc:
            from ..vectors.embedder import HashEmbedder
            if isinstance(embedder, HashEmbedder):
                raise
            log.warning("proximity_embedding_degraded_mode", error_type=type(exc).__name__, session_id=sid)
            embedder, _ = await admission.embed_with_status(self.deps, sid, HashEmbedder(self.deps.cfg), [])
            store, n_new = await admission.synced_store(self.deps, sid, embedder)
        clusters = None
        if task.payload.get("rebuild", False) and store.n >= 2:
            await self._activity(task, "更新相似簇标记(不删除假说)", n_items=store.n)
            clusters = await self._cluster(store, threshold=self.deps.cfg.vectors.cluster_threshold)
            if clusters:
                for hid, cluster in clusters.items():
                    await hyp_repo.set_dedup_cluster(self.deps.db, hid, cluster)
        sizes = {}
        for label in (clusters or {}).values():
            sizes[label] = sizes.get(label, 0) + 1
        similar = sum(n for n in sizes.values() if n > 1)
        await admission.note(self.deps, sid, "proximity_refreshed", indexed=store.n,
            clusters=len(sizes), similar_hypotheses=similar, backend=embedder.model,
            behavior="labels_only_no_deletion")
        return TaskResult(kind="proximity_updated", extra={"n_indexed": store.n, "n_new": n_new,
            "clustered": clusters is not None, "n_clusters": len(sizes), "similar_hypotheses": similar,
            "backend": embedder.model, "hash_fallback": embedder.model == "hash-fallback",
            "behavior": "labels_only_no_deletion"})

    # ----------------------------- clustering ----------------------------- #

    async def _cluster(
        self, store: FaissStore, *, threshold: float
    ) -> dict[str, str] | None:
        """Run sklearn agglomerative clustering on the cosine distance matrix.

        Returns {hypothesis_id: cluster_label}. Threshold is the maximum cosine
        *distance* (1 - cosine_similarity) that triggers merging.
        """
        import asyncio

        from sklearn.cluster import AgglomerativeClustering

        n = store.n
        if n < 2 or store.index is None:
            return None
        sim = await store.cosine_matrix()    # locked + threaded
        dist = 1.0 - sim
        np.fill_diagonal(dist, 0.0)
        # symmetrize numerically
        dist = (dist + dist.T) / 2.0
        np.clip(dist, 0.0, 2.0, out=dist)

        def _do() -> np.ndarray:
            ac = AgglomerativeClustering(
                n_clusters=None,
                metric="precomputed",
                linkage="average",
                distance_threshold=threshold,
            )
            return ac.fit_predict(dist)

        labels = await asyncio.to_thread(_do)
        out: dict[str, str] = {}
        for i, label in enumerate(labels):
            hid = store.hypothesis_at(i)
            if hid is None:
                continue
            out[hid] = f"c{int(label):04d}"
        return out
