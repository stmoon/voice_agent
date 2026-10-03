#!/usr/bin/env python3
"""테스트용 가짜 claude CLI.

실제 claude 의 관찰된 동작(PoC, 2.1.285)을 흉내 낸다:
- `--session-id` 로 받은 ID 로 $CLAUDE_CONFIG_DIR/projects/<cwd>/<id>.jsonl 에 기록
- `--settings` 의 훅(command)을 이벤트마다 실행
- 대기 시 화면에 "? for shortcuts", 대화상자는 "Enter to confirm · Esc to cancel"
- 작업 중엔 스피너를 계속 다시 그린다(출력 수백 자/초), 대기 중엔 출력 없음
- 프롬프트 규칙:
    SLOW  → 도구 실행 중 (Esc: tool_result 오류 + "[Request interrupted by user]" 기록)
    THINK → 생각 중 (Esc: transcript 에 아무것도 안 남기고 프롬프트를 입력창에 되돌림 — 실측 2.1.285 동작)
    PERM  → 권한 요청 후 'y' 입력 대기 (대화상자는 정적: 출력 없음)
    그 외 → 즉시 응답 "ECHO: <프롬프트>"
- 입력창: Ctrl+U = 현재 줄 지우기, Backspace = 한 글자 지우기, bracketed paste 의 줄바꿈은 제출 아님
- `agents --json`: 실행 중 세션 목록 (등록부 $CLAUDE_CONFIG_DIR/fake_agents/*.json). status = idle/busy/waiting
- `--bg -n 이름 --remote-control 이름`: 백그라운드 세션 등록 후 바로 종료 (transcript 에 권한 모드·RC 기록)
- `attach <id>`: 백그라운드 세션에 붙은 화면. 대기 표시는 "⏸ manual mode on · ← for agents" (실측)
환경변수 FAKE_DIALOG=1 기동 시 온보딩 대화상자, FAKE_TRUST=1 폴더 신뢰 대화상자, FAKE_NO_PERM_HOOKS=1 권한 훅 미발생,
FAKE_NO_RC=1 RC 미연결, FAKE_AGENTS_FAIL=1 agents 조회 실패, FAKE_OLD_CLI=1 오래된 CLI(2.1.131: agents --json 없음),
FAKE_LOGIN_INVALID=1 로그인 무효 (RC 연결 실패 기록 + 모든 프롬프트가 401 합성 응답 — 실측 2.1.286).
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid

WINDOWS = os.name == "nt"
if WINDOWS:
    import ctypes
    from ctypes import wintypes
else:
    import select
    import termios
    import tty
from datetime import datetime, timezone

args = sys.argv[1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


CFG = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
REG = os.path.join(CFG, "fake_agents")
os.makedirs(REG, exist_ok=True)
SID = opt("--session-id") or str(uuid.uuid4())
NAME = opt("--remote-control", "")
SETTINGS = json.load(open(opt("--settings"), encoding="utf-8")) if opt("--settings") else {}
ATTACH = False
MODE = opt("--permission-mode", "auto")
MODE_TEXT = {"default": "⏸ manual mode on", "auto": "⏵⏵ auto mode on", "plan": "⏸ plan mode on",
             "acceptEdits": "⏵⏵ accept edits on", "bypassPermissions": "⏵⏵ bypass permissions on"}
CYCLE = ["default", "auto", "acceptEdits"]
CWD = TRANSCRIPT = None
last_uuid = None


def setup_paths(cwd, sid):
    global CWD, TRANSCRIPT, SID
    CWD, SID = cwd, sid
    proj = os.path.join(CFG, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
    os.makedirs(proj, exist_ok=True)
    TRANSCRIPT = os.path.join(proj, sid + ".jsonl")


# ---------- 등록부 (claude agents 흉내) ----------

def reg_path(sid):
    return os.path.join(REG, sid + ".json")


def _retry(fn, tries=20):
    """Windows 는 다른 프로세스가 연 파일을 바꾸거나 읽을 때 PermissionError 가 난다 → 잠깐 재시도."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.02)


