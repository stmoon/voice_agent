"""토큰 인증 + OS 보안 저장소 (keyring: macOS 키체인 / Windows 자격 증명 관리자 / Linux Secret Service).

토큰은 평문 파일에 두지 않는다. 요청은 둘 중 하나로 인증:
- `Authorization: Bearer <토큰>` 헤더
- 경로 접두사 `/t/<토큰>/...`  (헤더를 넣을 수 없는 커넥터용)
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
from typing import Callable

import keyring

SERVICE = "voice-bridge"
MCP_TOKEN = "mcp-token"
NOTION_TOKEN = "notion-token"


class TokenMissing(RuntimeError):
    pass


def load_secret(name: str) -> str:
    v = keyring.get_password(SERVICE, name)
    if not v:
        raise TokenMissing(f"보안 저장소에 '{SERVICE}/{name}' 이 없습니다. `voice-bridge token init` 으로 등록하세요.")
    return v


def store_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def generate_mcp_token() -> str:
    tok = secrets.token_urlsafe(32)
    store_secret(MCP_TOKEN, tok)
    return tok


_PATH_TOKEN_RE = re.compile(r"/t/[^/?#\s]+")


def redact_path(path: str) -> str:
    """로그용: 경로의 토큰(/t/<토큰>/...)을 가린다."""
    return _PATH_TOKEN_RE.sub("/t/***", path)


class RedactTokenFilter(logging.Filter):
    """uvicorn 접속 로그에 경로 토큰이 찍히지 않게 한다. args = (client, method, path, http_ver, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_path(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact_path(record.msg)
        return True


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _fp(v: str) -> str:
    import hashlib

    return hashlib.sha256(v.encode()).hexdigest()[:8]


_log = logging.getLogger("uvicorn.error")


def _diag(reason: str, given: str | None, expected: str, headers: dict) -> None:
    """401 원인 진단. 토큰 값은 남기지 않고 길이·해시·대소문자 차이만."""
    g = given or ""
    _log.warning("[auth] 401 %s: 받은 토큰 len=%d sha=%s / 기대 len=%d sha=%s / 대소문자만다름=%s / auth헤더=%s / ua=%s",
                 reason, len(g), _fp(g) if g else "-", len(expected), _fp(expected),
                 bool(g) and g != expected and g.lower() == expected.lower(),
                 "있음" if headers.get(b"authorization") else "없음", headers.get(b"user-agent", "-")[:60])


LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_OAUTH_PATHS = ("/register", "/authorize", "/token")


def host_allowed(host: str, patterns) -> bool:
    """Host 헤더 검사 (DNS 리바인딩 방지). 로컬은 항상 허용.
    패턴: 정확한 호스트 / "*.trycloudflare.com" 같은 하위 도메인 와일드카드 / "host:*" 임의 포트."""
    host = (host or "").strip().lower()
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    if name in LOCAL_HOSTS:
        return True
    for p in patterns:
        p = p.strip().lower()
        if p.startswith("*."):
            if name.endswith(p[1:]) and len(name) > len(p) - 1:
                return True
        elif p.endswith(":*"):
            if name == p[:-2]:
                return True
        elif host == p or name == p:
            return True
    return False


def origin_allowed(origin: str | None, patterns) -> bool:
    """Origin 은 없으면(서버 간 요청) 허용. 있으면 claude.ai·로컬·허용 호스트만."""
    if not origin:
        return True
    from urllib.parse import urlsplit

    try:
        u = urlsplit(origin)
    except ValueError:
        return False
    name = (u.hostname or "").lower()
    if u.scheme == "https" and (name == "claude.ai" or name.endswith(".claude.ai")):
        return True
    return host_allowed(u.netloc, patterns) and (u.scheme == "https" or name in LOCAL_HOSTS)


class TokenAuthMiddleware:
    """ASGI 미들웨어. /healthz 외 모든 HTTP 요청에 대해
    Host 검사(421) → Origin 검사(403) → 토큰 검사(401)."""

    def __init__(self, app, token: str | Callable[[], str], allowed_hosts=()):
        self.app = app
        self._token = token
        self.allowed_hosts = list(allowed_hosts)

    def token(self) -> str:
        return self._token() if callable(self._token) else self._token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path: str = scope.get("path", "")
        if path == "/healthz":
            return await _respond(send, 200, {"ok": True})
        if path.startswith("/.well-known/") or path in _OAUTH_PATHS:
            # OAuth 를 쓰지 않는 서버임을 알린다. 401 로 답하면 claude.ai 가 OAuth 서버로 오인해
            # 클라이언트 등록(/register)을 시도하다 "로그인 서비스에 등록할 수 없습니다" 로 실패한다 (실측).
            return await _respond(send, 404, {"error": "not found"})
        headers = {k.lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not host_allowed(headers.get(b"host", ""), self.allowed_hosts):
            return await _respond(send, 421, {"error": "invalid host"})
        if not origin_allowed(headers.get(b"origin"), self.allowed_hosts):
            return await _respond(send, 403, {"error": "invalid origin"})
        expected = self.token()
        if path.startswith("/t/"):
            _, _, rest = path.partition("/t/")
            given, sep, tail = rest.partition("/")
            if not (given and _eq(given, expected)):
                _diag("경로 토큰 불일치", given, expected, headers)
                return await _respond(send, 401, {"error": "unauthorized"})
            scope = dict(scope)
            scope["path"] = "/" + tail if sep else "/"
            scope["raw_path"] = scope["path"].encode()
        else:
            val = headers.get(b"authorization", "")
            if not (val.lower().startswith("bearer ") and _eq(val[7:].strip(), expected)):
                _diag("토큰 없음/헤더 불일치", val[7:].strip() if val.lower().startswith("bearer ") else None,
                      expected, headers)
                return await _respond(send, 401, {"error": "unauthorized"})
        # 본문은 인증을 통과한 요청만 들여다본다 (먼저 읽으면 토큰 없는 큰 요청이 그대로 메모리에 올라온다)
        receive = await _peek_rpc(scope, receive)
        return await self.app(scope, receive, send)


async def _peek_rpc(scope, receive):
    """진단: POST 본문의 JSON-RPC 메서드(와 tools/call 의 도구 이름)만 로그에 남긴다. 인자·토큰은 남기지 않음.
    본문을 읽은 뒤 그대로 다시 흘려보내는 receive 를 돌려준다."""
    if scope.get("method") != "POST":
        return receive
    chunks, more = [], True
    while more:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        chunks.append(msg.get("body", b""))
        more = msg.get("more_body", False)
    body = b"".join(chunks)
    try:
        data = json.loads(body or b"null")
        for m in (data if isinstance(data, list) else [data]):
            if isinstance(m, dict) and "method" in m:
                name = (m.get("params") or {}).get("name") if m["method"] == "tools/call" else None
                _log.info("[mcp] %s%s", m["method"], f" → {name}" if name else "")
    except Exception:
        pass
    sent = False

    async def replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _respond(send, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="voice-bridge"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": data})
