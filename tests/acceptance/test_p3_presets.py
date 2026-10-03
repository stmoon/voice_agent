"""세션 프리셋 추가(voice-bridge add)와 재시작 없는 반영 — P3-4 세션 시작 방식의 보강."""
import json
import tomllib

import pytest

from bridge import transcript as tr
from bridge.config import BridgeConfig, add_preset, claude_trusts

pytestmark = pytest.mark.req("P3-4")

BASE = '''# 주석 유지 확인
port = 8765

# 자동 기동
autostart = ["시험"]

allowed_hosts = ["*.trycloudflare.com"]

[sessions]
"시험" = "~/Project/voice_commander/sandbox/live"
# "로그 플랫폼" = "~/Project/logplatform"
'''


def test_add_preset_keeps_comments_and_order(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(BASE)
    add_preset(p, "로그 플랫폼", "/Users/x/logplatform")
    text = p.read_text()
    d = tomllib.loads(text)
    assert d["sessions"] == {"시험": "~/Project/voice_commander/sandbox/live", "로그 플랫폼": "/Users/x/logplatform"}
    assert d["autostart"] == ["시험"]
    assert "# 주석 유지 확인" in text and '# "로그 플랫폼" = "~/Project/logplatform"' in text


def test_add_preset_autostart_and_replace(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(BASE)
    add_preset(p, "논문", "/a", autostart=True)
    add_preset(p, "논문", "/b", autostart=True)  # 같은 이름 → 폴더 교체, autostart 중복 없음
    d = tomllib.loads(p.read_text())
    assert d["sessions"]["논문"] == "/b" and d["autostart"] == ["시험", "논문"]
    assert list(d["sessions"]).count("논문") == 1


def test_add_preset_quotes_and_new_file(tmp_path):
    p = tmp_path / "new.toml"
    add_preset(p, 'IMM "EKF" 논문', r"C:\Users\x\paper", autostart=True)
    d = tomllib.loads(p.read_text())
    assert d["sessions"] == {'IMM "EKF" 논문': r"C:\Users\x\paper"} and d["autostart"] == ['IMM "EKF" 논문']


def test_running_bridge_picks_up_new_preset_without_restart(manager, tmp_path, workdir):
    cfg_file = tmp_path / "live.toml"
    cfg_file.write_text("[sessions]\n")
    manager.config.source = cfg_file
    manager.config.sessions = {}
    add_preset(cfg_file, "새 세션", str(workdir))  # 실행 중에 추가
    s = manager.start_session("새 세션")
    assert s.ready and s.info()["status"] == tr.IDLE


def test_claude_trust_lookup(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # Windows 는 HOME 대신 USERPROFILE
    (tmp_path / "proj" / "sub").mkdir(parents=True)
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"projects": {str((tmp_path / "proj").resolve()): {"hasTrustDialogAccepted": True}}}))
    assert claude_trusts(str(tmp_path / "proj" / "sub")) is True   # 상위 폴더 신뢰 상속
    assert claude_trusts(str(tmp_path)) is False
    (tmp_path / ".claude.json").write_text("not json")
    assert claude_trusts(str(tmp_path)) is None


def test_load_remembers_source(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(BASE)
    cfg = BridgeConfig.load(p)
    assert cfg.source == p and "시험" in cfg.sessions
