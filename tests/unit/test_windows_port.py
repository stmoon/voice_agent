"""Windows·Linux 포팅 단위 테스트 (OS 호출은 가짜로). 실제 Windows 실행은 CI(windows-latest)에서 전체 스위트로 검증."""
import os
import subprocess
import sys

import pytest

from bridge import pty_runner, service
from bridge.config import default_config_path, default_state_dir

pytestmark = pytest.mark.req("P7-1")


class FakeRun:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, args, **kw):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, self.stdout, "")


def test_resolve_argv_wraps_batch_files_on_windows():
    which = lambda n, path=None: {"claude": r"C:\Users\u\AppData\Roaming\npm\claude.cmd",
                                  "py": r"C:\Py\python.exe"}.get(n)
    env = {"PATH": "x", "COMSPEC": r"C:\Windows\System32\cmd.exe"}
    assert pty_runner.resolve_argv(["claude", "attach", "ab"], env, "win32", which) == \
        [r"C:\Windows\System32\cmd.exe", "/c", r"C:\Users\u\AppData\Roaming\npm\claude.cmd", "attach", "ab"]
    assert pty_runner.resolve_argv(["py", "f.py"], env, "win32", which) == [r"C:\Py\python.exe", "f.py"]
    assert pty_runner.resolve_argv(["claude", "x"], env, "darwin", which) == ["claude", "x"]


def test_windows_flattens_multiline_prompt(monkeypatch):
    sent = []

    class P:
        def clear_input(self, n):
            pass

        def write(self, d):
            sent.append(d)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(pty_runner.time, "sleep", lambda s: None)
    pty_runner.PtyProcess.send_prompt(P(), "첫 줄\n  둘째 줄\n\n셋째")
    assert sent == ["첫 줄 둘째 줄 셋째", "\r"]   # 붙여넣기 시퀀스 없이 한 줄로


def test_windows_default_paths(monkeypatch):
    monkeypatch.delenv("VC_CONFIG", raising=False)
    monkeypatch.setenv("APPDATA", "/W/Roaming")
    monkeypatch.setenv("LOCALAPPDATA", "/W/Local")
    assert default_config_path("win32").as_posix() == "/W/Roaming/voice-bridge/config.toml"
    assert default_state_dir("win32").as_posix() == "/W/Local/voice-bridge/state"
    assert service.log_dir("win32").as_posix() == "/W/Local/voice-bridge/logs"
    assert default_config_path("darwin").as_posix().endswith(".config/voice-bridge/config.toml")


def test_windows_task_xml(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "LAB")
    monkeypatch.setenv("USERNAME", "moon")
    monkeypatch.setenv("LOCALAPPDATA", "/W/Local")
    x = service.win_task_xml("serve")
    assert "<LogonTrigger><Enabled>true</Enabled><UserId>LAB\\moon</UserId></LogonTrigger>" in x
    assert "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>" in x
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in x
    assert "<LogonType>InteractiveToken</LogonType>" in x and "<RunLevel>LeastPrivilege</RunLevel>" in x
    assert "from bridge.__main__ import main" in x and " serve --log" in x and "serve.log" in x
    assert x.startswith('<?xml version="1.0" encoding="UTF-16"?>')


def test_windows_install_status_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    run = FakeRun()
    done = service._install_windows(run=run, state_dir=tmp_path / "state")
    assert done == ["VoiceBridge\\serve", "VoiceBridge\\tunnel"]
    creates = [c for c in run.calls if c[1] == "/Create"]
    assert len(creates) == 2 and all(c[c.index("/XML") + 1].endswith(".xml") and "/F" in c for c in creates)
    xml = (tmp_path / "state" / "tasks" / "serve.xml").read_bytes()
    assert xml[:2] in (b"\xff\xfe", b"\xfe\xff")   # UTF-16 BOM (schtasks /XML)
    assert [c[1] for c in run.calls].count("/Run") == 2
    st = service._status_windows(run=FakeRun(stdout="serve=Running\ntunnel=Ready\n"))
    assert st == {"VoiceBridge\\serve": "Running", "VoiceBridge\\tunnel": "Ready"}
    st = service._status_windows(run=FakeRun(stdout=""))
    assert set(st.values()) == {"등록 안 됨"}
    run2 = FakeRun()
    assert service._uninstall_windows(run=run2) == ["VoiceBridge\\serve", "VoiceBridge\\tunnel"]
    assert [c[1] for c in run2.calls] == ["/End", "/Delete", "/End", "/Delete"]


def test_linux_systemd_units(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path / "user")
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path))
    u = service.systemd_unit("serve")
    assert "Restart=always" in u and "RestartSec=10" in u and u.split("ExecStart=")[1].startswith('"') and " serve" in u
    run = FakeRun()
    service._install_linux(run=run)
    assert (tmp_path / "user" / "voice-bridge-serve.service").exists()
    assert ["systemctl", "--user", "enable", "--now", "voice-bridge-serve.service", "voice-bridge-tunnel.service"] in run.calls
    assert service._status_linux(run=FakeRun(stdout="active\n")) == {
        "voice-bridge-serve.service": "active", "voice-bridge-tunnel.service": "active"}
    removed = service._uninstall_linux(run=FakeRun())
    assert len(removed) == 2 and not list((tmp_path / "user").iterdir())


def test_install_dispatches_by_platform(monkeypatch):
    seen = []
    monkeypatch.setattr(service, "_install_windows", lambda run: seen.append("win") or [])
    monkeypatch.setattr(service, "_install_linux", lambda run: seen.append("linux") or [])
    monkeypatch.setattr(service, "_install_darwin", lambda run: seen.append("mac") or [])
    for p in ("win32", "linux", "darwin"):
        service.install(platform=p)
    assert seen == ["win", "linux", "mac"]


def test_windows_notification_does_not_block():
    started = []
    service.notify("음성 브리지 주소가 바뀌었습니다", "abc's host", popen=lambda args, **kw: started.append((args, kw)),
                   platform="win32")
    (args, kw), = started
    assert args[0] == "powershell" and "BalloonTipText = 'abc''s host'" in args[-1]   # 작은따옴표 이스케이프


def test_log_option_redirects_output(tmp_path):
    log = tmp_path / "logs" / "info.log"
    from bridge.__main__ import _redirect_output

    old = sys.stdout, sys.stderr
    try:
        _redirect_output(str(log))
        print("hello log")
        sys.stdout.flush()
    finally:
        sys.stdout.close()
        sys.stdout, sys.stderr = old
    assert "hello log" in log.read_text(encoding="utf-8")
    r = subprocess.run([sys.executable, "-c", "from bridge.__main__ import main; import sys; sys.argv=['x']; "
                        "import argparse; main(['tunnel', '--help'])"], capture_output=True, text=True)
    assert "--log" in r.stdout


def test_hook_command_uses_forward_slashes(tmp_path):
    from bridge.hook_sink import build_settings

    cmd = build_settings(sys.executable, str(tmp_path / "e.jsonl"))["hooks"]["Stop"][0]["hooks"][0]["command"]
    parts = cmd.split('" "')
    assert len(parts) == 3 and "\\" not in cmd and cmd.startswith('"') and cmd.endswith('"')
