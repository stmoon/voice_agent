"""세션 관리자: bridge 가 직접 띄운 RC 세션들의 등록·고정·명령 주입·결과 회수.

규칙 (노션 요구사항):
- bridge 는 자신이 띄운 세션에만 주입한다 (임의 터미널 접근 없음)
- 세션 지정은 명시적 select 로만. 고정 없이 send 하면 거부
- 대기 상태일 때만 주입. 작업 중·승인 대기면 거부하고 상태만 알림. 큐잉 없음
- 권한 모드는 default 고정
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import transcript as tr
from .config import BridgeConfig
from .hook_sink import build_settings, read_events
from .pty_runner import PtyProcess, child_env

STARTING = "starting"
EXITED = "exited"
FAILED = "failed"

STATUS_KO = {**tr.STATUS_KO, STARTING: "기동 중", EXITED: "종료됨", FAILED: "기동 실패"}


class BridgeError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


@dataclass
class Request:
    id: str
    session_name: str
    text: str
    offset: int
    sent_at: float


@dataclass
class Session:
    name: str
    cwd: str
    host: str
    session_id: str
    proc: PtyProcess
    events_file: Path
    config_dir: Path | None
    started_at: float = field(default_factory=time.time)
    ready: bool = False
    failure: str | None = None
    warning: str | None = None
    _transcript: Path | None = None
    inflight: Request | None = None  # 주입했지만 아직 transcript 에 프롬프트가 안 보인 요청
    last_inject: float = 0.0
    quiet_window: float = 4.0
    quiet_rate: float = 150.0

    def transcript_path(self) -> Path | None:
        if self._transcript is None or not self._transcript.exists():
            self._transcript = tr.find_transcript(self.session_id, self.config_dir)
        return self._transcript

    def records(self) -> list[dict]:
        return tr.read_jsonl(self.transcript_path())

    def events(self) -> list[dict]:
        return read_events(self.events_file)

    # ---- 상태 판별 ----
    def quiet(self) -> bool:
        """TUI 가 조용한가: 스피너가 안 돌고(출력 거의 없음) transcript 도 그대로이며 방금 주입하지 않았음."""
        now = time.time()
        if now - self.last_inject < self.quiet_window:
            return False
        if self.proc.output_rate(self.quiet_window) >= self.quiet_rate:
            return False
        p = self.transcript_path()
        try:
            if p is not None and now - p.stat().st_mtime < self.quiet_window:
                return False
        except OSError:
            pass
        return True

    def turn_state(self, turn: tr.Turn, events: list[dict], is_last: bool) -> str:
        st = tr.turn_state(turn, events)
        # 생각 중 Esc 중단은 transcript 에 흔적이 없다(실측). 마지막 턴이 끝나지 않았는데 TUI 가 조용하면 중단으로 본다.
        # 단, 결과 없는 tool_use 가 있으면(도구 실행 중이거나 권한 대화상자가 떠 있을 수 있음) 절대 적용하지 않는다.
        if st == tr.WORKING and is_last and not turn.pending_tool_uses() and self.quiet():
            return tr.INTERRUPTED
        return st

    def status(self) -> str:
        if self.failure:
            return FAILED
        if not self.proc.is_alive():
            return EXITED
        if not self.ready:
            return STARTING
        records = self.records()
        if self.inflight is not None:
            if tr.turn_after(records, self.inflight.offset) is None:
                return tr.WORKING
            self.inflight = None
        turns = tr.split_turns(records)
        if not turns:
            return tr.IDLE
        st = self.turn_state(turns[-1], self.events(), is_last=True)
        return tr.IDLE if st in (tr.DONE, tr.INTERRUPTED) else st

    def prompt_box_ready(self) -> bool:
        """안전장치: 화면 맨 뒤가 입력 대기 안내('? for shortcuts')인지. 권한·확인 대화상자가 떠 있으면 False.
        대화상자에 명령+Enter 를 넣으면 기본 선택(승인)이 눌릴 수 있으므로 주입 전에 반드시 확인한다."""
        scr = "".join(self.proc.screen().split()).lower()
        idle_at = scr.rfind("forshortcuts")
        dialog_at = max(scr.rfind(k) for k in _DIALOG_MARKERS)
        return idle_at >= 0 and idle_at > dialog_at

    def info(self) -> dict:
        st = self.status()
        d = {
            "name": self.name,
            "folder": self.cwd,
            "host": self.host,
            "status": st,
            "status_ko": STATUS_KO.get(st, st),
            "session_id": self.session_id,
        }
        url = tr.rc_url(self.records())
        d["rc_url"] = url  # None 이면 RC 미연결 (Code 탭에 안 보임)
        if self.failure:
            d["failure"] = self.failure
        if self.warning:
            d["warning"] = self.warning
        return d


class SessionManager:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._requests: dict[str, Request] = {}
        self._selected: str | None = None
        self._lock = threading.RLock()

    # ---------- 세션 기동 (P3-4) ----------
    def start_session(self, name: str, cwd: str | None = None, wait: bool = True) -> Session:
        """RC 세션 기동. cwd 를 주지 않으면 설정의 프리셋에서 찾는다 (MCP 경로는 프리셋만 허용)."""
        name = name.strip()
        if not name:
            raise BridgeError("bad_name", "세션 이름이 비었습니다.")
        with self._lock:
            old = self._sessions.get(name)
            if old and old.proc.is_alive() and not old.failure:
                raise BridgeError("already_running", f"'{name}' 세션이 이미 실행 중입니다.", session=old.info())
            if cwd is None:
                if name not in self.config.sessions:
                    self.config.reload_sessions()  # voice-bridge add 로 방금 추가된 프리셋 반영
                if name not in self.config.sessions:
                    raise BridgeError("unknown_preset", f"'{name}' 은 등록된 세션 프리셋이 아닙니다.",
                                      presets=sorted(self.config.sessions))
                cwd = self.config.sessions[name]
            cwd = str(Path(cwd).expanduser().resolve())
            if not Path(cwd).is_dir():
                raise BridgeError("no_folder", f"폴더가 없습니다: {cwd}")

            sid = str(uuid.uuid4())
            events_file = self.config.state_dir / f"{sid}.events.jsonl"
            settings_file = self.config.state_dir / f"{sid}.settings.json"
            settings_file.write_text(json.dumps(build_settings(self.config.python, str(events_file))), encoding="utf-8")
            argv = [
                *self.config.claude_bin,
                "--remote-control", name,
                "--session-id", sid,
                "--permission-mode", "default",
                "--settings", str(settings_file),
                *self.config.claude_extra_args,
            ]
            extra_env = {}
            if self.config.claude_config_dir:
                extra_env["CLAUDE_CONFIG_DIR"] = str(self.config.claude_config_dir)
            proc = PtyProcess(argv, cwd=cwd, env=child_env(extra_env))
            s = Session(name=name, cwd=cwd, host=self.config.host_id, session_id=sid, proc=proc,
                        events_file=events_file, config_dir=self.config.claude_config_dir,
                        quiet_window=self.config.quiet_window, quiet_rate=self.config.quiet_rate)
            self._sessions[name] = s
        if wait:
            self.wait_ready(s)
        return s

    def wait_ready(self, s: Session, timeout: float | None = None) -> None:
        """기동 대화상자(온보딩 안내 등)는 Esc 로 닫는다. 폴더 신뢰 확인은 자동 수락하지 않는다."""
        timeout = timeout or self.config.ready_timeout
        end = time.time() + timeout
        mark = s.proc.mark()
        while time.time() < end:
            time.sleep(0.3)
            if not s.proc.is_alive():
                if _is_trust_prompt(s.proc.screen().replace(" ", "").lower()):
                    s.failure = _TRUST_MSG.format(cwd=s.cwd)
                    raise BridgeError("folder_not_trusted", s.failure)
                s.failure = "claude 프로세스가 종료됨: " + _tail(s.proc.screen())
                raise BridgeError("start_failed", s.failure)
            scr = s.proc.screen_since(mark)
            compact = scr.replace(" ", "").lower()
            if "entertoconfirm" in compact:
                if _is_trust_prompt(compact):
                    s.proc.terminate()
                    s.failure = _TRUST_MSG.format(cwd=s.cwd)
                    raise BridgeError("folder_not_trusted", s.failure)
                s.proc.send_escape()
                mark = s.proc.mark()
                continue
            started = any(e.get("hook_event_name") == "SessionStart" for e in s.events())
            if started and "forshortcuts" in compact:
                time.sleep(0.5)
                if "entertoconfirm" in s.proc.screen_since(mark).replace(" ", "").lower():
                    continue
                s.ready = True
                full = s.proc.screen().replace(" ", "").lower()
                if "notloggedin" in full or "loginexpired" in full:
                    s.warning = "claude 로그인이 필요합니다 (해당 머신에서 /login)."
                return
        s.proc.terminate()
        s.failure = "기동 시간 초과: " + _tail(s.proc.screen())
        raise BridgeError("start_timeout", s.failure)

    def stop_session(self, name: str) -> None:
        with self._lock:
            s = self._sessions.pop(name, None)
            if self._selected == name:
                self._selected = None
        if s:
            s.proc.terminate()

    def shutdown(self) -> None:
        for name in list(self._sessions):
            self.stop_session(name)

    # ---------- 목록·고정 (P3-1, P3-2) ----------
    def list_sessions(self) -> list[dict]:
        with self._lock:
            out = []
            for s in self._sessions.values():
                d = s.info()
                d["selected"] = s.name == self._selected
                out.append(d)
            return out

    def get(self, name: str) -> Session:
        s = self._sessions.get(name)
        if s is None:
            raise BridgeError("no_session", f"'{name}' 세션이 없습니다.", sessions=[x.name for x in self._sessions.values()])
        return s

    def select_session(self, name: str) -> dict:
        with self._lock:
            s = self.get(name)
            self._selected = s.name
            return s.info()

    @property
    def selected(self) -> Session:
        if self._selected is None:
            raise BridgeError("no_selection", "고정된 세션이 없습니다. 먼저 list_sessions 로 확인하고 select_session 으로 고정하세요.")
        return self.get(self._selected)

    # ---------- 명령 주입 (P3-3) ----------
    def send_command(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise BridgeError("empty", "명령이 비었습니다.")
        with self._lock:
            s = self.selected
            st = s.status()
            if st != tr.IDLE:
                raise BridgeError("busy", f"'{s.name}' 세션이 {STATUS_KO.get(st, st)} 상태라 명령을 넣지 않았습니다.",
                                  session=s.info())
            if not s.prompt_box_ready():
                raise BridgeError("not_at_prompt", f"'{s.name}' 세션 화면에 확인·권한 대화상자가 떠 있어 명령을 넣지 않았습니다. "
                                  "Code 탭에서 확인해 주세요.", session=s.info())
            records = s.records()
            turns = tr.split_turns(records)
            # 중단된 프롬프트가 입력창에 되돌아와 있을 수 있으니 그 줄 수보다 넉넉히 지운다
            clear_lines = max(8, (turns[-1].prompt.count("\n") + 4) if turns else 0)
            req = Request(id=uuid.uuid4().hex[:12], session_name=s.name, text=text, offset=len(records),
                          sent_at=time.time())
            s.inflight = req
            s.last_inject = req.sent_at
            self._requests[req.id] = req
            s.proc.send_prompt(text, clear_lines=clear_lines)
        return {"ok": True, "request_id": req.id, "session": s.name, "status": tr.WORKING,
                "status_ko": STATUS_KO[tr.WORKING]}

    def get_result(self, request_id: str) -> dict:
        req = self._requests.get(request_id)
        if req is None:
            raise BridgeError("no_request", f"요청 ID {request_id} 를 찾을 수 없습니다.")
        s = self._sessions.get(req.session_name)
        if s is None:
            raise BridgeError("no_session", f"요청의 세션 '{req.session_name}' 이 없습니다.")
        records = s.records()
        turn = tr.turn_after(records, req.offset)
        base = {"ok": True, "request_id": req.id, "session": s.name}
        if turn is None:
            if not s.proc.is_alive():
                raise BridgeError("session_exited", "세션이 종료되어 결과가 없습니다.")
            if time.time() - req.sent_at > self.config.inject_ack_timeout:
                raise BridgeError("inject_unconfirmed", "명령을 넣었지만 transcript 에 기록되지 않았습니다. Code 탭에서 확인해 주세요.")
            return {**base, "status": tr.WORKING, "status_ko": STATUS_KO[tr.WORKING]}
        turns = tr.split_turns(records)
        st = s.turn_state(turn, s.events(), is_last=bool(turns) and turns[-1].prompt_index == turn.prompt_index)
        d = {**base, "status": st, "status_ko": STATUS_KO[st]}
        if " ".join(turn.prompt.split()) != " ".join(req.text.split()):
            d["prompt_mismatch"] = True  # 기록된 프롬프트가 보낸 것과 다름 (입력창 잔여물 등)
            d["recorded_prompt"] = turn.prompt
        if st in (tr.DONE, tr.INTERRUPTED):
            d["response"] = turn.final_text()
            if turn.is_error():
                d["api_error"] = True
        elif st == tr.AWAITING_APPROVAL:
            pend = turn.pending_tool_uses()
            d["pending_tools"] = [p["name"] for p in pend]
            d["message"] = "승인이 필요합니다. Code 탭에서 확인해 주세요."
        return d

    # ---------- 중단 ----------
    def interrupt(self) -> dict:
        with self._lock:
            s = self.selected
            st = s.status()
            if st not in (tr.WORKING, tr.AWAITING_APPROVAL):
                return {"ok": True, "session": s.name, "status": st, "status_ko": STATUS_KO.get(st, st),
                        "message": "진행 중인 작업이 없습니다."}
            s.proc.send_escape()
        return {"ok": True, "session": s.name, "message": "중단 신호(Esc)를 보냈습니다."}


_DIALOG_MARKERS = ("doyouwanttoproceed", "doyouwanttomake", "doyouwanttocreate", "entertoconfirm", "esctocancel")

_TRUST_MSG = "폴더 신뢰가 필요합니다. 해당 폴더에서 claude 를 한 번 직접 실행해 신뢰해 주세요: {cwd}"


def _is_trust_prompt(compact: str) -> bool:
    return any(k in compact for k in ("quicksafetycheck", "trustthisfolder", "oneyoutrust", "doyoutrust"))


def _tail(text: str, n: int = 400) -> str:
    return " ".join(text.split())[-n:]
