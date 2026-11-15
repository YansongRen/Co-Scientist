"""Register a researcher-written hypothesis in the same review chain."""
from __future__ import annotations

from datetime import UTC, datetime

from .. import ids
from ..agents.base import AgentDeps
from ..models import Task
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import sessions as sess_repo
from ..storage.repos import tasks as task_repo
from ..vectors import admission
from ..vectors.embedder import make_embedder


class _Publisher:
    name = "researcher"

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def _dedup_query(self, session_id, text):
        return await admission.query(self.deps, session_id, text, make_embedder)

    async def _dedup_commit(self, session_id, hypothesis_id, payload):
        await admission.commit(self.deps, session_id, hypothesis_id, payload)


def _markdown(statement: str) -> str:
    return (
        f"# Researcher hypothesis\n\n**Hypothesis.** {statement}\n\n"
        "Submitted by the researcher for the same review and tournament path. "
        "It has not been reviewed yet."
    )


async def submit_researcher_hypothesis(deps: AgentDeps, session_id: str, text: str) -> dict:
    statement = " ".join(text.split())
    if len(statement) < 20 or len(statement) > 4000:
        raise ValueError("假设正文需要 20 到 4000 个字符")
    session = await sess_repo.fetch(deps.db, session_id)
    if session is None:
        raise ValueError(f"no such session: {session_id}")
    record = {
        "title": statement[:180],
        "statement": statement,
        "mechanism": "Researcher-submitted hypothesis awaiting review.",
        "parent_ids": [],
    }
    expected = ids.hypothesis_id(session_id, "researcher/human", statement)
    hid, was_new = await admission.publish(
        _Publisher(deps), session_id, record, "human",
        lambda _record: _markdown(statement), [],
    )
    if hid is None:
        raise ValueError("假设池已满，没有加入这条假设")
    if not was_new and hid != expected:
        raise ValueError("这条假设与已有假设过于接近，没有另建一条")
    existing = await hyp_repo.fetch(deps.db, hid)
    queued = False
    if existing is not None and existing.state == "draft":
        queued = bool(await task_repo.enqueue(deps.db, Task(
            id=ids.task_id(), session_id=session_id, created_at=datetime.now(UTC),
            agent="reflection", action="ReviewHypothesis", target_id=hid,
            payload={"kind": "initial"}, priority=70, status="pending",
            idempotency_key=f"{hid}::review::initial",
        )))
    return {"hypothesis_id": hid, "created": was_new, "review_queued": queued}
