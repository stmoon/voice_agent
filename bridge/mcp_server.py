"""MCP 서버: 도구 5개(list_sessions, select_session, send_command, get_result, interrupt)
+ 프리셋 세션 기동용 start_session.
"""
from __future__ import annotations

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .auth import TokenAuthMiddleware
from .config import BridgeConfig
from .session_manager import BridgeError, SessionManager

INSTRUCTIONS = """\
이 서버는 한 머신에서 bridge 가 띄운 Claude Code Remote Control(RC) 세션에 명령을 넣고 결과를 받는다.
규칙:
1. 세션 지정은 사용자가 명시적으로 한다. 추론해서 고르지 말 것.
2. list_sessions 로 후보를 보여주고 "○○ 세션, 폴더 △△ - 맞습니까?" 로 확인을 받은 뒤 select_session 으로 고정.
3. 고정 후에는 send_command / get_result 로만 진행. 세션 변경은 사용자가 다시 지정할 때만.
4. send_command 는 세션이 '대기' 일 때만 들어간다. 거부되면 상태만 알리고 재시도·큐잉하지 말 것.
5. get_result 가 '승인 대기' 면 "승인이 필요합니다. Code 탭에서 확인해 주세요" 라고 알릴 것. 대신 승인하지 않는다.
6. 사용자가 "멈춰" 하면 interrupt.
결과 보고는 음성용으로 짧게 요약한다.
"""


def build_mcp(manager: SessionManager) -> MCPServer:
    mcp = MCPServer(name=f"voice-bridge-{manager.config.host_id}", instructions=INSTRUCTIONS, version="0.1.0")

    import logging

    log = logging.getLogger("uvicorn.error")

    async def call(fn, *a):
        name = getattr(fn, "__name__", "?")
        try:
            r = await anyio.to_thread.run_sync(fn, *a)
            log.info("[tool] %s → ok", name)
            return r
        except BridgeError as e:
            log.info("[tool] %s → %s", name, e.code)
            return e.to_dict()

    @mcp.tool()
    async def list_sessions() -> dict:
        """이 머신에서 bridge 가 띄운 RC 세션 목록 (이름, 작업 폴더, 호스트명, 상태: 대기/작업 중/승인 대기)."""
        sessions = await call(manager.list_sessions)
        return {"ok": True, "host": manager.config.host_id, "sessions": sessions,
                "presets": sorted(manager.config.sessions)}

    @mcp.tool()
    async def select_session(name: str) -> dict:
        """대상 세션 1개를 서버 측에 고정한다. 사용자에게 확인을 받은 뒤에만 호출."""
        r = await call(manager.select_session, name)
        return r if r.get("ok") is False else {"ok": True, "selected": r}

    @mcp.tool()
    async def send_command(text: str) -> dict:
        """고정된 세션에 명령(프롬프트)을 주입한다. 세션이 대기 상태가 아니면 거부하고 상태를 돌려준다. 반환: request_id."""
        return await call(manager.send_command, text)

    @mcp.tool()
    async def get_result(request_id: str) -> dict:
        """요청 ID 의 진행 상태(완료/작업 중/승인 대기/중단됨)와, 끝났으면 최종 응답."""
        return await call(manager.get_result, request_id)

    @mcp.tool()
    async def interrupt() -> dict:
        """고정된 세션의 현재 작업을 중단한다 (Esc 주입). 사용자가 '멈춰' 라고 할 때."""
        return await call(manager.interrupt)

    @mcp.tool()
    async def start_session(name: str) -> dict:
        """설정에 등록된 프리셋 이름으로 RC 세션을 띄운다 (임의 폴더 불가). 목록은 list_sessions 의 presets."""
        def run():
            return manager.start_session(name).info()
        r = await call(run)
        return r if r.get("ok") is False else {"ok": True, "session": r}

    return mcp


def build_app(manager: SessionManager, token):
    cfg: BridgeConfig = manager.config
    mcp = build_mcp(manager)
    # Host/Origin 검사는 TokenAuthMiddleware 가 한다 (라이브러리 검사는 "*.trycloudflare.com" 같은 와일드카드를 못 씀)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security, host=cfg.bind)
    return TokenAuthMiddleware(app, token, allowed_hosts=cfg.allowed_hosts)
