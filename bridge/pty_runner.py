"""의사 터미널(PTY)로 claude 를 띄우고 stdin 을 쥐는 계층.

- macOS·Linux: 표준 `pty` 모듈
- Windows: pywinpty (ConPTY)

화면 출력은 기동 단계의 대화상자 처리에만 쓴다. 결과 회수는 transcript(JSONL) 로 한다.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

ESC = "\x1b"
ENTER = "\r"
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

    def send_prompt(self, text: str) -> None:
        """프롬프트 입력 + 제출. 여러 줄이면 bracketed paste 로 넣어 중간 줄바꿈이 제출되지 않게 한다."""
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


class _WinPty:  # pragma: no cover - Windows 전용
    def __init__(self, owner: PtyProcess) -> None:
        from winpty import PtyProcess as WinPtyProcess  # pywinpty

        self._p = WinPtyProcess.spawn(
            owner.argv, cwd=owner.cwd, env=owner.env, dimensions=(owner.rows, owner.cols)
        )
        self.pid = self._p.pid

    def read(self) -> str | None:
        try:
            return self._p.read(65536)
        except EOFError:
            return None

    def write(self, data: str) -> None:
        self._p.write(data)

    def is_alive(self) -> bool:
        return self._p.isalive()

    def terminate(self, timeout: float) -> None:
        self._p.terminate(force=True)
