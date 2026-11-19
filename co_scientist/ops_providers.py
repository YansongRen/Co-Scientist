"""Admin overlay for API keys, session models, and named generation endpoints.

Non-secret fields persist in ``data_dir/runtime.toml``. Keys the administrator
explicitly sets persist in ``data_dir/runtime.env`` (mode 600). Neither file is
a dump of ``.env``. Existing Supervisors keep the provider they started with;
new sessions and a process restart pick up the overlay.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Config, EndpointCfg, ModelsCfg, Secrets, provider_key_env
from .llm.provider import KNOWN_PROVIDERS, reset_endpoint_gates

RUNTIME_TOML = "runtime.toml"
RUNTIME_ENV = "runtime.env"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ENDPOINT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MODEL_FIELDS = tuple(ModelsCfg.model_fields)
KNOWN_KEY_NAMES = tuple(Secrets.model_fields)
MODEL_LABELS = {
    "parse_goal": "解析目标",
    "generation": "生成",
    "reflection": "审查",
    "evolution": "演化",
    "ranking_pairwise": "两两比较",
    "ranking_debate": "比较辩论",
    "ranking_priority": "比较优先级",
    "metareview_feedback": "系统反馈",
    "metareview_final": "最终报告",
    "classifier": "安全分类",
    "judge": "评判",
}


class OpsError(ValueError):
    """User-facing validation failure for the admin API page."""


def runtime_toml_path(cfg: Config) -> Path:
    return cfg.data_dir / RUNTIME_TOML


def runtime_env_path(cfg: Config) -> Path:
    return cfg.data_dir / RUNTIME_ENV


def mask_secret(value: str) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "••••"
    return f"{value[:2]}…{value[-4:]}"


def listed_key_names(cfg: Config) -> list[str]:
    names = list(KNOWN_KEY_NAMES)
    extra = {ep.api_key_env for ep in cfg.llm.endpoints.values()} - set(names)
    names.extend(sorted(extra))
    return names


def secret_value(cfg: Config, name: str) -> str:
    if name in Secrets.model_fields:
        return str(getattr(cfg.secrets, name, "") or "") or os.environ.get(name, "")
    return os.environ.get(name, "")


def snapshot(cfg: Config) -> dict[str, Any]:
    keys = []
    for name in listed_key_names(cfg):
        value = secret_value(cfg, name)
        keys.append({"name": name, "configured": bool(value), "preview": mask_secret(value)})
    models = cfg.models.model_dump()
    return {
        "provider": cfg.llm.provider,
        "providers": sorted(KNOWN_PROVIDERS),
        "base_url": cfg.llm.openai.base_url or "",
        "session_key_env": provider_key_env(cfg),
        "models": models,
        "model_fields": [
            {"name": name, "label": MODEL_LABELS.get(name, name), "value": models[name]}
            for name in MODEL_FIELDS
        ],
        "authors": list(cfg.generation.authors),
        "endpoints": [
            {
                "name": name,
                "base_url": endpoint.base_url,
                "model": endpoint.model,
                "api_key_env": endpoint.api_key_env,
                "max_concurrent": endpoint.max_concurrent,
            }
            for name, endpoint in cfg.llm.endpoints.items()
        ],
        "api_keys": keys,
        "note": "已在运行的研究会继续使用启动时的配置。新研究和重启后的进程使用这次保存的设置。",
    }


def apply_ops_overlay(cfg: Config) -> Config:
    """Mutate ``cfg`` with ``runtime.toml`` then ``runtime.env`` if present."""
    data = _read_toml(runtime_toml_path(cfg))
    if data:
        llm = data.get("llm") or {}
        if isinstance(llm, dict):
            if "provider" in llm:
                cfg.llm.provider = str(llm["provider"]).strip() or cfg.llm.provider
            openai = llm.get("openai") or {}
            if isinstance(openai, dict) and "base_url" in openai:
                cfg.llm.openai.base_url = str(openai.get("base_url") or "") or None
            if llm.get("managed_endpoints") or "endpoints" in llm:
                raw_endpoints = llm.get("endpoints") or {}
                if not isinstance(raw_endpoints, dict):
                    raw_endpoints = {}
                cfg.llm.endpoints = {
                    str(name): EndpointCfg.model_validate(body)
                    for name, body in raw_endpoints.items()
                    if isinstance(body, dict)
                }
        models = data.get("models") or {}
        if isinstance(models, dict):
            for name, value in models.items():
                if name in ModelsCfg.model_fields:
                    setattr(cfg.models, name, str(value))
        generation = data.get("generation") or {}
        if isinstance(generation, dict) and "authors" in generation:
            authors = generation.get("authors") or []
            cfg.generation.authors = [str(item) for item in authors]
    apply_runtime_env(cfg)
    return cfg


def apply_runtime_env(cfg: Config) -> None:
    for name, value in _parse_env_file(runtime_env_path(cfg)).items():
        os.environ[name] = value
        if name in Secrets.model_fields:
            setattr(cfg.secrets, name, value)


def persist_runtime_toml(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Written by the admin API page. Do not put secrets here.",
        "",
        "[llm]",
        f"provider = {_toml_str(cfg.llm.provider)}",
        "managed_endpoints = true",
        "",
        "[llm.openai]",
        f"base_url = {_toml_str(cfg.llm.openai.base_url or '')}",
        "",
        "[generation]",
        f"authors = [{', '.join(_toml_str(name) for name in cfg.generation.authors)}]",
        "",
        "[models]",
    ]
    for name in MODEL_FIELDS:
        lines.append(f"{name} = {_toml_str(getattr(cfg.models, name))}")
    lines.append("")
    for name, endpoint in cfg.llm.endpoints.items():
        lines.append(f"[llm.endpoints.{name}]")
        lines.append(f"base_url = {_toml_str(endpoint.base_url)}")
        lines.append(f"model = {_toml_str(endpoint.model)}")
        lines.append(f"api_key_env = {_toml_str(endpoint.api_key_env)}")
        lines.append(f"max_concurrent = {int(endpoint.max_concurrent)}")
        lines.append("")
    runtime_toml_path(cfg).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def set_api_key(cfg: Config, name: str, value: str) -> None:
    name = validate_env_name(name)
    value = value.strip()
    if any(ord(char) < 32 for char in value):
        raise OpsError("密钥不能包含换行或控制字符")
    if len(value) > 4096:
        raise OpsError("密钥过长")
    persist_runtime_env(cfg, {name: value})
    if value:
        os.environ[name] = value
        if name in Secrets.model_fields:
            setattr(cfg.secrets, name, value)
        return
    os.environ.pop(name, None)
    if name in Secrets.model_fields:
        setattr(cfg.secrets, name, "")


def update_session(
    cfg: Config,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    models: dict[str, str] | None = None,
    authors: list[str] | str | None = None,
) -> None:
    if provider is not None:
        cfg.llm.provider = validate_provider(provider)
    if base_url is not None:
        cfg.llm.openai.base_url = validate_url(base_url, allow_empty=True) or None
    if models is not None:
        if not isinstance(models, dict):
            raise OpsError("模型字段需要是对象")
        for name, value in models.items():
            if name not in ModelsCfg.model_fields:
                raise OpsError(f"未知模型字段 {name}")
            setattr(cfg.models, name, validate_model_id(str(value)))
    if authors is not None:
        if not isinstance(authors, (list, str)):
            raise OpsError("作者需要是列表或字符串")
        cfg.generation.authors = validate_authors(authors, cfg.llm.endpoints)
    persist_runtime_toml(cfg)


def upsert_endpoint(
    cfg: Config,
    *,
    name: str,
    base_url: str,
    model: str,
    api_key_env: str,
    max_concurrent: int | str = 1,
    api_key: str | None = None,
) -> None:
    name = validate_endpoint_name(name)
    endpoint = EndpointCfg(
        base_url=validate_url(base_url),
        model=validate_model_id(model),
        api_key_env=validate_env_name(api_key_env),
        max_concurrent=validate_concurrency(max_concurrent),
    )
    cfg.llm.endpoints[name] = endpoint
    persist_runtime_toml(cfg)
    reset_endpoint_gates()
    if api_key is not None and str(api_key).strip():
        set_api_key(cfg, endpoint.api_key_env, str(api_key))


def delete_endpoint(cfg: Config, name: str) -> None:
    name = validate_endpoint_name(name)
    if name not in cfg.llm.endpoints:
        raise OpsError("没有这个端点")
    if name in cfg.generation.authors:
        raise OpsError("请先从作者列表里去掉这个端点")
    del cfg.llm.endpoints[name]
    persist_runtime_toml(cfg)
    reset_endpoint_gates()


def validate_env_name(name: str) -> str:
    name = name.strip()
    if _ENV_NAME.fullmatch(name) is None:
        raise OpsError("环境变量名需要大写字母开头，只能包含大写字母、数字和下划线")
    return name


def validate_endpoint_name(name: str) -> str:
    name = name.strip().lower()
    if _ENDPOINT_NAME.fullmatch(name) is None:
        raise OpsError("端点名需要小写字母开头，只能包含小写字母、数字和下划线")
    if name == "session":
        raise OpsError("session 是会话模型保留名，不能当作端点名")
    return name


def validate_provider(name: str) -> str:
    name = name.strip().lower()
    if name not in KNOWN_PROVIDERS:
        raise OpsError("不支持这个 Provider")
    return name


def validate_url(url: str, *, allow_empty: bool = False) -> str:
    url = url.strip()
    if not url:
        if allow_empty:
            return ""
        raise OpsError("请填写接口地址")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise OpsError("接口地址需要 http 或 https")
    if parsed.username or parsed.password:
        raise OpsError("接口地址不能带账号密码")
    return url


def validate_model_id(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise OpsError("模型名需要 1 到 200 个字符，不能包含控制字符")
    return value


def validate_concurrency(value: int | str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OpsError("并发需要 0 到 32 的整数") from exc
    if parsed < 0 or parsed > 32:
        raise OpsError("并发需要 0 到 32")
    return parsed


def validate_authors(raw: list[str] | str, endpoints: dict[str, EndpointCfg]) -> list[str]:
    if isinstance(raw, str):
        parts = [item.strip() for item in raw.replace(",", " ").split() if item.strip()]
    else:
        parts = [str(item).strip() for item in raw if str(item).strip()]
    seen: set[str] = set()
    authors: list[str] = []
    for name in parts:
        if name in seen:
            continue
        seen.add(name)
        if name != "session" and name not in endpoints:
            raise OpsError(f"作者 {name} 需要先新增同名端点，或填写 session")
        authors.append(name)
    if len(authors) > 16:
        raise OpsError("作者最多 16 个")
    return authors


def persist_runtime_env(cfg: Config, updates: dict[str, str]) -> None:
    path = runtime_env_path(cfg)
    current = _parse_env_file(path)
    for name, value in updates.items():
        if value:
            current[name] = value
        else:
            current.pop(name, None)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by the admin API page. Not for git."]
    lines.extend(f"{name}={current[name]}" for name in sorted(current))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    parsed: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if _ENV_NAME.fullmatch(name) is None:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        parsed[name] = value
    return parsed
