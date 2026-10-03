"""자동 실행 등록: 로그인하면 브리지·터널이 뜨고, 죽으면 다시 띄운다.

    voice-bridge service install     등록 + 바로 시작
    voice-bridge service uninstall   해제
    voice-bridge service status      실행 상태

두 개의 작업: serve (voice-bridge serve), tunnel (cloudflared 임시 터널 + 주소가 바뀌면 알림)

| OS | 방식 | 재시작 | 로그 |
|---|---|---|---|
| macOS | launchd LaunchAgent (com.voicebridge.*) | 10초 뒤 | ~/Library/Logs/voice-bridge/ |
| Windows | 작업 스케줄러 VoiceBridge-{serve,tunnel} (로그온 + 1분 반복 감시, pythonw 로 창 없이) | 1분 안 | %LOCALAPPDATA%\\voice-bridge\\logs |
| Linux | systemd 사용자 서비스 voice-bridge-{serve,tunnel} | 10초 뒤 | ~/.local/state/voice-bridge/logs |
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

def log_dir(platform: str = sys.platform) -> Path:
    if platform == "darwin":
        return Path.home() / "Library" / "Logs" / "voice-bridge"
    if platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "voice-bridge" / "logs"
    return Path.home() / ".local" / "state" / "voice-bridge" / "logs"


AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = log_dir()
SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
WIN_TASK_PREFIX = "VoiceBridge-"   # 루트에 등록 (일반 권한으로 하위 폴더를 만들 수 없는 경우가 있음)
COMMANDS = ("serve", "tunnel")
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
    return f"gui/{getattr(os, 'getuid', lambda: 0)()}"


def _launchctl(*args: str, run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["launchctl", *args], capture_output=True, text=True)


def _install_darwin(run=subprocess.run) -> list[str]:
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


def _uninstall_darwin(run=subprocess.run) -> list[str]:
    removed = []
    for label in LABELS:
        _launchctl("bootout", f"{_domain()}/{label}", run=run)
        path = AGENTS_DIR / f"{label}.plist"
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def _status_darwin(run=subprocess.run) -> dict[str, str]:
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



# ---------- Windows: 작업 스케줄러 ----------

def pythonw_exe() -> str:
    """콘솔 창 없이 실행할 파이썬 (venv 의 pythonw.exe)."""
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w if w.exists() else sys.executable)


_WIN_ENTRY = "import sys; from bridge.__main__ import main; sys.exit(main(sys.argv[1:]))"


def win_task_xml(cmd: str, user: str | None = None) -> str:
    """로그온 시 시작 + 1분마다 반복 트리거(이미 돌고 있으면 무시 = 죽어 있으면 다시 띄우는 감시자).
    '실패 시 다시 시작'은 비정상 종료 코드에는 동작하지 않을 수 있어 반복 트리거를 주 장치로 쓴다.
    실행 시간 제한 없음, 배터리여도 실행, 보통 우선순위. 파이썬은 -P 로 홈 폴더를 import 경로에 넣지 않는다."""
    from xml.sax.saxutils import escape

    user = user or (f"{os.environ.get('USERDOMAIN')}\\{os.environ.get('USERNAME')}"
                    if os.environ.get("USERNAME") else None)
    log = log_dir("win32") / f"{cmd}.log"
    args = f'-P -c "{_WIN_ENTRY}" {cmd} --log "{log}"'
    uid = f"<UserId>{escape(user)}</UserId>" if user else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>voice-bridge {cmd}</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <Enabled>true</Enabled>{uid}
    </LogonTrigger>
  </Triggers>
  <Principals><Principal id="Author">{uid}<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>255</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(pythonw_exe())}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(Path.home()))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _win_task(cmd: str) -> str:
    return f"{WIN_TASK_PREFIX}{cmd}"


def _win_run_kw() -> dict:
    """schtasks·powershell 출력은 OEM 코드 페이지 (UTF-8 모드에서도 깨지지 않게)."""
    kw = {"capture_output": True, "text": True, "errors": "replace"}
    if sys.platform == "win32":
        kw["encoding"] = "oem"
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kw


def _install_windows(run=subprocess.run, state_dir: Path | None = None) -> list[str]:
    from .config import default_state_dir

    task_dir = (state_dir or default_state_dir("win32")) / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    log_dir("win32").mkdir(parents=True, exist_ok=True)
    done = []
    for cmd in COMMANDS:
        xml = task_dir / f"{cmd}.xml"
        xml.write_text(win_task_xml(cmd), encoding="utf-16")  # schtasks /XML 은 UTF-16 을 기대
        run(["schtasks", "/End", "/TN", _win_task(cmd)], **_win_run_kw())  # 재설치: 옛 인스턴스 먼저 내림 (IgnoreNew)
        r = run(["schtasks", "/Create", "/TN", _win_task(cmd), "/XML", str(xml), "/F"], **_win_run_kw())
        if r.returncode != 0:
            raise SystemExit(f"작업 등록 실패({cmd}): {(r.stderr or r.stdout).strip()}")
        run(["schtasks", "/Run", "/TN", _win_task(cmd)], **_win_run_kw())
        done.append(_win_task(cmd))
    return done


def _uninstall_windows(run=subprocess.run) -> list[str]:
    removed = []
    for cmd in COMMANDS:
        run(["schtasks", "/End", "/TN", _win_task(cmd)], **_win_run_kw())
        r = run(["schtasks", "/Delete", "/TN", _win_task(cmd), "/F"], **_win_run_kw())
        if r.returncode == 0:
            removed.append(_win_task(cmd))
    return removed


def _status_windows(run=subprocess.run) -> dict[str, str]:
    """PowerShell 의 State 는 OS 언어와 무관하게 영문(Running/Ready/Disabled)이라 schtasks 출력 대신 쓴다."""
    ps = (f"Get-ScheduledTask -TaskName '{WIN_TASK_PREFIX}*' -ErrorAction SilentlyContinue | "
          "ForEach-Object { $_.TaskName + '=' + $_.State }")
    r = run(["powershell", "-NoProfile", "-Command", ps], **_win_run_kw())
    found = dict(line.strip().split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
    return {_win_task(c): found.get(_win_task(c), "등록 안 됨") for c in COMMANDS}


# ---------- Linux: systemd 사용자 서비스 ----------

def systemd_unit(cmd: str) -> str:
    exe = bridge_exe()
    return f"""[Unit]
