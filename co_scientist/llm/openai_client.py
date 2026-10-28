"""OpenAI Chat Completions provider.

Translates the project's Anthropic-flavored `AgentCallSpec` into OpenAI's
Chat Completions request format, calls the SDK, then wraps the response in
adapter classes that mimic anthropic.types.Message (so `resp.raw.content`,
`resp.raw.stop_reason`, etc. behave the same way agents already expect).

Supports:
- OpenAI (chat.completions, function calling, reasoning_effort for o-series).
- Any OpenAI-compatible endpoint via `cfg.llm.openai.base_url`: Groq,
  Together, OpenRouter, Mistral, Ollama local, Google Gemini's OpenAI-compat
  endpoint, vLLM, etc.

Caveats (intentional gaps vs. AnthropicClient):
- cache_control breakpoints are stripped — only Anthropic supports them.
- Thinking budgets are translated to `reasoning_effort` when the model name
  starts with "o" (o1/o3/o4 family); else dropped.
- Gemini thinking levels can be configured explicitly; otherwise the model's
  provider default is used.
- Provider-owned tool-call fields are retained as opaque response metadata.
  Outbound replay is provider-aware; Gemini's documented `extra_content`
  thought-signature envelope is replayed, while other extensions are not.
- The Anthropic Batch API has no OpenAI analogue here; BatchPool still
  routes through Anthropic.
- `tool_result.is_error` is encoded into the tool message content; OpenAI
  has no first-class is_error flag.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from ..config import Config
from ..ids import transcript_id
from ..models import Transcript
from ..obs.activity import observe_llm_call
from ..storage.artifacts import write_json
from .accounting import persist_call
from .anthropic_client import (
    AgentCallSpec,
    AnthropicResponse,
    CallContext,
    _rough_token_count,
)
from .budgets import TokenBudget
from .retry import RetryPolicy, with_retry
from .routing import estimate_cost_usd

_CANONICAL_TOOL_CALL_FIELDS = frozenset({"id", "type", "function"})
_NO_TOOL_CALL_REPLAY_FIELDS: frozenset[str] = frozenset()

# Response schemas are often broader than request schemas. Keep provider
# metadata losslessly in normalized history, but only replay fields documented
# as valid request input for the configured endpoint.
_TOOL_CALL_REPLAY_FIELDS_BY_PROVIDER: dict[str, frozenset[str]] = {
    "gemini": frozenset({"extra_content"}),
    "google": frozenset({"extra_content"}),
}

# --------------------------------------------------------------------------- #
# Adapter types that quack like anthropic.types.Message / content blocks

@dataclass
class _Block:
    """Adapter that exposes the same attribute surface as Anthropic blocks."""

    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    # Opaque fields returned alongside an OpenAI-compatible tool call. They
    # survive normalization unchanged, but the request adapter decides which
    # fields are safe to replay for its configured provider.
    provider_fields: dict[str, Any] = field(default_factory=dict)
    signature: str = ""
    data: str = ""
    thinking: str = ""


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def model_dump(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@dataclass
class _Message:
    """Anthropic-Message-shaped wrapper around an OpenAI ChatCompletion."""

    content: list[_Block]
    stop_reason: str
    usage: _Usage
    model: str
    id: str = ""

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "content": [b.__dict__ for b in self.content],
            "usage": self.usage.model_dump(),
        }


# --------------------------------------------------------------------------- #
# OpenAIClient

class OpenAIClient:
    """OpenAI + OpenAI-compatible provider. One instance per session."""

    def __init__(
        self,
        cfg: Config,
        *,
        db: aiosqlite.Connection,
        budget: TokenBudget,
        retry_policy: RetryPolicy | None = None,
        compat_mode: bool = False,
        provider_name: str | None = None,
        preset_base_url: str | None = None,
        preset_api_key_env: str | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        """One client per session.

        `preset_*` params come from `provider.get_provider()` when the user
        configured a named preset (openrouter / gemini / groq / ...). They
        provide a sensible default base_url and the env-var name we expect
        the API key under, but user `[llm.openai] base_url` and
        `OPENAI_API_KEY` always win if explicitly set.
        """
        try:
            from openai import AsyncOpenAI
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "openai SDK is required for provider=openai / openai_compatible. "
                "Install with `pip install openai`."
            ) from e

        self._cfg = cfg
        self._db = db
        self._budget = budget
        self._retry = retry_policy or RetryPolicy(
            max_attempts_429=cfg.retry.max_attempts_429,
            max_attempts_529=cfg.retry.max_attempts_529,
            max_attempts_5xx=cfg.retry.max_attempts_5xx,
            max_attempts_timeout=cfg.retry.max_attempts_timeout,
            base_ms=cfg.retry.base_ms,
            cap_ms=cfg.retry.cap_ms,
        )
        self._compat_mode = compat_mode or preset_base_url is not None
        self._provider_name = provider_name or (
            "openai_compatible" if self._compat_mode else "openai"
        )

        # API key resolution precedence:
        #   1. explicit OPENAI_API_KEY (cfg.secrets or env)
        #   2. preset-specific env var (e.g. OPENROUTER_API_KEY, GEMINI_API_KEY)
        api_key = (
            cfg.secrets.OPENAI_API_KEY
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        if not api_key and preset_api_key_env:
            api_key = (
                getattr(cfg.secrets, preset_api_key_env, "")
                or os.environ.get(preset_api_key_env)
                or ""
            )
        # Local OpenAI-compat servers (Ollama, vLLM, LM Studio) often don't
        # need a real key but the SDK rejects an empty string.
        if not api_key and self._compat_mode:
            api_key = "compat-no-key"
        if not api_key:
            raise RuntimeError(
                f"no API key set ({preset_api_key_env or 'OPENAI_API_KEY'})"
            )

        # base_url precedence: explicit cfg / env > preset default.
        base_url = (
            getattr(cfg.llm.openai, "base_url", None)
            or os.environ.get("OPENAI_BASE_URL")
            or preset_base_url
        )
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if default_headers:
            kwargs["default_headers"] = default_headers
        self._client = AsyncOpenAI(**kwargs)

    # ----------------------------- main call ----------------------------- #

    @observe_llm_call
    async def call(
        self,
        spec: AgentCallSpec,
        ctx: CallContext,
        *,
        est_input_tokens: int | None = None,
    ) -> AnthropicResponse:
        is_gemini = self._provider_name in ("gemini", "google")
        reasoning_effort = (
            _gemini_reasoning_effort(self._cfg, spec) if is_gemini else None
        )
        request = _build_openai_request(
            spec,
            reasoning_effort=reasoning_effort,
            translate_thinking_tokens=not is_gemini,
            tool_call_replay_fields=_TOOL_CALL_REPLAY_FIELDS_BY_PROVIDER.get(
                self._provider_name, _NO_TOOL_CALL_REPLAY_FIELDS
            ),
        )
        # Provider extensions, e.g. DeepSeek's explicit non-thinking mode
        # required for forced function calls, are opt-in configuration.
        if self._cfg.llm.openai.extra_body:
            request["extra_body"] = self._cfg.llm.openai.extra_body

        # Estimate + admit (same accounting as AnthropicClient).
        est_in = est_input_tokens or _rough_token_count(spec)
        est_out = spec.max_output_tokens
        est_cost = estimate_cost_usd(
            model=spec.route.model, input_tokens=est_in, output_tokens=est_out
        )
        await self._budget.admit(
            ctx.agent, est_tokens=est_in + est_out, est_usd=est_cost
        )

        started = datetime.now(UTC)
        t0 = time.monotonic()

        async def _do() -> Any:
            return await self._client.chat.completions.create(**request)

        try:
            raw = await with_retry(_do, policy=self._retry)
        except BaseException:
            await self._budget.settle(
                ctx.agent,
                est_tokens=est_in + est_out, est_usd=est_cost,
                actual_input_tokens=0, actual_output_tokens=0, actual_usd=0.0,
            )
            raise
        finished = datetime.now(UTC)

        message = _adapt_response(raw, spec.route.model)
        in_tok = message.usage.input_tokens
        out_tok = message.usage.output_tokens
        cost_usd = estimate_cost_usd(
            model=spec.route.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

        await self._budget.settle(
            ctx.agent,
            est_tokens=est_in + est_out,
            est_usd=est_cost,
            actual_input_tokens=in_tok,
            actual_output_tokens=out_tok,
            actual_usd=cost_usd,
        )

        trn_id = transcript_id()
        artifact = {
            "provider": self._provider_name,
            "request": _redact(request),
            "response": message.model_dump(),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }
        artifact_path = await write_json(
            self._cfg, ctx.session_id, f"transcripts/{ctx.agent}", trn_id, artifact
        )

        t = Transcript(
            id=trn_id,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            agent=ctx.agent,
            action=ctx.action,
            model=spec.route.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read=0,
            cache_write=0,
            cost_usd=cost_usd,
            started_at=started,
            finished_at=finished,
            artifact_path=artifact_path,
        )
        await persist_call(self._db, t)

        return AnthropicResponse(
            raw=message,
            transcript_id=trn_id,
            cost_usd=cost_usd,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read=0,
            cache_write=0,
        )


# --------------------------------------------------------------------------- #
# Request translation: AgentCallSpec → OpenAI Chat Completions

def _build_openai_request(
    spec: AgentCallSpec,
    *,
    reasoning_effort: str | None = None,
    translate_thinking_tokens: bool = True,
    tool_call_replay_fields: frozenset[str] = _NO_TOOL_CALL_REPLAY_FIELDS,
) -> dict[str, Any]:
    """Translate normalized spec to OpenAI's chat.completions request."""
    messages: list[dict[str, Any]] = []

    # System prompt: OpenAI accepts a single `developer` (or `system`) message
    # at the top. Concatenate all system_blocks; drop cache_control markers.
    system_text = "\n\n".join(b.text for b in spec.system_blocks if b.text).strip()
    if system_text:
        messages.append({"role": "system", "content": system_text})

    # First user turn from user_blocks.
    user_text = "\n\n".join(b.text for b in spec.user_blocks if b.text).strip()
    if spec.input_images:
        content = [{"type": "text", "text": user_text or "User attachments"}]
        for image in spec.input_images:
            content.extend([{"type": "text", "text": f"Attachment {image.attachment_id}"},
                {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{image.data_base64}"}}])
        messages.append({"role": "user", "content": content})
    elif user_text:
        messages.append({"role": "user", "content": user_text})

    # extra_messages comes from the tool loop in Anthropic shape; translate.
    for m in spec.extra_messages:
        messages.extend(
            _translate_anthropic_message(
                m,
                tool_call_replay_fields=tool_call_replay_fields,
            )
        )

    request: dict[str, Any] = {
        "model": spec.route.model,
        "messages": messages,
        "max_completion_tokens": spec.max_output_tokens,
    }
    if spec.stop_sequences:
        request["stop"] = spec.stop_sequences

    # Tools: Anthropic `[{name, description, input_schema}]` →
    # OpenAI `[{type:"function", function:{name, description, parameters}}]`.
    if spec.tools:
        request["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object"}),
                },
            }
            for t in spec.tools
        ]

    # tool_choice: Anthropic `{"type":"auto"|"any"|"tool", "name":?}` →
    # OpenAI "auto" | "required" | {"type":"function","function":{"name":...}}.
    if spec.tool_choice is not None:
        tc = spec.tool_choice
        kind = tc.get("type", "auto")
        if kind == "auto":
            request["tool_choice"] = "auto"
        elif kind == "any":
            request["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            request["tool_choice"] = {
                "type": "function",
                "function": {"name": tc["name"]},
            }
        elif kind == "none":
            request["tool_choice"] = "none"

    # An explicit provider semantic level wins. Otherwise, reasoning-capable
    # OpenAI models receive a level translated from the legacy token budget.
    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort
    elif (
        translate_thinking_tokens
        and spec.route.thinking_tokens > 0
        and _is_reasoning_model(spec.route.model)
    ):
        request["reasoning_effort"] = _budget_to_effort(spec.route.thinking_tokens)

    return request


