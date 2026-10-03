"""Claude Code 세션 transcript(JSONL) 파서.

화면이 아니라 `~/.claude/projects/<cwd>/<session_id>.jsonl` 만으로
세션 상태(대기/작업 중/승인 대기)와 요청별 최종 응답을 판별한다.

승인 대기는 JSONL 만으로는 "도구 실행 중" 과 구분되지 않으므로,
bridge 가 --settings 로 심은 훅(PermissionRequest/Notification)이 남기는
사이드카 이벤트 파일(events.jsonl)을 함께 본다.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

IDLE = "idle"                    # 대기
WORKING = "working"              # 작업 중
AWAITING_APPROVAL = "awaiting_approval"  # 승인 대기

DONE = "done"                    # (요청) 완료
INTERRUPTED = "interrupted"      # (요청) 중단됨

STATUS_KO = {
    IDLE: "대기",
    WORKING: "작업 중",
    AWAITING_APPROVAL: "승인 대기",
    DONE: "완료",
    INTERRUPTED: "중단됨",
}

_END_STOP_REASONS = {"end_turn", "stop_sequence", "max_tokens", "refusal"}
INTERRUPT_PREFIX = "[Request interrupted by user"


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def find_transcript(session_id: str, config_dir: Path | None = None) -> Path | None:
    """세션 ID 로 transcript 파일 위치를 찾는다 (프로젝트 폴더명 규칙에 의존하지 않고 탐색)."""
    root = (config_dir or claude_config_dir()) / "projects"
    if not root.is_dir():
        return None
    hits = sorted(root.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def read_jsonl(path: Path | None) -> list[dict]:
    """완결된 줄만 읽는다. 기록 중인 마지막 줄(개행 없음/깨진 JSON)은 건너뛴다."""
    if path is None or not path.exists():
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.endswith("\n"):
                break
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


class JsonlReader:
    """append 만 되는 transcript 를 이어 읽는다: 지난번 이후 새로 완결된 줄만 파싱.
    긴 세션의 transcript 는 수십 MB 라(실측 38MB, 전체 파싱 0.17초) 상태를 볼 때마다 처음부터 읽으면 느리다.
    다른 파일이 되었거나 크기가 줄었으면(교체·잘림) 처음부터 다시 읽는다. 결과는 read_jsonl 과 같다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ident: tuple | None = None
        self._offset = 0                 # 파싱을 마친 바이트 위치 (항상 줄 끝 다음)
        self._records: list[dict] = []

    def read(self, path: Path | None) -> list[dict]:
        if path is None:
            return []
        try:
            st = path.stat()
        except OSError:
            return []
        ident = (str(path), st.st_dev, st.st_ino)
        with self._lock:
            if ident != self._ident or st.st_size < self._offset:
                self._ident, self._offset, self._records = ident, 0, []
            if st.st_size > self._offset:
                try:
                    with open(path, "rb") as f:
                        f.seek(self._offset)
                        chunk = f.read()
                except OSError:
                    return list(self._records)
                end = chunk.rfind(b"\n")
                if end >= 0:   # 개행 없는 마지막 조각은 아직 기록 중 → 다음에
                    for line in chunk[:end].split(b"\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:  # 깨진 UTF-8 은 대체 문자로, 깨진 JSON 은 건너뜀 (read_jsonl 과 같게)
                            self._records.append(json.loads(line.decode("utf-8", "replace")))
                        except json.JSONDecodeError:
                            continue
                    self._offset += end + 1
            return list(self._records)  # 호출자가 들고 있는 목록이 나중에 바뀌지 않게 복사본


def rc_url_in_file(path: Path | None) -> str | None:
    """보기 전용 세션 목록용 rc_url: 큰 transcript 를 전부 JSON 으로 파싱하지 않고 system 레코드 줄만 골라 판정."""
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    recs = []
    for line in data.split(b"\n")[:-1]:  # 완결된 줄만
        if b'"system"' not in line:
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
    return rc_url(recs)


# ---------- 레코드 분류 ----------

def _content_blocks(rec: dict) -> list[dict]:
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c or [] if isinstance(b, dict)]


