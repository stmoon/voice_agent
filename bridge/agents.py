"""`claude agents --json` 조회: 이 머신에서 실행 중인 모든 Claude Code 세션과 실시간 상태.

실측(2.1.286):
- kind: "interactive"(터미널·데스크톱 앱·bridge 가 띄운 세션) / "background"(`claude --bg`)
- status: idle(입력 대기) / busy(작업 중 — 승인 뒤 도구 실행 중 포함) / waiting(권한 승인창)
- background 세션은 `id`(짧은 ID)가 있어 `claude attach <id>` 로 붙을 수 있고, 여러 곳에서 동시에 붙어도 된다.
- 조회 시간 약 0.2초.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from . import transcript as tr

LIVE_STATUS = {"idle": tr.IDLE, "busy": tr.WORKING, "waiting": tr.AWAITING_APPROVAL}


MIN_VERSION = (2, 1, 285)   # 실측·검증한 CLI 버전 (--bg·attach·agents --json·--remote-control [name])


def query_agents(claude_bin: list[str], env: dict | None = None,
                 timeout: float = 10.0) -> tuple[list[dict] | None, str | None]:
    """(실행 중인 세션 목록, 실패 이유). 실패하면 목록은 None 이고 이유는 사람이 읽을 문장."""
    from .proc import run_quiet

    try:
        # 창 없이(Windows 작업 스케줄러), claude.cmd 도 실행되게, UTF-8 로 (한국어 Windows 기본 cp949 로 읽으면 깨짐)
        r = run_quiet([*claude_bin, "agents", "--json"], env=env, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, not_found_message(claude_bin)
    except subprocess.TimeoutExpired:
        return None, f"`claude agents --json` 이 {timeout:.0f}초 안에 끝나지 않았습니다."
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return None, f"claude 를 실행하지 못했습니다: {e}"
    if r.returncode != 0:
        err = " ".join((r.stderr or r.stdout or "").split())[-300:]
        if "unknown option" in err and "--json" in err:
            return None, outdated_message(claude_bin)   # 실측: 2.1.131 "error: unknown option '--json'"
        return None, f"`claude agents --json` 실패 (종료 코드 {r.returncode}): {err or '출력 없음'}"
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None, "`claude agents --json` 출력을 해석하지 못했습니다."
    if not isinstance(data, list):
        return None, "`claude agents --json` 출력 형식이 예상과 다릅니다."
    return [d for d in data if isinstance(d, dict) and d.get("sessionId")], None


def list_agents(claude_bin: list[str], env: dict | None = None, timeout: float = 10.0) -> list[dict] | None:
    """실행 중인 세션 목록. 조회 실패 시 None (상태 판정은 transcript·훅으로 대체)."""
    return query_agents(claude_bin, env, timeout)[0]


def not_found_message(claude_bin: list[str]) -> str:
    return (f"claude 실행 파일을 찾을 수 없습니다 ({claude_bin[0]}). Claude Code CLI 를 설치하고 PATH 에 넣거나, "
            "설정 파일의 claude_bin 에 전체 경로를 적어 주세요.")


def outdated_message(claude_bin: list[str], version: str | None = None) -> str:
    need = ".".join(map(str, MIN_VERSION))
    have = f" (현재 {version})" if version else ""
    return (f"claude CLI 가 너무 오래되었습니다{have}. 음성 브리지에는 {need} 이상이 필요합니다 "
            "(--bg·attach·agents --json). `claude update` 로 올려 주세요.")


def parse_version(text: str) -> tuple[int, ...] | None:
    import re

    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def claude_doctor(claude_bin: list[str], env: dict | None = None) -> dict:
    """claude CLI 점검: 실행 파일 위치, 버전, 세션 목록 조회 가능 여부. 문제가 있으면 problem 에 사람이 읽을 문장."""
    import shutil

    from .proc import run_quiet

    path = (env or os.environ).get("PATH")
    exe = claude_bin[0]
    found = shutil.which(exe, path=path) or (exe if os.path.isfile(exe) else None)
    info: dict = {"claude_bin": claude_bin, "path": found, "version": None, "agents_ok": False, "problem": None}
    if not found:
        info["problem"] = not_found_message(claude_bin)
        return info
    try:
        r = run_quiet([*claude_bin, "--version"], env=env, text=True, timeout=30)
        info["version"] = (r.stdout or "").strip().splitlines()[0] if (r.stdout or "").strip() else None
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        info["problem"] = f"claude --version 실행 실패: {e}"
        return info
    ver = parse_version(info["version"] or "")
    if ver is not None and ver < MIN_VERSION:
        info["problem"] = outdated_message(claude_bin, info["version"])
        return info
    agents, err = query_agents(claude_bin, env)
    info["agents_ok"] = agents is not None
    info["problem"] = err
    return info


class AgentsCache:
    """짧게 캐시해서 상태 조회가 몰려도 claude 를 매번 띄우지 않는다. 마지막 조회 실패 이유는 error."""

    def __init__(self, claude_bin: list[str], env: dict | None = None, max_age: float = 1.0):
        self.claude_bin = claude_bin
        self.env = env
        self.max_age = max_age
        self._lock = threading.Lock()
        self._at = 0.0
        self._data: list[dict] | None = None
        self.error: str | None = None

    def get(self, fresh: bool = False) -> list[dict] | None:
        with self._lock:
            if fresh or time.time() - self._at > self.max_age:
                self._data, self.error = query_agents(self.claude_bin, self.env)
                self._at = time.time()
            return self._data

    def by_session(self, session_id: str) -> dict | None:
        for a in self.get() or []:
            if a.get("sessionId") == session_id:
                return a
        return None

    def live_status(self, session_id: str) -> str | None:
        a = self.by_session(session_id)
        return LIVE_STATUS.get(a.get("status")) if a else None
