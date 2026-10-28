"""Normalized request / response types shared by every LLM backend.

These began life as Anthropic Messages API shapes. They are now the canonical
intermediate form: a backend takes an `AgentCallSpec` and returns an
`LLMResponse` whose `.raw` quacks like an Anthropic `Message` — it exposes
`.content` (a list of blocks with `.type`), `.stop_reason`, and `.usage`.

That contract is what lets three quite different families of backend serve the
same six agents with no change to agent code, because `agents/base.py` reads
responses purely through `getattr(block, "type")`, `.name`, `.input`, `.text`:

- **Anthropic** (`anthropic_client.py`) — returns real SDK `Message` objects.
- **OpenAI and OpenAI-compatible** (`openai_client.py`) — adapts Chat
  Completions into the same shape.
- **CLI-driven** (`cli_backend/`) — `claude -p` / `codex exec` on a
  subscription login; synthesizes the shape from CLI text plus the records its
  MCP server captured.

The one place the families genuinely differ is `LLMResponse.capture`: a CLI
runs its own agentic loop, so tool use is reported after the fact in a
`CaptureBundle` rather than driven turn by turn. It is `None` for the API
backends, and `tool_loop.py` branches on that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .routing import ModelRoute


@dataclass
class CallContext:
    """Per-call metadata for accounting and persistence."""

    session_id: str
    task_id: str | None
    agent: str
    action: str
    mode: str | None = None     # e.g. "literature", "verification"


@dataclass
class CachedBlock:
    """One text block with an optional cache hint.

    `cache` was an Anthropic `cache_control` breakpoint. Under the CLI
    backends the agent harness manages its own prompt cache, so the flag is
    advisory — it is kept so no agent call site has to change.
    """

    text: str
    cache: bool = False


@dataclass
class InputImage:
    media_type: str
    data_base64: str = field(repr=False)
    attachment_id: str = ""


@dataclass
class AgentCallSpec:
    """Inputs to one LLM call, before a backend serializes them."""

    route: ModelRoute
    system_blocks: list[CachedBlock] = field(default_factory=list)
    user_blocks: list[CachedBlock] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: dict[str, Any] | None = None
    """`{"type": "auto"}` or `{"type": "tool", "name": "..."}`.

    The CLIs have no forced-tool-choice flag; backends translate a forced
    choice into a hard prompt instruction plus a post-hoc capture check.
    """
    max_output_tokens: int = 4096
    stop_sequences: list[str] | None = None
    extra_messages: list[dict[str, Any]] = field(default_factory=list)
    input_images: list[InputImage] = field(default_factory=list)
    """Appended *after* the user_blocks message — tool_use/tool_result threads."""


# --------------------------------------------------------------------------- #
# Synthetic response blocks
#
# The CLI backends do not get Anthropic SDK objects back, so they build these.
# Attribute names match the SDK's so `agents/base.py` cannot tell the
# difference.


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = ""
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class SynthMessage:
    """Stand-in for `anthropic.types.Message`."""

    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def model_dump(self) -> dict[str, Any]:
        """Mirrors the SDK method used when persisting transcript artifacts."""
        blocks: list[dict[str, Any]] = []
        for b in self.content:
            if getattr(b, "type", None) == "tool_use":
                blocks.append({
                    "type": "tool_use", "id": b.id, "name": b.name, "input": b.input,
                })
            else:
                blocks.append({"type": "text", "text": getattr(b, "text", "")})
        return {
            "model": self.model,
            "stop_reason": self.stop_reason,
            "content": blocks,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cache_read_input_tokens": self.usage.cache_read_input_tokens,
                "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
            },
        }


@dataclass
class CaptureBundle:
    """What a CLI backend's MCP server observed during one call.

    The CLI runs its own agentic loop, so instead of the caller driving turns
    and watching tool results go by, the tool server records them and the
    backend hands the log back here.
    """

    records: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    """(tool_name, payload) for each record_* call, in call order."""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)
    """Every URL that appeared in a tool result — the citation allowlist."""


@dataclass
class LLMResponse:
    """Backend-agnostic response wrapper with accounting attached."""

    raw: Any                       # anthropic.types.Message | SynthMessage
    transcript_id: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    num_turns: int = 1
    """How many assistant turns the backend used internally (CLI backends run
    their own agentic loop, so this is the analogue of tool-loop iterations)."""
    capture: CaptureBundle | None = None
    """Set by CLI backends; None for direct-API responses.

    `tool_loop.py` uses `capture is None` to decide whether it must drive the
    tool-use cycle itself or merely read back what the CLI already did.
    """


# The API clients predate the rename and construct this by its old name.
AnthropicResponse = LLMResponse


def rough_token_count(spec: AgentCallSpec) -> int:
    """Cheap heuristic: 1 token ≈ 4 chars. Used only for budget admission."""
    n = 0
    for b in spec.system_blocks:
        n += len(b.text) // 4
    for b in spec.user_blocks:
        n += len(b.text) // 4
    for m in spec.extra_messages:
        n += len(json.dumps(m)) // 4
    # Tool schemas are sent on every call and can dominate input on tool-heavy
    # agents (generation, reflection). Skipping them systematically
    # under-reserves the budget.
    if spec.tools:
        n += len(json.dumps(spec.tools)) // 4
    n += len(spec.input_images) * 8192
    return max(n, 32)
