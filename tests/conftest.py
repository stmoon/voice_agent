import os
import shutil
import sys
from pathlib import Path

import pytest

from bridge.config import BridgeConfig
from bridge.session_manager import SessionManager

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"
FIXTURES = ROOT / "tests" / "fixtures"

LIVE = os.environ.get("VC_LIVE") == "1"


def pytest_collection_modifyitems(config, items):
    # JUnit XML 에 req ID 를 property 로 남긴다 → tools/notion_sync.py 가 읽음
    for item in items:
        for m in item.iter_markers("req"):
            for rid in m.args:
                item.user_properties.append(("req", rid))
    if LIVE:
        return
    skip = pytest.mark.skip(reason="실제 claude 필요: VC_LIVE=1 로 실행")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return d


@pytest.fixture
def fake_config(tmp_path, workdir):
    cfg = BridgeConfig(
        host_id="testhost",
        state_dir=tmp_path / "state",
        claude_bin=[sys.executable, str(FAKE)],
        claude_config_dir=tmp_path / "claude_cfg",
        sessions={"테스트 세션": str(workdir), "다른 세션": str(workdir)},
        ready_timeout=15,
        inject_ack_timeout=10,
        wait_timeout=3,
    )
    return cfg


@pytest.fixture
def manager(fake_config):
    m = SessionManager(fake_config)
    yield m
    m.shutdown()


@pytest.fixture
def live_workdir():
    """실제 claude 용 작업 폴더. 신뢰된 프로젝트(이 레포) 아래 sandbox 를 쓴다."""
    d = ROOT / "sandbox" / "live"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def live_manager(tmp_path, live_workdir):
    if not shutil.which("claude"):
        pytest.fail("claude CLI 없음")
    cfg = BridgeConfig(host_id="livehost", state_dir=tmp_path / "state",
                       sessions={"vc-live": str(live_workdir)}, ready_timeout=90, inject_ack_timeout=30)
    m = SessionManager(cfg)
    yield m
    m.shutdown()


def wait_until(fn, timeout=20.0, interval=0.2):
    import time

    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    return last
