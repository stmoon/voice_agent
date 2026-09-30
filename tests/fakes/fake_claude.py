#!/usr/bin/env python3
"""테스트용 가짜 claude CLI.

실제 claude 의 관찰된 동작(PoC, 2.1.285)을 흉내 낸다:
- `--session-id` 로 받은 ID 로 $CLAUDE_CONFIG_DIR/projects/<cwd>/<id>.jsonl 에 기록
- `--settings` 의 훅(command)을 이벤트마다 실행
- 대기 시 화면에 "? for shortcuts", 대화상자는 "Enter to confirm · Esc to cancel"
- 프롬프트 규칙: SLOW → 도구 실행 중(ESC 로 중단), PERM → 권한 요청 후 'y' 입력 대기, 그 외 → 즉시 응답
환경변수 FAKE_DIALOG=1 이면 기동 시 온보딩 대화상자, FAKE_TRUST=1 이면 폴더 신뢰 대화상자.
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


SID = opt("--session-id") or str(uuid.uuid4())
NAME = opt("--remote-control", "")
SETTINGS = json.load(open(opt("--settings"))) if opt("--settings") else {}
CWD = os.getcwd()
CFG = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJ = os.path.join(CFG, "projects", re.sub(r"[^A-Za-z0-9]", "-", CWD))
os.makedirs(PROJ, exist_ok=True)
TRANSCRIPT = os.path.join(PROJ, SID + ".jsonl")
last_uuid = None


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
    out("\r\n\x1b[2m────────\x1b[0m\r\n❯ \r\n\x1b[2m⏸ manual mode on · ? for shortcuts\x1b[0m\r\n")


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


def wait_key(accept, seconds):
    end = time.time() + seconds
    while time.time() < end:
        k = read_key(0.2)
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
    rec({"type": "user", "message": {"role": "user", "content": text}, "promptId": str(uuid.uuid4())})
    hook("UserPromptSubmit", prompt=text)
    if "SLOW" in text:
        tid = "toolu_" + uuid.uuid4().hex[:8]
        assistant([{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "sleep 30"}}], "tool_use")
        out("\r\n⏺ Bash(sleep 30)  esc to interrupt\r\n")
        if wait_key(["\x1b"], 30):
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
        hook("PermissionRequest", tool_name="Bash", tool_input={"command": "rm x"})
        hook("Notification", message="Claude needs your permission to use Bash", notification_type="permission_prompt")
        out("\r\nDo you want to proceed?\r\n❯ 1. Yes\r\n  2. No\r\n\r\nEsc to cancel\r\n")
        k = wait_key(["y", "\x1b"], 60)
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


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        out(f"Claude Code (fake) · RC: {NAME}\r\n")
        if os.environ.get("FAKE_TRUST") == "1":
            if dialog("Quick safety check: Is this a project you created or one you trust?") == "esc":
                return
        rec({"type": "permission-mode", "permissionMode": opt("--permission-mode", "auto")})
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
            buf += k
            if "\r" in buf:
                text, _, buf = buf.partition("\r")
                if text.strip():
                    run_prompt(text.strip())
                idle_screen()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
