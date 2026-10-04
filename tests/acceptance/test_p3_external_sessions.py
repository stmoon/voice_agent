"""이 머신의 모든 세션 목록(P3-5)과 백그라운드 세션 attach 주입(P3-6), 명령 정리(보안).

가짜 claude 가 `agents --json` / `--bg` / `attach` 를 실측 동작대로 흉내 낸다.
실제 claude 로 하는 확인은 live 표시 (VC_LIVE=1).
"""
import json
import os
import subprocess
import sys
import time

import pytest
from mcp import Client

from bridge import transcript as tr
from bridge.mcp_server import build_mcp
from bridge.session_manager import BACKGROUND, BRIDGE, DESKTOP, BridgeError, sanitize_command
from tests.conftest import FAKE, wait_until


def env_for(cfg, **extra):
    return {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg.claude_config_dir), **extra}


def make_bg(cfg, name, cwd, rc=True, mode="default", **env):
    args = [sys.executable, str(FAKE), "--bg", "-n", name, "--permission-mode", mode]
    if rc:
        args += ["--remote-control", name]
    out = subprocess.run(args, cwd=cwd, env=env_for(cfg, **env), capture_output=True, text=True, check=True).stdout
    return out.split("·")[1].strip()  # 짧은 ID


def make_desktop(cfg, name, cwd, status="idle"):
    reg = cfg.claude_config_dir / "fake_agents"
    reg.mkdir(parents=True, exist_ok=True)
    sid = f"d{abs(hash(name)) % 10**7:07d}-0000-4000-8000-000000000000"
    (reg / f"{sid}.json").write_text(json.dumps({
        "pid": os.getpid(), "cwd": str(cwd), "kind": "interactive", "sessionId": sid, "name": name,
        "status": status, "startedAt": 0}, ensure_ascii=False))
    return sid


def done(manager, rid):
    r = manager.get_result(rid)
    return r if r["status"] in (tr.DONE, tr.INTERRUPTED) else None


def by_name(lst, name):
    return next(d for d in lst if d["name"] == name)


# ---------------- P3-5: 모든 세션 목록 ----------------

@pytest.mark.req("P3-5")
def test_list_shows_bridge_background_and_desktop(manager, fake_config, workdir):
    manager.start_session("테스트 세션")
    make_bg(fake_config, "위키", workdir)
    make_desktop(fake_config, "voice_commander", workdir)
    make_desktop(fake_config, "테스트", workdir, status="busy")
    lst = manager.list_sessions()
    names = [d["name"] for d in lst]
    assert names.count("테스트 세션") == 1  # bridge 세션이 데스크톱으로 중복 표시되지 않음
    assert [d["kind"] for d in lst] == [BRIDGE, BACKGROUND, DESKTOP, DESKTOP]  # 정렬: 명령 가능 먼저
    b, w, v = by_name(lst, "테스트 세션"), by_name(lst, "위키"), by_name(lst, "voice_commander")
    assert b["commandable"] and b["kind_ko"] == "브리지 세션"
    assert w["commandable"] and w["kind_ko"] == "백그라운드 세션" and w["rc_url"] and w["status_ko"] == "대기"
    assert not v["commandable"] and "claude --bg" in v["reason"]  # 음성용 세션 띄우는 법 안내
    assert by_name(lst, "테스트")["status_ko"] == "작업 중"  # 실시간 상태(busy)
    for d in lst:
        assert d["folder"] == str(workdir.resolve()) or d["folder"] == str(workdir)
        assert d["host"] == "testhost"


@pytest.mark.req("P3-5")
def test_view_only_and_unsafe_background_sessions_are_refused(manager, fake_config, workdir):
    make_desktop(fake_config, "데스크톱", workdir)
    make_bg(fake_config, "RC없음", workdir, rc=False)
    make_bg(fake_config, "편집자동", workdir, mode="acceptEdits")
    lst = manager.list_sessions()
    assert not by_name(lst, "RC없음")["commandable"] and "Remote Control" in by_name(lst, "RC없음")["reason"]
    assert not by_name(lst, "편집자동")["commandable"] and "default·auto" in by_name(lst, "편집자동")["reason"]
    with pytest.raises(BridgeError) as e:
        manager.select_session("데스크톱")
    assert e.value.code == "view_only"
    for n in ("RC없음", "편집자동"):
        with pytest.raises(BridgeError) as e:
            manager.select_session(n)
        assert e.value.code == "not_commandable"
    with pytest.raises(BridgeError) as e:
        manager.send_command("hi")
    assert e.value.code == "no_selection"


