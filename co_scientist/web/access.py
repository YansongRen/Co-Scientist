"""Workspace login gate and secret-transport checks."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

OPEN_PATHS = frozenset({"/login", "/register", "/logout", "/healthz"})
OPEN_PREFIXES = ("/static/",)
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_TEST_HOSTS = frozenset({"test", "testserver"})


def is_open_path(path: str) -> bool:
    if path in OPEN_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in OPEN_PREFIXES)


def request_is_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded == "https"


def is_loopback_host(host: str | None) -> bool:
    return (host or "").lower() in _LOOPBACK


def secrets_transport_allowed(request: Request) -> bool:
    """Keys may be posted over HTTPS, loopback, or the ASGI test host."""
    host = (request.url.hostname or "").lower()
    return request_is_secure(request) or is_loopback_host(host) or host in _TEST_HOSTS


def unauthenticated_response(request: Request) -> JSONResponse | RedirectResponse:
    path = request.url.path
    accept = request.headers.get("accept", "")
    if path.startswith("/api/") or "application/json" in accept:
        return JSONResponse({"detail": "未登录"}, status_code=401)
    return RedirectResponse("/login", status_code=303)
