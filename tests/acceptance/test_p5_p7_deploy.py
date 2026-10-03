"""P5-3 터널 규칙·발급 스크립트, P7-1 설치 스크립트 인수 테스트."""
import os
import subprocess
import sys

import pytest

from tests.conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import tunnel_setup as ts  # noqa: E402


@pytest.mark.req("P5-3")
@pytest.mark.parametrize("hostname,expected", [
    ("stmoonui-macmini.local", "stmoonui-macmini.bridge.example.com"),
    ("DESKTOP-AB12CD", "desktop-ab12cd.bridge.example.com"),
    ("lab_server_01", "lab-server-01.bridge.example.com"),
    ("Moon's MacBook Pro", "moon-s-macbook-pro.bridge.example.com"),
])
def test_subdomain_rule(hostname, expected):
    assert ts.fqdn(hostname, "Example.com.") == expected
    assert ts.tunnel_name(hostname) == "vc-" + expected.split(".")[0]


@pytest.mark.req("P5-3")
@pytest.mark.parametrize("bad", ["", "---", "..."])
def test_subdomain_rule_rejects_unusable_hostnames(bad):
    with pytest.raises(ValueError):
        ts.host_label(bad)


@pytest.mark.req("P5-3")
def test_issue_script_dry_run_and_config(tmp_path, capsys):
    assert ts.main(["--domain", "example.com", "--hostname", "labpc", "--port", "9000",
                    "--cf-dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "cloudflared tunnel create vc-labpc" in out
    assert "cloudflared tunnel route dns vc-labpc labpc.bridge.example.com" in out
    y = ts.config_yaml("vc-labpc", "labpc.bridge.example.com", 9000, "/c.json")
    assert "hostname: labpc.bridge.example.com" in y and "http://127.0.0.1:9000" in y
    assert y.rstrip().endswith("http_status:404")  # 규칙 밖 호스트는 차단


@pytest.mark.live
@pytest.mark.req("P5-4")
def test_mcp_call_through_external_domain():
    """외부 도메인 경유 호출. VC_TUNNEL_URL=https://<host>.bridge.<도메인>/t/<토큰>/mcp 필요."""
    import asyncio
    import json

    from mcp import Client

    url = os.environ.get("VC_TUNNEL_URL")
    assert url, "VC_TUNNEL_URL 미설정"

    async def go():
        async with Client(url) as c:
            r = await c.call_tool("list_sessions", {})
            return json.loads(r.content[0].text)

    assert asyncio.run(go())["ok"]
    import httpx

    assert httpx.post(url.split("/t/")[0] + "/mcp", json={}).status_code == 401


@pytest.mark.req("P7-1")
def test_installer_creates_working_install(tmp_path):
    prefix = tmp_path / "inst"
    env = {**os.environ, "VC_CONFIG": str(tmp_path / "cfg" / "config.toml")}
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "install.py"), "--prefix", str(prefix)],
                       env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "cfg" / "config.toml").exists()
    exe = prefix / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("voice-bridge" + (".exe" if os.name == "nt" else ""))
    info = subprocess.run([str(exe), "info"], env=env, capture_output=True, text=True, timeout=60)
    assert info.returncode == 0, info.stderr
    assert "listen" in info.stdout and "8765" in info.stdout
    # 재실행해도 설정을 덮어쓰지 않음
    (tmp_path / "cfg" / "config.toml").write_text('port = 9999\n')
    r2 = subprocess.run([sys.executable, str(ROOT / "tools" / "install.py"), "--prefix", str(prefix)],
                        env=env, capture_output=True, text=True, timeout=600)
    assert r2.returncode == 0 and (tmp_path / "cfg" / "config.toml").read_text() == 'port = 9999\n'


def _serve_proc(tmp_path, config_text, extra_args=()):
    code = (
        "import keyring, sys\n"
        "from keyring.backend import KeyringBackend\n"
        "class M(KeyringBackend):\n"
        "    priority = 1\n"
        "    def get_password(self, s, u): return 'tok'\n"
        "    def set_password(self, *a): pass\n"
        "    def delete_password(self, *a): pass\n"
        "keyring.set_keyring(M())\n"
        "from bridge.__main__ import main\n"
        f"sys.exit(main(['serve', *{list(extra_args)!r}]))\n"
    )
    cfg = tmp_path / "c.toml"
    cfg.write_text(config_text)
    env = {**os.environ, "VC_CONFIG": str(cfg)}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.req("P7-1")
def test_serve_refuses_busy_port_before_starting_sessions(tmp_path):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        r = _serve_proc(tmp_path, f'port = {port}\nautostart = ["a"]\n[sessions]\na = "{ws}"\n')
    finally:
        s.close()
    assert r.returncode == 2
    assert "이미 다른 프로세스" in r.stderr and "세션 기동" not in r.stderr


@pytest.mark.req("P7-1")
def test_autostart_must_name_registered_presets():
    from bridge.config import BridgeConfig

    with pytest.raises(ValueError):
        BridgeConfig(sessions={"a": "/tmp"}, autostart=["b"])
    assert BridgeConfig(sessions={"a": "/tmp"}, autostart=["a"]).autostart == ["a"]


@pytest.mark.req("P5-3")
def test_url_command_builds_connector_url(tmp_path, monkeypatch, capsys):
    import keyring
    from keyring.backend import KeyringBackend

    from bridge import __main__ as cli
    from bridge.config import BridgeConfig

    class M(KeyringBackend):
        priority = 1
        def get_password(self, s, u): return "TOK123"
        def set_password(self, *a): pass
        def delete_password(self, *a): pass

    old = keyring.get_keyring()
    keyring.set_keyring(M())
    try:
        monkeypatch.setattr(cli, "tunnel_hosts", lambda cfg: ["abc.trycloudflare.com"])
        assert cli.cmd_url(None, BridgeConfig()) == 0
        assert "https://abc.trycloudflare.com/t/TOK123/mcp" in capsys.readouterr().out
        monkeypatch.setattr(cli, "tunnel_hosts", lambda cfg: [])
        assert cli.cmd_url(None, BridgeConfig()) == 2
    finally:
        keyring.set_keyring(old)


@pytest.mark.req("P7-1")
def test_imports_work_outside_repo(tmp_path):
    """launchd 는 홈 폴더에서 실행한다. 편집 가능 설치에선 bridge 가 네임스페이스 패키지로 잡혀
    bridge/__init__.py 가 실행되지 않으므로, 거기에 기대는 코드가 있으면 재시작 루프에 빠진다(실측)."""
    code = ("from bridge.mcp_server import BRIDGE_VERSION, build_app\n"
            "from bridge.__main__ import main\n"
            "from bridge import service, session_manager, agents, auth\n"
            "print(BRIDGE_VERSION)")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
