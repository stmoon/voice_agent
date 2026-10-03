"""사용자용 명령: voice-bridge session new/list/stop, setup (가짜 claude)."""
import json
import sys

import pytest

from bridge import sessions_cli as sc
from bridge.config import BridgeConfig
from tests.conftest import FAKE

pytestmark = pytest.mark.req("P3-4")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))   # 가짜 등록부 위치 (child_env 가 물려줌)
    monkeypatch.setattr(sc, "claude_trusts", lambda p: True)
    return BridgeConfig(host_id="t", state_dir=tmp_path / "s", claude_bin=[sys.executable, str(FAKE)],
                        claude_config_dir=tmp_path / "c")


def reg(cfg):
    d = cfg.claude_config_dir / "fake_agents"
    return [json.loads(f.read_text(encoding="utf-8")) for f in d.glob("*.json")] if d.exists() else []


def test_new_session_reports_ready(cfg, tmp_path, capsys):
    (tmp_path / "w").mkdir()
    assert sc.session_new(cfg, "위키", str(tmp_path / "w"), wait=10) == 0
    out = capsys.readouterr().out
    assert "backgrounded" in out and "준비됨" in out and "위키 세션으로 하자" in out
    (e,) = reg(cfg)
    assert e["name"] == "위키" and e["cwd"] == str((tmp_path / "w").resolve())


def test_new_session_options_and_duplicates(cfg, tmp_path, capsys):
    (tmp_path / "w").mkdir()
    assert sc.session_new(cfg, "승인", str(tmp_path / "w"), approve=True, chrome=True, wait=10) == 0
    assert reg(cfg)[0]["mode"] == "default"
    assert sc.session_new(cfg, "승인", str(tmp_path / "w"), wait=1) == 1   # 같은 이름 중복 방지
    assert "이미 있습니다" in capsys.readouterr().out


def test_new_session_refuses_untrusted_or_missing_folder(cfg, tmp_path, monkeypatch, capsys):
    assert sc.session_new(cfg, "x", str(tmp_path / "없음"), wait=1) == 1
    monkeypatch.setattr(sc, "claude_trusts", lambda p: False)
    (tmp_path / "w").mkdir()
    assert sc.session_new(cfg, "x", str(tmp_path / "w"), wait=1) == 1
    assert "신뢰" in capsys.readouterr().out and reg(cfg) == []


def test_list_and_stop(cfg, tmp_path, capsys):
    (tmp_path / "w").mkdir()
    sc.session_new(cfg, "위키", str(tmp_path / "w"), wait=10)
    capsys.readouterr()
    assert sc.session_list(cfg, ask_bridge=False) == 0
    assert "위키" in capsys.readouterr().out
    assert sc.session_stop(cfg, "위키") == 0 and reg(cfg) == []
    assert sc.session_stop(cfg, "위키") == 1


def test_setup_creates_token_once_and_prints_url(cfg, monkeypatch, capsys):
    import keyring
    from keyring.backend import KeyringBackend

    from bridge import __main__ as cli
    from bridge import service

    class M(KeyringBackend):
        priority = 1
        store = {}

        def get_password(self, s, u):
            return self.store.get((s, u))

        def set_password(self, s, u, p):
            self.store[(s, u)] = p

        def delete_password(self, s, u):
            self.store.pop((s, u), None)

    old = keyring.get_keyring()
    keyring.set_keyring(M())
    try:
        monkeypatch.setattr(sc.shutil, "which", lambda n: None)
        assert sc.setup(cfg, wait=1) == 2 and "cloudflared" in capsys.readouterr().err
        monkeypatch.setattr(sc.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setattr(service, "install", lambda: ["agent-a", "agent-b"])
        monkeypatch.setattr(cli, "tunnel_hosts", lambda c: ["abc.trycloudflare.com"])
        assert sc.setup(cfg, wait=1) == 0
        out = capsys.readouterr().out
        assert "이미 있음" in out and "https://abc.trycloudflare.com/t/" in out   # 두 번째엔 토큰을 새로 만들지 않음
        assert len(M.store) == 1
    finally:
        keyring.set_keyring(old)


def test_list_prefers_running_bridge(cfg, monkeypatch, capsys):
    rows = [{"name": "시험", "kind": "bridge", "kind_ko": "브리지 세션", "status_ko": "대기", "commandable": True,
             "permission_mode": "default", "permission_mode_ko": "승인 필요"},
            {"name": "데스크톱", "kind": "desktop", "kind_ko": "데스크톱 앱·터미널 세션", "status_ko": "대기",
             "commandable": False, "reason": "아주 긴 안내 문구"}]
    monkeypatch.setattr(sc, "_bridge_list", lambda c: (rows, None))
    assert sc.session_list(cfg) == 0
    out = capsys.readouterr().out
    assert "시험  [브리지 세션] 대기 · 음성 명령 가능 · 승인 필요" in out
    assert "데스크톱  [데스크톱 앱·터미널 세션] 대기 · 보기 전용" in out and "아주 긴 안내" not in out
    assert "경고" not in out


# ---- Windows 실기에서 나온 문제 (2026-10-04) ----

MISSING = ["vb-no-such-claude-cli"]


def test_trust_hint_works_in_powershell(cfg, tmp_path, monkeypatch, capsys):
    """PowerShell 5.1 은 `&&` 를 못 쓴다 → 안내 명령을 그대로 붙여 넣어도 되도록 두 줄로."""
    monkeypatch.setattr(sc, "claude_trusts", lambda p: False)
    (tmp_path / "w").mkdir()
    assert sc.session_new(cfg, "x", str(tmp_path / "w"), wait=1) == 1
    out = capsys.readouterr().out
    assert "&&" not in out and f'cd "{(tmp_path / "w").resolve()}"' in out and "\n  claude" in out


def test_new_session_without_claude_explains(cfg, tmp_path, capsys):
    (tmp_path / "w").mkdir()
    cfg.claude_bin = MISSING
    assert sc.session_new(cfg, "x", str(tmp_path / "w"), wait=1) == 1   # traceback 이 아니라 안내
    assert "찾을 수 없습니다" in capsys.readouterr().err


def test_list_shows_warning_instead_of_silent_empty(cfg, monkeypatch, capsys):
    cfg.claude_bin = MISSING
    assert sc.session_list(cfg, ask_bridge=False) == 0
    out = capsys.readouterr().out
    assert "경고: 이 머신의 세션 목록을 읽지 못했습니다" in out and "찾을 수 없습니다" in out
    monkeypatch.setattr(sc, "_bridge_list", lambda c: ([], "claude CLI 가 너무 오래되었습니다"))
    assert sc.session_list(cfg) == 0
    assert "경고: claude CLI 가 너무 오래되었습니다" in capsys.readouterr().out


def test_new_session_with_invalid_login_says_login_now(cfg, tmp_path, monkeypatch, capsys):
    """로그인이 무효면 RC 는 기다려도 붙지 않는다 → 바로 `claude /login` 을 안내하고, '다시 만드세요' 는 말하지 않는다."""
    monkeypatch.setenv("FAKE_LOGIN_INVALID", "1")
    (tmp_path / "w").mkdir()
    import time

    t = time.time()
    assert sc.session_new(cfg, "만료", str(tmp_path / "w"), wait=30) == 2
    assert time.time() - t < 20
    out = capsys.readouterr().out
    assert "claude /login" in out and "다시 만드세요" not in out
