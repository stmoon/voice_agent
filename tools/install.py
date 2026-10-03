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
# 외부 주소 허용 (임시 터널은 주소가 바뀌므로 와일드카드). 고정 도메인을 쓰면 그 주소를 넣는다
allowed_hosts = ["*.trycloudflare.com"]

# 음성으로 띄울 수 있는 세션 프리셋: 이름 = 작업 폴더 (claude 로 한 번 열어 신뢰해 둔 폴더)
[sessions]
# "로그 플랫폼" = "~/Project/logplatform"
"""


def default_prefix() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "voice-bridge"
    return Path.home() / ".local" / "share" / "voice-bridge"


def config_path() -> Path:
    """브리지와 같은 규칙 (macOS·Linux ~/.config/…, Windows %APPDATA%\\voice-bridge\\config.toml)."""
    sys.path.insert(0, str(REPO))
    from bridge.config import default_config_path  # 표준 라이브러리만 쓰는 모듈이라 설치 전에도 import 가능

    return default_config_path()


def venv_bin(prefix: Path, name: str) -> Path:
    d = prefix / "venv" / ("Scripts" if os.name == "nt" else "bin")
    return d / (name + (".exe" if os.name == "nt" else ""))


def make_shim(prefix: Path, shim_dir: Path) -> Path | None:
    """어디서든 `voice-bridge` 로 부를 수 있게 shim_dir(기본 ~/.local/bin, claude 공식 설치와 같은 곳)에 실행 링크를 둔다."""
    exe = venv_bin(prefix, "voice-bridge")
    shim_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        shim = shim_dir / "voice-bridge.cmd"
        shim.write_text(f'@"{exe}" %*\r\n', encoding="utf-8")
        return shim
    shim = shim_dir / "voice-bridge"
    if shim.is_symlink() or not shim.exists():
        if shim.is_symlink():
            shim.unlink()
        shim.symlink_to(exe)
        return shim
    print(f"[install] {shim} 가 이미 있어 그대로 둡니다 (다른 파일).")
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", type=Path, default=default_prefix())
    ap.add_argument("--editable", action="store_true", help="레포를 편집 가능 모드로 설치 (개발용)")
    ap.add_argument("--shim-dir", type=Path, default=Path.home() / ".local" / "bin",
                    help="voice-bridge 실행 링크를 둘 폴더 (PATH 에 있어야 함)")
    ap.add_argument("--no-shim", action="store_true", help="실행 링크를 만들지 않음")
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
    if not a.no_shim:
        shim = make_shim(prefix, a.shim_dir)
        if shim:
            on_path = str(a.shim_dir) in os.environ.get("PATH", "").split(os.pathsep)
            print(f"[install] 실행 링크: {shim}" + ("" if on_path else f"  (PATH 에 {a.shim_dir} 를 추가하세요)"))
            if on_path:
                exe = Path("voice-bridge")
    print("\n[install] 완료. 다음 단계 (README '사용 방법' 참고):")
    if os.name == "nt":
        print("  0) winget install Cloudflare.cloudflared   # 없으면 (설치 뒤 새 터미널)")
    print(f"  1) {exe} setup                       # 토큰 + 자동 실행 + 커넥터 주소 (한 번)")
    print(f"  2) {exe} session new \"이름\" 작업폴더   # 휴대폰에서 부를 세션 만들기")
    return 0


if __name__ == "__main__":
    sys.exit(main())
