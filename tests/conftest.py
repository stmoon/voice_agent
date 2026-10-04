import os
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


@pytest.fixture(scope="session")
def live_claude():
    """실제 claude CLI: PATH 의 claude, 또는 VC_CLAUDE=<전체 경로>. 브리지와 같은 점검(경로·버전·세션 조회)을 한 번 하고,
    문제가 있으면 무엇을 하면 되는지 알려 주며 실패한다."""
    from bridge.agents import claude_doctor
    from bridge.pty_runner import child_env

    claude_bin = [os.environ["VC_CLAUDE"]] if os.environ.get("VC_CLAUDE") else ["claude"]
    doc = claude_doctor(claude_bin, env=child_env())
    if doc["problem"]:
        pytest.fail(f"{doc['problem']}\n(live 테스트는 PATH 의 claude 를 쓴다. 다른 claude 는 VC_CLAUDE=<전체 경로> 로 지정)",
                    pytrace=False)
    return claude_bin


@pytest.fixture
def live_manager(tmp_path, live_workdir, live_claude):
    cfg = BridgeConfig(host_id="livehost", state_dir=tmp_path / "state", claude_bin=list(live_claude),
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
