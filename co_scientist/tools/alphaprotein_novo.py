"""Tool boundary for the optional AlphaProtein Novo adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..ids import tool_run_id
from ..science.alphaprotein_novo import AlphaProteinNovoAdapter
from ..storage.artifacts import session_root, write_json
from .base import ToolCtx, ToolResult


class AlphaProteinNovoTool:
    name = "alphaprotein_novo"
    description = (
        "Validate or explicitly run the official AlphaProtein Novo campaign, or "
        "inspect its pipeline_index/evaluation JSON/CSV outputs. Real runs are "
        "disabled unless local configuration enables them; mock results are labeled mock."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["validate_manifest", "mock_campaign", "run_pipeline", "inspect_campaign"]},
            "manifest_path": {"type": "string"},
            "output_dir": {"type": "string"},
            "confirm_real_run": {"type": "boolean"},
        },
        "required": ["operation"],
    }

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg

    async def call(self, args: dict[str, Any], ctx: ToolCtx) -> ToolResult:
        operation = str(args.get("operation", ""))
        adapter = AlphaProteinNovoAdapter(ctx.cfg)
        try:
            if ctx.session_id and operation in {"mock_campaign", "inspect_campaign"}:
                root = session_root(ctx.cfg, ctx.session_id).resolve()
                output = Path(str(args.get("output_dir", ""))).expanduser().resolve()
                if not output.is_relative_to(root):
                    raise ValueError("LLM scientific tool output must stay within its session artifacts")
            if operation == "validate_manifest":
                inspection = adapter.validate_manifest(str(args.get("manifest_path", "")))
                content = inspection.__dict__
            elif operation == "inspect_campaign":
                content = adapter.inspect_campaign(str(args.get("output_dir", "")))
            elif operation == "mock_campaign":
                content = adapter.mock_campaign(str(args.get("manifest_path", "")), str(args.get("output_dir", "")))
                content["evidence_status"] = "mock"
            elif operation == "run_pipeline":
                raise PermissionError("real runs are disabled at the LLM tool boundary; submit a Supervisor task for human approval")
            else:
                raise ValueError(f"unknown operation: {operation}")
            artifact_path = None
            if ctx.session_id:
                artifact_path = await write_json(
                    ctx.cfg, ctx.session_id, "scientific_tools/alphaprotein_novo",
                    ctx.run_id or tool_run_id(), {"operation": operation, "args": args, "result": content},
                )
            return ToolResult(content=content, artifact_path=artifact_path,
                              result_bytes=len(json.dumps(content, default=str)))
        except Exception as exc:
            return ToolResult(is_error=True, error_message=str(exc)[:2000])
