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
    # RC 가 실제로 붙으면 CLI 가 Code 탭 세션 URL 을 transcript 에 남긴다 → list_sessions 의 rc_url
    url = wait_until(lambda: tr.rc_url(s.records()), timeout=30)
    assert url and url.startswith("https://claude.ai/code/session_")
    assert live_manager.list_sessions()[0]["rc_url"] == url
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


@pytest.mark.req("P1-3")
def test_escape_interrupts_while_streaming(live_manager):
    s = live_manager.start_session("vc-live")
    live_manager.select_session("vc-live")
    r = live_manager.send_command(
        "Count from 1 to 600, each number on its own line, with no other text. Do not think, just start counting immediately."
    )
    assert wait_until(lambda: "12" in s.proc.screen()[-3000:] and s.proc.output_rate(1) > 200, timeout=60)
    live_manager.interrupt()
    done = wait_until(lambda: (x := live_manager.get_result(r["request_id"]))["status"] != tr.WORKING and x, timeout=30)
    assert done and done["status"] == tr.INTERRUPTED, done
    assert done["response"].startswith("1\n2\n3")
    assert wait_until(lambda: s.status() == tr.IDLE, timeout=15)


@pytest.mark.req("P1-2")
def test_next_prompt_after_thinking_interrupt_is_recorded_exactly(live_manager):
    """생각 중 Esc 로 중단하면 프롬프트가 입력창에 되돌아온다(실측). 다음 주입이 그 뒤에 이어 붙으면 안 된다."""
    s = live_manager.start_session("vc-live")
    live_manager.select_session("vc-live")
    r = live_manager.send_command(
        "Think very carefully for a long time, then write a 3000-word essay about bridges. Do not use any tools."
    )
    time.sleep(5)
    live_manager.interrupt()
    assert wait_until(lambda: s.status() == tr.IDLE, timeout=30)
    prompt = "Reply with exactly the word PONG and nothing else."
    r2 = live_manager.send_command(prompt)
    done = wait_until(lambda: (x := live_manager.get_result(r2["request_id"]))["status"] == tr.DONE and x, timeout=120)
    assert done and "prompt_mismatch" not in done, done
    assert tr.split_turns(s.records())[-1].prompt == prompt
    assert "PONG" in done["response"]