def user_text(rec: dict) -> str | None:
    """사용자 '프롬프트' 레코드면 그 텍스트, 아니면 None (tool_result·메타·명령 출력 제외)."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    if (rec.get("origin") or {}).get("kind") not in (None, "human"):
        return None  # 백그라운드 작업 알림(task-notification) 등 사람이 넣은 프롬프트가 아님
    blocks = _content_blocks(rec)
    if not blocks or any(b.get("type") == "tool_result" for b in blocks):
        return None
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if text.lstrip().startswith(("<command-", "<local-command", "<bash-", "<system-reminder", "<task-notification")):
        return None
    return text


def is_interrupt(rec: dict) -> bool:
    t = user_text(rec)
    return t is not None and t.lstrip().startswith(INTERRUPT_PREFIX)


def is_prompt(rec: dict) -> bool:
    t = user_text(rec)
    return t is not None and not t.lstrip().startswith(INTERRUPT_PREFIX)


@dataclass
class Turn:
    """프롬프트 1개 ~ 다음 프롬프트 직전까지."""

    prompt_index: int          # transcript 내 레코드 인덱스
    prompt: str
    prompt_uuid: str | None
    records: list[dict] = field(default_factory=list)  # 프롬프트 이후 레코드
    superseded: bool = False  # 뒤에 다른 프롬프트가 이어짐

    @property
    def interrupted(self) -> bool:
        """명시적 중단 표시가 있거나, 끝나지 않은 채 다음 프롬프트가 들어온 턴.
        (생각 중 Esc 중단은 transcript 에 아무것도 남기지 않는다 — 실측 2.1.285)"""
        if any(is_interrupt(r) for r in self.records):
            return True
        return self.superseded and not self._completed()

    def pending_tool_uses(self) -> list[dict]:
        """결과(tool_result)가 아직 없는 tool_use 레코드들. [{'id','name','timestamp'}]"""
        uses: dict[str, dict] = {}
        done: set[str] = set()
        for r in self.records:
            if r.get("isSidechain"):
                continue
            for b in _content_blocks(r):
                if r.get("type") == "assistant" and b.get("type") == "tool_use":
                    uses[b.get("id")] = {"id": b.get("id"), "name": b.get("name"), "timestamp": r.get("timestamp")}
                elif r.get("type") == "user" and b.get("type") == "tool_result":
                    done.add(b.get("tool_use_id"))
        return [u for k, u in uses.items() if k not in done]

    def ended(self) -> bool:
        return self.interrupted or self._completed()

    def _completed(self) -> bool:
        main = [r for r in self.records if not r.get("isSidechain")]
        if any(r.get("type") == "system" and r.get("subtype") == "turn_duration" for r in main):
            return True
        last_assistant = None
        last_assistant_i = -1
        for i, r in enumerate(main):
            if r.get("type") == "assistant":
                last_assistant, last_assistant_i = r, i
        if last_assistant is None:
            return False
        stop = (last_assistant.get("message") or {}).get("stop_reason")
        if stop not in _END_STOP_REASONS:
            return False
        # 그 뒤로 tool_result 가 이어졌다면 아직 진행 중
        after = main[last_assistant_i + 1:]
        return not any(r.get("type") == "user" and not is_interrupt(r) for r in after) and not self.pending_tool_uses()

    def final_text(self) -> str:
        """턴의 최종 응답: 마지막 tool_result 이후 assistant 텍스트 블록들을 이어 붙임."""
        main = [r for r in self.records if not r.get("isSidechain")]
        start = 0
        for i, r in enumerate(main):
            if r.get("type") == "user" and any(b.get("type") == "tool_result" for b in _content_blocks(r)):
                start = i + 1
        texts: list[str] = []
        for r in main[start:]:
            if r.get("type") != "assistant":
                continue
            for b in _content_blocks(r):
                if b.get("type") == "text" and b.get("text"):
                    texts.append(b["text"])
        return "\n".join(texts).strip()

    def is_error(self) -> bool:
        """API 오류·로그인 만료 등 모델이 아닌 CLI 가 만든(<synthetic>) 응답."""
        return any(r.get("type") == "assistant" and (r.get("isApiErrorMessage")
                   or (r.get("message") or {}).get("model") == "<synthetic>") for r in self.records)


def split_turns(records: list[dict]) -> list[Turn]:
    turns: list[Turn] = []
    for i, r in enumerate(records):
        if is_prompt(r):
            if turns:
                turns[-1].superseded = True
            turns.append(Turn(i, user_text(r) or "", r.get("uuid")))
        elif turns:
            turns[-1].records.append(r)
    return turns


# ---------- 사이드카(훅) 이벤트 ----------

def permission_events(events: Iterable[dict]) -> list[dict]:
    out = []
    for e in events:
        name = e.get("hook_event_name")
        if name == "PermissionRequest":
            out.append(e)
        elif name == "Notification":
            kind = e.get("notification_type")
            if kind == "permission_prompt" or (kind is None and "permission" in e.get("message", "").lower()):
                out.append(e)
    return out


def _ts(s: str | None) -> float:
    from datetime import datetime

    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def turn_state(turn: Turn, events: list[dict] | None = None) -> str:
    if turn.ended():
        return INTERRUPTED if turn.interrupted else DONE
    pending = turn.pending_tool_uses()
    if pending and events:
        oldest = min(_ts(p["timestamp"]) for p in pending)
        for e in permission_events(events):
            # 훅 이벤트 시각(bridge 가 기록한 epoch)이 대기 중인 tool_use 이후면 승인 대기
            if e.get("_ts", 0.0) >= oldest - 1.0:
                return AWAITING_APPROVAL
    return WORKING


def session_status(records: list[dict], events: list[dict] | None = None) -> str:
    turns = split_turns(records)
    if not turns:
        return IDLE
    st = turn_state(turns[-1], events)
    return IDLE if st in (DONE, INTERRUPTED) else st


_RC_OFF = re.compile(r"remote.?control.{0,20}(disconnect|inactive|stopped|ended|off\b)", re.I)


def rc_url(records: list[dict]) -> str | None:
    """Remote Control 이 붙으면 CLI 가 system/bridge_status 레코드에 Code 탭 세션 URL 을 남긴다 (실측 2.1.285).
    그 뒤에 RC 가 끊겼다는 system 레코드가 있으면 None."""
    for r in reversed(records):
        if r.get("type") != "system":
            continue
        if r.get("subtype") == "bridge_status" and r.get("url"):
            return r["url"]
        if _RC_OFF.search(str(r.get("content") or "")):
            return None
    return None


def _norm(t: str) -> str:
    return " ".join(t.split())


def turn_after(records: list[dict], offset: int, text: str | None = None) -> Turn | None:
    """offset(레코드 개수) 이후의 턴 = bridge 가 주입한 요청의 턴.
    text 를 주면 그 프롬프트와 일치하는 첫 턴을 우선하고, 없으면 offset 이후 첫 턴."""
    later = [t for t in split_turns(records) if t.prompt_index >= offset]
    if text is not None:
        for t in later:
            if _norm(t.prompt) == _norm(text):
                return t
    return later[0] if later else None
