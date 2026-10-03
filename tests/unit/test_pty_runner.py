"""pty_runner 단위 테스트 (가짜 claude). P1 체크는 live 테스트가 담당."""
import sys

from bridge.pty_runner import PtyProcess, child_env, strip_ansi
from tests.conftest import FAKE, wait_until


def test_strip_ansi():
    assert strip_ansi("\x1b[2m? for\x1b[0m shortcuts\x1b]0;title\x07") == "? for shortcuts"


def test_child_env_drops_parent_session_markers(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    env = child_env({"X": "1"})
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_CHILD_SESSION" not in env
    assert env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] == "1" and env["X"] == "1"


def test_spawn_write_and_terminate(tmp_path):
    p = PtyProcess([sys.executable, str(FAKE), "--remote-control", "t", "--permission-mode", "default"], cwd=str(tmp_path),
                   env=child_env({"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}))
    assert wait_until(lambda: "for shortcuts" in p.screen(), timeout=10)
    m = p.mark()
    p.send_prompt("hello")
    assert wait_until(lambda: "for shortcuts" in p.screen_since(m), timeout=10)
    files = list((tmp_path / "cfg" / "projects").glob("*/*.jsonl"))
    assert files and "ECHO: hello" in files[0].read_text()
    p.terminate()
    assert not p.is_alive()
