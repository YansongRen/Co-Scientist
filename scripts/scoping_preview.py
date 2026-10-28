"""Isolated, free UI preview. All scientific launches are captured, never run.

Run with the existing venv: .venv/bin/python scripts/scoping_preview.py
No production config/database is loaded or migrated. Temporary data is retained
until the OS/user cleans the printed directory, so browser refresh works.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from co_scientist.config import Config, StorageCfg
from co_scientist.storage import db
from co_scientist.web import app as app_module
from co_scientist.web import scoping as scoping_module


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7882)
    parser.add_argument("--data-dir", help="Reuse a previous isolated preview directory")
    args = parser.parse_args()
    if args.data_dir:
        preview_path = Path(args.data_dir)
        try:
            preview_path.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        except ValueError:
            parser.error("Preview data must be in the system temporary directory")
        if preview_path.is_symlink() or not preview_path.name.startswith("co-scientist-scoping-preview-"):
            parser.error("Only an existing isolated scoping-preview directory can be reused")
        data_dir = str(preview_path)
    else:
        data_dir = tempfile.mkdtemp(prefix="co-scientist-scoping-preview-")
    cfg = Config(storage=StorageCfg(data_dir=data_dir))
    cfg.run.budget_usd = 1
    cfg.run.wall_clock_seconds = 600
    cfg.run.budget_tokens = 60000
    asyncio.run(db.init_db(cfg))

    def capture_only(app, cfg, options, session):
        print(f"OFFLINE_PREVIEW: saved {session.id}; Supervisor NOT started", flush=True)

    app_module.schedule_research = capture_only
    scoping_module.schedule_research = capture_only
    app = app_module.create_app(cfg)
    app.state.preview_only = True
    print(f"Isolated preview data: {data_dir}", flush=True)
    print(f"Open http://127.0.0.1:{args.port}/#new (Mock only; no scientific execution)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