@pytest.mark.req("P3-5")
def test_background_session_with_invalid_login_says_login(manager, fake_config, workdir):
    """실측(2.1.286, Windows): 로그인이 무효면 RC 가 'Remote Control disconnected — /login' 만 남기고 붙지 않는다.
    '다시 띄우세요' 는 소용없으니 claude /login 을 안내해야 한다."""
    make_bg(fake_config, "로그인만료", workdir, FAKE_LOGIN_INVALID="1")
    make_bg(fake_config, "RC없음", workdir, rc=False)
    lst = manager.list_sessions()
    expired = by_name(lst, "로그인만료")
    assert not expired["commandable"] and "claude /login" in expired["reason"] and "다시 띄우세요" not in expired["reason"]
    assert "claude /login" in expired["warning"]
    plain = by_name(lst, "RC없음")
    assert "다시 띄우세요" in plain["reason"] and "warning" not in plain   # 그냥 RC 를 안 켠 세션은 기존 안내
    with pytest.raises(BridgeError) as e:
        manager.select_session("로그인만료")
    assert e.value.code == "not_commandable" and "claude /login" in e.value.message


@pytest.mark.req("P3-5")
def test_ambiguous_name_needs_session_id(manager, fake_config, workdir):
    make_bg(fake_config, "같은이름", workdir)
    make_bg(fake_config, "같은이름", workdir)
    lst = manager.list_sessions()
    with pytest.raises(BridgeError) as e:
        manager.select_session("같은이름")
    assert e.value.code == "ambiguous" and len(e.value.extra["candidates"]) == 2
    sid = [d for d in lst if d["name"] == "같은이름"][0]["session_id"]
    assert manager.select_session(sid)["session_id"] == sid


@pytest.mark.req("P3-5")
def test_vanished_background_session_drops_from_list(manager, fake_config, workdir):
    short = make_bg(fake_config, "잠깐", workdir)
    assert "잠깐" in [d["name"] for d in manager.list_sessions()]
    for f in (fake_config.claude_config_dir / "fake_agents").glob("*.json"):
        if json.loads(f.read_text()).get("id") == short:
            f.unlink()
    assert "잠깐" not in [d["name"] for d in manager.list_sessions()]


@pytest.mark.req("P3-5")
async def test_list_sessions_tool_reports_kinds(manager, fake_config, workdir):
    make_bg(fake_config, "위키", workdir)
    make_desktop(fake_config, "데스크톱", workdir)
    async with Client(build_mcp(manager)) as c:
        r = json.loads((await c.call_tool("list_sessions", {})).content[0].text)
        kinds = {s["name"]: (s["kind"], s["commandable"]) for s in r["sessions"]}
        assert kinds == {"위키": (BACKGROUND, True), "데스크톱": (DESKTOP, False)}
        r = json.loads((await c.call_tool("select_session", {"name": "데스크톱"})).content[0].text)
        assert r["ok"] is False and r["error"] == "view_only"


# ---------------- P3-6: 백그라운드 세션에 attach 로 주입 ----------------

@pytest.mark.req("P3-6")
def test_select_background_attaches_and_runs_command(manager, fake_config, workdir):
    make_bg(fake_config, "위키", workdir)
    manager.list_sessions()
    info = manager.select_session("위키")
    s = manager.get("위키")
    assert info["kind"] == BACKGROUND and s.attached() and s.prompt_box_ready()  # attach 화면 대기 표시 인식
    r = manager.send_command("hello background")
    d = wait_until(lambda: done(manager, r["request_id"]))
    assert d["status"] == tr.DONE and d["response"] == "ECHO: hello background" and "prompt_mismatch" not in d
    assert [t.prompt for t in tr.split_turns(s.records())] == ["hello background"]


@pytest.mark.req("P3-6")
def test_background_approval_flow_uses_live_status(manager, fake_config, workdir):
    """백그라운드 세션엔 훅이 없다. 승인 대기는 claude agents 의 waiting 으로 판정, 승인 뒤엔 작업 중 → 완료."""
    make_bg(fake_config, "위키", workdir)
    manager.list_sessions()
    manager.select_session("위키")
    s = manager.get("위키")
    r = manager.send_command("PERM rm")
    got = wait_until(lambda: (x := manager.get_result(r["request_id"]))["status"] == tr.AWAITING_APPROVAL and x)
    assert got and "Code 탭" in got["message"]
    with pytest.raises(BridgeError) as e:
        manager.send_command("y")
    assert e.value.code == "busy"
    s.proc.write("y")  # 사람이 Code 탭에서 승인한 것에 해당
    assert wait_until(lambda: done(manager, r["request_id"]))["response"] == "APPROVED"


@pytest.mark.req("P3-6")
def test_background_interrupt_and_clean_next_command(manager, fake_config, workdir):
    make_bg(fake_config, "위키", workdir)
    manager.list_sessions()
    manager.select_session("위키")
    s = manager.get("위키")
    r = manager.send_command("THINK long")
    assert wait_until(lambda: manager.get_result(r["request_id"])["status"] == tr.WORKING)
    manager.interrupt()
    assert wait_until(lambda: s.status() == tr.IDLE, timeout=15)
    assert manager.get_result(r["request_id"])["status"] == tr.INTERRUPTED
    r2 = manager.send_command("after")
    d = wait_until(lambda: done(manager, r2["request_id"]))
    assert d["response"] == "ECHO: after" and "prompt_mismatch" not in d