def _read_json(path):
    def go():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return _retry(go)


def reg_load(sid):
    try:
        return _read_json(reg_path(sid))
    except (OSError, ValueError):
        return None


def reg_save(entry):
    tmp = reg_path(entry["sessionId"]) + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False)
    _retry(lambda: os.replace(tmp, reg_path(entry["sessionId"])))


def set_status(status):
    e = reg_load(SID)
    if e is not None:
        e["status"] = status
        reg_save(e)


def alive(pid):
    """Windows 의 os.kill(pid, 0) 은 확인이 아니라 그 프로세스를 강제 종료한다 → ctypes 로 확인."""
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    if WINDOWS:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_agents():
    if os.environ.get("FAKE_AGENTS_FAIL") == "1":
        sys.exit(1)
    out_list = []
    for f in sorted(os.listdir(REG)):
        if not f.endswith(".json"):
            continue
        try:
            e = _read_json(os.path.join(REG, f))
        except (OSError, ValueError):
            continue
        if e.get("kind") == "interactive" and e.get("pid") is not None and not alive(e["pid"]):
            continue  # 끝난 대화형 세션
        out_list.append(e)
    sys.stdout.buffer.write(json.dumps(out_list, ensure_ascii=False).encode("utf-8"))  # node 처럼 항상 UTF-8


def cmd_bg():
    name = opt("-n") or opt("--name")
    short = uuid.uuid4().hex[:8]
    sid = short + "-0000-4000-8000-" + uuid.uuid4().hex[:12]
    setup_paths(os.getcwd(), sid)
    rec({"type": "permission-mode", "permissionMode": opt("--permission-mode", "default")})
    if "--remote-control" in args and os.environ.get("FAKE_NO_RC") != "1":
        rc_record(short)
    reg_save({"id": short, "cwd": CWD, "kind": "background", "sessionId": sid, "name": name or short,
              "status": "idle", "state": "blocked", "startedAt": int(time.time() * 1000),
              "mode": opt("--permission-mode", "default")})
    print(f"backgrounded · {short}" + (f" · {name}" if name else "") + " (idle — send a prompt to start)")


LOGIN_INVALID = os.environ.get("FAKE_LOGIN_INVALID") == "1"


