#!/usr/bin/env python3
"""voice-bridge 설치 스크립트 (Windows·macOS·Linux 공통, Python 3.11+ 만 필요).

    python tools/install.py                 # 기본 위치에 설치
    python tools/install.py --prefix DIR    # 설치 위치 지정

하는 일: 전용 venv 생성 → 이 레포 설치 → 기본 config.toml 생성(없을 때만) → 다음 단계 안내.
토큰은 설치 후 `voice-bridge token init` 으로 OS 보안 저장소에 등록한다.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DEFAULT_CONFIG = """\
# voice-bridge 설정
# host_id = "{host}"      # 생략하면 호스트명
port = 8765
# 터널 도메인 (tools/tunnel_setup.py 가 알려줌)
allowed_hosts = []

# 음성으로 띄울 수 있는 세션 프리셋: 이름 = 작업 폴더 (claude 로 한 번 열어 신뢰해 둔 폴더)
[sessions]
# "로그 플랫폼" = "~/Project/logplatform"
"""


def default_prefix() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "voice-bridge"
    return Path.home() / ".local" / "share" / "voice-bridge"


def config_path() -> Path:
    return Path(os.environ.get("VC_CONFIG") or Path.home() / ".config" / "voice-bridge" / "config.toml")


def venv_bin(prefix: Path, name: str) -> Path:
    d = prefix / "venv" / ("Scripts" if os.name == "nt" else "bin")
    return d / (name + (".exe" if os.name == "nt" else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", type=Path, default=default_prefix())
    ap.add_argument("--editable", action="store_true", help="레포를 편집 가능 모드로 설치 (개발용)")
    a = ap.parse_args(argv)

    if sys.version_info < (3, 11):
        print("Python 3.11 이상이 필요합니다.", file=sys.stderr)
        return 2
    prefix = a.prefix.expanduser()
    prefix.mkdir(parents=True, exist_ok=True)
    if not venv_bin(prefix, "python").exists():
        print(f"[install] venv 생성: {prefix / 'venv'}")
        venv.EnvBuilder(with_pip=True).create(prefix / "venv")
    py = str(venv_bin(prefix, "python"))
    pip = [py, "-m", "pip", "install", "-q"]
    subprocess.check_call([*pip, "--upgrade", "pip"])
    subprocess.check_call([*pip, *(["-e"] if a.editable else []), str(REPO)])

    cfg = config_path()
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        import socket

        cfg.write_text(DEFAULT_CONFIG.format(host=socket.gethostname().split(".")[0].lower()), encoding="utf-8")
        print(f"[install] 기본 설정 생성: {cfg}")

    exe = venv_bin(prefix, "voice-bridge")
    print("\n[install] 완료. 다음 단계:")
    print(f"  1) {exe} token init                  # MCP 토큰 → OS 보안 저장소")
    print(f"  2) {sys.executable} {REPO / 'tools' / 'tunnel_setup.py'} --domain <도메인>")
    print(f"  3) {cfg} 에 세션 프리셋·allowed_hosts 입력")
    print(f"  4) {exe} serve --start \"<세션이름>\"")
    print("  5) 모바일 Claude 에 커넥터 등록 (token init 이 출력한 URL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
