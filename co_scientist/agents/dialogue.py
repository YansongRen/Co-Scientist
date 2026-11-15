"""Bounded model turns that cannot register a hypothesis by themselves."""
from __future__ import annotations

from ..llm.routing import route
from ..llm.types import AgentCallSpec, CachedBlock, CallContext
from ..safety.quoting import quote_untrusted


async def text_turns(agent, *, agent_name: str, mode: str, action: str,
                     session_id: str, task_id: str | None, prompts: list[str],
                     system_blocks: list[CachedBlock], routes: list | None = None) -> list[str]:
    """Each prompt sees the earlier turns. Tools stay off, so a turn cannot publish."""
    texts: list[str] = []
    for index, prompt in enumerate(prompts):
        user = prompt
        if texts:
            prior = "\n\n".join(
                f"## Turn {number}\n{quote_untrusted(text, id_=f'{mode}-turn-{number}')}"
                for number, text in enumerate(texts, start=1)
            )
            user += "\n\n# Prior simulated turns\n" + prior
        spec = AgentCallSpec(
            route=routes[index] if routes else route(agent.deps.cfg, agent_name, mode),
            system_blocks=system_blocks,
            user_blocks=[CachedBlock(user, cache=False)],
            tools=[],
            tool_choice=None,
            max_output_tokens=1024,
        )
        ctx = CallContext(
            session_id=session_id, task_id=task_id,
            agent=agent_name, action=action, mode=mode,
        )
        response = await agent.deps.llm.call(spec, ctx)
        texts.append(agent._final_text(response) or "(no text in this turn)")
    return texts