def rc_record(short):
    """RC 연결 결과 기록. 로그인이 무효면 연결 실패 경고만 남는다 (실측 2.1.286, Windows)."""
    if LOGIN_INVALID:
        rec({"type": "system", "subtype": "informational", "level": "warning",
             "content": "Remote Control disconnected — /login"})
    else:
        rec({"type": "system", "subtype": "bridge_status", "url": f"https://claude.ai/code/session_fake{short}"})


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def rec(d):
    global last_uuid
    d.setdefault("uuid", str(uuid.uuid4()))
    d.setdefault("parentUuid", last_uuid)
    d.setdefault("isSidechain", False)
    d.setdefault("timestamp", now())
    d.setdefault("sessionId", SID)
    d.setdefault("cwd", CWD)
    last_uuid = d["uuid"]
    with open(TRANSCRIPT, "a", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def hook(event, **payload):
    for group in SETTINGS.get("hooks", {}).get(event, []):
        for h in group.get("hooks", []):
            data = {"hook_event_name": event, "session_id": SID, "cwd": CWD, **payload}
            # 실제 claude 처럼 UTF-8 바이트로 (한글 그대로) 보낸다
            subprocess.run(h["command"], shell=True, input=json.dumps(data, ensure_ascii=False).encode("utf-8"))


def out(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def idle_screen():
    set_status("idle")
    mode = MODE_TEXT.get(MODE, MODE)
    # 실측: '? for shortcuts' 는 default 모드에서만, 그 밖엔 '(shift+tab to cycle)'. attach 화면의 비default 표시는 추정
    if ATTACH:
        footer = f"{mode} · ← for agents"
    else:
        footer = f"{mode} · ? for shortcuts" if MODE == "default" else f"{mode} (shift+tab to cycle)"
    out(f"\r\n\x1b[2m────────\x1b[0m\r\n❯ \r\n\x1b[2m{footer}\x1b[0m\r\n")


def assistant(content, stop):
    mid = "msg_" + uuid.uuid4().hex[:10]
    for i, b in enumerate(content):
        rec({"type": "assistant", "message": {"id": mid, "role": "assistant", "model": "fake",
                                               "content": [b], "stop_reason": stop if i == len(content) - 1 else None}})


def read_key(timeout):
    if WINDOWS:
        return _win_read_key(timeout)
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    return os.read(sys.stdin.fileno(), 4096).decode("utf-8", "replace")


# ---------- Windows 콘솔 입력 ----------
# ConPTY 입력은 콘솔 키 이벤트(INPUT_RECORD)로 들어온다. 실제 claude(node)처럼 ReadConsoleInputW 로 직접 읽는다.
# msvcrt.kbhit/getwch 는 DBCS 코드 페이지(한국어 Windows 949)에서 한글 키를 읽은 뒤 남은 키 해제 이벤트가
# 2바이트로 쪼개져 보여, 뒤따르는 Enter 를 놓친다 (실측: 다른 키가 더 들어올 때까지 Enter 가 안 읽힘).

if WINDOWS:
    class _KeyEvent(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                    ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR),
                    ("dwControlKeyState", wintypes.DWORD)]

    class _EventUnion(ctypes.Union):
        _fields_ = [("KeyEvent", _KeyEvent), ("_pad", ctypes.c_byte * 16)]

    class _InputRecord(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _EventUnion)]

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.GetStdHandle.restype = wintypes.HANDLE
    _K32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _K32.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_InputRecord), wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD)]
    _K32.GetNumberOfConsoleInputEvents.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _STDIN = _K32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    _KEY_EVENT, _VK_TAB, _VK_BACK, _SHIFT = 0x0001, 0x09, 0x08, 0x0010


def _win_key(vk, ch, ctrl_state):
    """키 이벤트 → 터미널 바이트: Backspace → \\x7f, Shift+Tab → \\x1b[Z. 나머지는 입력된 문자 그대로."""
    if vk == _VK_TAB and ctrl_state & _SHIFT:
        return "\x1b[Z"
    if vk == _VK_BACK:
        return "\x7f"
    return ch if ch != "\x00" else ""


def _win_drain():
    """지금 쌓인 콘솔 입력 이벤트를 모두 읽어 키 입력 문자열로."""
    n = wintypes.DWORD()
    if not _K32.GetNumberOfConsoleInputEvents(_STDIN, ctypes.byref(n)) or not n.value:
        return ""
    buf = (_InputRecord * n.value)()
    got = wintypes.DWORD()
    if not _K32.ReadConsoleInputW(_STDIN, buf, n.value, ctypes.byref(got)):
        return ""
    keys = []
    for r in buf[: got.value]:
        if r.EventType != _KEY_EVENT or not r.Event.KeyEvent.bKeyDown:
            continue
        ke = r.Event.KeyEvent
        keys.append(_win_key(ke.wVirtualKeyCode, ke.UnicodeChar, ke.dwControlKeyState) * max(1, ke.wRepeatCount))
    # UTF-16 서로게이트 쌍(이모지 등)은 이벤트 2개로 온다 → 합친다
    return "".join(keys).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _win_read_key(timeout):
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return None
        if _K32.WaitForSingleObject(_STDIN, max(1, int(min(left, 0.05) * 1000))) != 0:  # WAIT_OBJECT_0
            continue
        keys = _win_drain()
        if keys:
            return keys


SPIN = "✻✶✳✢·"


def wait_key(accept, seconds, spinner=False):
    end = time.time() + seconds
    i = 0
    while time.time() < end:
        if spinner:
            i += 1
            out(f"\r\x1b[2K{SPIN[i % len(SPIN)]} Thinking… ({i // 10}s · esc to interrupt)")
        k = read_key(0.1 if spinner else 0.2)
        if k is None:
            continue
        for ch in accept:
            if ch in k:
                return ch
    return None


