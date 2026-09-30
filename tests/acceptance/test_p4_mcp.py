"""P4 MCP 서버 인수 테스트 (가짜 claude). 실제 claude 종단 테스트는 live 표시."""
import asyncio
import json
import socket
import threading
import time

import pytest
from mcp import Client

from bridge import transcript as tr
from bridge.mcp_server import build_app, build_mcp


def payload(result):
    assert not result.is_error, result
    return json.loads(result.content[0].text)


async def call(client, _tool, **args):
    return payload(await client.call_tool(_tool, args))


async def poll_result(client, rid, until=(tr.DONE, tr.INTERRUPTED), timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        r = await call(client, "get_result", request_id=rid)
        if r.get("status") in until:
            return r
        await asyncio.sleep(0.3)
    raise AssertionError(f"timeout: {r}")


@pytest.mark.req("P4-1")
async def test_list_and_select_tools(manager, workdir):
    manager.start_session("테스트 세션")
    async with Client(build_mcp(manager)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"list_sessions", "select_session", "send_command", "get_result", "interrupt"} <= names
        lst = await call(c, "list_sessions")
        assert lst["host"] == "testhost"
        assert [s["name"] for s in lst["sessions"]] == ["테스트 세션"]
        s = lst["sessions"][0]
        assert s["folder"] == str(workdir.resolve()) and s["status_ko"] == "대기"
        assert "다른 세션" in lst["presets"]
        sel = await call(c, "select_session", name="테스트 세션")
        assert sel["ok"] and sel["selected"]["name"] == "테스트 세션"
        bad = await call(c, "select_session", name="없는 세션")
        assert bad["ok"] is False and bad["error"] == "no_session"


@pytest.mark.req("P4-2")
async def test_send_command_and_get_result_tools(manager):
    manager.start_session("테스트 세션")
    async with Client(build_mcp(manager)) as c:
        refused = await call(c, "send_command", text="hi")
        assert refused["ok"] is False and refused["error"] == "no_selection"
        await call(c, "select_session", name="테스트 세션")
        sent = await call(c, "send_command", text="안녕")
        assert sent["ok"] and sent["request_id"]
        done = await poll_result(c, sent["request_id"])
        assert done["status"] == tr.DONE and done["status_ko"] == "완료"
        assert done["response"] == "ECHO: 안녕"
        # 승인 대기 → 거부 → (사람 승인) → 완료
        s = manager.get("테스트 세션")
        p = await call(c, "send_command", text="PERM x")
        w = await poll_result(c, p["request_id"], until=(tr.AWAITING_APPROVAL,))
        assert w["status_ko"] == "승인 대기"
        busy = await call(c, "send_command", text="more")
        assert busy["ok"] is False and busy["error"] == "busy"
        s.proc.write("y")
        assert (await poll_result(c, p["request_id"]))["response"] == "APPROVED"
        missing = await call(c, "get_result", request_id="nope")
        assert missing["ok"] is False


@pytest.mark.req("P4-3")
async def test_interrupt_tool(manager):
    manager.start_session("테스트 세션")
    async with Client(build_mcp(manager)) as c:
        await call(c, "select_session", name="테스트 세션")
        idle = await call(c, "interrupt")
        assert idle["message"] == "진행 중인 작업이 없습니다."
        sent = await call(c, "send_command", text="SLOW long job")
        await poll_result(c, sent["request_id"], until=(tr.WORKING,))
        r = await call(c, "interrupt")
        assert r["ok"]
        done = await poll_result(c, sent["request_id"])
        assert done["status"] == tr.INTERRUPTED and done["status_ko"] == "중단됨"
        lst = await call(c, "list_sessions")
        assert lst["sessions"][0]["status"] == tr.IDLE


@pytest.mark.req("P3-4")
async def test_start_session_tool_presets_only(manager):
    async with Client(build_mcp(manager)) as c:
        bad = await call(c, "start_session", name="/etc")
        assert bad["ok"] is False and bad["error"] == "unknown_preset"
        ok = await call(c, "start_session", name="테스트 세션")
        assert ok["ok"] and ok["session"]["status"] == tr.IDLE


# ---------- HTTP (로컬 MCP 클라이언트 → 인증 → 서버) ----------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, app, port):
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.t = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.t.start()
        end = time.time() + 10
        while not self.server.started and time.time() < end:
            time.sleep(0.05)
        return self

    def __exit__(self, *a):
        self.server.should_exit = True
        self.t.join(5)


@pytest.fixture
def http_server(manager):
    port = free_port()
    with Server(build_app(manager, "secret-token"), port):
        yield f"http://127.0.0.1:{port}"


async def e2e(url_or_transport, manager):
    async with Client(url_or_transport) as c:
        await call(c, "start_session", name="테스트 세션")
        await call(c, "select_session", name="테스트 세션")
        sent = await call(c, "send_command", text="e2e check")
        return await poll_result(c, sent["request_id"])


async def test_http_e2e_with_path_token(http_server, manager):
    done = await e2e(f"{http_server}/t/secret-token/mcp", manager)
    assert done["response"] == "ECHO: e2e check"


@pytest.mark.live
@pytest.mark.req("P4-4")
async def test_local_mcp_client_to_real_claude(live_manager):
    port = free_port()
    with Server(build_app(live_manager, "live-token"), port):
        async with Client(f"http://127.0.0.1:{port}/t/live-token/mcp") as c:
            st = await call(c, "start_session", name="vc-live")
            assert st["ok"], st
            await call(c, "select_session", name="vc-live")
            sent = await call(c, "send_command", text="Reply with exactly the word PONG and nothing else.")
            done = await poll_result(c, sent["request_id"], timeout=120)
            assert done["status"] == tr.DONE
            assert not done.get("api_error"), done
            assert "PONG" in done["response"]
