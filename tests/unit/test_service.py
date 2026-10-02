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
    def __init__(self, lines):
        self.stdout = iter(lines)

    def wait(self):
        return 0


LINES = [
    "INF Requesting new quick Tunnel on trycloudflare.com...\n",
    "INF |  https://api.trycloudflare.com  |\n",
    "INF |  https://fusion-topics-such-rotary.trycloudflare.com   |\n",
    "INF Registered tunnel connection\n",
]


def test_tunnel_records_host_and_notifies_on_change(tmp_path):
    run = FakeRun()
    out = io.StringIO()
    service.run_tunnel(8765, tmp_path, popen=lambda *a, **k: FakePopen(LINES), run=run, out=out)
    assert service.host_file(tmp_path).read_text().strip() == "fusion-topics-such-rotary.trycloudflare.com"
    notes = [c for c in run.calls if c[0] == "osascript"]
    if sys.platform == "darwin":
        assert len(notes) == 1 and "fusion-topics-such-rotary" in notes[0][2]
        assert "/t/" not in notes[0][2]  # 알림에 토큰 없음
    # 같은 주소로 다시 뜨면 알림 없음
    run2 = FakeRun()
    service.run_tunnel(8765, tmp_path, popen=lambda *a, **k: FakePopen(LINES), run=run2, out=io.StringIO())
    assert not [c for c in run2.calls if c[0] == "osascript"]
