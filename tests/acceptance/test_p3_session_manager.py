"""P3 세션 관리자 인수 테스트 (가짜 claude 사용)."""
import time

import pytest

from bridge import transcript as tr
from bridge.session_manager import BridgeError
from tests.conftest import wait_until


def _done(manager, rid):
    r = manager.get_result(rid)
    return r if r["status"] in (tr.DONE, tr.INTERRUPTED) else None


@pytest.mark.req("P3-1")
def test_register_and_list(manager, workdir):
    manager.start_session("테스트 세션")
    manager.start_session("다른 세션")
    lst = manager.list_sessions()
    assert {s["name"] for s in lst} == {"테스트 세션", "다른 세션"}
    for s in lst:
        assert s["folder"] == str(workdir.resolve())
        assert s["host"] == "testhost"
        assert s["status"] == tr.IDLE and s["status_ko"] == "대기"
        assert s["selected"] is False


@pytest.mark.req("P3-2")
def test_send_without_selection_is_refused(manager):
    manager.start_session("테스트 세션")
    with pytest.raises(BridgeError) as e:
        manager.send_command("hello")
    assert e.value.code == "no_selection"


@pytest.mark.req("P3-2")
def test_select_pins_target(manager):
    manager.start_session("테스트 세션")
    manager.start_session("다른 세션")
    info = manager.select_session("다른 세션")
    assert info["name"] == "다른 세션"
    r = manager.send_command("hello")
    assert r["session"] == "다른 세션"
    done = wait_until(lambda: _done(manager, r["request_id"]))
    assert done["response"] == "ECHO: hello"
    # 고정은 다시 지정할 때까지 유지
    assert [s["name"] for s in manager.list_sessions() if s["selected"]] == ["다른 세션"]
    with pytest.raises(BridgeError):
        manager.select_session("없는 세션")
    assert [s["name"] for s in manager.list_sessions() if s["selected"]] == ["다른 세션"]


@pytest.mark.req("P3-3")
def test_refuse_while_working(manager):
    manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    r = manager.send_command("SLOW task")
    assert wait_until(lambda: manager.get_result(r["request_id"])["status"] == tr.WORKING)
    with pytest.raises(BridgeError) as e:
        manager.send_command("second")
    assert e.value.code == "busy"
    assert e.value.extra["session"]["status"] == tr.WORKING
    manager.interrupt()
    assert wait_until(lambda: _done(manager, r["request_id"]))["status"] == tr.INTERRUPTED


@pytest.mark.req("P3-3")
def test_refuse_while_awaiting_approval_then_resume(manager):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    r = manager.send_command("PERM delete file")
    got = wait_until(lambda: (x := manager.get_result(r["request_id"]))["status"] == tr.AWAITING_APPROVAL and x)
    assert got["pending_tools"] == ["Bash"]
    assert "Code 탭" in got["message"]
    with pytest.raises(BridgeError) as e:
        manager.send_command("another")
    assert e.value.code == "busy"
    s.proc.write("y")  # (실제로는 사람이 Code 탭에서 승인)
    done = wait_until(lambda: _done(manager, r["request_id"]))
    assert done["status"] == tr.DONE and done["response"] == "APPROVED"
    # 대기로 돌아오면 다시 주입 가능 (큐잉 없음: 거부된 명령은 실행되지 않았다)
    r2 = manager.send_command("next")
    assert wait_until(lambda: _done(manager, r2["request_id"]))["response"] == "ECHO: next"
    prompts = [t.prompt for t in tr.split_turns(s.records())]
    assert prompts == ["PERM delete file", "next"]


@pytest.mark.req("P3-4")
def test_start_only_presets(manager, tmp_path):
    with pytest.raises(BridgeError) as e:
        manager.start_session("임의 세션")
    assert e.value.code == "unknown_preset"
    assert "테스트 세션" in e.value.extra["presets"]
    s = manager.start_session("테스트 세션")
    assert s.ready
    with pytest.raises(BridgeError) as e:
        manager.start_session("테스트 세션")
    assert e.value.code == "already_running"


@pytest.mark.req("P3-4")
def test_startup_dialog_dismissed_with_escape(manager, monkeypatch):
    monkeypatch.setenv("FAKE_DIALOG", "1")
    s = manager.start_session("테스트 세션")
    assert s.ready and s.info()["status"] == tr.IDLE


@pytest.mark.req("P3-4")
def test_untrusted_folder_is_not_auto_accepted(manager, monkeypatch):
    monkeypatch.setenv("FAKE_TRUST", "1")
    with pytest.raises(BridgeError) as e:
        manager.start_session("테스트 세션")
    assert e.value.code == "folder_not_trusted"


