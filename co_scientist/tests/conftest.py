"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from co_scientist.config import Config, RunCfg, SafetyCfg, StorageCfg
from co_scientist.storage import db as db_mod


@pytest_asyncio.fixture
async def tmp_cfg(tmp_path: Path) -> Config:
    """A Config rooted at a fresh tmp dir, with init_db applied."""
    cfg = Config(
        run=RunCfg(),
        storage=StorageCfg(data_dir=str(tmp_path)),
        # Offline tests do not call the safety model unless a case turns this on.
        safety=SafetyCfg(enable_classifier=False),
    )
    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "vectors").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    await db_mod.init_db(cfg)
    return cfg


@pytest_asyncio.fixture
async def conn(tmp_cfg: Config):
    c = await db_mod.connect(tmp_cfg)
    try:
        yield c
    finally:
        await c.close()
