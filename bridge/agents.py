"""`claude agents --json` 조회: 이 머신에서 실행 중인 모든 Claude Code 세션과 실시간 상태.

실측(2.1.286):
- kind: "interactive"(터미널·데스크톱 앱·bridge 가 띄운 세션) / "background"(`claude --bg`)
- status: idle(입력 대기) / busy(작업 중 — 승인 뒤 도구 실행 중 포함) / waiting(권한 승인창)
- background 세션은 `id`(짧은 ID)가 있어 `claude attach <id>` 로 붙을 수 있고, 여러 곳에서 동시에 붙어도 된다.
- 조회 시간 약 0.2초.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

from . import transcript as tr

LIVE_STATUS = {"idle": tr.IDLE, "busy": tr.WORKING, "waiting": tr.AWAITING_APPROVAL}


def list_agents(claude_bin: list[str], env: dict | None = None, timeout: float = 10.0) -> list[dict] | None:
    """실행 중인 세션 목록. 조회 실패 시 None (상태 판정은 transcript·훅으로 대체)."""
    try:
        r = subprocess.run([*claude_bin, "agents", "--json"], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return [d for d in data if isinstance(d, dict) and d.get("sessionId")] if isinstance(data, list) else None


class AgentsCache:
    """짧게 캐시해서 상태 조회가 몰려도 claude 를 매번 띄우지 않는다."""

    def __init__(self, claude_bin: list[str], env: dict | None = None, max_age: float = 1.0):
        self.claude_bin = claude_bin
        self.env = env
        self.max_age = max_age
        self._lock = threading.Lock()
        self._at = 0.0
        self._data: list[dict] | None = None

    def get(self, fresh: bool = False) -> list[dict] | None:
        with self._lock:
            if fresh or time.time() - self._at > self.max_age:
                self._data = list_agents(self.claude_bin, self.env)
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
