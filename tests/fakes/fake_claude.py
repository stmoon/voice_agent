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
FAKE_NO_RC=1 RC 미연결, FAKE_AGENTS_FAIL=1 agents 조회 실패.
"""
import json
import os
import re
import select
import subprocess
import sys
import termios
import time
import tty
import uuid
from datetime import datetime, timezone

args = sys.argv[1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


CFG = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
REG = os.path.join(CFG, "fake_agents")
os.makedirs(REG, exist_ok=True)
SID = opt("--session-id") or str(uuid.uuid4())
NAME = opt("--remote-control", "")
SETTINGS = json.load(open(opt("--settings"))) if opt("--settings") else {}
ATTACH = False
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


def reg_load(sid):
    try:
        return json.load(open(reg_path(sid)))
    except (OSError, ValueError):
        return None


def reg_save(entry):
    tmp = reg_path(entry["sessionId"]) + ".tmp"
    json.dump(entry, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, reg_path(entry["sessionId"]))


def set_status(status):
    e = reg_load(SID)
    if e is not None:
        e["status"] = status
        reg_save(e)


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def cmd_agents():
    if os.environ.get("FAKE_AGENTS_FAIL") == "1":
        sys.exit(1)
    out_list = []
    for f in sorted(os.listdir(REG)):
        if not f.endswith(".json"):
            continue
        try:
            e = json.load(open(os.path.join(REG, f)))
        except (OSError, ValueError):
            continue
        if e.get("kind") == "interactive" and e.get("pid") is not None and not alive(e["pid"]):
            continue  # 끝난 대화형 세션
        out_list.append(e)
    print(json.dumps(out_list, ensure_ascii=False))


def cmd_bg():
    name = opt("-n") or opt("--name")
    short = uuid.uuid4().hex[:8]
    sid = short + "-0000-4000-8000-" + uuid.uuid4().hex[:12]
    setup_paths(os.getcwd(), sid)
    rec({"type": "permission-mode", "permissionMode": opt("--permission-mode", "default")})
    if "--remote-control" in args and os.environ.get("FAKE_NO_RC") != "1":
        rec({"type": "system", "subtype": "bridge_status", "url": f"https://claude.ai/code/session_fake{short}"})
    reg_save({"id": short, "cwd": CWD, "kind": "background", "sessionId": sid, "name": name or short,
              "status": "idle", "state": "blocked", "startedAt": int(time.time() * 1000)})
    print(f"backgrounded · {short}" + (f" · {name}" if name else "") + " (idle — send a prompt to start)")


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
    with open(TRANSCRIPT, "a") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def hook(event, **payload):
    for group in SETTINGS.get("hooks", {}).get(event, []):
        for h in group.get("hooks", []):
            data = {"hook_event_name": event, "session_id": SID, "cwd": CWD, **payload}
            subprocess.run(h["command"], shell=True, input=json.dumps(data), text=True)


def out(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def idle_screen():
    set_status("idle")
    footer = "⏸ manual mode on · ← for agents" if ATTACH else "⏸ manual mode on · ? for shortcuts"
    out(f"\r\n\x1b[2m────────\x1b[0m\r\n❯ \r\n\x1b[2m{footer}\x1b[0m\r\n")


def assistant(content, stop):
    mid = "msg_" + uuid.uuid4().hex[:10]
    for i, b in enumerate(content):
        rec({"type": "assistant", "message": {"id": mid, "role": "assistant", "model": "fake",
                                               "content": [b], "stop_reason": stop if i == len(content) - 1 else None}})


def read_key(timeout):
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    return os.read(sys.stdin.fileno(), 4096).decode("utf-8", "replace")


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
    if "THINK" in text:
        if wait_key(["\x1b"], 30, spinner=True):
            out("\r\n")
            return text  # 기록 없음 + 프롬프트 복원
        assistant([{"type": "text", "text": "THOUGHT"}], "end_turn")
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
            rec({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "removed"}]}})
            assistant([{"type": "text", "text": "APPROVED"}], "end_turn")
        else:
            rec({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "denied", "is_error": True}]}})
            rec({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "[Request interrupted by user for tool use]"}]}})
            return
    else:
        assistant([{"type": "thinking", "thinking": ""}, {"type": "text", "text": "ECHO: " + text}], "end_turn")
    rec({"type": "system", "subtype": "turn_duration", "durationMs": 5})
    hook("Stop")
    return ""


def main():
    global ATTACH, NAME
    import signal

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # 종료돼도 finally(등록부 정리) 실행
    signal.signal(signal.SIGHUP, lambda *a: sys.exit(0))
    if args[:2] == ["agents", "--json"]:
        return cmd_agents()
    if "--bg" in args:
        return cmd_bg()
    if args[:1] == ["attach"]:
        e = next((json.load(open(os.path.join(REG, f))) for f in os.listdir(REG)
                  if f.endswith(".json") and json.load(open(os.path.join(REG, f))).get("id") == args[1]), None)
        if e is None:
            print(f"no background session {args[1]}")
            sys.exit(1)
        ATTACH, NAME = True, e["name"]
        os.chdir(e["cwd"])
        setup_paths(e["cwd"], e["sessionId"])
    else:
        setup_paths(os.getcwd(), SID)
    fd = sys.stdin.fileno()
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
                rec({"type": "system", "subtype": "bridge_status", "url": f"https://claude.ai/code/session_fake{SID[:8]}"})
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
            if k == "\x1b":
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
