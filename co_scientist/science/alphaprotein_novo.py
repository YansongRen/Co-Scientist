"""Small adapter around the official AlphaProtein Novo command line pipeline.

This module never reimplements generation or evaluation.  It validates a
manifest, invokes the official ``run_pipeline.py`` only when explicitly
enabled, and parses the files documented by the upstream repository.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.protein_design import ProteinDesignRequest
from ..models.scientific_evidence import ScientificEvidence, valid_metric

OFFICIAL_STAGES = ("generation", "resequence", "folding", "evaluation")


@dataclass
class CampaignInspection:
    ok: bool
    manifest_path: str
    manifest_sha256: str | None
    errors: list[str]
    warnings: list[str]
    summary: dict[str, Any]


class AlphaProteinNovoAdapter:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def validate_manifest(self, manifest_path: str) -> CampaignInspection:
        path = Path(manifest_path).expanduser().resolve()
        errors: list[str] = []
        warnings: list[str] = []
        if not path.exists():
            return CampaignInspection(False, str(path), None, ["manifest does not exist"], [], {})
        try:
            raw = path.read_bytes()
            manifest = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return CampaignInspection(False, str(path), None, [f"invalid manifest: {exc}"], [], {})
        if not isinstance(manifest, dict):
            return CampaignInspection(False, str(path), _sha256(raw), ["manifest must be a JSON object"], [], {})
        for key in ("defaults", "folding", "evaluation", "resequence", "settings"):
            if key in manifest and not isinstance(manifest[key], dict):
                errors.append(f"manifest.{key} must be an object")
        if errors:
            return CampaignInspection(False, str(path), _sha256(raw), errors, [], {})
        designs = manifest.get("designs")
        if not isinstance(designs, list) or not designs:
            return CampaignInspection(False, str(path), _sha256(raw), ["manifest.designs must contain at least one design job"], [], {})
        defaults = manifest.get("defaults") or {}
        for idx, design in enumerate(designs or []):
            if not isinstance(design, dict):
                errors.append(f"designs[{idx}] must be an object")
                continue
            merged = {**defaults, **design}
            for key in ("input_file", "motif_str", "motif_atoms"):
                if not merged.get(key):
                    errors.append(f"designs[{idx}] missing {key}")
            if merged.get("input_file"):
                motif = (path.parent / str(merged["input_file"])).resolve()
                if not motif.exists():
                    errors.append(f"designs[{idx}] input_file not found: {motif}")
        folding = manifest.get("folding") or {}
        seeds = folding.get("seeds", [230])
        if not isinstance(seeds, list) or not seeds or not all(type(s) is int for s in seeds):
            errors.append("folding.seeds must contain integers")
        evaluation = manifest.get("evaluation") or {}
        if evaluation.get("suite") and evaluation["suite"] not in {
            "kemp_eliminase", "serine_esterase", "dehp_esterase",
            "carbene_transfer", "nitrene_transfer",
        }:
            warnings.append(f"evaluation suite is not one of the documented examples: {evaluation['suite']}")
        if not manifest.get("resequence"):
            warnings.append("resequence is omitted; folding will use generated sequences")
        summary = {
            "design_jobs": len(designs or []),
            "evaluation_suite": evaluation.get("suite"),
            "folding_seeds": folding.get("seeds", [230]),
            "resequence_enabled": bool((manifest.get("resequence") or {}).get("enabled")),
            "official_stages": list(OFFICIAL_STAGES),
        }
        return CampaignInspection(not errors, str(path), _sha256(raw), errors, warnings, summary)

    def validate_design_request(
        self, request: dict[str, Any] | ProteinDesignRequest,
        *, parent_hypothesis_id: str | None = None,
        design_iteration: int | None = None,
    ) -> ProteinDesignRequest:
        """Validate an agent request before it can enter the task queue.

        This is deliberately stricter than the LLM tool schema: the manifest
        must exist locally and its motif/input references must pass the same
        checks as a user-supplied campaign.  No scientific values are accepted
        from the model.
        """
        parsed = request if isinstance(request, ProteinDesignRequest) else ProteinDesignRequest.model_validate(request)
        manifest = Path(parsed.manifest_path).expanduser().resolve()
        output = Path(parsed.output_dir).expanduser().resolve()
        inspection = self.validate_manifest(str(manifest))
        if not inspection.ok:
            raise ValueError("invalid AlphaProtein Novo request: " + "; ".join(inspection.errors))
        _validate_referenced_inputs(manifest, json.loads(manifest.read_text(encoding="utf-8")))
        digest = input_fingerprint(str(manifest))
        if parsed.input_sha256 and parsed.input_sha256 != digest:
            raise ValueError("scientific inputs changed since submission/approval")
        iteration = design_iteration if design_iteration is not None else parsed.design_iteration
        if iteration < 1:
            raise ValueError("design_iteration must be >= 1")
        return parsed.model_copy(update={
            "manifest_path": str(manifest),
            "output_dir": str(output),
            "parent_hypothesis_id": parent_hypothesis_id or parsed.parent_hypothesis_id,
            "design_iteration": iteration,
            "input_sha256": digest,
        })

    def inspect_campaign(self, output_dir: str) -> dict[str, Any]:
        _check_campaign_paths(Path(output_dir).expanduser())
        root = Path(output_dir).expanduser().resolve()
        index_path = root / "pipeline_index.json"
        index = _read_json(index_path) or {}
        rows: list[dict[str, Any]] = []
        summary_path = root / "04_eval" / "evaluation_summary.csv"
        if summary_path.exists():
            with summary_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        evaluations: list[dict[str, Any]] = []
        for path in sorted((root / "04_eval").glob("*_evaluation.json")) if (root / "04_eval").exists() else []:
            payload = _read_json(path)
            if isinstance(payload, dict):
                evaluations.append({"path": str(path), "payload": payload})
        confidences = [str(p) for p in sorted((root / "03_folded").glob("*_confidences_seed*.json"))] if (root / "03_folded").exists() else []
        declared = index.get("status")
        simulated = declared == "mock" or any(
            item["payload"].get("spec_name") == "mock" for item in evaluations
        ) or any(row.get("spec_name") == "mock" for row in rows)
        status = ("mock" if simulated else declared if declared in {"error", "unavailable"}
                  else "real" if evaluations or rows or confidences else "unavailable")
        return {
            "status": status,
            "output_dir": str(root),
            "pipeline_index": index,
            "evaluation_summary_csv": str(summary_path) if summary_path.exists() else None,
            "summary_rows": rows,
            "evaluations": evaluations,
            "confidence_files": confidences,
        }

    def mock_campaign(self, manifest_path: str, output_dir: str) -> dict[str, Any]:
        inspection = self.validate_manifest(manifest_path)
        if not inspection.ok:
            raise ValueError("; ".join(inspection.errors))
        root = Path(output_dir).expanduser().resolve()
        _check_campaign_paths(Path(output_dir).expanduser())
        if root.exists() and any(root.iterdir()):
            existing = _read_json(root / "pipeline_index.json") or {}
            if existing.get("status") == "mock" and existing.get("manifest_sha256") == inspection.manifest_sha256:
                return self.inspect_campaign(str(root))
            raise ValueError("refuse to overwrite an existing campaign with mock data")
        eval_dir = root / "04_eval"
        meta_dir = root / "metadata"
        eval_dir.mkdir(parents=True, exist_ok=True)
        meta_dir.mkdir(parents=True, exist_ok=True)
        prefix = "kemp_0000"
        metrics = {
            "sequence/length": 198,
            "sampled/percent_ligand_bb_clashes": 1.2,
            "self_consistency/pocket_rmsd/mean": 0.84,
            "mechanism/kemp_eliminase/catalytic_geometry/mean": 0.71,
            "mechanism/kemp_eliminase/catalytic_geometry/std": 0.09,
        }
        evaluation = {"sample_name": prefix, "spec_name": "mock", "metrics": metrics}
        (eval_dir / f"{prefix}_evaluation.json").write_text(json.dumps(evaluation, indent=2) + "\n", encoding="utf-8")
        with (eval_dir / "evaluation_summary.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_name", "spec_name", *metrics])
            writer.writerow([prefix, "mock", *metrics.values()])
        (meta_dir / "designs.json").write_text(json.dumps({"designs": [prefix], "status": "mock"}, indent=2) + "\n", encoding="utf-8")
        (root / "pipeline_index.json").write_text(json.dumps({
            "manifest_path": str(Path(manifest_path).resolve()),
            "manifest_sha256": inspection.manifest_sha256,
            "evaluation_suite": inspection.summary.get("evaluation_suite"),
            "stages_run": list(OFFICIAL_STAGES),
            "status": "mock",
            "finished_at": time.time(),
        }, indent=2) + "\n", encoding="utf-8")
        return self.inspect_campaign(str(root)) | {"status": "mock"}

    async def run_pipeline(self, manifest_path: str, output_dir: str, *, allow_real: bool) -> dict[str, Any]:
        inspection = self.validate_manifest(manifest_path)
        if not inspection.ok:
            raise ValueError("; ".join(inspection.errors))
        if not allow_real:
            raise PermissionError("real AlphaProtein Novo runs are disabled; enable explicitly in config")
        repo = Path(self.cfg.alpha_protein_novo.repo_path).expanduser().resolve()
        script = repo / "run_pipeline.py"
        if not script.exists():
            raise FileNotFoundError(f"official run_pipeline.py not found under {repo}")
        cmd = [self.cfg.alpha_protein_novo.python_executable or sys.executable,
               str(script), f"--manifest={Path(manifest_path).resolve()}",
               f"--output_dir={Path(output_dir).resolve()}"]
        if self.cfg.alpha_protein_novo.apn_model_dir:
            cmd.append(f"--apn_model_dir={self.cfg.alpha_protein_novo.apn_model_dir}")
        if self.cfg.alpha_protein_novo.af3_model_dir:
            cmd.append(f"--af3_model_dir={self.cfg.alpha_protein_novo.af3_model_dir}")
        output = Path(output_dir).resolve()
        _check_campaign_paths(Path(output_dir))
        if (_read_json(output / "pipeline_index.json") or {}).get("status") == "mock":
            raise ValueError("real execution requires an output directory without Mock data")
        output.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = output / "pipeline.stdout.log", output / "pipeline.stderr.log"
        with stdout_path.open("ab") as stdout_log, stderr_path.open("ab") as stderr_log:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo), stdout=stdout_log, stderr=stderr_log, start_new_session=True)
            try:
                await asyncio.wait_for(proc.communicate(), timeout=self.cfg.alpha_protein_novo.timeout_seconds)
            except (TimeoutError, asyncio.CancelledError):
                if proc.returncode is None:
                    with suppress(ProcessLookupError):
                        if getattr(proc, "pid", None):
                            os.killpg(proc.pid, signal.SIGKILL)
                        else:
                            proc.kill()
                await proc.wait()
                raise
        if proc.returncode:
            raise RuntimeError(f"AlphaProtein Novo exited {proc.returncode}: {_log_tail(stderr_path)}")
        return self.inspect_campaign(output_dir) | {"stdout_tail": _log_tail(stdout_path),
            "stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}


def evidence_from_campaign(
    campaign: dict[str, Any], *, session_id: str, task_id: str,
    hypothesis_id: str | None, artifact_path: str,
    parent_ids: list[str] | None = None, design_iteration: int | None = None,
) -> list[ScientificEvidence]:
    """Convert official evaluation JSON/CSV records into traceable evidence rows."""
    status = campaign.get("status") if campaign.get("status") in {"real", "mock", "error", "unavailable"} else "unavailable"
    index = campaign.get("pipeline_index") or {}
    if index.get("status") == "mock":
        status = "mock"
    model_version = index.get("model_version") or index.get("generator_version")
    config_sha = index.get("manifest_sha256")
    out: list[ScientificEvidence] = []
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)

    evaluation_condition = f"suite={index.get('evaluation_suite') or campaign.get('evaluation_suite') or 'unknown'}"

    def add(kind: str, design_id: str | None, name: str, value: Any, source: str, *, notes: str | None = None, unit: str | None = None) -> None:
        raw_id = f"{task_id}:{design_id or ''}:{kind}:{name}:{source}"
        evidence_id = "sci_" + hashlib.sha256(raw_id.encode()).hexdigest()[:24]
        metric_status = status
        if kind != "campaign_manifest" and not valid_metric(value):
            metric_status = "unavailable" if value is None else "error"
            notes = (notes or "") + " Invalid or missing numeric metric; excluded from scientific decisions."
        out.append(ScientificEvidence(
            id=evidence_id, session_id=session_id, task_id=task_id,
            hypothesis_id=hypothesis_id, design_id=design_id, design_iteration=design_iteration, kind=kind,
            status=metric_status, metric_name=name, metric_value=value,
            model_version=model_version, config_sha256=config_sha,
            source_path=source, artifact_path=artifact_path,
            unit=unit, evaluation_condition=evaluation_condition,
            parent_ids=parent_ids or [],
            notes=notes, created_at=now,
        ))

    for row in campaign.get("summary_rows") or []:
        design_id = row.get("sample_name")
        for name, value in row.items():
            if name in {"sample_name", "spec_name"} or value in (None, ""):
                continue
            add("ap_novo_metric", design_id, name, _coerce_number(value), str(campaign.get("evaluation_summary_csv") or ""), unit=_metric_unit(name))
    for item in campaign.get("evaluations") or []:
        payload = item.get("payload") or {}
        design_id = payload.get("sample_name")
        metrics = payload.get("metrics") or {}
        for name, value in metrics.items():
            add("ap_novo_metric", design_id, name, _coerce_number(value), str(item.get("path") or ""), unit=_metric_unit(name))
    for source in campaign.get("confidence_files") or []:
        payload = _read_json(Path(source)) or {}
        design_id = Path(source).name.rsplit("_confidences_seed", 1)[0]
        for name in ("plddt", "ptm", "iptm", "ranking_confidence", "chain_pair_pde_mean"):
            if name in payload:
                add("structure_prediction", design_id, name, payload[name], source,
                    notes="AlphaFold 3 confidence; does not establish catalytic activity")
    add("campaign_manifest", None, "manifest_sha256", config_sha, str(index.get("manifest_path") or ""))
    return out


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _log_tail(path: Path, size: int = 2000) -> str:
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - size))
        return handle.read(size).decode(errors="replace")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _coerce_number(value: Any) -> Any:
    try:
        if isinstance(value, str) and value.strip():
            return float(value) if any(c in value.lower() for c in (".", "e")) else int(value)
    except ValueError:
        pass
    return value


def _metric_unit(name: str) -> str | None:
    if name == "sequence/length":
        return "aa"
    if "percent_" in name:
        return "%"
    if "rmsd" in name:
        return "angstrom"
    return None


def _validate_referenced_inputs(manifest_path: Path, manifest: dict[str, Any]) -> None:
    """Reject path-like manifest references that do not exist.

    Official manifests use a few different names for ligand/motif assets. We
    inspect only path-like keys and leave symbolic motif expressions such as
    ``motif_str`` to the official validator.
    """
    path_keys = {"input_file", "motif_file", "ligand_file", "ligand_path", "cif_file"}
    missing: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in path_keys and isinstance(child, str) and child.strip():
                    candidate = Path(child).expanduser()
                    if not candidate.is_absolute():
                        candidate = manifest_path.parent / candidate
                    if not candidate.resolve().is_file():
                        missing.append(f"{path}.{key}: {candidate.resolve()}")
                else:
                    visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                visit(child, f"{path}[{idx}]")

    visit(manifest, "manifest")
    if missing:
        raise ValueError("referenced input file(s) do not exist: " + "; ".join(missing))


def input_fingerprint(manifest_path: str) -> str:
    """Bind approval and idempotency to manifest AND referenced scientific assets."""
    manifest = Path(manifest_path).resolve()
    digest = hashlib.sha256(manifest.read_bytes())
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in sorted(value.items()):
                if key in {"input_file", "motif_file", "ligand_file", "ligand_path", "cif_file"} and isinstance(child, str):
                    asset = (manifest.parent / child).resolve()
                    digest.update(str(asset).encode())
                    digest.update(asset.read_bytes())
                else:
                    visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(json.loads(manifest.read_text()))
    return digest.hexdigest()


def _check_campaign_paths(root: Path) -> None:
    """No stage or output file may redirect to a foreign campaign via symlinks."""
    base = root.resolve()
    if root.is_symlink():
        raise ValueError("campaign root must not be a symlink")
    if root.exists():
        for path in root.rglob("*"):
            if path.is_symlink() or not path.resolve().is_relative_to(base):
                raise ValueError("campaign contains a symlink or escaping path")
