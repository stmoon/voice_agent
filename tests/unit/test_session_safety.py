"""주입 안전장치 단위 테스트."""
from bridge.session_manager import Session


class StubProc:
    def __init__(self, screen):
        self._s = screen

    def screen(self):
        return self._s


def box(screen):
    s = Session.__new__(Session)
    s.proc = StubProc(screen)
    s.allowed_modes = ("default", "auto")
    return Session.prompt_box_ready(s)


def test_idle_prompt_is_ready():
    assert box("❯ Try \"refactor\"\n⏸ manual mode on · ? for shortcuts · ← for agents")


def test_permission_dialog_after_idle_footer_blocks():
    assert not box("? for shortcuts\n⏺ Bash(rm -rf x)\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n\nEsc to cancel")


def test_answered_dialog_then_idle_footer_is_ready():
    assert box("Do you want to proceed?\n❯ 1. Yes\nEsc to cancel\n⏺ done\n❯ \n? for shortcuts")


def test_no_footer_at_all_blocks():
    assert not box("loading…")


def test_startup_confirm_dialog_blocks():
    assert not box("? for shortcuts\nTry the new fullscreen renderer?\n❯ 1. Yes\nEnter to confirm · Esc to cancel")
