"""P2 transcript 파서 인수 테스트. 화면 없이 JSONL(+훅 사이드카)만으로 판별."""
import json
import shutil

import pytest

from bridge import transcript as tr
from bridge.hook_sink import read_events
from tests.conftest import FIXTURES


def load(name):
    recs = tr.read_jsonl(FIXTURES / f"{name}.jsonl")
    ev = read_events(FIXTURES / f"{name}.events.jsonl")
    return recs, ev


# ---------- P2-1 ----------

@pytest.mark.req("P2-1")
def test_find_transcript_by_session_id(tmp_path):
    sid = "c1c18d3b-5d92-441a-9e98-d9aa87fc1ed9"
    proj = tmp_path / "projects" / "-Users-x-Project-voice-commander-sandbox-poc"
    proj.mkdir(parents=True)
    (tmp_path / "projects" / "-other").mkdir()
    (tmp_path / "projects" / "-other" / "different.jsonl").write_text("{}\n")
    target = proj / f"{sid}.jsonl"
    shutil.copy(FIXTURES / "real_cli_2.1.285_synthetic_reply.jsonl", target)
    assert tr.find_transcript(sid, tmp_path) == target
    assert tr.find_transcript("00000000-0000-0000-0000-000000000000", tmp_path) is None
    assert tr.find_transcript(sid, tmp_path / "nowhere") is None


@pytest.mark.req("P2-1")
def test_find_transcript_uses_env_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "projects" / "p").mkdir(parents=True)
    f = tmp_path / "projects" / "p" / "abc.jsonl"
    f.write_text("")
    assert tr.find_transcript("abc") == f


@pytest.mark.req("P2-1")
def test_partial_last_line_is_ignored(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(json.dumps({"type": "mode"}) + "\n" + '{"type": "user", "mess')
    assert tr.read_jsonl(f) == [{"type": "mode"}]


@pytest.mark.req("P2-1")
def test_incremental_reader_matches_full_read(tmp_path):
    """이어 읽기: 새로 완결된 줄만 파싱하되 결과는 매번 처음부터 읽은 것과 같아야 한다."""
    f = tmp_path / "t.jsonl"
    r = tr.JsonlReader()
    assert r.read(None) == [] and r.read(tmp_path / "none.jsonl") == []
    f.write_bytes(b"")
    assert r.read(f) == []
    with open(f, "ab") as w:
        w.write(json.dumps({"n": 1}).encode() + b"\n" + '{"n": 2, "t": "한'.encode())  # 기록 중인 줄 (한글 중간에서 끊김)
    assert r.read(f) == [{"n": 1}] == tr.read_jsonl(f)
    with open(f, "ab") as w:
        w.write('글"}\n'.encode() + b"not json\n\n" + json.dumps({"n": 3}).encode() + b"\r\n"
                + b'{"bad": "\xff"}\n')
    got = r.read(f)
    assert got == [{"n": 1}, {"n": 2, "t": "한글"}, {"n": 3}, {"bad": "�"}] == tr.read_jsonl(f)
    got.append({"x": 1})                      # 돌려받은 목록을 바꿔도 다음 결과에 영향 없음
    assert r.read(f) == tr.read_jsonl(f)
    f.write_text(json.dumps({"n": 9}) + "\n")  # 줄어듦(교체·잘림) → 처음부터
    assert r.read(f) == [{"n": 9}]
    for name in ("fake_approved", "real_2.1.285_pong_done", "real_2.1.285_interrupt_while_streaming"):
        assert tr.JsonlReader().read(FIXTURES / f"{name}.jsonl") == tr.read_jsonl(FIXTURES / f"{name}.jsonl")


@pytest.mark.req("P2-1")
def test_rc_url_in_file_matches_rc_url(tmp_path):
    f = tmp_path / "t.jsonl"
    url = "https://claude.ai/code/session_x"
    lines = [{"type": "user", "message": {"role": "user", "content": "system 이라는 단어"}},
             {"type": "system", "subtype": "bridge_status", "url": url},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}]
    f.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
    assert tr.rc_url_in_file(f) == tr.rc_url(tr.read_jsonl(f)) == url
    with open(f, "a", encoding="utf-8") as w:
        w.write(json.dumps({"type": "system", "content": "Remote Control disconnected"}) + "\n")
    assert tr.rc_url_in_file(f) is None and tr.rc_url(tr.read_jsonl(f)) is None
    assert tr.rc_url_in_file(None) is None and tr.rc_url_in_file(tmp_path / "none.jsonl") is None


# ---------- P2-2 ----------

@pytest.mark.req("P2-2")
def test_request_maps_to_its_turn_final_response():
    recs, ev = load("fake_approved")
    prompts = [t.prompt for t in tr.split_turns(recs)]
    assert prompts == ["hello", "SLOW job", "PERM rm"]
    # 요청 ID ↔ 주입 직전 레코드 수(offset) → 그 뒤 첫 턴
    first = tr.turn_after(recs, 0)
    assert first.prompt == "hello" and first.final_text() == "ECHO: hello"
    slow_off = first.prompt_index + 1 + len(first.records)
    slow = tr.turn_after(recs, slow_off)
    assert slow.prompt == "SLOW job" and tr.turn_state(slow, ev) == tr.INTERRUPTED
    perm = tr.turn_after(recs, slow.prompt_index + 1)
    assert perm.prompt == "PERM rm"
    # 최종 응답 = 마지막 tool_result 이후 텍스트 (도구 호출 전 설명문 제외)
    assert perm.final_text() == "APPROVED"
    assert tr.turn_state(perm, ev) == tr.DONE


