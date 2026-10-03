"""자동 실행(launchd)·터널 래퍼 단위 테스트."""
import io
import plistlib
import subprocess
import sys

import pytest

from bridge import service

pytestmark = pytest.mark.req("P7-1")


def test_plists_run_serve_and_tunnel_with_keepalive():
    pl = service.plists()
    serve, tunnel = pl[service.LABEL_SERVE], pl[service.LABEL_TUNNEL]
    assert serve["ProgramArguments"][1:] == ["serve"] and tunnel["ProgramArguments"][1:] == ["tunnel"]
    for p in (serve, tunnel):
        assert p["KeepAlive"] is True and p["RunAtLoad"] is True
        assert p["LimitLoadToSessionType"] == "Aqua"
        assert "/opt/homebrew/bin" in p["EnvironmentVariables"]["PATH"]
        assert p["StandardOutPath"].endswith(".log")
        plistlib.dumps(p)  # 직렬화 가능


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(args)
        rc = 0
        out = ""
        if args[:2] == ["launchctl", "print"]:
            out = "\tstate = running\n\tpid = 4242\n"
        return subprocess.CompletedProcess(args, rc, out, "")


def test_install_writes_plists_and_bootstraps(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(sys, "platform", "darwin")
    run = FakeRun()
    paths = service.install(run=run)
    assert len(paths) == 2 and all((tmp_path / "agents").joinpath(p.split("/")[-1]).exists() for p in paths)
    verbs = [c[1] for c in run.calls]
    assert verbs.count("bootstrap") == 2 and verbs.count("enable") == 2
    assert service.status(run=run)[service.LABEL_SERVE] == "running (pid 4242)"
    removed = service.uninstall(run=run)
    assert len(removed) == 2 and not list((tmp_path / "agents").iterdir())


class FakePopen:
    """subprocess.Popen 대역. 프로세스 핸들(_handle)이 없다 → Windows Job Object 등록은 건너뛰어야 한다."""

    def __init__(self, lines):
        self.stdout = iter(lines)

    def wait(self):
        return 0


class FakePopenFactory:
    """cloudflared 는 LINES 를 출력하고, 그 밖의 실행(Windows 트레이 알림 powershell)은 기록만 한다."""

    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(args)
        return FakePopen(self.lines if args[0] == "cloudflared" else [])

    def notes(self):
        return [c for c in self.calls if c[0] == "powershell"]


LINES = [
    "INF Requesting new quick Tunnel on trycloudflare.com...\n",
    "INF |  https://api.trycloudflare.com  |\n",
    "INF |  https://fusion-topics-such-rotary.trycloudflare.com   |\n",
    "INF Registered tunnel connection\n",
]


def _notes(run, popen):
    """플랫폼별 알림 호출: macOS osascript(run), Windows powershell(popen), Linux notify-send(run)."""
    if sys.platform == "darwin":
        return [c for c in run.calls if c[0] == "osascript"]
    if sys.platform == "win32":
        return popen.notes()
    return [c for c in run.calls if c[0] == "notify-send"]


def test_tunnel_records_host_and_notifies_on_change(tmp_path):
    run, popen = FakeRun(), FakePopenFactory(LINES)
    out = io.StringIO()
    service.run_tunnel(8765, tmp_path, popen=popen, run=run, out=out)
    assert service.host_file(tmp_path).read_text().strip() == "fusion-topics-such-rotary.trycloudflare.com"
    notes = _notes(run, popen)
    if sys.platform in ("darwin", "win32"):
        assert len(notes) == 1 and "fusion-topics-such-rotary" in notes[0][-1]
        assert "/t/" not in notes[0][-1]  # 알림에 토큰 없음
    # 같은 주소로 다시 뜨면 알림 없음
    run2, popen2 = FakeRun(), FakePopenFactory(LINES)
    service.run_tunnel(8765, tmp_path, popen=popen2, run=run2, out=io.StringIO())
    assert not _notes(run2, popen2)
