"""Local attachment endpoints; no model calls on upload/preview/download."""
import asyncio
import hashlib
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import inputs
from ..storage import db
from ..storage.repos.scoping import now


class InputBodyLimit:
    """Bound multipart bodies before the framework spools them to disk."""
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not (scope["path"].startswith("/api/inputs/groups/") and scope["path"].endswith("/files")):
            return await self.app(scope, receive, send)
        data = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(data) + len(chunk) > self.max_bytes:
                from fastapi.responses import JSONResponse

                return await JSONResponse({"detail": "上传请求超出大小上限"}, status_code=413)(scope, receive, send)
            data.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()

        return await self.app(scope, bounded_receive, send)


class NewGroup(BaseModel):
    draft_id: str | None = None


def create_inputs_router(cfg):
    router = APIRouter(prefix="/api/inputs")

    @router.post("/groups")
    async def create(body: NewGroup):
        conn = await db.connect(cfg)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            gid = await inputs.create_group(conn, body.draft_id)
            await conn.commit()
            return {"id": gid, "files": [inputs.public_file(cfg, r) for r in await inputs.list_files(conn, gid)]}
        except inputs.InputError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            await conn.rollback()
            await conn.close()

    @router.get("/groups/{group_id}")
    async def listing(group_id: str):
        conn = await db.connect(cfg)
        try:
            async with conn.execute("SELECT id,session_id FROM input_groups WHERE id=?", (group_id,)) as cur:
                group = await cur.fetchone()
                if not group:
                    raise HTTPException(404, "附件组不存在")
            return {"id": group_id, "session_id": group["session_id"], "files": [inputs.public_file(cfg, r) for r in await inputs.list_files(conn, group_id)]}
        finally:
            await conn.close()

    @router.post("/groups/{group_id}/files")
    async def upload(group_id: str, files: Annotated[list[UploadFile], File()]):
        if len(files) > cfg.uploads.max_files:
            for f in files:
                await f.close()
            raise HTTPException(413, "附件数量超出上限")
        parsed, total = [], 0
        try:
            for f in files:
                data = await f.read(cfg.uploads.max_file_bytes + 1)
                total += len(data)
                if total > cfg.uploads.max_group_bytes:
                    raise inputs.InputError("附件总大小超出上限", 413)
                meta = await asyncio.to_thread(inputs.parse_file, cfg, f.filename, data)
                parsed.append((meta, data))
            conn = await db.connect(cfg)
            try:
                await conn.execute("BEGIN IMMEDIATE")
                await inputs.attach_files(conn, cfg, group_id, parsed)
                await conn.commit()
                return {"id": group_id, "files": [inputs.public_file(cfg, r) for r in await inputs.list_files(conn, group_id)]}
            finally:
                await conn.rollback()
                await conn.close()
        except inputs.InputError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            for f in files:
                await f.close()

    @router.delete("/groups/{group_id}/files/{file_id}")
    async def remove(group_id: str, file_id: str):
        conn = await db.connect(cfg)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            owner = await inputs.group_mutable(conn, group_id)
            cur = await conn.execute("UPDATE input_files SET removed_at=? WHERE id=? AND group_id=? AND removed_at IS NULL", (now(), file_id, group_id))
            if cur.rowcount != 1:
                raise HTTPException(404, "附件不存在")
            if owner["draft_id"]:
                await conn.execute("UPDATE scoping_drafts SET version=version+1,updated_at=? WHERE id=?", (now(), owner["draft_id"]))
            await conn.commit()
            return {"id": group_id, "files": [inputs.public_file(cfg, r) for r in await inputs.list_files(conn, group_id)]}
        except inputs.InputError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            await conn.rollback()
            await conn.close()

    @router.get("/files/{file_id}")
    async def download(file_id: str, preview: bool = False):
        conn = await db.connect(cfg)
        try:
            async with conn.execute("SELECT * FROM input_files WHERE id=? AND removed_at IS NULL", (file_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "附件不存在")
            path = inputs.checked_path(cfg, row["path"])
            if not path.is_file() or path.stat().st_size != row["size_bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise HTTPException(409, "附件文件缺失或发生变化")
            inline = preview and row["media_type"].startswith("image/")
            return FileResponse(path, media_type=row["media_type"], filename=row["name"],
                content_disposition_type="inline" if inline else "attachment",
                headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})
        except inputs.InputError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            await conn.close()

    return router