Description=voice-bridge {cmd}
After=network-online.target

[Service]
ExecStart="{exe}" {cmd}
Restart=always
RestartSec=10
Environment=PATH={service_path()}
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:{log_dir("linux") / f"{cmd}.log"}
StandardError=append:{log_dir("linux") / f"{cmd}.log"}

[Install]
WantedBy=default.target
"""


def _unit(cmd: str) -> str:
    return f"voice-bridge-{cmd}.service"


def _install_linux(run=subprocess.run) -> list[str]:
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    log_dir("linux").mkdir(parents=True, exist_ok=True)
    done = []
    for cmd in COMMANDS:
        path = SYSTEMD_DIR / _unit(cmd)
        path.write_text(systemd_unit(cmd), encoding="utf-8")
        done.append(str(path))
    run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    r = run(["systemctl", "--user", "enable", "--now", *[_unit(c) for c in COMMANDS]], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"systemd 등록 실패: {(r.stderr or r.stdout).strip()}")
    return done


def _uninstall_linux(run=subprocess.run) -> list[str]:
    run(["systemctl", "--user", "disable", "--now", *[_unit(c) for c in COMMANDS]], capture_output=True, text=True)
    removed = []
    for cmd in COMMANDS:
        path = SYSTEMD_DIR / _unit(cmd)
        if path.exists():
            path.unlink()
            removed.append(str(path))
    run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    return removed


def _status_linux(run=subprocess.run) -> dict[str, str]:
    out = {}
    for cmd in COMMANDS:
        r = run(["systemctl", "--user", "is-active", _unit(cmd)], capture_output=True, text=True)
        out[_unit(cmd)] = (r.stdout or "").strip() or "등록 안 됨"
    return out


# ---------- 공통 입구 ----------

def install(run=subprocess.run, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    if platform == "darwin":
        return _install_darwin(run)
    if platform == "win32":
        return _install_windows(run)
    return _install_linux(run)


def uninstall(run=subprocess.run, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    if platform == "darwin":
        return _uninstall_darwin(run)
    if platform == "win32":
        return _uninstall_windows(run)
    return _uninstall_linux(run)


def status(run=subprocess.run, platform: str | None = None) -> dict[str, str]:
    platform = platform or sys.platform
    if platform == "darwin":
        return _status_darwin(run)
    if platform == "win32":
        return _status_windows(run)
    return _status_linux(run)


# ---------- 터널 ----------

def notify(title: str, message: str, run=subprocess.run, popen=subprocess.Popen, platform: str | None = None) -> None:
    """데스크톱 알림. 실패해도 무시. macOS 알림 센터 / Windows 트레이 풍선 알림 / Linux notify-send."""
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
            run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'],
                capture_output=True, timeout=10)
        elif platform == "win32":
            q = lambda t: t.replace("'", "''")
            ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                  "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
                  f"$n.BalloonTipTitle = '{q(title)}'; $n.BalloonTipText = '{q(message)}'; $n.Visible = $true; "
                  "$n.ShowBalloonTip(15000); Start-Sleep -Seconds 15; $n.Dispose()")
            popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # 기다리지 않음
        else:
            import shutil

            if shutil.which("notify-send"):
                run(["notify-send", title, message], capture_output=True, timeout=10)
    except Exception:
        pass


_JOBS: list = []   # Job Object 핸들을 프로세스 수명 동안 붙잡아 둔다


def host_file(state_dir: Path) -> Path:
    return state_dir / "tunnel_host"


def run_tunnel(port: int, state_dir: Path, popen=subprocess.Popen, run=subprocess.run, out=sys.stdout) -> int:
    """cloudflared 임시 터널을 띄우고, 발급된 주소를 기록한다. 이전 주소와 다르면 맥 알림."""
    state_dir.mkdir(parents=True, exist_ok=True)
    hf = host_file(state_dir)
    prev = hf.read_text().strip() if hf.exists() else None
    p = popen(["cloudflared", "tunnel", "--url", f"http://localhost:{port}", "--metrics", QUICKTUNNEL_METRICS],
              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
              encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if sys.platform == "win32":
        # Windows 는 부모가 강제 종료돼도(작업 스케줄러 /End) cloudflared 가 남는다 → 함께 끝나도록 Job Object 에 묶음
        from .proc import KillOnCloseJob

        _JOBS.append(KillOnCloseJob())
        _JOBS[-1].add(p)
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
                       f"{host} — 터미널에서 voice-bridge url 로 새 주소를 확인해 커넥터를 갱신하세요.", run=run)  # noqa
    return p.wait()
