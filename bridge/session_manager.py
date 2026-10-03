"""세션 관리자: RC 세션들의 목록·고정·명령 주입·결과 회수.

세션 종류
- bridge     : bridge 가 직접 띄운 세션 (PTY 의 stdin 을 쥠)
- background : 사용자가 `claude --bg --remote-control` 로 띄운 백그라운드 세션. `claude attach <id>` 를
               bridge 의 PTY 로 열어 입력한다 (여러 곳에서 동시에 붙어도 됨, 떨어져도 세션은 계속 돔)
- desktop    : 데스크톱 앱·다른 터미널의 대화형 세션. 입력 통로가 없어 **보기 전용** (목록에만 표시)

규칙 (노션 요구사항)
- 주입 대상은 bridge 가 띄운 세션, 또는 RC 가 켜진 백그라운드 세션뿐이고, 어느 쪽이든 권한 모드가 허용 목록
  (기본: default·auto)에 있을 때만. 권한 우회(bypassPermissions)는 어떤 설정으로도 허용하지 않음
  (데스크톱 앱 세션·임의 터미널에는 주입하지 않음)
- 세션 지정은 명시적 select 로만. 고정 없이 send 하면 거부
- 대기 상태일 때만 주입. 작업 중·승인 대기면 거부하고 상태만 알림. 큐잉 없음
- 실시간 상태는 `claude agents` (idle/busy/waiting) 를 우선 쓰고, 결과는 transcript 에서 읽는다
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import transcript as tr
from .agents import AgentsCache
from .config import BridgeConfig
from .hook_sink import build_settings, read_events
from .pty_runner import PtyProcess, child_env

STARTING = "starting"
EXITED = "exited"
FAILED = "failed"

STATUS_KO = {**tr.STATUS_KO, STARTING: "기동 중", EXITED: "종료됨", FAILED: "기동 실패"}

BRIDGE, BACKGROUND, DESKTOP = "bridge", "background", "desktop"
KIND_KO = {BRIDGE: "브리지 세션", BACKGROUND: "백그라운드 세션", DESKTOP: "데스크톱 앱·터미널 세션"}

INJECT_GRACE = 2.0      # 주입 직후 이 시간 동안은 실시간 상태가 idle 이어도 작업 중으로 본다
SETTLE = 2.0            # 끝나지 않은 턴이 idle 이 되면 transcript 가 이만큼 조용해진 뒤에 '중단됨'으로 본다


class BridgeError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


BG_LAUNCH = 'claude --bg -n "이름" --remote-control "이름"'


def launch_hint(allowed_modes) -> str:
    """음성용 백그라운드 세션을 띄우는 명령. 이 머신의 CLI 기본 모드(auto)가 허용 목록에 없으면 모드를 명시한다."""
    allowed = list(allowed_modes)
    return BG_LAUNCH if "auto" in allowed else f"{BG_LAUNCH} --permission-mode {allowed[0]}"


MODE_KO = {"default": "승인 필요", "auto": "자동(auto)", "acceptEdits": "편집 자동 승인", "plan": "계획",
           "bypassPermissions": "권한 우회"}


def sanitize_command(text: str) -> str:
    """음성 경로 명령 정리 (보안).
    - 제어문자 제거: ESC 시퀀스로 권한 모드 순환(Shift+Tab=\\x1b[Z)·붙여넣기 탈출·조기 제출(\\r)을 막는다
    - '!' 로 시작하면 거부: Claude Code bash 모드라 권한 확인 없이 셸 명령이 실행된다
    - '/' 로 시작하면 거부: 슬래시 명령은 세션 설정을 바꿀 수 있고, 프롬프트 기록을 남기지 않아 결과를 추적할 수 없다"""
    cleaned = "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and not 0x7F <= ord(ch) <= 0x9F))
    cleaned = cleaned.strip()
    if not cleaned:
        raise BridgeError("empty", "명령이 비었습니다.")
    if cleaned.startswith("!"):
        raise BridgeError("shell_mode_blocked", "'!' 로 시작하는 명령은 권한 확인 없이 셸에서 바로 실행되므로 음성 경로에서는 받지 않습니다. "
                          "'○○ 명령을 실행해줘' 처럼 말로 요청해 주세요.")
    if cleaned.startswith("/"):
        raise BridgeError("slash_command_blocked", "'/' 로 시작하는 슬래시 명령은 음성 경로에서 받지 않습니다 (세션 설정 변경 가능, 결과 추적 불가). "
                          "필요하면 Code 탭에서 직접 입력해 주세요.")
    return cleaned


@dataclass
class Request:
    id: str
    session_id: str
    session_name: str
    text: str
    offset: int
    sent_at: float
    last_reported: str = tr.WORKING   # 마지막으로 돌려준 상태 (승인 대기를 한 번만 알리기 위해)


@dataclass
class Session:
    name: str
    cwd: str
    host: str
    session_id: str
    proc: PtyProcess | None
    events_file: Path | None
    config_dir: Path | None
    kind: str = BRIDGE
    agent_id: str | None = None          # background: `claude attach` 에 쓰는 짧은 ID
    allowed_modes: tuple = ("default", "auto")
    live: AgentsCache | None = None
    started_at: float = field(default_factory=time.time)
    ready: bool = False
    failure: str | None = None
    warning: str | None = None
    _transcript: Path | None = None
    inflight: Request | None = None  # 주입했지만 아직 transcript 에 프롬프트가 안 보인 요청
    last_inject: float = 0.0
    inflight_timeout: float = 20.0
    quiet_window: float = 4.0
    quiet_rate: float = 150.0

    def transcript_path(self) -> Path | None:
        if self._transcript is None or not self._transcript.exists():
            self._transcript = tr.find_transcript(self.session_id, self.config_dir)
        return self._transcript

    def records(self) -> list[dict]:
        return tr.read_jsonl(self.transcript_path())

    def events(self) -> list[dict]:
        return read_events(self.events_file) if self.events_file else []

    def attached(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def live_status(self) -> str | None:
        return self.live.live_status(self.session_id) if self.live else None

    def vanished(self) -> bool:
        """claude agents 목록에서 사라졌는가 (조회 실패면 판단하지 않음 = False). 스냅샷 한 번으로 판단."""
        if self.live is None:
            return False
        snap = self.live.get()
        return snap is not None and not any(a.get("sessionId") == self.session_id for a in snap)

    def permission_mode(self, records: list[dict] | None = None) -> str | None:
        """transcript 상 권한 모드: permission-mode 레코드와 사용자 프롬프트 레코드의 permissionMode 중 가장 최근 값.
        주의: CLI 는 모드 변경을 늦게 기록한다(실측) → 주입 직전엔 screen_mode() 도 함께 본다."""
        for r in reversed(records if records is not None else self.records()):
            if r.get("type") == "permission-mode":
                return r.get("permissionMode")
            if r.get("type") == "user" and isinstance(r.get("permissionMode"), str):
                return r["permissionMode"]
        return None

    def screen_mode(self) -> str | None:
        """화면 하단에 마지막으로 그려진 모드 표시 (지금 실제 모드). 표시가 없으면 None."""
        if self.proc is None:
            return None
        scr = "".join(self.proc.screen().split()).lower()
        best, mode = -1, None
        for marker, m in _MODE_MARKERS.items():
            at = scr.rfind(marker)
            if at > best:
                best, mode = at, m
        return mode

    def transcript_age(self) -> float:
        p = self.transcript_path()
        try:
            return time.time() - p.stat().st_mtime if p else 1e9
        except OSError:
            return 1e9

    # ---- 상태 판별 ----
    def quiet(self) -> bool:
        """(대체 판정) TUI 가 조용한가: 스피너가 안 돌고 transcript 도 그대로이며 방금 주입하지 않았음."""
        if self.proc is None:
            return False
        if time.time() - self.last_inject < self.quiet_window:
            return False
        if self.proc.output_rate(self.quiet_window) >= self.quiet_rate:
            return False
        return self.transcript_age() >= self.quiet_window

    def turn_state(self, turn: tr.Turn, events: list[dict], is_last: bool) -> str:
        st = tr.turn_state(turn, events)
        if st in (tr.DONE, tr.INTERRUPTED) or not is_last:
            return st
        live = self.live_status()
        if live is not None:
            if live == tr.AWAITING_APPROVAL:
                return tr.AWAITING_APPROVAL
            if live == tr.WORKING or time.time() - self.last_inject < INJECT_GRACE:
                return tr.WORKING  # 승인 뒤 도구 실행 중도 여기 (훅 기록만으론 계속 승인 대기로 보였음)
            # 끝나지 않은 턴인데 입력 대기 = 중단 (생각 중 Esc 는 transcript 에 흔적이 없다 — 실측)
            return tr.INTERRUPTED if self.transcript_age() >= SETTLE else tr.WORKING
        # 대체 판정 (claude agents 조회 불가): 결과 없는 tool_use 가 있으면 절대 중단으로 보지 않음
        if st == tr.WORKING and self.kind == BRIDGE and not turn.pending_tool_uses() and self.quiet():
            return tr.INTERRUPTED
        return st

    def status(self) -> str:
        if self.failure:
            return FAILED
        if self.kind == BRIDGE:
            if self.proc is None or not self.proc.is_alive():
                return EXITED
            if not self.ready:
                return STARTING
        elif self.vanished():
            return EXITED  # 백그라운드 세션이 사라짐
        live = self.live_status()
        records = self.records()
        if self.inflight is not None:
            if tr.turn_after(records, self.inflight.offset, self.inflight.text) is not None:
                self.inflight = None
            elif time.time() - self.inflight.sent_at > self.inflight_timeout and live in (None, tr.IDLE):
                self.inflight = None  # 기록이 끝내 안 남은 주입(유실 등)이 세션을 영원히 막지 않게
            else:
                return tr.WORKING
        # 실시간 작업 중·승인 대기가 우선 (transcript 상 마지막 턴이 끝나 있어도: Code 탭의 /compact, Stop 훅 등)
        if live in (tr.WORKING, tr.AWAITING_APPROVAL):
            return live
        turns = tr.split_turns(records)
        if not turns:
            return tr.IDLE
        st = self.turn_state(turns[-1], self.events(), is_last=True)
        return tr.IDLE if st in (tr.DONE, tr.INTERRUPTED) else st

    def blocker(self, records: list[dict] | None = None) -> str | None:
        """명령을 넣을 수 없는 이유 (없으면 None).
        - 모든 세션: 권한 모드가 허용 목록(기본: default·auto)에 있어야 함. Code 탭에서 acceptEdits·권한 우회로 바뀌면 거부
        - 백그라운드 세션: Remote Control 이 켜져 있어야 함 (휴대폰 Code 탭에서 보고 승인할 수 있도록)"""
        records = records if records is not None else self.records()
        if self.kind == BACKGROUND and not tr.rc_url(records):
            return f"Remote Control 이 켜지지 않은 세션입니다. 다음처럼 다시 띄우세요: {launch_hint(self.allowed_modes)}"
        mode = self.permission_mode(records)
        if (mode is not None and mode not in self.allowed_modes) or (mode is None and self.kind == BACKGROUND):
            allowed = "·".join(self.allowed_modes)
            fix = (f" 다음처럼 다시 띄우세요: {launch_hint(self.allowed_modes)}" if self.kind == BACKGROUND
                   else " Code 탭에서 허용 모드로 되돌려 주세요.")
            return f"권한 모드가 '{mode or '알 수 없음'}' 입니다. 음성 명령은 {allowed} 모드 세션에만 넣습니다.{fix}"
        return None

    def prompt_box_ready(self) -> bool:
        """안전장치: 화면 맨 뒤가 입력 대기 표시이고, 지금 화면의 모드가 허용 모드인지.
        - 권한·확인 대화상자가 떠 있으면 False (대화상자에 명령+Enter 가 들어가면 기본 선택=승인이 눌릴 수 있음)
        - 대기 표시: 일반 화면 '? for shortcuts'(default 모드에서만 나옴), 그 밖엔 하단 모드 표시 (실측)
        - 하단에 마지막으로 그려진 모드가 허용 모드가 아니면 False (CLI 는 모드 변경을 transcript 에 늦게 기록)"""
        if self.proc is None:
            return False
        scr = "".join(self.proc.screen().split()).lower()
        dialog_at = max(scr.rfind(k) for k in _DIALOG_MARKERS)
        allowed_markers = ["forshortcuts"] + [k for k, m in _MODE_MARKERS.items() if m in self.allowed_modes]
        idle_at = max(scr.rfind(k) for k in allowed_markers)
        mode = self.screen_mode()
        if mode is not None and mode not in self.allowed_modes:
            return False
        return idle_at >= 0 and idle_at > dialog_at

    def info(self) -> dict:
        st = self.status()
        records = self.records()
        block = self.blocker(records)
        d = {
            "name": self.name,
            "folder": self.cwd,
            "host": self.host,
            "kind": self.kind,
            "kind_ko": KIND_KO[self.kind],
            "status": st,
            "status_ko": STATUS_KO.get(st, st),
            "commandable": block is None and st not in (EXITED, FAILED),
            "session_id": self.session_id,
            "rc_url": tr.rc_url(records),  # None 이면 RC 미연결 (Code 탭에 안 보임)
        }
        mode = self.screen_mode() or self.permission_mode(records)
        d["permission_mode"] = mode
        d["permission_mode_ko"] = MODE_KO.get(mode, mode or "알 수 없음")
        if block:
            d["reason"] = block
        if self.failure:
            d["failure"] = self.failure
        if self.warning:
            d["warning"] = self.warning
        return d


class SessionManager:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}   # session_id → Session (bridge·background)
        self._requests: dict[str, Request] = {}
        self._selected: str | None = None         # session_id
        self._lock = threading.RLock()
        self.agents = AgentsCache(config.claude_bin, env=self._env())

    def _env(self) -> dict:
        extra = {}
        if self.config.claude_config_dir:
            extra["CLAUDE_CONFIG_DIR"] = str(self.config.claude_config_dir)
        return child_env(extra)

    # ---------- 세션 기동 (P3-4) ----------
    def start_session(self, name: str, cwd: str | None = None, wait: bool = True) -> Session:
        """bridge 세션 기동. cwd 를 주지 않으면 설정의 프리셋에서 찾는다 (MCP 경로는 프리셋만 허용)."""
        name = name.strip()
        if not name:
            raise BridgeError("bad_name", "세션 이름이 비었습니다.")
        with self._lock:
            old = self._bridge_by_name(name)
            if old and old.proc and old.proc.is_alive() and not old.failure:
                raise BridgeError("already_running", f"'{name}' 세션이 이미 실행 중입니다.", session=old.info())
            if old:
                self._sessions.pop(old.session_id, None)
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
                "--permission-mode", self.config.session_permission_mode,
                "--settings", str(settings_file),
                *self.config.claude_extra_args,
            ]
            try:
                proc = PtyProcess(argv, cwd=cwd, env=self._env())
            except OSError as e:  # Windows: claude 를 찾지 못함·ConPTY 실패 → 서버는 살아 있어야 한다
                raise BridgeError("start_failed", f"'{name}' 세션을 띄우지 못했습니다: {e}")
            s = Session(name=name, cwd=cwd, host=self.config.host_id, session_id=sid, proc=proc,
                        events_file=events_file, config_dir=self.config.claude_config_dir, kind=BRIDGE,
                        live=self.agents, quiet_window=self.config.quiet_window, quiet_rate=self.config.quiet_rate,
                        inflight_timeout=self.config.inject_ack_timeout,
                        allowed_modes=tuple(self.config.allowed_permission_modes))
            self._sessions[sid] = s
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
            if started and any(k in compact for k in _ready_markers(s.allowed_modes)):
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

    # ---------- 백그라운드 세션 ----------
    def _sync_background(self, agents: list[dict] | None) -> None:
        """claude agents 의 백그라운드 세션을 등록·갱신하고, 사라진 것은 정리한다."""
        if agents is None:
            return
        seen = set()
        for a in agents:
            if a.get("kind") != "background":
                continue
            sid = a["sessionId"]
            seen.add(sid)
            s = self._sessions.get(sid)
            if s is None:
                s = Session(name=a.get("name") or a.get("id") or sid[:8], cwd=a.get("cwd") or "", host=self.config.host_id,
                            session_id=sid, proc=None, events_file=None, config_dir=self.config.claude_config_dir,
                            kind=BACKGROUND, agent_id=a.get("id"), live=self.agents, ready=True,
                            inflight_timeout=self.config.inject_ack_timeout,
                            allowed_modes=tuple(self.config.allowed_permission_modes))
                self._sessions[sid] = s
            else:
                s.name = a.get("name") or s.name
        for sid, s in list(self._sessions.items()):
            if s.kind == BACKGROUND and sid not in seen:
                self._detach(s)
                self._sessions.pop(sid, None)
                if self._selected == sid:
                    self._selected = None  # 멈춘 세션에 다시 attach 하면 세션이 깨어나므로 고정을 푼다

    def _attach(self, s: Session) -> None:
        """`claude attach <id>` 를 PTY 로 연다. 이미 붙어 있으면 그대로."""
        if s.attached():
            return
        if not s.agent_id:
            raise BridgeError("attach_failed", f"'{s.name}' 세션의 attach ID 를 알 수 없습니다.")
        snap = self.agents.get(fresh=True)
        if snap is not None and not any(a.get("sessionId") == s.session_id for a in snap):
            # 멈춘 백그라운드 세션에 attach 하면 세션이 다시 깨어난다 (실측) → 붙지 않는다
            raise BridgeError("session_exited", f"'{s.name}' 세션이 실행 중이 아닙니다 (멈췄거나 종료됨).")
        if s.proc is not None:
            s.proc.terminate()  # 스스로 끝난 이전 attach 정리
        try:
            s.proc = PtyProcess([*self.config.claude_bin, "attach", s.agent_id], cwd=s.cwd or str(Path.home()),
                                env=self._env())
        except OSError as e:
            s.proc = None
            raise BridgeError("attach_failed", f"'{s.name}' 세션에 붙지 못했습니다: {e}")
        end = time.time() + min(self.config.ready_timeout, 20.0)
        while time.time() < end:
            time.sleep(0.3)
            if not s.proc.is_alive():
                raise BridgeError("attach_failed", f"'{s.name}' 세션에 붙지 못했습니다: " + _tail(s.proc.screen(), 200))
            scr = "".join(s.proc.screen().split()).lower()
            if any(k in scr for k in _IDLE_MARKERS + _DIALOG_MARKERS):
                return
        s.proc.terminate()
        raise BridgeError("attach_failed", f"'{s.name}' 세션에 붙는 데 시간이 너무 걸립니다.")

    @staticmethod
    def _detach(s: Session) -> None:
        """attach 만 닫는다. 백그라운드 세션 자체는 계속 돈다."""
        if s.proc is not None:
            s.proc.terminate()
            s.proc = None

    # ---------- 정리 ----------
    def _bridge_by_name(self, name: str) -> Session | None:
        return next((s for s in self._sessions.values() if s.kind == BRIDGE and s.name == name), None)

    def stop_session(self, name: str) -> None:
        """bridge 세션은 종료, 백그라운드 세션은 attach 만 해제."""
        with self._lock:
            targets = [s for s in self._sessions.values() if s.name == name]
            for s in targets:
                if self._selected == s.session_id:
                    self._selected = None
                if s.kind == BRIDGE:
                    self._sessions.pop(s.session_id, None)
        for s in targets:
            if s.kind == BRIDGE:
                if s.proc:
                    s.proc.terminate()
            else:
                self._detach(s)

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._selected = None
        for s in sessions:
            if s.kind == BRIDGE:
                if s.proc:
                    s.proc.terminate()
            else:
                self._detach(s)

    # ---------- 목록·고정 (P3-1, P3-2, P3-5) ----------
    def list_sessions(self) -> list[dict]:
        """이 머신의 모든 세션: 명령 가능(bridge·백그라운드) + 보기 전용(데스크톱 앱·다른 터미널)."""
        agents = self.agents.get(fresh=True)
        with self._lock:
            self._sync_background(agents)
            out = []
            for s in self._sessions.values():
                d = s.info()
                d["selected"] = s.session_id == self._selected
                out.append(d)
            ours = set(self._sessions)
            for a in agents or []:
                if a.get("kind") == "background" or a["sessionId"] in ours:
                    continue
                st = _LIVE_TO_STATUS.get(a.get("status"), tr.IDLE)
                url = tr.rc_url(tr.read_jsonl(tr.find_transcript(a["sessionId"], self.config.claude_config_dir)))
                reason = ("데스크톱 앱(또는 다른 터미널)이 입력을 쥐고 있어 음성 명령을 넣을 수 없습니다. "
                          + ("휴대폰 Code 탭에서 직접 입력하세요." if url else "Remote Control 도 꺼져 있어 휴대폰에서는 볼 수 없습니다.")
                          + f" 음성으로 쓸 세션은 이렇게 띄우세요: {launch_hint(self.config.allowed_permission_modes)}")
                out.append({
                    "name": a.get("name") or a["sessionId"][:8],
                    "folder": a.get("cwd"),
                    "host": self.config.host_id,
                    "kind": DESKTOP,
                    "kind_ko": KIND_KO[DESKTOP],
                    "status": st,
                    "status_ko": STATUS_KO.get(st, st),
                    "commandable": False,
                    "reason": reason,
                    "rc_url": url,
                    "session_id": a["sessionId"],
                    "selected": False,
                })
            order = {BRIDGE: 0, BACKGROUND: 1, DESKTOP: 2}
            out.sort(key=lambda d: (order[d["kind"]], d["name"]))
            return out

    def _resolve(self, key: str) -> Session:
        """이름 또는 session_id 로 명령 가능한 세션을 찾는다. 보기 전용이면 이유와 함께 거부."""
        key = key.strip()
        agents = self.agents.get()
        self._sync_background(agents)
        if key in self._sessions:
            return self._sessions[key]
        hits = [s for s in self._sessions.values() if s.name == key and s.status() not in (EXITED, FAILED)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            cands = [s.info() for s in hits]
            ok = [c for c in cands if c["commandable"]]
            hint = f" 명령 가능한 것은 session_id {ok[0]['session_id']} 하나입니다." if len(ok) == 1 else ""
            raise BridgeError("ambiguous", f"'{key}' 라는 세션이 여러 개입니다. session_id 로 지정해 주세요.{hint}",
                              candidates=[{k: c.get(k) for k in ("session_id", "kind_ko", "folder", "status_ko",
                                                                    "commandable", "reason")} for c in cands])
        for a in agents or []:
            if (a.get("name") == key or a["sessionId"] == key) and a.get("kind") != "background":
                raise BridgeError("view_only", f"'{key}' 은 데스크톱 앱(또는 다른 터미널) 세션이라 음성 명령을 넣을 수 없습니다. "
                                  f"Code 탭에서 직접 입력하거나, 음성으로 쓸 세션은 이렇게 띄우세요: "
                                  f"{launch_hint(self.config.allowed_permission_modes)}")
        raise BridgeError("no_session", f"'{key}' 세션이 없습니다.",
                          sessions=sorted({s.name for s in self._sessions.values()}))

    def get(self, key: str) -> Session:
        with self._lock:
            return self._resolve(key)

    def select_session(self, name: str) -> dict:
        with self._lock:
            s = self._resolve(name)
            block = s.blocker()
            if block:
                raise BridgeError("not_commandable", f"'{s.name}': {block}", session=s.info())
            if s.kind == BACKGROUND:
                self._attach(s)
            self._selected = s.session_id
            return s.info()

    @property
    def selected(self) -> Session:
        if self._selected is None or self._selected not in self._sessions:
            self._selected = None
            raise BridgeError("no_selection", "고정된 세션이 없습니다. 먼저 list_sessions 로 확인하고 select_session 으로 고정하세요.")
        return self._sessions[self._selected]

    # ---------- 명령 주입 (P3-3, P3-6) ----------
    def send_command(self, text: str, wait: bool = False) -> dict:
        """명령 주입. wait=True 면 결과가 나오거나 승인이 필요해질 때까지 최대 wait_timeout 초 기다렸다가 함께 돌려준다."""
        text = sanitize_command(text)
        with self._lock:
            s = self.selected
            records = s.records()
            block = s.blocker(records)
            if block:
                raise BridgeError("not_commandable", f"'{s.name}': {block}", session=s.info())
            if s.kind == BACKGROUND:
                self._attach(s)
            st = self._settled_status(s, records)
            if st != tr.IDLE:
                raise BridgeError("busy", f"'{s.name}' 세션이 {STATUS_KO.get(st, st)} 상태라 명령을 넣지 않았습니다.",
                                  session=s.info())
            smode = s.screen_mode()
            if smode is not None and smode not in s.allowed_modes:
                raise BridgeError("not_commandable", f"'{s.name}' 세션의 현재 권한 모드가 '{smode}' 입니다. "
                                  f"음성 명령은 {'·'.join(s.allowed_modes)} 모드에서만 넣습니다.", session=s.info())
            if not s.prompt_box_ready():
                raise BridgeError("not_at_prompt", f"'{s.name}' 세션 화면에 확인·권한 대화상자가 떠 있어 명령을 넣지 않았습니다. "
                                  "Code 탭에서 확인해 주세요.", session=s.info())
            turns = tr.split_turns(records)
            # 중단된 프롬프트가 입력창에 되돌아와 있을 수 있으니 그 줄 수보다 넉넉히 지운다
            clear_lines = max(8, (turns[-1].prompt.count("\n") + 4) if turns else 0)
            req = Request(id=uuid.uuid4().hex[:12], session_id=s.session_id, session_name=s.name, text=text,
                          offset=len(records), sent_at=time.time())
            s.inflight = req
            s.last_inject = req.sent_at
            try:
                s.proc.send_prompt(text, clear_lines=clear_lines)
            except OSError as e:
                s.inflight = None
                raise BridgeError("inject_failed", f"'{s.name}' 세션에 입력하지 못했습니다 ({e.__class__.__name__}).")
            self._requests[req.id] = req
        started = {"ok": True, "request_id": req.id, "session": s.name, "status": tr.WORKING,
                   "status_ko": STATUS_KO[tr.WORKING]}
        if not wait:
            return started
        return {**started, **self.wait_result(req.id)}

    def wait_result(self, request_id: str, timeout: float | None = None, poll: float = 0.5) -> dict:
        """결과가 나오거나(완료·중단), 새로 승인이 필요해질 때까지 최대 timeout 초 기다린다.
        MCP 는 서버가 대화에 먼저 말을 걸 수 없으므로, 휴대폰 Claude 가 이걸 반복 호출해 결과가 나오는 즉시 보고한다.
        반환: get_result 결과 + final(끝났는지). 이미 알린 승인 대기는 다시 즉시 돌려주지 않는다(헛도는 호출 방지)."""
        timeout = self.config.wait_timeout if timeout is None else timeout
        req = self._requests.get(request_id)
        if req is None:
            raise BridgeError("no_request", f"요청 ID {request_id} 를 찾을 수 없습니다.")
        end = time.time() + timeout
        seen_other = False
        while True:
            d = self.get_result(request_id)
            st = d["status"]
            if st in (tr.DONE, tr.INTERRUPTED):
                break
            if st == tr.AWAITING_APPROVAL:
                if req.last_reported != tr.AWAITING_APPROVAL or seen_other:
                    break  # 새로 승인이 필요해짐 → 바로 알린다
            else:
                seen_other = True
            if time.time() >= end:
                break
            time.sleep(poll)
        req.last_reported = st
        d["final"] = st in (tr.DONE, tr.INTERRUPTED)
        if not d["final"]:
            d["next"] = "끝나지 않았습니다. 사용자에게 묻지 말고 get_result(request_id, wait=true) 를 다시 호출해 계속 기다리세요."
        return d

    def _settled_status(self, s: Session, records: list[dict]) -> str:
        """주입 직전 상태: 실시간 상태를 새로 조회하고, 방금 끝난 턴이면 상태가 idle 로 바뀌기를 잠깐 기다린다
        (마지막 기록과 claude agents 상태 갱신 사이 시차, 1초 캐시)."""
        self.agents.get(fresh=True)
        st = s.status()
        turns = tr.split_turns(records)
        just_ended = bool(turns) and tr.turn_state(turns[-1], s.events()) in (tr.DONE, tr.INTERRUPTED) \
            and s.transcript_age() < 3.0
        deadline = time.time() + 2.5
        while just_ended and st in (tr.WORKING, tr.AWAITING_APPROVAL) and time.time() < deadline:
            time.sleep(0.25)
            self.agents.get(fresh=True)
            st = s.status()
        return st

    def get_result(self, request_id: str) -> dict:
        req = self._requests.get(request_id)
        if req is None:
            raise BridgeError("no_request", f"요청 ID {request_id} 를 찾을 수 없습니다.")
        s = self._sessions.get(req.session_id)
        if s is None:
            raise BridgeError("no_session", f"요청의 세션 '{req.session_name}' 이 없습니다.")
        records = s.records()
        turn = tr.turn_after(records, req.offset, req.text)
        base = {"ok": True, "request_id": req.id, "session": s.name}
        if turn is None:
            if s.kind == BRIDGE and (s.proc is None or not s.proc.is_alive()):
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
            self.agents.get(fresh=True)
            st = s.status()
            if st not in (tr.WORKING, tr.AWAITING_APPROVAL):
                return {"ok": True, "session": s.name, "status": st, "status_ko": STATUS_KO.get(st, st),
                        "message": "진행 중인 작업이 없습니다."}
            if s.kind == BACKGROUND:
                self._attach(s)
            s.proc.send_escape()
        return {"ok": True, "session": s.name, "message": "중단 신호(Esc)를 보냈습니다."}


# 하단 모드 표시 → 권한 모드 (실측: '⏸ manual mode on', '⏵⏵ auto mode on (shift+tab to cycle)')
_MODE_MARKERS = {"manualmodeon": "default", "automodeon": "auto", "accepteditson": "acceptEdits",
                 "planmodeon": "plan", "bypasspermissionson": "bypassPermissions"}
# attach 준비 판정에 쓰는 표시 (어떤 모드든 화면이 그려졌는지). 주입 허용 여부는 prompt_box_ready 가 따로 판단
_IDLE_MARKERS = ("forshortcuts", *_MODE_MARKERS)


def _ready_markers(allowed_modes) -> list[str]:
    """bridge 가 띄운 세션의 준비 판정: '? for shortcuts' 는 default 모드에서만 나오므로 허용 모드 표시도 인정."""
    return ["forshortcuts"] + [k for k, m in _MODE_MARKERS.items() if m in allowed_modes]
_DIALOG_MARKERS = ("doyouwanttoproceed", "doyouwanttomake", "doyouwanttocreate", "entertoconfirm", "esctocancel")
_LIVE_TO_STATUS = {"idle": tr.IDLE, "busy": tr.WORKING, "waiting": tr.AWAITING_APPROVAL}

_TRUST_MSG = "폴더 신뢰가 필요합니다. 해당 폴더에서 claude 를 한 번 직접 실행해 신뢰해 주세요: {cwd}"


def _is_trust_prompt(compact: str) -> bool:
    return any(k in compact for k in ("quicksafetycheck", "trustthisfolder", "oneyoutrust", "doyoutrust"))


def _tail(text: str, n: int = 400) -> str:
    return " ".join(text.split())[-n:]
