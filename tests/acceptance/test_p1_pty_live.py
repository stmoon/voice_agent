"""P1 PTY 주입 PoC 인수 테스트 — 실제 claude CLI 사용 (VC_LIVE=1, 로그인 필요)."""
import time

import pytest

from bridge import transcript as tr
from tests.conftest import wait_until

pytestmark = pytest.mark.live


@pytest.mark.req("P1-1")
def test_start_and_stop_real_rc_session(live_manager):
    s = live_manager.start_session("vc-live")
    assert s.ready and s.proc.is_alive()
    argv = s.proc.argv
    assert "--remote-control" in argv and argv[argv.index("--remote-control") + 1] == "vc-live"
    assert any(e.get("hook_event_name") == "SessionStart" for e in s.events())
    live_manager.stop_session("vc-live")
    assert wait_until(lambda: not s.proc.is_alive(), timeout=10)


@pytest.mark.req("P1-2")
def test_injected_prompt_and_reply_recorded_in_transcript(live_manager):
    s = live_manager.start_session("vc-live")
    assert s.warning is None, s.warning  # 로그인 만료 등
    live_manager.select_session("vc-live")
    prompt = "Reply with exactly the word PONG and nothing else."
    r = live_manager.send_command(prompt)
    done = wait_until(lambda: (x := live_manager.get_result(r["request_id"]))["status"] == tr.DONE and x, timeout=120)
    assert done, live_manager.get_result(r["request_id"])
    recs = s.records()
    assert any(tr.user_text(x) == prompt for x in recs)
    assistants = [x for x in recs if x.get("type") == "assistant"]
    assert assistants and all((x["message"].get("model") != "<synthetic>") for x in assistants)
    assert "PONG" in done["response"]


@pytest.mark.req("P1-3")
def test_escape_interrupts_running_turn(live_manager):
    s = live_manager.start_session("vc-live")
    assert s.warning is None, s.warning
    live_manager.select_session("vc-live")
    r = live_manager.send_command(
        "Write a very long, detailed essay (at least 3000 words) about the history of railways. Do not use any tools."
    )
    assert wait_until(lambda: live_manager.get_result(r["request_id"])["status"] == tr.WORKING, timeout=30)
    time.sleep(4)
    live_manager.interrupt()
    done = wait_until(lambda: (x := live_manager.get_result(r["request_id"]))["status"] != tr.WORKING and x, timeout=30)
    assert done and done["status"] == tr.INTERRUPTED, done
    assert wait_until(lambda: s.status() == tr.IDLE, timeout=10)
