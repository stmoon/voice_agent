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