def _translate_anthropic_message(
    m: dict[str, Any],
    *,
    tool_call_replay_fields: frozenset[str] = _NO_TOOL_CALL_REPLAY_FIELDS,
) -> list[dict[str, Any]]:
    """Translate one Anthropic-shaped message dict to OpenAI message(s).

    Anthropic assistant messages can contain mixed content blocks (text,
    thinking, tool_use). OpenAI wants assistant `content` (text) plus a
    parallel `tool_calls` list, and tool_result blocks must be returned as
    role=tool messages keyed by tool_call_id.
    """
    role = m.get("role", "user")
    content = m.get("content")

    if role == "assistant" and isinstance(content, list):
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                # OpenAI has no first-class thinking block in the chat
                # transcript. Keep the text in a hidden comment-like prefix
                # so the model can still see its own reasoning trail when
                # the tool loop re-sends history; or drop. We drop to avoid
                # token bloat — the next turn does its own reasoning.
                continue
            elif btype == "tool_use":
                args = block.get("input", {})
                args_str = json.dumps(args, default=str, ensure_ascii=False)
                provider_fields = block.get("provider_fields", {})
                tool_call: dict[str, Any] = {}
                if isinstance(provider_fields, dict):
                    # A response may contain output-only extensions that a
                    # strict endpoint rejects on input. Replay only fields
                    # explicitly allowed for this configured provider.
                    tool_call.update(
                        _provider_fields_for_replay(
                            provider_fields,
                            allowed_fields=tool_call_replay_fields,
                        )
                    )
                tool_call.update({
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": args_str,
                    },
                })
                tool_calls.append(tool_call)
        msg: dict[str, Any] = {"role": "assistant"}
        if text_parts:
            msg["content"] = "\n".join(text_parts)
        else:
            msg["content"] = None
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return [msg]

    if role == "user" and isinstance(content, list):
        # Anthropic puts tool_result blocks under role=user; OpenAI wants a
        # separate role=tool message per tool_call_id.
        out: list[dict[str, Any]] = []
        extra_text: list[str] = []
        for block in content:
            btype = block.get("type")
            if btype == "tool_result":
                tool_call_id = block.get("tool_use_id", "")
                body = block.get("content", "")
                if not isinstance(body, str):
                    body = json.dumps(body, default=str, ensure_ascii=False)
                if block.get("is_error"):
                    body = f"[tool error] {body}"
                out.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": body,
                })
            elif btype == "text":
                extra_text.append(block.get("text", ""))
        if extra_text:
            out.append({"role": "user", "content": "\n".join(extra_text)})
        return out

    # Fallback: pass through with stringified content.
    if isinstance(content, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
        )
        return [{"role": role, "content": text}]
    return [{"role": role, "content": content if isinstance(content, str) else ""}]


