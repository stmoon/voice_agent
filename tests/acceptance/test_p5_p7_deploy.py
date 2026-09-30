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
