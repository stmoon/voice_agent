"""bridge 설정. 기본 위치: ~/.config/voice-bridge/config.toml (VC_CONFIG 로 변경).

예시::

    host_id = "macmini"          # 생략 시 호스트명
    port = 8765
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

# 음성 경로로 권한 우회 모드를 쓰지 않는다
FORBIDDEN_ARGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--permission-mode")


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
    ready_timeout: float = 60.0
    inject_ack_timeout: float = 20.0
    quiet_window: float = 4.0     # 이 시간 동안 조용하면(아래 속도 미만) 대기로 본다
    quiet_rate: float = 150.0     # 출력 문자/초. 실측: 작업 중 500~1100, 대기 0~50
    allowed_hosts: list[str] = field(default_factory=list)  # 터널 도메인 (Host 헤더)
    python: str = sys.executable

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir).expanduser()
        if self.claude_config_dir is not None:
            self.claude_config_dir = Path(self.claude_config_dir).expanduser()
        self.sessions = {k: str(Path(v).expanduser()) for k, v in self.sessions.items()}
        for a in self.claude_extra_args:
            if a.split("=")[0] in FORBIDDEN_ARGS:
                raise ValueError(f"허용되지 않는 claude 인자: {a} (권한 모드는 default 고정)")

    @classmethod
    def load(cls, path: Path | None = None) -> "BridgeConfig":
        path = path or default_config_path()
        data: dict = {}
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        if isinstance(data.get("claude_bin"), str):
            data["claude_bin"] = shlex.split(data["claude_bin"])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
