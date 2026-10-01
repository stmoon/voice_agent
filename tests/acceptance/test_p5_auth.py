"""P5 인증·보안 저장소·터널 인수 테스트."""
import os
import subprocess
import sys

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend

from bridge import auth
from bridge.mcp_server import build_app
from tests.acceptance.test_p4_mcp import Server, free_port
from tests.conftest import ROOT

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def server(manager):
    port = free_port()
    with Server(build_app(manager, "right-token"), port):
        yield f"http://127.0.0.1:{port}"


@pytest.mark.req("P5-1")
@pytest.mark.parametrize("path,headers", [
    ("/mcp", {}),
    ("/mcp", {"Authorization": "Bearer wrong"}),
    ("/mcp", {"Authorization": "Basic cmlnaHQtdG9rZW4="}),
    ("/t/wrong/mcp", {}),
    ("/t//mcp", {}),
    ("/t/right-token-extra/mcp", {}),
])
def test_missing_or_wrong_token_is_401(server, path, headers):
    r = httpx.post(server + path, json=INIT, headers={**HDR, **headers})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Bearer")


@pytest.mark.req("P5-1")
@pytest.mark.parametrize("path,headers", [
    ("/mcp", {"Authorization": "Bearer right-token"}),
    ("/t/right-token/mcp", {}),
])
def test_valid_token_passes(server, path, headers):
    r = httpx.post(server + path, json=INIT, headers={**HDR, **headers})
    assert r.status_code == 200, r.text


@pytest.mark.req("P5-1")
def test_healthz_reveals_nothing(server):
    r = httpx.get(server + "/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


# ---------- P5-2 ----------

class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture
def mem_keyring():
    old = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(old)


@pytest.mark.req("P5-2")
def test_token_roundtrip_through_os_store(mem_keyring):
    with pytest.raises(auth.TokenMissing):
        auth.load_secret(auth.MCP_TOKEN)
    tok = auth.generate_mcp_token()
    assert len(tok) >= 40
    assert mem_keyring.store[("voice-bridge", "mcp-token")] == tok
    assert auth.load_secret(auth.MCP_TOKEN) == tok


@pytest.mark.req("P5-2")
def test_no_plaintext_token_on_disk(mem_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tok = auth.generate_mcp_token()
    hits = []
    for base in (tmp_path, ROOT):
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".venv", ".git", "node_modules", "sandbox")]
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    if tok.encode() in open(p, "rb").read():
                        hits.append(p)
                except OSError:
                    pass
    assert hits == []


@pytest.mark.req("P5-2")
def test_serve_refuses_to_start_without_stored_token(tmp_path):
    # 보안 저장소에 토큰이 없으면 평문 대체 없이 실패해야 한다 (빈 메모리 keyring 강제)
    code = (
        "import keyring, sys\n"
        "from keyring.backends.fail import Keyring as F\n"
        "keyring.set_keyring(F())\n"
        "from bridge.__main__ import main\n"
        "try:\n    main(['serve'])\nexcept Exception as e:\n    print(type(e).__name__); sys.exit(3)\n"
    )
    env = {**os.environ, "VC_CONFIG": str(tmp_path / "none.toml"), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 3, r.stdout + r.stderr


@pytest.mark.req("P5-2")
def test_access_log_redacts_path_token():
    import logging

    assert auth.redact_path("/t/abc-DEF_123/mcp") == "/t/***/mcp"
    assert auth.redact_path("/t/abc?x=1") == "/t/***?x=1"
    assert auth.redact_path("/mcp") == "/mcp"
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:0", "POST", "/t/SECRETTOKEN/mcp", "1.1", 200), None)
    auth.RedactTokenFilter().filter(rec)
    assert "SECRETTOKEN" not in rec.getMessage() and "/t/***/mcp" in rec.getMessage()


@pytest.mark.req("P5-2")
def test_serve_access_log_has_no_token(tmp_path):
    """실제 serve 경로(uvicorn 로깅 설정 이후 필터 부착)에서 토큰이 로그에 안 찍히는지."""
    import socket
    import time as _t

    import httpx

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"port = {port}\n")
    code = (
        "import keyring\n"
        "from keyring.backend import KeyringBackend\n"
        "class M(KeyringBackend):\n"
        "    priority = 1\n"
        "    def get_password(self, s, u): return 'LOGSECRET123'\n"
        "    def set_password(self, *a): pass\n"
        "    def delete_password(self, *a): pass\n"
        "keyring.set_keyring(M())\n"
        "from bridge.__main__ import main\n"
        "main(['serve'])\n"
    )
    env = {**os.environ, "VC_CONFIG": str(cfg)}
    p = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                _t.sleep(0.1)
        r = httpx.post(f"http://127.0.0.1:{port}/t/LOGSECRET123/mcp", json=INIT, headers=HDR, timeout=5)
        assert r.status_code == 200
        _t.sleep(0.5)
    finally:
        p.terminate()
        out, _ = p.communicate(timeout=10)
    assert "/t/***/mcp" in out, out
    assert "LOGSECRET123" not in out


@pytest.mark.req("P5-1")
@pytest.mark.parametrize("host,patterns,ok", [
    ("127.0.0.1:8765", [], True),
    ("localhost:8765", [], True),
    ("evil.example.com", [], False),
    ("abc-def.trycloudflare.com", ["*.trycloudflare.com"], True),
    ("trycloudflare.com", ["*.trycloudflare.com"], False),
    ("abc.trycloudflare.com.evil.com", ["*.trycloudflare.com"], False),
    ("eviltrycloudflare.com", ["*.trycloudflare.com"], False),
    ("macmini.bridge.example.com", ["macmini.bridge.example.com"], True),
    ("other.bridge.example.com", ["macmini.bridge.example.com"], False),
    ("", ["*.trycloudflare.com"], False),
])
def test_host_rule(host, patterns, ok):
    assert auth.host_allowed(host, patterns) is ok


@pytest.mark.req("P5-1")
@pytest.mark.parametrize("origin,ok", [
    (None, True),
    ("https://claude.ai", True),
    ("https://abc.trycloudflare.com", True),
    ("http://abc.trycloudflare.com", False),
    ("https://evil.com", False),
    ("http://localhost:3000", True),
])
def test_origin_rule(origin, ok):
    assert auth.origin_allowed(origin, ["*.trycloudflare.com"]) is ok


@pytest.mark.req("P5-1")
def test_wrong_host_or_origin_blocked_even_with_valid_token(server):
    good = {**HDR, "Authorization": "Bearer right-token"}
    assert httpx.post(server + "/mcp", json=INIT, headers={**good, "Host": "evil.example.com"}).status_code == 421
    assert httpx.post(server + "/mcp", json=INIT, headers={**good, "Origin": "https://evil.com"}).status_code == 403
    assert httpx.post(server + "/mcp", json=INIT, headers=good).status_code == 200


@pytest.mark.req("P5-1")
def test_wildcard_tunnel_host_accepted(manager):
    manager.config.allowed_hosts = ["*.trycloudflare.com"]
    port = free_port()
    with Server(build_app(manager, "right-token"), port):
        r = httpx.post(f"http://127.0.0.1:{port}/t/right-token/mcp", json=INIT,
                       headers={**HDR, "Host": "rec-buyers-x.trycloudflare.com"})
        assert r.status_code == 200
        r = httpx.post(f"http://127.0.0.1:{port}/t/right-token/mcp", json=INIT,
                       headers={**HDR, "Host": "evil.example.com"})
        assert r.status_code == 421