@pytest.mark.req("P2-2")
def test_real_cli_sample_maps_prompt_and_reply():
    recs = tr.read_jsonl(FIXTURES / "real_cli_2.1.285_synthetic_reply.jsonl")
    t = tr.turn_after(recs, 0)
    assert t.prompt == "Reply with exactly the word PONG and nothing else."
    assert t.final_text() == "Login expired · Please run /login"
    assert tr.turn_state(t) == tr.DONE
    assert t.is_error()  # 로그인 만료 → CLI 합성 응답은 오류로 표시
    assert tr.turn_after(recs, t.prompt_index + 1) is None


@pytest.mark.req("P2-2")
def test_assistant_blocks_split_across_lines_are_joined():
    recs = [
        {"type": "user", "message": {"role": "user", "content": "q"}, "uuid": "u1"},
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": ""}], "stop_reason": None}},
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "첫 줄"}], "stop_reason": None}},
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "둘째 줄"}], "stop_reason": "end_turn"}},
    ]
    t = tr.turn_after(recs, 0)
    assert t.final_text() == "첫 줄\n둘째 줄"
    assert tr.turn_state(t) == tr.DONE


# ---------- P2-3 ----------

@pytest.mark.req("P2-3")
@pytest.mark.parametrize("name,expected", [
    ("real_cli_2.1.285_synthetic_reply", tr.IDLE),
    ("fake_done", tr.IDLE),
    ("fake_working", tr.WORKING),
    ("fake_interrupted", tr.IDLE),
    ("fake_awaiting_approval", tr.AWAITING_APPROVAL),
    ("fake_approved", tr.IDLE),
])
def test_status_from_recorded_samples(name, expected):
    recs, ev = load(name)
    assert tr.session_status(recs, ev) == expected


@pytest.mark.req("P2-3")
def test_pending_tool_without_permission_event_is_working():
    recs, _ = load("fake_awaiting_approval")
    assert tr.session_status(recs, []) == tr.WORKING


@pytest.mark.req("P2-3")
def test_old_permission_event_does_not_mark_new_tool_as_awaiting():
    recs, ev = load("fake_awaiting_approval")
    old = [dict(e, _ts=e["_ts"] - 3600) for e in ev]
    assert tr.session_status(recs, old) == tr.WORKING


@pytest.mark.req("P2-3")
def test_empty_transcript_is_idle():
    assert tr.session_status([], []) == tr.IDLE


@pytest.mark.req("P2-3")
def test_prompt_without_reply_is_working():
    recs = [{"type": "user", "message": {"role": "user", "content": "q"}}]
    assert tr.session_status(recs) == tr.WORKING


@pytest.mark.req("P2-3")
def test_meta_and_command_records_are_not_prompts():
    recs = [
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "caveat"}},
        {"type": "user", "message": {"role": "user", "content": "<command-name>/model</command-name>"}},
        {"type": "user", "message": {"role": "user", "content": "<local-command-stdout>x</local-command-stdout>"}},
    ]
    assert tr.split_turns(recs) == []
    assert tr.session_status(recs) == tr.IDLE


# ---------- 실측 샘플 (CLI 2.1.285, 로그인 후 녹화) ----------

@pytest.mark.req("P2-2")
def test_real_done_turn_maps_to_reply():
    recs = tr.read_jsonl(FIXTURES / "real_2.1.285_pong_done.jsonl")
    t = tr.turn_after(recs, 0)
    assert t.prompt == "Reply with exactly the word PONG and nothing else."
    assert tr.turn_state(t) == tr.DONE and t.final_text() == "PONG" and not t.is_error()
    assert tr.rc_url(recs) == "https://claude.ai/code/session_REDACTED"


@pytest.mark.req("P2-3")
def test_real_interrupt_while_thinking_leaves_no_marker_but_is_superseded():
    """생각 중 Esc → transcript 에 흔적 없음. 다음 프롬프트가 오면 앞 턴은 중단으로 본다."""
    recs = tr.read_jsonl(FIXTURES / "real_2.1.285_interrupt_while_thinking.jsonl")
    first, second = tr.split_turns(recs)
    assert not any(tr.is_interrupt(r) for r in first.records)  # 실제로 표시가 없다
    assert first.superseded and tr.turn_state(first) == tr.INTERRUPTED
    assert tr.turn_state(second) == tr.DONE and second.final_text() == "PONG"
    # 입력창 잔여물 때문에 이어 붙은 프롬프트(버그 재현 기록) — bridge 는 이제 주입 전에 입력창을 비운다
    assert second.prompt.startswith(first.prompt) and second.prompt.endswith("Reply with exactly the word PONG and nothing else.")
    assert tr.session_status(recs) == tr.IDLE


@pytest.mark.req("P2-3")
def test_real_interrupt_while_streaming_has_marker_and_partial_text():
    recs = tr.read_jsonl(FIXTURES / "real_2.1.285_interrupt_while_streaming.jsonl")
    done, cut = tr.split_turns(recs)
    assert tr.turn_state(done) == tr.DONE and done.final_text() == "L3"
    assert tr.turn_state(cut) == tr.INTERRUPTED
    assert cut.final_text().startswith("1\n2\n3")
    assert tr.session_status(recs) == tr.IDLE


@pytest.mark.req("P2-3")
def test_last_unfinished_turn_without_marker_is_working_in_pure_parser():
    """파서 단독으론 마지막 미완료 턴을 작업 중으로 둔다 (조용함 판정은 세션 관리자가 PTY 활동으로 보완)."""
    recs = tr.read_jsonl(FIXTURES / "real_2.1.285_interrupt_while_thinking.jsonl")
    first_only = recs[: tr.split_turns(recs)[1].prompt_index]
    assert tr.session_status(first_only) == tr.WORKING
