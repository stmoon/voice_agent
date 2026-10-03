"""의사 터미널(PTY)로 claude 를 띄우고 stdin 을 쥐는 계층.

- macOS·Linux: 표준 `pty` 모듈
- Windows: pywinpty (ConPTY)

화면 출력은 기동 대화상자 처리·승인창 감지(안전장치)·대체 중단 판정에만 쓴다. 결과 회수는 transcript(JSONL) 로 한다.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

ESC = "\x1b"
ENTER = "\r"
CTRL_U = "\x15"      # 커서 앞 줄 지우기
BACKSPACE = "\x7f"
PASTE_BEGIN = "\x1b[200~"
PASTE_END = "\x1b[201~"

# 부모가 Claude Code 세션 안에서 돌 때 물려받으면 자식 transcript 저장이 꺼지는 표식들
_SESSION_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
)

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?<>=]*[a-zA-Z~]"      # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][A-Z0-9]|\x1b[=>]"
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def resolve_argv(argv: list[str], env: dict[str, str] | None = None, platform: str = sys.platform,
                 which=shutil.which) -> list[str]:
    """Windows: 실행 파일을 PATH 에서 찾고, npm 설치본(claude.cmd 같은 배치 파일)은 cmd.exe /c 로 감싼다.
    (ConPTY 는 배치 파일을 직접 띄우지 못한다.) 다른 OS 는 그대로."""
    if platform != "win32":
        return list(argv)
    exe = which(argv[0], path=(env or os.environ).get("PATH")) or argv[0]
    if exe.lower().endswith((".cmd", ".bat")):
        comspec = (env or os.environ).get("COMSPEC", "cmd.exe")
        return [comspec, "/c", exe, *argv[1:]]
    return [exe, *argv[1:]]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for k in _SESSION_MARKERS:
        env.pop(k, None)
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    env.setdefault("TERM", "xterm-256color")
    if extra:
        env.update(extra)
    return env


@dataclass
class PtyProcess:
    """argv 를 PTY 안에서 실행한다. 출력은 백그라운드 스레드가 계속 읽어 tail 버퍼에 보관."""

    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=child_env)
    cols: int = 160
    rows: int = 50
    tail_limit: int = 64 * 1024

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._tail = ""
        self._seq = 0  # 지금까지 받은 문자 수 (since() 용)
        self._activity: deque[tuple[float, int]] = deque(maxlen=4096)  # (시각, 받은 문자 수)
        self.started_at = time.time()
        self._impl = _WinPty(self) if sys.platform == "win32" else _PosixPty(self)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---- 출력 ----
    def _read_loop(self) -> None:
        while True:
            chunk = self._impl.read()
            if chunk is None:
                return
            with self._lock:
                self._tail = (self._tail + chunk)[-self.tail_limit:]
                self._seq += len(chunk)
                self._activity.append((time.time(), len(chunk)))

    def output_rate(self, window: float) -> float:
        """최근 window 초 동안 초당 출력 문자 수. 작업 중엔 스피너가 계속 다시 그려져 수백/초, 대기 중엔 거의 0."""
        cutoff = time.time() - window
        with self._lock:
            n = sum(c for t, c in self._activity if t >= cutoff)
        return n / window

    def mark(self) -> int:
        with self._lock:
            return self._seq

    def screen_since(self, mark: int) -> str:
        """mark 이후 들어온 출력(ANSI 제거). tail 밖으로 밀려난 부분은 잘린다."""
        with self._lock:
            n = min(self._seq - mark, len(self._tail))
            raw = self._tail[len(self._tail) - n:] if n > 0 else ""
        return strip_ansi(raw)

    def screen(self) -> str:
        with self._lock:
            return strip_ansi(self._tail)

    # ---- 입력 ----
    def write(self, data: str) -> None:
        self._impl.write(data)

    def clear_input(self, lines: int = 8) -> None:
        """입력창 비우기. 생각 중 Esc 로 중단하면 claude 가 이전 프롬프트를 입력창에 되돌려 놓으므로,
        그냥 타이핑하면 새 명령이 뒤에 이어 붙는다(실측). (Ctrl+U, Backspace) 반복으로 여러 줄까지 지운다.
        빈 입력에서는 아무 효과 없음(실측). Esc 두 번은 빈 입력에서 되감기 메뉴를 열어 쓰지 않는다."""
        for _ in range(lines):
            self.write(CTRL_U + BACKSPACE)
            time.sleep(0.05)
        self.write(CTRL_U)
        time.sleep(0.2)

    def send_prompt(self, text: str, clear_lines: int = 8) -> None:
        """입력창을 비운 뒤 프롬프트 입력 + 제출. 여러 줄이면 bracketed paste 로 넣어 중간 줄바꿈이 제출되지 않게 한다.
        Windows(ConPTY)는 붙여넣기 시퀀스 전달이 확인되지 않아 줄바꿈을 공백으로 합친다 (중간 Enter 로 제출되는 것 방지)."""
        self.clear_input(clear_lines)
        if "\n" in text and sys.platform == "win32":
            text = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if "\n" in text:
            self.write(PASTE_BEGIN + text + PASTE_END)
        else:
            self.write(text)
        time.sleep(0.3)  # TUI 가 입력을 반영할 틈
        self.write(ENTER)

    def send_escape(self) -> None:
        self.write(ESC)

    # ---- 수명 ----
    @property
    def pid(self) -> int:
        return self._impl.pid

    def is_alive(self) -> bool:
        return self._impl.is_alive()

    def terminate(self, timeout: float = 5.0) -> None:
        self._impl.terminate(timeout)


class _PosixPty:
    def __init__(self, owner: PtyProcess) -> None:
        import fcntl
        import pty
        import struct
        import termios

        import warnings

        with warnings.catch_warnings():
            # 자식은 곧바로 exec 하므로 멀티스레드 fork 경고는 해당 없음
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, fd = pty.fork()
        if pid == 0:  # 자식
            try:
                os.chdir(owner.cwd)
                os.execvpe(owner.argv[0], owner.argv, owner.env)
            finally:
                os._exit(127)
        self.pid = pid
        self.fd = fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", owner.rows, owner.cols, 0, 0))
        self._status: int | None = None

    def read(self) -> str | None:
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return None
        if not data:
            return None
        return data.decode("utf-8", "replace")

    def write(self, data: str) -> None:
        os.write(self.fd, data.encode("utf-8"))

    def is_alive(self) -> bool:
        if self._status is not None:
            return False
        pid, status = os.waitpid(self.pid, os.WNOHANG)
        if pid == 0:
            return True
        self._status = status
        return False

    def terminate(self, timeout: float) -> None:
        import signal

        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not self.is_alive():
                break
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                break
            end = time.time() + timeout
            while time.time() < end and self.is_alive():
                time.sleep(0.05)
        try:
            os.close(self.fd)
        except OSError:
            pass


class _WinPty:  # pragma: no cover - Windows 전용 (CI 의 windows 러너에서 검증)
    """pywinpty(ConPTY). 명령줄은 bridge.proc.pty_command 로 직접 만들어 저수준 PTY.spawn 에 넘긴다
    (PtyProcess.spawn 의 list2cmdline 인용은 cmd.exe 규칙과 달라 claude.cmd·공백 경로에서 깨진다).
    read 는 내부 신호로 빈 문자열을 줄 수 있고 닫히면 EOFError. 실패는 OSError 로 바꿔 올린다."""

    def __init__(self, owner: PtyProcess) -> None:
        from winpty import PTY, PtyProcess as WinPtyProcess  # pywinpty

        from .proc import pty_command

        appname, cmdline = pty_command(owner.argv, owner.env)
        envstr = "\0".join(f"{k}={v}" for k, v in owner.env.items()) + "\0"
        try:
            pty = PTY(owner.cols, owner.rows)
            pty.spawn(appname, cmdline=cmdline, cwd=owner.cwd, env=envstr)
            self._p = WinPtyProcess(pty)
        except Exception as e:
            raise OSError(f"ConPTY 로 실행하지 못했습니다: {appname}{cmdline} ({e})") from e
        self.pid = self._p.pid

    def read(self) -> str | None:
        while True:
            try:
                data = self._p.read(65536)
            except (EOFError, OSError):
                return None
            if data:
                return data
            if not self.is_alive():
                return None
            time.sleep(0.01)

    def write(self, data: str) -> None:
        try:
            self._p.write(data)
        except Exception as e:  # EOFError(닫힘), WinptyError(파이프 오류)
            raise OSError(f"pty write failed: {e}") from e

    def is_alive(self) -> bool:
        try:
            return self._p.isalive()
        except Exception:
            return False

    def terminate(self, timeout: float) -> None:
        try:
            self._p.terminate(force=True)
        except Exception:
            pass
        if self.is_alive() and self.pid:
            from .proc import kill_tree

            kill_tree(self.pid)  # cmd.exe 로 감싼 경우 node 같은 손자 프로세스까지
