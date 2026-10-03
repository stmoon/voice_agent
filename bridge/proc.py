"""하위 프로세스 실행의 OS 차이를 한곳에 모은다 (주로 Windows).

- 창 숨김: 작업 스케줄러(pythonw, 콘솔 없음)에서 콘솔 프로그램을 띄우면 매번 콘솔 창이 뜬다 → CREATE_NO_WINDOW
- 배치 파일: npm 설치본 claude.cmd 는 cmd.exe 로만 실행된다. list2cmdline 인용은 cmd 규칙과 달라
  경로 공백·& 에서 깨지므로 `cmd.exe /d /s /c "<각 인자를 따옴표로 감싼 전체>"` 를 직접 만든다
- 인코딩: node(claude)·cloudflared 출력은 UTF-8. 한국어 Windows 기본(cp949)으로 읽으면 깨진다
- 정리: Windows 의 종료는 단일 pid TerminateProcess 라 손자 프로세스가 남는다 → taskkill /T, Job Object
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _q(arg: str) -> str:
    """cmd·MSVC 인자 모두에 안전한 큰따옴표 인용. 안쪽 큰따옴표는 \\" (node 의 argv 해석 규칙)."""
    return '"' + str(arg).replace('"', '\\"') + '"'


def short_path(path: str) -> str:
    """공백이 든 경로를 8.3 짧은 경로로 (실행 파일 경로를 따옴표 없이 넘기는 하위 계층 대비). 실패하면 그대로."""
    if not WINDOWS or " " not in path:
        return path
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
    return buf.value if 0 < n < len(buf) else path


def is_batch(exe: str) -> bool:
    return exe.lower().endswith((".cmd", ".bat"))


def which(cmd: str, env: dict | None = None) -> str:
    return shutil.which(cmd, path=(env or os.environ).get("PATH")) or cmd


def pty_command(argv: list[str], env: dict | None = None) -> tuple[str, str]:
    """ConPTY 로 띄울 (실행 파일, 인자 명령줄). 배치 파일은 cmd.exe /d /s /c 로 감싼다."""
    exe = which(argv[0], env)
    if is_batch(exe):
        comspec = (env or os.environ).get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        inner = " ".join(_q(a) for a in [exe, *argv[1:]])
        return comspec, f' /d /s /c "{inner}"'
    return short_path(exe), " " + subprocess.list2cmdline(argv[1:])


def command_line(argv: list[str], env: dict | None = None) -> str:
    """subprocess 에 그대로 넘길 Windows 명령줄 문자열."""
    exe = which(argv[0], env)
    if is_batch(exe):
        comspec = (env or os.environ).get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        inner = " ".join(_q(a) for a in [exe, *argv[1:]])
        return f'{_q(comspec)} /d /s /c "{inner}"'
    return subprocess.list2cmdline([exe, *argv[1:]])


def run_quiet(argv: list[str], env: dict | None = None, **kw) -> subprocess.CompletedProcess:
    """창 없이, 배치 파일도 실행되게, UTF-8 로 읽는 subprocess.run."""
    kw.setdefault("capture_output", True)
    if kw.get("text") or "encoding" in kw:
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    if WINDOWS:
        kw["creationflags"] = kw.get("creationflags", 0) | NO_WINDOW
        return subprocess.run(command_line(argv, env), env=env, **kw)
    return subprocess.run(argv, env=env, **kw)


def kill_tree(pid: int) -> None:
    """프로세스와 그 자식들까지 종료 (Windows: taskkill /T /F)."""
    if WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
    else:
        import signal

        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class KillOnCloseJob:
    """Windows Job Object(KILL_ON_JOB_CLOSE): 이 프로세스가 어떤 식으로 끝나든(작업 스케줄러 /End 의 강제 종료 포함)
    등록한 자식 프로세스도 함께 끝난다. 다른 OS 에선 아무것도 하지 않는다."""

    def __init__(self) -> None:
        self.handle = None
        if not WINDOWS:
            return
        import ctypes
        from ctypes import wintypes

        k = ctypes.windll.kernel32

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                          "OtherOperationCount", "ReadTransferCount",
                                                          "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k.CreateJobObjectW.restype = wintypes.HANDLE
        job = k.CreateJobObjectW(None, None)
        if not job:
            return
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # ExtendedLimitInformation
            self.handle = job

    def add(self, popen: subprocess.Popen) -> bool:
        handle = getattr(popen, "_handle", None)  # 진짜 Popen 만 프로세스 핸들이 있다
        if not self.handle or handle is None:
            return False
        import ctypes

        return bool(ctypes.windll.kernel32.AssignProcessToJobObject(self.handle, int(handle)))
