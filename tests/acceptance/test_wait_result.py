"""결과가 나오면 명령을 보낸 대화에 바로 보고되도록: send_command 기다림 + get_result(wait) (2026-10-04 사용자 요청).
MCP 서버는 대화에 먼저 말을 걸 수 없으므로, 휴대폰 Claude 가 기다리는 호출을 반복해 결과가 나오는 즉시 받는다."""
import json
import time

import pytest
from mcp import Client

from bridge import transcript as tr
from bridge.mcp_server import build_mcp
from tests.conftest import wait_until

pytestmark = pytest.mark.req("P4-2")


def started(manager):
    manager.start_session("테스트 세션")
    manager.select_session("테스트 세션")


def test_send_waits_and_returns_result(manager):
    started(manager)
    t = time.time()
    d = manager.send_command("hello", wait=True)
    assert d["final"] and d["status"] == tr.DONE and d["response"] == "ECHO: hello"
    assert time.time() - t < manager.config.wait_timeout  # 끝나는 즉시 돌아옴


def test_long_task_keeps_waiting_until_done(manager):
    started(manager)
    d = manager.send_command("PAUSE then answer", wait=True)          # 4초 작업, 기다림 상한 3초
    assert d["final"] is False and d["status"] == tr.WORKING and "wait=true" in d["next"]
    for _ in range(5):
        d = manager.wait_result(d["request_id"])
        if d["final"]:
            break
    assert d["final"] and d["response"] == "PAUSED DONE"


def test_approval_is_reported_once_then_waits(manager):
    manager.config.wait_timeout = 6  # 훅 실행이 느린 환경(Windows CI)에서도 '즉시 알림' 판단이 흔들리지 않게
    started(manager)
    s = manager.get("테스트 세션")
    t = time.time()
    d = manager.send_command("PERM rm", wait=True)
    assert d["status"] == tr.AWAITING_APPROVAL and not d["final"]
    assert time.time() - t < manager.config.wait_timeout              # 승인 필요해지면 바로 알림
    t = time.time()
    d2 = manager.wait_result(d["request_id"])                          # 이미 알린 승인 대기 → 즉시 되돌리지 않음
    assert d2["status"] == tr.AWAITING_APPROVAL and time.time() - t >= manager.config.wait_timeout * 0.8
    s.proc.write("y")                                                  # 사람이 Code 탭에서 승인
    d3 = manager.wait_result(d["request_id"])
    assert d3["final"] and d3["response"] == "APPROVED"


def test_approval_then_long_tool_then_result(manager):
    started(manager)
    s = manager.get("테스트 세션")
    d = manager.send_command("PERM SLOW rm", wait=True)
    assert d["status"] == tr.AWAITING_APPROVAL
    s.proc.write("y")
    seen = []
    for _ in range(6):
        d = manager.wait_result(d["request_id"])
        seen.append(d["status"])
        if d["final"]:
            break
    assert d["final"] and d["response"] == "APPROVED"
    assert tr.AWAITING_APPROVAL not in seen   # 승인 뒤 실행 중엔 다시 '승인 대기'로 보고하지 않음


def test_interrupted_while_waiting_returns_final(manager):
    started(manager)
    d = manager.send_command("THINK long", wait=False)
    assert wait_until(lambda: manager.get_result(d["request_id"])["status"] == tr.WORKING)
    manager.interrupt()
    for _ in range(8):
        r = manager.wait_result(d["request_id"])
        if r["final"]:
            break
    assert r["final"] and r["status"] == tr.INTERRUPTED


async def test_mcp_send_command_returns_result_directly(manager):
    started(manager)
    async with Client(build_mcp(manager)) as c:
        r = json.loads((await c.call_tool("send_command", {"text": "via mcp"})).content[0].text)
        assert r["final"] and r["response"] == "ECHO: via mcp"
        r = json.loads((await c.call_tool("send_command", {"text": "PAUSE x"})).content[0].text)
        assert not r["final"]
        for _ in range(5):
            r = json.loads((await c.call_tool("get_result", {"request_id": r["request_id"], "wait": True})).content[0].text)
            if r["final"]:
                break
        assert r["response"] == "PAUSED DONE"
        r2 = json.loads((await c.call_tool("send_command", {"text": "no wait", "wait": False})).content[0].text)
        assert r2["status"] == tr.WORKING and "final" not in r2
