"""Deterministic execution diagnostics, separate from scientific validation."""
from __future__ import annotations

import json
from collections import Counter

from ..storage.artifacts import write_json
from ..storage.repos import hypotheses, sessions
from ..vectors.store import FaissStore


async def collect(cfg, conn, sid):
    async with conn.execute("SELECT id,payload,status,result_json,agent FROM tasks WHERE session_id=? ORDER BY rowid", (sid,)) as cur:
        jobs = [dict(r) for r in await cur.fetchall()]
    async with conn.execute("SELECT event,payload FROM events WHERE session_id=? ORDER BY rowid", (sid,)) as cur:
        logs = [(r["event"], json.loads(r["payload"] or "{}")) for r in await cur.fetchall()]
    pool = await hypotheses.list_for_session(conn, sid)
    gen = [r for r in jobs if r["agent"] == "generation"]
    evo = [r for r in jobs if r["agent"] == "evolution"]
    allocation, success, effective = (Counter(dict.fromkeys(cfg.generation.strategies, 0)) for _ in range(3))
    for r in gen:
        strategy = json.loads(r["payload"]).get("strategy", "literature")
        allocation[strategy] += 1
        if r["status"] == "done":
            success[strategy] += 1
        result = json.loads(r["result_json"] or "{}")
        if result.get("extra", {}).get("effective_strategy"):
            effective[result["extra"]["effective_strategy"]] += 1
    generated = Counter(dict.fromkeys(cfg.generation.strategies, 0))
    generated.update(h.strategy for h in pool if h.created_by == "generation")
    clusters = Counter(h.dedup_cluster for h in pool if h.dedup_cluster)
    modes = [p for name, p in logs if name == "embedding_mode"]
    backends = sorted({p["backend"] for p in modes if p.get("backend")})
    active = modes[-1] if modes else None
    similar, indexed, ratio = set(), set(), None
    error = None
    # Bounded exact pair scan. Unknown coverage is reported, never invented as 0.
    if active and len(pool) <= 500:
        try:
            store = FaissStore(cfg, sid, active["dim"], model=active["backend"])
            await store.load_or_create()
            positions = [(h.id, store.offset_of(h.id)) for h in pool]
            positions = [(hid, off) for hid, off in positions if off is not None]
            indexed = {hid for hid, _ in positions}
            matrix = await store.cosine_matrix()
            for i, (hid, off) in enumerate(positions):
                for other, off2 in positions[i+1:]:
                    if float(matrix[off, off2]) >= cfg.vectors.dedup_cosine_threshold:
                        similar.update([hid, other])
            if len(indexed) == len(pool) and pool:
                ratio = len(similar) / len(pool)
        except (OSError, ValueError, RuntimeError) as exc:
            error = type(exc).__name__
    blocked = {p.get("candidate_id") for name, p in logs if name == "hypothesis_dedup_blocked"}
    evolution = [{"task_id": r["id"], "status": r["status"],
        "requested_strategies": json.loads(r["payload"]).get("strategies", []),
        "strategies_used": json.loads(r["result_json"] or "{}").get("extra", {}).get("strategies_used", [])}
        for r in evo]
    return {"version": 1, "initial_generation_tasks": sum(json.loads(r["payload"]).get("generation_round") == "initial" for r in gen),
        "generation_tasks_by_strategy": dict(allocation), "generation_completed_by_requested_strategy": dict(success),
        "generation_completed_by_effective_strategy": dict(effective), "generation_hypotheses_by_strategy": dict(generated),
        "dedup_blocked_unique_candidates": len(blocked),
        "similarity_flagged_candidates": len({p.get("candidate_id") for name,p in logs if name == "hypothesis_similarity_flagged"}),
        "cluster_count": len(clusters), "similar_cluster_hypotheses": sum(n for n in clusters.values() if n > 1),
        "evolution_trigger_count": len(evolution), "evolution_tasks": evolution,
        "embedding_backends": backends, "active_embedding_backend": active["backend"] if active else "unavailable",
        "used_hash_fallback": any(p.get("hash_fallback") for p in modes),
        "embedding_commit_failures": sum(name == "embedding_commit_failed" for name,_ in logs),
        "embedding_cache_repairs": sum(name == "embedding_cache_repaired" for name,_ in logs),
        "high_similarity_ratio": ratio, "high_similarity_hypotheses": len(similar),
        "similarity_coverage": {"indexed": len(indexed), "pool": len(pool), "error": error,
                                "scan_limit": 500, "complete": ratio is not None},
        "dedup_cosine_threshold": cfg.vectors.dedup_cosine_threshold,
        "cluster_cosine_distance_threshold": cfg.vectors.cluster_threshold,
        "interpretation": "Software diagnostics only. Cosine similarity and Elo do not establish scientific novelty or experimental validity."}


def render(summary):
    ratio = summary["high_similarity_ratio"]
    lines = ["## 假说多样性与执行诊断", "", "以下为程序记录, 不是实验验证或模型科研效果评分。", "",
        f"- 初始 Generation 任务: {summary['initial_generation_tasks']}",
        f"- 策略任务数: {json.dumps(summary['generation_tasks_by_strategy'], ensure_ascii=False)}",
        f"- 策略完成数(按请求策略, 完成不等于产生新假说): {json.dumps(summary['generation_completed_by_requested_strategy'])}",
        f"- 策略生成假说数: {json.dumps(summary['generation_hypotheses_by_strategy'])}",
        f"- 去重拦截候选数: {summary['dedup_blocked_unique_candidates']}",
        f"- 相似簇成员数: {summary['similar_cluster_hypotheses']}(仅标记, 不删除)",
        f"- Evolution 调度次数: {summary['evolution_trigger_count']}; 各轮策略见 diagnostics JSON",
        f"- Embedding backend: {', '.join(summary['embedding_backends']) or 'unavailable'}",
        f"- 最终池高相似假说比例: {ratio:.1%}" if ratio is not None else "- 最终池高相似假说比例: 不可确定(索引覆盖不完整或超过扫描上限)",
        f"- 去重 cosine similarity 阈值: {summary['dedup_cosine_threshold']}; 聚类 cosine distance 阈值: {summary['cluster_cosine_distance_threshold']}",
        f"- 相似度计算覆盖: {summary['similarity_coverage']['indexed']}/{summary['similarity_coverage']['pool']}"]
    if summary["used_hash_fallback"]:
        lines += ["", "**Degraded mode: 使用过 hash embedding fallback。它主要捕捉词汇重叠, 不能可靠识别语义改写。未被拦截不表示语义上不相似, 也不表示科学上新颖。**"]
    return "\n".join(lines)


async def record(cfg, conn, sid):
    summary = await collect(cfg, conn, sid)
    path = await write_json(cfg, sid, "diagnostics", "hypothesis_diversity", summary)
    session = await sessions.fetch(conn, sid)
    snapshot = session.config_snapshot
    snapshot["diversity_diagnostics"] = {**summary, "artifact_path": path}
    await conn.execute("UPDATE sessions SET config_snapshot=? WHERE id=?", (json.dumps(snapshot), sid))
    await conn.commit()
    return summary