# --------------------------------------------------------------------------- #
# Response adaptation: OpenAI ChatCompletion → Anthropic-shaped Message

_STOP_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",  # legacy
    # Values are normalized to Anthropic's stop_reason vocabulary, not the
    # OpenAI request field name — keep "max_tokens" (see _build_openai_request
    # which separately sends the "max_completion_tokens" request field).
    "length": "max_tokens",
    "content_filter": "refusal",
}

def _adapt_response(raw: Any, model: str) -> _Message:
    choice = raw.choices[0] if raw.choices else None
    finish = (getattr(choice, "finish_reason", None) or "stop") if choice else "stop"
    stop_reason = _STOP_REASON_MAP.get(finish, "end_turn")

    blocks: list[_Block] = []
    if choice is not None:
        msg = choice.message
        text = getattr(msg, "content", None) or ""
        if text:
            blocks.append(_Block(type="text", text=text))
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            args_raw = getattr(fn, "arguments", "{}") if fn else "{}"
            try:
                args_obj = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                if not isinstance(args_obj, dict):
                    args_obj = {"_args": args_obj}
            except json.JSONDecodeError:
                args_obj = {"_raw_arguments": args_raw}
            blocks.append(_Block(
                type="tool_use",
                id=getattr(tc, "id", "") or f"call_{uuid.uuid4().hex[:12]}",
                name=name,
                input=args_obj,
                provider_fields=_provider_fields_from_tool_call(tc),
            ))

    usage_obj = getattr(raw, "usage", None)
    usage = _Usage(
        input_tokens=int(getattr(usage_obj, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage_obj, "completion_tokens", 0) or 0),
    )
    # Newer OpenAI usage objects expose `prompt_tokens_details.cached_tokens`.
    details = getattr(usage_obj, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached:
            usage.cache_read_input_tokens = int(cached)

    return _Message(
        content=blocks,
        stop_reason=stop_reason,
        usage=usage,
        model=model,
        id=getattr(raw, "id", "") or "",
    )


# --------------------------------------------------------------------------- #
# Heuristics

def _provider_fields_for_replay(
    provider_fields: dict[str, Any],
    *,
    allowed_fields: frozenset[str],
) -> dict[str, Any]:
    """Filter opaque response metadata down to valid request extensions."""
    replayable: dict[str, Any] = {}
    for key in allowed_fields:
        if key in _CANONICAL_TOOL_CALL_FIELDS or key not in provider_fields:
            continue
        value = provider_fields[key]
        # Google's documented extra_content envelope is a non-empty object.
        # Do not replay malformed response metadata into a strict endpoint.
        if key == "extra_content" and (not isinstance(value, dict) or not value):
            continue
        replayable[key] = value
    return replayable


def _provider_fields_from_tool_call(tool_call: Any) -> dict[str, Any]:
    """Retain nonstandard response fields without interpreting them.

    Prefer the SDK's serialized form, then merge Pydantic extras explicitly
    because SDK versions differ in how unknown fields are exposed. Retention
    does not imply replay: request translation applies a provider allowlist.
    """
    dumped: dict[str, Any] = {}
    model_dump = getattr(tool_call, "model_dump", None)
    if callable(model_dump):
        candidate = model_dump()
        if isinstance(candidate, dict):
            dumped.update(candidate)
    elif isinstance(tool_call, dict):
        dumped.update(tool_call)
    else:
        raw_fields = getattr(tool_call, "__dict__", None)
        if isinstance(raw_fields, dict):
            dumped.update(raw_fields)

    model_extra = getattr(tool_call, "model_extra", None)
    if isinstance(model_extra, dict):
        dumped.update(model_extra)

    return {
        key: value
        for key, value in dumped.items()
        if key not in _CANONICAL_TOOL_CALL_FIELDS
    }


def _gemini_reasoning_effort(cfg: Config, spec: AgentCallSpec) -> str | None:
    """Resolve optional Gemini thinking level for one routed agent call."""
    gemini_cfg = cfg.llm.gemini
    route_key = (
        f"{spec.route.agent}.{spec.route.mode}"
        if spec.route.mode
        else spec.route.agent
    )
    level = gemini_cfg.thinking_by_mode.get(route_key, gemini_cfg.thinking_level)
    return None if level == "default" else level


def _is_reasoning_model(model: str) -> bool:
    m = model.lower()
    return m.startswith(("o1", "o3", "o4")) or "reasoning" in m


def _budget_to_effort(tokens: int) -> str:
    if tokens <= 1024:
        return "minimal"
    if tokens <= 4096:
        return "low"
    if tokens <= 12_000:
        return "medium"
    return "high"


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    return payload
