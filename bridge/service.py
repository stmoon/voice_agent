"""자동 실행 등록 (macOS launchd LaunchAgent).

    voice-bridge service install     브리지·터널을 로그인 시 자동 실행 + 죽으면 재시작
    voice-bridge service uninstall   해제
    voice-bridge service status      실행 상태

두 개의 에이전트:
- com.voicebridge.serve  : voice-bridge serve
- com.voicebridge.tunnel : voice-bridge tunnel (cloudflared 임시 터널 + 주소 바뀌면 맥 알림)
로그: ~/Library/Logs/voice-bridge/{serve,tunnel}.log
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

LABEL_SERVE = "com.voicebridge.serve"
LABEL_TUNNEL = "com.voicebridge.tunnel"
LABELS = (LABEL_SERVE, LABEL_TUNNEL)

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "voice-bridge"
TUNNEL_HOST_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")
QUICKTUNNEL_METRICS = "127.0.0.1:20241"


def bridge_exe() -> str:
    exe = Path(sys.executable).parent / ("voice-bridge.exe" if os.name == "nt" else "voice-bridge")
    return str(exe)


def service_path() -> str:
    """launchd 는 로그인 셸 PATH 를 안 물려준다. claude(~/.local/bin)·cloudflared(brew) 를 찾을 수 있게."""
    import shutil

    dirs = []
    for tool in ("claude", "cloudflared"):
        p = shutil.which(tool)
        if p:
            dirs.append(str(Path(p).parent))
    dirs += [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
             "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


def plist_for(label: str, args: list[str], log_name: str) -> dict:
    return {
        "Label": label,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",  # 로그인 세션 안에서만 (키체인 접근)
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "PATH": service_path(),
            "LANG": "en_US.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(LOG_DIR / f"{log_name}.log"),
        "StandardErrorPath": str(LOG_DIR / f"{log_name}.log"),
        "ProcessType": "Background",
    }


def plists() -> dict[str, dict]:
    exe = bridge_exe()
    return {
        LABEL_SERVE: plist_for(LABEL_SERVE, [exe, "serve"], "serve"),
        LABEL_TUNNEL: plist_for(LABEL_TUNNEL, [exe, "tunnel"], "tunnel"),
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["launchctl", *args], capture_output=True, text=True)


def install(run=subprocess.run) -> list[str]:
    if sys.platform != "darwin":
        raise SystemExit("자동 실행 등록은 현재 macOS(launchd)만 지원합니다.")
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    done = []
    for label, pl in plists().items():
        path = AGENTS_DIR / f"{label}.plist"
        _launchctl("bootout", f"{_domain()}/{label}", run=run)  # 이미 있으면 내림 (없으면 오류 무시)
        with open(path, "wb") as f:
            plistlib.dump(pl, f)
        r = _launchctl("bootstrap", _domain(), str(path), run=run)
        if r.returncode != 0:
            raise SystemExit(f"{label} 등록 실패: {r.stderr.strip() or r.stdout.strip()}")
        _launchctl("enable", f"{_domain()}/{label}", run=run)
        done.append(str(path))
    return done


def uninstall(run=subprocess.run) -> list[str]:
    removed = []
    for label in LABELS:
        _launchctl("bootout", f"{_domain()}/{label}", run=run)
        path = AGENTS_DIR / f"{label}.plist"
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def status(run=subprocess.run) -> dict[str, str]:
    out = {}
    for label in LABELS:
        r = _launchctl("print", f"{_domain()}/{label}", run=run)
        if r.returncode != 0:
            out[label] = "등록 안 됨"
            continue
        state = re.search(r"^\s*state = (\S+)", r.stdout, re.M)
        pid = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
        out[label] = (state.group(1) if state else "?") + (f" (pid {pid.group(1)})" if pid else "")
    return out


# ---------- 터널 ----------

def notify(title: str, message: str, run=subprocess.run) -> None:
    """맥 알림 센터에 표시. 실패해도 무시."""
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'],
            capture_output=True, timeout=10)
    except Exception:
        pass


def host_file(state_dir: Path) -> Path:
    return state_dir / "tunnel_host"


def run_tunnel(port: int, state_dir: Path, popen=subprocess.Popen, run=subprocess.run, out=sys.stdout) -> int:
    """cloudflared 임시 터널을 띄우고, 발급된 주소를 기록한다. 이전 주소와 다르면 맥 알림."""
    state_dir.mkdir(parents=True, exist_ok=True)
    hf = host_file(state_dir)
    prev = hf.read_text().strip() if hf.exists() else None
    p = popen(["cloudflared", "tunnel", "--url", f"http://localhost:{port}", "--metrics", QUICKTUNNEL_METRICS],
              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    seen = False
    for line in p.stdout:
        out.write(line)
        out.flush()
        if seen:
            continue
        m = TUNNEL_HOST_RE.search(line)
        if m and m.group(1) != "api.trycloudflare.com":
            seen = True
            host = m.group(1)
            hf.write_text(host + "\n")
            out.write(f"[tunnel] 주소: https://{host}\n")
            if host != prev:
                notify("음성 브리지 주소가 바뀌었습니다",
                       f"{host} — 터미널에서 voice-bridge url 로 새 주소를 확인해 커넥터를 갱신하세요.", run=run)
    return p.wait()
