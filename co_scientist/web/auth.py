"""Local accounts for the research workspace login.

Passwords stay hashed in SQLite. The signed cookie names the account; it does
not grant a tenant ACL over another person's data.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request

from ..config import Config
from ..ids import account_id
from ..storage import db as db_mod

COOKIE = "co_scientist_account"
MAX_AGE = 14 * 24 * 60 * 60
ROLES = ("user", "admin")
_USERNAME = re.compile(r"^[^\s<>]{2,32}$")


def public_account(row: dict[str, Any] | None) -> dict[str, str] | None:
    if row is None:
        return None
    username = str(row["username"])
    role = str(row["role"])
    return {
        "id": str(row["id"]),
        "username": username,
        "initial": username[:1].upper(),
        "role": role,
        "role_label": "管理员" if role == "admin" else "普通用户",
        "created_at": str(row["created_at"]),
    }


def validate_username(value: str) -> str:
    username = value.strip()
    if _USERNAME.fullmatch(username) is None:
        raise ValueError("账号需要 2 到 32 个字符，不能包含空格")
    return username


def validate_password(value: str) -> str:
    if not 8 <= len(value) <= 128:
        raise ValueError("密码需要 8 到 128 个字符")
    return value


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _key(cfg: Config) -> bytes:
    path = cfg.data_dir / "auth.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_bytes()


def sign_account(cfg: Config, account_id_: str, role: str) -> str:
    expires = int((datetime.now(UTC) + timedelta(seconds=MAX_AGE)).timestamp())
    body = f"{account_id_}|{role}|{expires}"
    signature = hmac.new(_key(cfg), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{signature}"


def unsign_account(cfg: Config, token: str | None) -> tuple[str, str] | None:
    if not token:
        return None
    parts = token.split("|")
    if len(parts) != 4:
        return None
    account_id_, role, expires, signature = parts
    if role not in ROLES:
        return None
    body = f"{account_id_}|{role}|{expires}"
    expected = hmac.new(_key(cfg), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        if int(expires) < int(datetime.now(UTC).timestamp()):
            return None
    except ValueError:
        return None
    return account_id_, role


async def role_open(conn, role: str) -> bool:
    async with conn.execute("SELECT 1 FROM accounts WHERE role=? LIMIT 1", (role,)) as cur:
        return await cur.fetchone() is None


async def any_accounts(conn) -> bool:
    async with conn.execute("SELECT 1 FROM accounts LIMIT 1") as cur:
        return await cur.fetchone() is not None


async def account_by_username(conn, username: str) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT id, username, password_hash, role, created_at FROM accounts WHERE username=?",
        (username,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def account_by_id(conn, account_id_: str) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT id, username, password_hash, role, created_at FROM accounts WHERE id=?",
        (account_id_,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def insert_account(conn, *, username: str, password: str, role: str) -> dict[str, Any]:
    row = {
        "id": account_id(),
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "created_at": datetime.now(UTC).isoformat(),
    }
    await conn.execute(
        "INSERT INTO accounts(id, username, password_hash, role, created_at) VALUES(?,?,?,?,?)",
        (row["id"], row["username"], row["password_hash"], row["role"], row["created_at"]),
    )
    await conn.commit()
    return row


async def list_accounts(conn) -> list[dict[str, str]]:
    async with conn.execute(
        "SELECT id, username, role, created_at FROM accounts ORDER BY created_at, username",
    ) as cur:
        rows = await cur.fetchall()
    return [public_account(dict(row)) for row in rows]  # type: ignore[misc]


async def current_account(request: Request, cfg: Config, conn=None) -> dict[str, str] | None:
    claims = unsign_account(cfg, request.cookies.get(COOKIE))
    if claims is None:
        return None
    account_id_, role = claims
    own = conn is None
    if own:
        conn = await db_mod.connect(cfg)
    try:
        row = await account_by_id(conn, account_id_)
    finally:
        if own:
            await conn.close()
    if row is None or row["role"] != role:
        return None
    return public_account(row)
