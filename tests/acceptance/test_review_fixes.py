"""독립 검토(2026-10-03)에서 확인된 문제들의 회귀 테스트."""
import json
import time

import pytest

from bridge import transcript as tr
from bridge.session_manager import BACKGROUND, BridgeError, sanitize_command
from tests.acceptance.test_p3_external_sessions import by_name, done, make_bg, make_desktop
from tests.conftest import wait_until


def set_live(cfg, sid, status):
    f = cfg.claude_config_dir / "fake_agents" / f"{sid}.json"
    e = json.loads(f.read_text())
    e["status"] = status
    f.write_text(json.dumps(e, ensure_ascii=False))


def append(s, rec):
    with open(s.transcript_path(), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --- 승인 뒤 도구 실행 중엔 '작업 중' (예전엔 훅 기록 때문에 계속 '승인 대기') ---

@pytest.mark.req("P2-3")
@pytest.mark.parametrize("hooks", [True, False])
def test_after_approval_long_tool_is_working(manager, monkeypatch, hooks):
    if not hooks:
        monkeypatch.setenv("FAKE_NO_PERM_HOOKS", "1")
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    r = manager.send_command("PERM SLOW rm")
    assert wait_until(lambda: manager.get_result(r["request_id"])["status"] == tr.AWAITING_APPROVAL)
    s.proc.write("y")
    time.sleep(1.5)  # 승인 뒤 4초 동안 도구 실행
    assert manager.get_result(r["request_id"])["status"] == tr.WORKING
    assert s.status() == tr.WORKING
    assert wait_until(lambda: done(manager, r["request_id"]), timeout=20)["response"] == "APPROVED"


@pytest.mark.req("P3-6")
def test_background_after_approval_long_tool_is_working(manager, fake_config, workdir):
    make_bg(fake_config, "위키", workdir)
    manager.list_sessions()
    manager.select_session("위키")
    s = manager.get("위키")
    r = manager.send_command("PERM SLOW rm")
    assert wait_until(lambda: manager.get_result(r["request_id"])["status"] == tr.AWAITING_APPROVAL)
    s.proc.write("y")
    time.sleep(1.5)
    assert manager.get_result(r["request_id"])["status"] == tr.WORKING
    assert wait_until(lambda: done(manager, r["request_id"]), timeout=20)["response"] == "APPROVED"


# --- 보안: 권한 모드가 바뀐 bridge 세션도 거부 ---

@pytest.mark.req("P3-3")
def test_bridge_session_with_changed_permission_mode_is_refused(manager):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    append(s, {"type": "permission-mode", "permissionMode": "acceptEdits", "sessionId": s.session_id})
    with pytest.raises(BridgeError) as e:
        manager.send_command("hello")
    assert e.value.code == "not_commandable" and "acceptEdits" in e.value.message
    assert not s.info()["commandable"]
    append(s, {"type": "permission-mode", "permissionMode": "default", "sessionId": s.session_id})
    r = manager.send_command("hello")
    assert wait_until(lambda: done(manager, r["request_id"]))["response"] == "ECHO: hello"


@pytest.mark.req("P3-3")
@pytest.mark.parametrize("raw", ["/compact", "  /permissions", "/clear"])
def test_slash_commands_refused(raw):
    with pytest.raises(BridgeError) as e:
        sanitize_command(raw)
    assert e.value.code == "slash_command_blocked"


# --- 유실된 주입이 세션을 영원히 막지 않음 ---

@pytest.mark.req("P3-3")
def test_lost_injection_does_not_block_forever(manager):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    from bridge.session_manager import Request

    s.inflight = Request(id="x", session_id=s.session_id, session_name=s.name, text="never recorded",
                         offset=len(s.records()), sent_at=time.time())
    assert s.status() == tr.WORKING
    s.inflight.sent_at -= s.inflight_timeout + 1
    assert s.status() == tr.IDLE and s.inflight is None


@pytest.mark.req("P3-3")
def test_send_failure_clears_inflight(manager, monkeypatch):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")

    def boom(*a, **k):
        raise OSError("pty closed")

    monkeypatch.setattr(s.proc, "send_prompt", boom)
    with pytest.raises(BridgeError) as e:
        manager.send_command("hello")
    assert e.value.code == "inject_failed" and s.inflight is None


# --- transcript 상 끝난 턴이라도 실시간 busy/waiting 이 우선 ---

@pytest.mark.req("P3-3")
def test_live_busy_overrides_finished_turn(manager, fake_config):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    r = manager.send_command("first")
    wait_until(lambda: done(manager, r["request_id"]))
    set_live(fake_config, s.session_id, "busy")       # 예: Code 탭에서 /compact 실행 중
    manager.agents._at = 0
    assert s.status() == tr.WORKING
    with pytest.raises(BridgeError) as e:
        manager.send_command("second")
    assert e.value.code == "busy"
    set_live(fake_config, s.session_id, "idle")
    manager.agents._at = 0
    assert s.status() == tr.IDLE


# --- 멈춘 백그라운드 세션은 깨우지 않음 ---

@pytest.mark.req("P3-6")
def test_vanished_selected_background_is_unselected_and_not_reattached(manager, fake_config, workdir):
    short = make_bg(fake_config, "위키", workdir)
    manager.list_sessions()
    manager.select_session("위키")
    s = manager.get("위키")
    manager._detach(s)
    for f in (fake_config.claude_config_dir / "fake_agents").glob("*.json"):
        if json.loads(f.read_text()).get("id") == short:
            f.unlink()          # claude stop 에 해당
    manager.agents._at = 0
    with pytest.raises(BridgeError) as e:
        manager._attach(s)
    assert e.value.code == "session_exited" and s.proc is None
    assert "위키" not in [d["name"] for d in manager.list_sessions()]
    with pytest.raises(BridgeError) as e:
        manager.send_command("hello")
    assert e.value.code == "no_selection"


# --- 같은 이름: 후보 정보와 힌트, 종료된 세션은 제외 ---

@pytest.mark.req("P3-5")
def test_ambiguous_candidates_explain_commandability(manager, fake_config, workdir):
    make_bg(fake_config, "위키", workdir, mode="auto")
    make_bg(fake_config, "위키", workdir)
    manager.list_sessions()
    with pytest.raises(BridgeError) as e:
        manager.select_session("위키")
    cands = e.value.extra["candidates"]
    assert sorted(c["commandable"] for c in cands) == [False, True]
    assert "하나입니다" in e.value.message


# --- RC 끊김, 작업 알림, 데스크톱 안내 ---

@pytest.mark.req("P3-6")
def test_rc_disconnect_makes_background_uncommandable(manager, fake_config, workdir):
    make_bg(fake_config, "위키", workdir)
    lst = manager.list_sessions()
    assert by_name(lst, "위키")["commandable"]
    s = manager.get("위키")
    append(s, {"type": "system", "subtype": "informational", "content": "Remote Control disconnected", "sessionId": s.session_id})
    w = by_name(manager.list_sessions(), "위키")
    assert not w["commandable"] and "Remote Control" in w["reason"] and w["rc_url"] is None


@pytest.mark.req("P2-2")
def test_task_notification_is_not_a_prompt():
    recs = [
        {"type": "user", "message": {"role": "user", "content": "real prompt"}, "origin": {"kind": "human"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}},
        {"type": "user", "message": {"role": "user", "content": "<task-notification>\n<task-id>x</task-id>"},
         "origin": {"kind": "task-notification", "producer": "session-task"}},
        {"type": "user", "message": {"role": "user", "content": "plain text but from a task"},
         "origin": {"kind": "task-notification"}},
    ]
    assert [t.prompt for t in tr.split_turns(recs)] == ["real prompt"]


@pytest.mark.req("P2-2")
def test_turn_after_prefers_matching_prompt():
    recs = [
        {"type": "user", "message": {"role": "user", "content": "someone else in Code tab"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "A"}], "stop_reason": "end_turn"}},
        {"type": "user", "message": {"role": "user", "content": "my  command"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "B"}], "stop_reason": "end_turn"}},
    ]
    assert tr.turn_after(recs, 0, "my command").final_text() == "B"
    assert tr.turn_after(recs, 0).final_text() == "A"


@pytest.mark.req("P3-5")
def test_desktop_entries_report_rc_and_correct_guidance(manager, fake_config, workdir):
    make_desktop(fake_config, "RC없는터미널", workdir)
    d = by_name(manager.list_sessions(), "RC없는터미널")
    assert d["rc_url"] is None and "Remote Control 도 꺼져" in d["reason"] and "--permission-mode default" in d["reason"]


@pytest.mark.req("P3-5")
async def test_list_sessions_reports_bridge_version(manager):
    from mcp import Client

    from bridge.mcp_server import build_mcp

    async with Client(build_mcp(manager)) as c:
        r = json.loads((await c.call_tool("list_sessions", {})).content[0].text)
    assert r["bridge_version"].startswith("0.1.0")
