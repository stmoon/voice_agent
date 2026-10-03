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
    for bad in ("acceptEdits", "plan"):
        with pytest.raises(ValueError):
            BridgeConfig(allowed_permission_modes=["default", bad])


class _P:
    def __init__(self, screen):
        self._s = screen

    def screen(self):
        return self._s


@pytest.mark.parametrize("screen,ok", [
    ("❯ \n⏵⏵ auto mode on · ← for agents", True),
    ("❯ \n⏵⏵ auto mode on (shift+tab to cycle)", True),
    ("⏵⏵ auto mode on · ← for agents\nDo you want to proceed?\n❯ 1. Yes\nEsc to cancel", False),
    ("⏸ manual mode on · ← for agents\n⏵⏵ accept edits on · ← for agents", False),   # 마지막 모드가 비허용
    ("⏵⏵ bypass permissions on (shift+tab to cycle)", False),
])
def test_prompt_box_ready_recognizes_auto_footer(screen, ok):
    s = Session.__new__(Session)
    s.proc = _P(screen)
    s.allowed_modes = ("default", "auto")
    assert Session.prompt_box_ready(s) is ok


# --- 모드 변경은 transcript 에 늦게 기록된다 (실측) → 화면 하단의 현재 모드로 막는다 ---

@pytest.mark.req("P3-3")
def test_mode_switched_on_screen_blocks_before_transcript_records_it(manager):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    s.proc.write("\x1b[Z")            # default → auto (기록 없음)
    assert wait_until(lambda: s.screen_mode() == "auto", timeout=5)
    r = manager.send_command("ok in auto")
    assert wait_until(lambda: done(manager, r["request_id"]))["response"] == "ECHO: ok in auto"
    s.proc.write("\x1b[Z")            # auto → acceptEdits (기록 없음)
    assert wait_until(lambda: s.screen_mode() == "acceptEdits", timeout=5)
    assert s.permission_mode() in ("default", "auto")   # transcript 는 아직 모름
    with pytest.raises(BridgeError) as e:
        manager.send_command("edit everything")
    assert e.value.code == "not_commandable" and "acceptEdits" in e.value.message
    assert not s.prompt_box_ready()
    s.proc.write("\x1b[Z")            # → default
    assert wait_until(lambda: s.screen_mode() == "default", timeout=5)
    r = manager.send_command("back")
    assert wait_until(lambda: done(manager, r["request_id"]))["response"] == "ECHO: back"


@pytest.mark.req("P3-6")
def test_background_session_screen_mode_blocks(manager, fake_config, workdir):
    short = make_bg(fake_config, "위키", workdir, mode="default")
    for f in (fake_config.claude_config_dir / "fake_agents").glob("*.json"):   # 대기 중 Shift+Tab → acceptEdits
        e = json.loads(f.read_text())
        if e.get("id") == short:
            e["mode"] = "acceptEdits"
            f.write_text(json.dumps(e, ensure_ascii=False))
    manager.list_sessions()
    manager.select_session("위키")          # transcript 는 default 라 고정은 됨
    s = manager.get("위키")
    assert s.screen_mode() == "acceptEdits"
    with pytest.raises(BridgeError) as e:
        manager.send_command("hello")
    assert e.value.code == "not_commandable"


@pytest.mark.req("P3-5")
def test_list_reports_permission_mode(manager, fake_config, workdir):
    make_bg(fake_config, "자동", workdir, mode="auto")
    make_bg(fake_config, "승인", workdir, mode="default")
    lst = manager.list_sessions()
    assert by_name(lst, "자동")["permission_mode"] == "auto" and by_name(lst, "자동")["permission_mode_ko"] == "자동(auto)"
    assert by_name(lst, "승인")["permission_mode_ko"] == "승인 필요"


@pytest.mark.req("P3-5")
def test_launch_hint_follows_allowed_modes(fake_config, workdir):
    fake_config.allowed_permission_modes = ["default"]
    m = SessionManager(fake_config)
    try:
        make_bg(fake_config, "자동", workdir, mode="auto")
        r = by_name(m.list_sessions(), "자동")["reason"]
        assert "--permission-mode default" in r   # 안내대로 다시 띄우면 실제로 명령 가능한 세션이 됨
    finally:
        m.shutdown()
