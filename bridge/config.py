"""bridge 설정. 기본 위치: ~/.config/voice-bridge/config.toml (VC_CONFIG 로 변경).

예시::

    host_id = "macmini"          # 생략 시 호스트명
    port = 8765
    autostart = ["로그 플랫폼"]   # serve 할 때 자동으로 띄울 프리셋
    [sessions]                   # 음성으로 띄울 수 있는 세션 프리셋 (이름 = 작업 폴더)
    "로그 플랫폼" = "~/Project/logplatform"
    "IMM-EKF 논문" = "~/Papers/imm-ekf"
"""
from __future__ import annotations

import os
import shlex
import socket
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 음성 경로로 권한 우회 모드를 쓰지 않는다 (권한 모드는 아래 설정 항목으로만 정한다)
FORBIDDEN_ARGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--permission-mode")

# 음성 명령을 받을 수 있는 권한 모드 후보. bypassPermissions(모든 권한 검사 끔)는 어떤 설정으로도 허용하지 않는다.
#   default     : 위험한 작업마다 사람이 승인
#   auto        : Claude 가 작업마다 위험도를 판단해 안전한 것은 자동 실행, 위험한 것은 차단 (2026-10-03 사용자 결정으로 기본 허용)
#   plan        : 계획만 세우고 실행하지 않음
#   acceptEdits : 파일 편집을 무조건 자동 승인 (기본 비허용)
PERMISSION_MODES = ("default", "auto", "plan", "acceptEdits")


def default_config_path() -> Path:
    return Path(os.environ.get("VC_CONFIG") or Path.home() / ".config" / "voice-bridge" / "config.toml")


def default_host_id() -> str:
    return socket.gethostname().split(".")[0].lower()


@dataclass
class BridgeConfig:
    host_id: str = field(default_factory=default_host_id)
    bind: str = "127.0.0.1"
    port: int = 8765
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local" / "state" / "voice-bridge")
    claude_bin: list[str] = field(default_factory=lambda: ["claude"])
    claude_extra_args: list[str] = field(default_factory=list)
    claude_config_dir: Path | None = None   # transcript 탐색 위치 (기본 ~/.claude)
    sessions: dict[str, str] = field(default_factory=dict)  # 프리셋 이름 → 폴더
    autostart: list[str] = field(default_factory=list)      # serve 시 자동으로 띄울 프리셋 이름
    ready_timeout: float = 60.0
    inject_ack_timeout: float = 20.0
    quiet_window: float = 4.0     # 이 시간 동안 조용하면(아래 속도 미만) 대기로 본다
    quiet_rate: float = 150.0     # 출력 문자/초. 실측: 작업 중 500~1100, 대기 0~50
    allowed_hosts: list[str] = field(default_factory=list)  # 터널 도메인 (Host 헤더)
    allowed_permission_modes: list[str] = field(default_factory=lambda: ["default", "auto"])  # 음성 명령을 받을 세션 모드
    session_permission_mode: str = "default"   # bridge 가 직접 띄우는 세션의 권한 모드
    python: str = sys.executable
    source: Path | None = None    # 읽어 온 설정 파일 (프리셋 다시 읽기용)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir).expanduser()
        if self.claude_config_dir is not None:
            self.claude_config_dir = Path(self.claude_config_dir).expanduser()
        self.sessions = {k: str(Path(v).expanduser()) for k, v in self.sessions.items()}
        unknown = [n for n in self.autostart if n not in self.sessions]
        if unknown:
            raise ValueError(f"autostart 에 등록되지 않은 프리셋: {', '.join(unknown)} (등록: {', '.join(self.sessions) or '없음'})")
        for a in self.claude_extra_args:
            if a.split("=")[0] in FORBIDDEN_ARGS:
                raise ValueError(f"허용되지 않는 claude 인자: {a} (권한 모드는 session_permission_mode 로 정함)")
        bad = [m for m in self.allowed_permission_modes if m not in PERMISSION_MODES]
        if bad:
            raise ValueError(f"허용할 수 없는 권한 모드: {', '.join(bad)} (가능: {', '.join(PERMISSION_MODES)})")
        if self.session_permission_mode not in self.allowed_permission_modes:
            raise ValueError(f"session_permission_mode '{self.session_permission_mode}' 가 allowed_permission_modes 에 없습니다.")

    @classmethod
    def load(cls, path: Path | None = None) -> "BridgeConfig":
        path = path or default_config_path()
        data: dict = {}
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        if isinstance(data.get("claude_bin"), str):
            data["claude_bin"] = shlex.split(data["claude_bin"])
        known = {f for f in cls.__dataclass_fields__} - {"source"}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.source = path
        return cfg

    def reload_sessions(self) -> None:
        """설정 파일의 [sessions] 를 다시 읽는다 (브리지 재시작 없이 새 프리셋 반영)."""
        if self.source is None or not self.source.exists():
            return
        data = tomllib.loads(self.source.read_text(encoding="utf-8"))
        self.sessions = {k: str(Path(v).expanduser()) for k, v in (data.get("sessions") or {}).items()}


def _toml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def add_preset(path: Path, name: str, folder: str, autostart: bool = False) -> None:
    """설정 파일에 프리셋 한 줄을 추가한다(주석·순서 보존). 같은 이름이 있으면 폴더를 바꾼다."""
    name = name.strip()
    if not name:
        raise ValueError("세션 이름이 비었습니다.")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    data = tomllib.loads(text) if text else {}
    line = f"{_toml_str(name)} = {_toml_str(folder)}"
    lines = text.splitlines()
    sess_at = next((i for i, l in enumerate(lines) if l.strip() == "[sessions]"), None)
    if sess_at is None:
        lines += ["", "[sessions]", line]
    else:
        end = next((i for i in range(sess_at + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        existing = None
        for i in range(sess_at + 1, end):
            st = lines[i].strip()
            if st and not st.startswith("#") and "=" in st:
                try:
                    if name in tomllib.loads("[s]\n" + st).get("s", {}):
                        existing = i
                except tomllib.TOMLDecodeError:
                    pass
        if existing is not None:
            lines[existing] = line
        else:
            ins = end
            while ins > sess_at + 1 and not lines[ins - 1].strip():
                ins -= 1
            lines.insert(ins, line)
    if autostart:
        auto = list(data.get("autostart") or [])
        if name not in auto:
            auto.append(name)
        new = "autostart = [" + ", ".join(_toml_str(a) for a in auto) + "]"
        at = next((i for i, l in enumerate(lines) if l.strip().startswith("autostart")), None)
        if at is not None:
            lines[at] = new
        else:
            top = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))
            lines.insert(top, new)
    out = "\n".join(lines) + "\n"
    tomllib.loads(out)  # 깨진 TOML 은 쓰지 않는다
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out, encoding="utf-8")


def claude_trusts(folder: str) -> bool | None:
    """claude 가 이 폴더(또는 상위 폴더)를 신뢰했는지 ~/.claude.json 으로 확인. 알 수 없으면 None."""
    import json

    cfg = Path.home() / ".claude.json"
    try:
        projects = json.loads(cfg.read_text(encoding="utf-8")).get("projects", {})
    except Exception:
        return None
    p = Path(folder).expanduser().resolve()
    for cand in (p, *p.parents):
        if projects.get(str(cand), {}).get("hasTrustDialogAccepted"):
            return True
    return False
