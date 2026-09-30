"""P3 세션 관리자 인수 테스트 (가짜 claude 사용)."""
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
