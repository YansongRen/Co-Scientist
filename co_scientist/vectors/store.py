"""Per-session FAISS index wrapper.

Uses `IndexFlatIP` over L2-normalized vectors → exact cosine similarity.
Tradeoff: O(N) search, fine for N < ~10k hypotheses per session. If a session
ever grows past that, swap to `IndexHNSWFlat` (changes file format; bump
embeddings_meta migration).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from contextlib import contextmanager

import faiss
import numpy as np

from ..config import Config
from ..logging import get_logger


class FaissStore:
    def __init__(self, cfg: Config, session_id: str, dim: int) -> None:
        self.cfg = cfg
        self.session_id = session_id
        self.dim = dim
        self.index: faiss.IndexFlatIP | None = None
        self._dir = cfg.session_vector_dir(session_id)
        self._index_path = self._dir / "index.faiss"
        self._meta_path = self._dir / "index.meta.json"
        self._ordered_ids: list[str] = []   # hypothesis_id at each faiss offset
        self._offset_by_id: dict[str, int] = {}  # mirror of _ordered_ids for O(1) offset_of()
        # FAISS itself is not thread-safe. Even though our `to_thread` calls
        # serialize on the asyncio loop, they spawn into the default thread
        # pool — multiple coroutines calling `add` / `search` / `save`
        # concurrently can land on different threads and corrupt the index.
        self._lock = asyncio.Lock()

    # ------------------------- lifecycle -------------------------------- #

    async def load_or_create(self) -> None:
        """Load the session's index, or start a fresh one.

        An index whose width no longer matches `embeddings.dim` is discarded and
        rebuilt. Changing the embedding model or its `dimensions` invalidates
        every vector already stored, and FAISS would otherwise fail deep inside
        `add`/`search` with an assertion that says nothing about the cause.
        Rebuilding costs re-embedding; keeping it would mean silent nonsense.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        def _do() -> tuple[faiss.IndexFlatIP, list[str], int | None]:
            if self._index_path.exists() and self._meta_path.exists():
                idx = faiss.read_index(str(self._index_path))
                meta = json.loads(self._meta_path.read_text())
                # `idx.d` is authoritative; meta["dim"] is only a record of what
                # wrote it, and predates this check on older indices.
                stored_dim = getattr(idx, "d", None) or meta.get("dim")
                if stored_dim == self.dim:
                    return idx, list(meta.get("ordered_ids", [])), None
                return faiss.IndexFlatIP(self.dim), [], stored_dim
            return faiss.IndexFlatIP(self.dim), [], None

        self.index, self._ordered_ids, discarded_dim = await asyncio.to_thread(_do)
        if discarded_dim is not None:
            get_logger("vectors.store").warning(
                "faiss_index_dim_mismatch_rebuilding",
                session_id=self.session_id,
                found_dim=discarded_dim,
                expected_dim=self.dim,
                path=str(self._index_path),
            )
        self._offset_by_id = {hid: i for i, hid in enumerate(self._ordered_ids)}

    async def save(self) -> None:
        assert self.index is not None

        # File locking protects multiple stores; each leaf is atomically replaced.

        def _do() -> None:
            with self._disk_lock():
                self._merge_disk()
                self._save_disk()

        async with self._lock:
            await asyncio.to_thread(_do)

    @contextmanager
    def _disk_lock(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        with (self._dir / ".index.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _merge_disk(self):
        if not (self._index_path.exists() and self._meta_path.exists()):
            return
        latest = faiss.read_index(str(self._index_path))
        ids = json.loads(self._meta_path.read_text())["ordered_ids"]
        if latest.d != self.dim:
            return
        if latest.ntotal != len(ids) or len(set(ids)) != len(ids):
            raise ValueError("FAISS index/metadata inconsistent; rebuild derived index")
        for index, hid in enumerate(self._ordered_ids):
            if hid not in ids:
                latest.add(self.index.reconstruct(index)[None, :])
                ids.append(hid)
        self.index = latest
        self._ordered_ids = ids
        self._offset_by_id = {hid: i for i, hid in enumerate(ids)}

    def _save_disk(self):
        idx_tmp = self._index_path.with_suffix(".faiss.tmp")
        meta_tmp = self._meta_path.with_suffix(".json.tmp")
        faiss.write_index(self.index, str(idx_tmp))
        meta_tmp.write_text(json.dumps({"dim": self.dim, "ordered_ids": self._ordered_ids}))
        os.replace(idx_tmp, self._index_path)
        os.replace(meta_tmp, self._meta_path)

    # ------------------------- ops -------------------------------------- #

    @property
    def n(self) -> int:
        return self.index.ntotal if self.index is not None else 0

    async def add(self, hypothesis_id: str, vec: np.ndarray) -> int:
        """Append one vector. Returns its FAISS offset."""
        assert self.index is not None
        if vec.ndim == 1:
            vec = vec[None, :]

        def _do() -> int:
            with self._disk_lock():
                self._merge_disk()
                if hypothesis_id in self._offset_by_id:
                    return self._offset_by_id[hypothesis_id]
                off = self.index.ntotal
                self.index.add(vec.astype("float32"))
                self._ordered_ids.append(hypothesis_id)
                self._offset_by_id[hypothesis_id] = off
                self._save_disk()
                return off

        async with self._lock:
            offset = await asyncio.to_thread(_do)
        return offset

    async def search(
        self, query: np.ndarray, k: int = 5
    ) -> list[tuple[str, float]]:
        """Return [(hypothesis_id, cosine_sim)] best matches.

        Vectors are L2-normalized so inner product == cosine.
        """
        assert self.index is not None
        if self.n == 0:
            return []
        if query.ndim == 1:
            query = query[None, :]

        def _do(qk: int) -> tuple[np.ndarray, np.ndarray]:
            dists, idxs = self.index.search(query.astype("float32"), qk)
            return dists, idxs

        async with self._lock:
            k = min(k, self.n)
            dists, idxs = await asyncio.to_thread(_do, k)
            ordered = list(self._ordered_ids)
        out: list[tuple[str, float]] = []
        for sim, idx in zip(dists[0], idxs[0], strict=True):
            if idx < 0 or idx >= len(ordered):
                continue
            out.append((ordered[int(idx)], float(sim)))
        return out

    async def cosine_matrix(self) -> np.ndarray:
        """Full N×N cosine similarity matrix. Used for clustering."""
        assert self.index is not None
        if self.n == 0:
            return np.zeros((0, 0), dtype="float32")

        def _do() -> np.ndarray:
            vecs = self.index.reconstruct_n(0, self.n)
            return vecs @ vecs.T

        async with self._lock:
            return await asyncio.to_thread(_do)

    def offset_of(self, hypothesis_id: str) -> int | None:
        return self._offset_by_id.get(hypothesis_id)

    def hypothesis_at(self, offset: int) -> str | None:
        if 0 <= offset < len(self._ordered_ids):
            return self._ordered_ids[offset]
        return None