def dialog(text):
    out(f"\r\n{text}\r\n❯ 1. Yes\r\n  2. No\r\n\r\nEnter to confirm · Esc to cancel\r\n")
    while True:
        k = read_key(60)
        if k is None:
            sys.exit(1)
        if k == "\x1b":
            return "esc"
        if "\r" in k:
            return "enter"


def run_prompt(text):
    """반환값: 입력창에 되돌려 놓을 텍스트 (THINK 중단 시)."""
    rec({"type": "user", "message": {"role": "user", "content": text}, "promptId": str(uuid.uuid4())})
    set_status("busy")
    hook("UserPromptSubmit", prompt=text)
    if LOGIN_INVALID:  # 실측 2.1.286: API 가 401 로 거부 → CLI 가 만든 합성 응답
        rec({"type": "assistant", "isApiErrorMessage": True, "error": "authentication_failed",
             "message": {"id": "msg_" + uuid.uuid4().hex[:10], "role": "assistant", "model": "<synthetic>",
                         "stop_reason": "stop_sequence", "content": [
                             {"type": "text", "text": "Please run /login · API Error: 401 OAuth access token is invalid."}]}})
    elif "THINK" in text:
        if wait_key(["\x1b"], 30, spinner=True):
            out("\r\n")
            return text  # 기록 없음 + 프롬프트 복원
        assistant([{"type": "text", "text": "THOUGHT"}], "end_turn")
    elif "PERM" in text:
        tid = "toolu_" + uuid.uuid4().hex[:8]
        assistant([{"type": "text", "text": "파일을 지우겠습니다."},
                   {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "rm x"}}], "tool_use")
        if os.environ.get("FAKE_NO_PERM_HOOKS") != "1":
            hook("PermissionRequest", tool_name="Bash", tool_input={"command": "rm x"})
            hook("Notification", message="Claude needs your permission to use Bash",
                 notification_type="permission_prompt")
        out("\r\nDo you want to proceed?\r\n❯ 1. Yes\r\n  2. No\r\n\r\nEsc to cancel\r\n")
        set_status("waiting")
        k = wait_key(["y", "\x1b"], 60)
        set_status("busy")
        if k == "y":
            if "SLOW" in text:  # 승인 뒤 도구가 한동안 돈다 (실측: 그동안 claude agents 는 busy)
                out("\r\n⏺ Bash(rm x)  esc to interrupt\r\n")
                wait_key(["\x1b"], 4, spinner=True)
            rec({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "removed"}]}})
            assistant([{"type": "text", "text": "APPROVED"}], "end_turn")
        else:
            rec({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "denied", "is_error": True}]}})
            rec({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "[Request interrupted by user for tool use]"}]}})
            return
    elif "PAUSE" in text:  # 4초 동안 일한 뒤 응답 (기다림 테스트용)
        wait_key(["\x1b"], 4, spinner=True)
        assistant([{"type": "text", "text": "PAUSED DONE"}], "end_turn")
    elif "SLOW" in text:
        tid = "toolu_" + uuid.uuid4().hex[:8]
        assistant([{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "sleep 30"}}], "tool_use")
        out("\r\n⏺ Bash(sleep 30)  esc to interrupt\r\n")
        if wait_key(["\x1b"], 30, spinner=True):
            rec({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "interrupted", "is_error": True}]}})
            rec({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "[Request interrupted by user]"}]}})
            return
        rec({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
        assistant([{"type": "text", "text": "SLOW DONE"}], "end_turn")
    else:
        assistant([{"type": "thinking", "thinking": ""}, {"type": "text", "text": "ECHO: " + text}], "end_turn")
    rec({"type": "system", "subtype": "turn_duration", "durationMs": 5})
    hook("Stop")
    return ""


def main():
    global ATTACH, NAME, MODE
    import signal

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # 종료돼도 finally(등록부 정리) 실행
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *a: sys.exit(0))
    if args[:1] == ["--version"]:
        print("2.1.131 (Claude Code)" if os.environ.get("FAKE_OLD_CLI") == "1" else "2.1.286 (Claude Code)")
        return
    if args[:2] == ["agents", "--json"]:
        if os.environ.get("FAKE_OLD_CLI") == "1":  # 실측 2.1.131: agents 에 --json 이 없다
            sys.stderr.write("error: unknown option '--json'\n")
            sys.exit(1)
        return cmd_agents()
    if "--bg" in args:
        return cmd_bg()
    if args[:1] == ["stop"]:   # 백그라운드 세션 멈춤 = 목록에서 빠짐
        for f in os.listdir(REG):
            if f.endswith(".json") and _read_json(os.path.join(REG, f)).get("id") == args[1]:
                os.remove(os.path.join(REG, f))
                print(f"stopped {args[1]}")
                return
        print(f"no session {args[1]}")
        sys.exit(1)
    if args[:1] == ["attach"]:
        e = next((x for x in (_read_json(os.path.join(REG, f)) for f in os.listdir(REG) if f.endswith(".json"))
                  if x.get("id") == args[1]), None)
        if e is None:
            print(f"no background session {args[1]}")
            sys.exit(1)
        ATTACH, NAME, MODE = True, e["name"], e.get("mode", "default")
        os.chdir(e["cwd"])
        setup_paths(e["cwd"], e["sessionId"])
    else:
        setup_paths(os.getcwd(), SID)
    fd = sys.stdin.fileno()
    old = None
    if not WINDOWS:
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
    try:
        out(f"Claude Code (fake) · RC: {NAME}\r\n")
        if not ATTACH:
            if os.environ.get("FAKE_TRUST") == "1":
                if dialog("Quick safety check: Is this a project you created or one you trust?") == "esc":
                    return
            reg_save({"pid": os.getpid(), "cwd": CWD, "kind": "interactive", "sessionId": SID, "name": NAME or SID[:8],
                      "status": "idle", "startedAt": int(time.time() * 1000)})
            rec({"type": "permission-mode", "permissionMode": opt("--permission-mode", "auto")})
            if NAME and os.environ.get("FAKE_NO_RC") != "1":
                rc_record(SID[:8])
            hook("SessionStart", source="startup")
            if os.environ.get("FAKE_DIALOG") == "1":
                dialog("Try the new fullscreen renderer?")
        idle_screen()
        buf = ""
        while True:
            k = read_key(1.0)
            if k is None:
                continue
            k = k.replace("\x1b[200~", "").replace("\x1b[201~", "")
            while "\x1b[Z" in k:  # Shift+Tab: 권한 모드 순환. transcript 엔 바로 기록하지 않음 (실측: 늦게 기록)
                k = k.replace("\x1b[Z", "", 1)
                MODE = CYCLE[(CYCLE.index(MODE) + 1) % len(CYCLE)] if MODE in CYCLE else "default"
                e = reg_load(SID)
                if e is not None:
                    e["mode"] = MODE
                    reg_save(e)
                idle_screen()
            if k == "\x1b" or not k:
                continue
            for ch in k:
                if ch == "\x15":            # Ctrl+U: 현재 줄(마지막 줄바꿈 뒤) 지우기
                    buf = buf[: buf.rfind("\n") + 1] if "\n" in buf else ""
                elif ch == "\x7f":          # Backspace
                    buf = buf[:-1]
                elif ch == "\r":
                    text, buf = buf, ""
                    if text.strip():
                        buf = run_prompt(text.strip())
                    idle_screen()
                    if buf:
                        out(f"❯ {buf}")
                else:
                    buf += ch
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if ATTACH:
            set_status("idle")      # 떨어져도 백그라운드 세션은 남는다
        else:
            try:
                os.remove(reg_path(SID))
            except OSError:
                pass


if __name__ == "__main__":
    main()