@pytest.mark.req("P3-4")
def test_permission_mode_is_default(manager):
    s = manager.start_session("테스트 세션")
    argv = s.proc.argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--remote-control") + 1] == "테스트 세션"
    assert argv[argv.index("--session-id") + 1] == s.session_id
    recs = s.records()
    assert any(r.get("type") == "permission-mode" and r.get("permissionMode") == "default" for r in recs)


def test_forbidden_extra_args_rejected(fake_config):
    from bridge.config import BridgeConfig

    with pytest.raises(ValueError):
        BridgeConfig(claude_extra_args=["--dangerously-skip-permissions"])
    with pytest.raises(ValueError):
        BridgeConfig(claude_extra_args=["--permission-mode=bypassPermissions"])


def _status_is(manager, name, st):
    return manager.get(name).status() == st


@pytest.mark.req("P3-3")
def test_interrupt_while_thinking_then_next_command_is_clean(manager):
    """실측: 생각 중 Esc 는 기록을 남기지 않고 프롬프트를 입력창에 되돌린다.
    → 조용해지면 대기로 판정, 다음 명령은 입력창을 비운 뒤 넣어 이어 붙지 않아야 한다."""
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    r = manager.send_command("THINK hard about it")
    assert wait_until(lambda: manager.get_result(r["request_id"])["status"] == tr.WORKING)
    with pytest.raises(BridgeError):
        manager.send_command("too early")
    manager.interrupt()
    assert wait_until(lambda: _status_is(manager, "테스트 세션", tr.IDLE), timeout=15)
    assert manager.get_result(r["request_id"])["status"] == tr.INTERRUPTED
    r2 = manager.send_command("next one")
    done = wait_until(lambda: _done(manager, r2["request_id"]))
    assert done["response"] == "ECHO: next one" and "prompt_mismatch" not in done
    assert [t.prompt for t in tr.split_turns(s.records())] == ["THINK hard about it", "next one"]
    # 앞 요청은 다음 프롬프트로 대체되어도 계속 '중단됨'
    assert manager.get_result(r["request_id"])["status"] == tr.INTERRUPTED


@pytest.mark.req("P3-3")
def test_restored_multiline_prompt_is_cleared(manager):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    r = manager.send_command("THINK\n둘째 줄\n셋째 줄")
    assert wait_until(lambda: manager.get_result(r["request_id"])["status"] == tr.WORKING)
    manager.interrupt()
    assert wait_until(lambda: _status_is(manager, "테스트 세션", tr.IDLE), timeout=15)
    r2 = manager.send_command("clean")
    assert wait_until(lambda: _done(manager, r2["request_id"]))["response"] == "ECHO: clean"


@pytest.mark.req("P3-3")
def test_permission_dialog_without_hooks_is_awaiting_approval(manager, monkeypatch):
    """훅 이벤트가 없어도(백그라운드 세션처럼) claude agents 의 waiting 으로 승인 대기를 정확히 판정.
    대화상자에 명령+Enter 가 들어가면 승인이 눌릴 수 있으므로 주입은 거부되어야 한다."""
    monkeypatch.setenv("FAKE_NO_PERM_HOOKS", "1")
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    r = manager.send_command("PERM rm")
    assert wait_until(lambda: s.status() == tr.AWAITING_APPROVAL, timeout=15)
    assert manager.get_result(r["request_id"])["status"] == tr.AWAITING_APPROVAL
    assert not s.prompt_box_ready()
    with pytest.raises(BridgeError) as e:
        manager.send_command("y")
    assert e.value.code == "busy"
    s.proc.write("y")  # 사람이 승인
    assert wait_until(lambda: _done(manager, r["request_id"]))["response"] == "APPROVED"
    assert s.prompt_box_ready()


@pytest.mark.req("P3-3")
def test_fallback_without_agents_never_mistakes_dialog_for_idle(manager, monkeypatch):
    """claude agents 조회가 안 될 때의 대체 판정: 화면이 조용해도 결과 없는 tool_use 가 있으면 대기로 보지 않는다."""
    monkeypatch.setenv("FAKE_NO_PERM_HOOKS", "1")
    manager.agents.env = {**manager.agents.env, "FAKE_AGENTS_FAIL": "1"}
    manager.agents._at = 0
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    r = manager.send_command("PERM rm")
    time.sleep(s.quiet_window + 2)
    assert manager.agents.get(fresh=True) is None
    assert s.quiet()  # 화면은 조용하다
    assert s.status() == tr.WORKING
    assert not s.prompt_box_ready()
    with pytest.raises(BridgeError) as e:
        manager.send_command("y")
    assert e.value.code == "busy"
    s.proc.write("y")
    assert wait_until(lambda: _done(manager, r["request_id"]))["response"] == "APPROVED"
