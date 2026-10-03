"""claude CLI 점검: 없거나 오래된 CLI 를 '세션 없음'으로 오해하지 않게 이유를 알려 준다.
(실측: 이 레포를 처음 받은 Windows 머신의 PATH 에 claude 가 없었고, ~/.local/bin 의 claude 는 2.1.131 이라 agents --json 미지원)"""
import json
import sys

import pytest
from mcp import Client

from bridge import agents
from bridge.mcp_server import build_mcp
from bridge.pty_runner import child_env
from bridge.session_manager import BridgeError, SessionManager
from tests.conftest import FAKE

MISSING = ["vb-no-such-claude-cli"]


def test_parse_version():
    assert agents.parse_version("2.1.286 (Claude Code)") == (2, 1, 286)
    assert agents.parse_version("garbage") is None
    assert agents.parse_version("2.1.131 (Claude Code)") < agents.MIN_VERSION <= (2, 1, 286)


def test_query_agents_reasons(monkeypatch):
    data, err = agents.query_agents([sys.executable, str(FAKE)], env=child_env())
    assert data is not None and err is None
    data, err = agents.query_agents(MISSING, env=child_env())
    assert data is None and "찾을 수 없습니다" in err and MISSING[0] in err
    data, err = agents.query_agents([sys.executable, str(FAKE)], env=child_env({"FAKE_OLD_CLI": "1"}))
    assert data is None and "오래되었습니다" in err and "claude update" in err


def test_doctor_reports_path_version_and_problems():
    ok = agents.claude_doctor([sys.executable, str(FAKE)], env=child_env())
    assert ok["path"] and ok["version"].startswith("2.1.286") and ok["agents_ok"] and ok["problem"] is None
    old = agents.claude_doctor([sys.executable, str(FAKE)], env=child_env({"FAKE_OLD_CLI": "1"}))
    assert old["version"].startswith("2.1.131") and "2.1.285" in old["problem"] and not old["agents_ok"]
    missing = agents.claude_doctor(MISSING, env=child_env())
    assert missing["path"] is None and "찾을 수 없습니다" in missing["problem"]


def test_missing_claude_is_explained_not_silent(fake_config):
    fake_config.claude_bin = MISSING
    m = SessionManager(fake_config)
    try:
        with pytest.raises(BridgeError) as e:
            m.start_session("테스트 세션")
        assert e.value.code == "claude_not_found"
        assert m.list_sessions() == [] and "찾을 수 없습니다" in m.agents.error
    finally:
        m.shutdown()


async def test_list_sessions_tool_warns_when_agents_unavailable(fake_config):
    fake_config.claude_bin = MISSING
    m = SessionManager(fake_config)
    try:
        async with Client(build_mcp(m)) as c:
            r = json.loads((await c.call_tool("list_sessions", {})).content[0].text)
        assert r["ok"] and r["sessions"] == [] and "세션 목록을 읽지 못했습니다" in r["warning"]
    finally:
        m.shutdown()


async def test_list_sessions_tool_has_no_warning_when_fine(manager):
    async with Client(build_mcp(manager)) as c:
        r = json.loads((await c.call_tool("list_sessions", {})).content[0].text)
    assert "warning" not in r
