"""auto 권한 모드 세션도 음성 명령 대상 (2026-10-03 사용자 결정). acceptEdits·권한 우회는 계속 거부."""
import json

import pytest

from bridge import transcript as tr
from bridge.config import BridgeConfig
from bridge.session_manager import BridgeError, Session, SessionManager
from tests.acceptance.test_p3_external_sessions import by_name, done, make_bg
from tests.conftest import wait_until


def append(s, rec):
    with open(s.transcript_path(), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


@pytest.mark.req("P3-6")
def test_auto_mode_background_session_is_commandable(manager, fake_config, workdir):
    make_bg(fake_config, "자동", workdir, mode="auto")
    d = by_name(manager.list_sessions(), "자동")
    assert d["commandable"], d
    manager.select_session("자동")
    s = manager.get("자동")
    assert s.prompt_box_ready()  # attach 화면 하단 '⏵⏵ auto mode on' 을 입력 대기로 인식
    r = manager.send_command("hello auto")
    assert wait_until(lambda: done(manager, r["request_id"]))["response"] == "ECHO: hello auto"


@pytest.mark.req("P3-4")
def test_bridge_session_can_start_in_auto_mode(fake_config):
    fake_config.session_permission_mode = "auto"
    m = SessionManager(fake_config)
    try:
        s = m.start_session("테스트 세션")
        argv = s.proc.argv
        assert argv[argv.index("--permission-mode") + 1] == "auto"
        assert s.info()["commandable"]
        m.select_session("테스트 세션")
        r = m.send_command("hi")
        assert wait_until(lambda: done(m, r["request_id"]))["response"] == "ECHO: hi"
    finally:
        m.shutdown()


@pytest.mark.req("P3-3")
def test_switching_to_auto_is_allowed_but_accept_edits_is_not(manager):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    append(s, {"type": "permission-mode", "permissionMode": "auto", "sessionId": s.session_id})
    r = manager.send_command("one")
    assert wait_until(lambda: done(manager, r["request_id"]))["response"] == "ECHO: one"
    for bad in ("acceptEdits", "bypassPermissions"):
        append(s, {"type": "permission-mode", "permissionMode": bad, "sessionId": s.session_id})
        with pytest.raises(BridgeError) as e:
            manager.send_command("two")
        assert e.value.code == "not_commandable" and bad in e.value.message


@pytest.mark.req("P3-3")
def test_auto_can_be_disallowed_by_config(fake_config, workdir):
    fake_config.allowed_permission_modes = ["default"]
    m = SessionManager(fake_config)
    try:
        make_bg(fake_config, "자동", workdir, mode="auto")
        make_bg(fake_config, "승인", workdir, mode="default")
        lst = m.list_sessions()
        assert not by_name(lst, "자동")["commandable"] and by_name(lst, "승인")["commandable"]
    finally:
        m.shutdown()


@pytest.mark.req("P3-3")
def test_config_never_allows_permission_bypass():
    with pytest.raises(ValueError):
        BridgeConfig(allowed_permission_modes=["default", "bypassPermissions"])
    with pytest.raises(ValueError):
        BridgeConfig(session_permission_mode="acceptEdits")  # 허용 목록 밖
    with pytest.raises(ValueError):
        BridgeConfig(claude_extra_args=["--permission-mode", "auto"])  # 모드는 설정 항목으로만
    assert BridgeConfig(allowed_permission_modes=["default", "auto", "acceptEdits"],
                        session_permission_mode="acceptEdits").session_permission_mode == "acceptEdits"


class _P:
    def __init__(self, screen):
        self._s = screen

    def screen(self):
        return self._s


@pytest.mark.parametrize("screen,ok", [
    ("❯ \n⏵⏵ auto mode on · ← for agents", True),
    ("❯ \n⏵⏵ auto mode on (shift+tab to cycle)", True),
    ("⏵⏵ auto mode on · ← for agents\nDo you want to proceed?\n❯ 1. Yes\nEsc to cancel", False),
])
def test_prompt_box_ready_recognizes_auto_footer(screen, ok):
    s = Session.__new__(Session)
    s.proc = _P(screen)
    assert Session.prompt_box_ready(s) is ok