@pytest.mark.req("P3-6")
def test_shutdown_detaches_but_keeps_background_session(manager, fake_config, workdir):
    short = make_bg(fake_config, "위키", workdir)
    manager.start_session("테스트 세션")
    manager.list_sessions()
    manager.select_session("위키")
    attach_proc = manager.get("위키").proc
    manager.shutdown()
    assert wait_until(lambda: not attach_proc.is_alive(), timeout=10)
    entries = [json.loads(f.read_text()) for f in (fake_config.claude_config_dir / "fake_agents").glob("*.json")]
    assert any(e.get("id") == short for e in entries)  # 백그라운드 세션 자체는 남음


# ---------------- 보안: 명령 정리 ----------------

@pytest.mark.req("P3-3")
@pytest.mark.parametrize("raw,clean", [
    ("hello", "hello"),
    ("  테스트 돌려줘  ", "테스트 돌려줘"),
    ("\x1b[Zswitch mode", "[Zswitch mode"),          # Shift+Tab(권한 모드 순환) 시퀀스 무력화
    ("line1\rline2", "line1line2"),                  # 조기 제출 방지
    ("a\x1b[201~b", "a[201~b"),                      # 붙여넣기 탈출 방지
    ("여러\n줄\t명령", "여러\n줄\t명령"),
    ("wow! nice", "wow! nice"),
])
def test_sanitize_command(raw, clean):
    assert sanitize_command(raw) == clean


@pytest.mark.req("P3-3")
@pytest.mark.parametrize("raw", ["!rm -rf ~", "  !ls", "\x1b!whoami", "", "  \x07 "])
def test_sanitize_command_refuses(raw):
    with pytest.raises(BridgeError) as e:
        sanitize_command(raw)
    assert e.value.code in ("shell_mode_blocked", "empty")


@pytest.mark.req("P3-3")
def test_control_sequences_never_reach_session(manager):
    s = manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")
    with pytest.raises(BridgeError):
        manager.send_command("!touch pwned")
    r = manager.send_command("\x1b[Zhello")
    d = wait_until(lambda: done(manager, r["request_id"]))
    assert d["response"] == "ECHO: [Zhello"
    assert [t.prompt for t in tr.split_turns(s.records())] == ["[Zhello"]


# ---------------- 실제 claude ----------------

def _claude(claude_bin, *args, **kw):
    """실제 claude 실행: 브리지와 같은 방식 (UTF-8 로 읽음 — 한국어 Windows 기본 cp949 로 읽으면 '·' 가 깨진다,
    npm 설치본 claude.cmd 도 실행)."""
    from bridge.proc import run_quiet
    from bridge.pty_runner import child_env

    return run_quiet([*claude_bin, *args], env=child_env(), text=True, timeout=60, **kw)


def _real_agents(claude_bin):
    return json.loads(_claude(claude_bin, "agents", "--json").stdout or "[]")


@pytest.fixture
def real_bg(live_workdir, live_claude):
    name = f"vc-live-bg-{int(time.time()) % 100000}"
    r = _claude(live_claude, "--bg", "-n", name, "--remote-control", name, "--permission-mode", "default",
                cwd=live_workdir)
    assert "·" in (r.stdout or ""), f"claude --bg 실패 (종료 코드 {r.returncode}): {r.stdout!r} {r.stderr!r}"
    short = r.stdout.split("·")[1].strip()
    yield name, short
    _claude(live_claude, "stop", short)
    _claude(live_claude, "rm", short)


@pytest.mark.live
@pytest.mark.req("P3-5")
def test_live_list_matches_claude_agents(live_manager, real_bg):
    name, _ = real_bg
    bridge_s = live_manager.start_session("vc-live")
    lst = wait_until(lambda: (x := live_manager.list_sessions()) and any(d["name"] == name for d in x) and x, timeout=20)
    assert lst, "백그라운드 세션이 목록에 없음"
    agents = _real_agents(live_manager.config.claude_bin)
    assert {d["session_id"] for d in lst} == {a["sessionId"] for a in agents}
    assert by_name(lst, name)["kind"] == BACKGROUND
    assert [d["kind"] for d in lst if d["session_id"] == bridge_s.session_id] == [BRIDGE]  # 중복 없이 bridge 로 한 번
    assert all(not d["commandable"] for d in lst if d["kind"] == DESKTOP)


@pytest.mark.live
@pytest.mark.req("P3-6")
def test_live_command_into_background_session(live_manager, real_bg):
    name, _ = real_bg
    ok = wait_until(lambda: by_name(live_manager.list_sessions(), name)["commandable"], timeout=30)
    assert ok, by_name(live_manager.list_sessions(), name)   # 실패하면 이유(reason·warning)가 보이게 (RC·로그인 등)
    live_manager.select_session(name)
    r = live_manager.send_command("Reply with exactly the word PONG and nothing else.")
    d = wait_until(lambda: (x := live_manager.get_result(r["request_id"]))["status"] == tr.DONE and x, timeout=120)
    assert d and "PONG" in d["response"] and "prompt_mismatch" not in d, d
    s = live_manager.get(name)
    live_manager.shutdown()
    assert any(a["sessionId"] == s.session_id for a in _real_agents(live_manager.config.claude_bin))  # 떨어져도 세션은 살아 있음
