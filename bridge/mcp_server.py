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


def _version() -> str:
    """실행 중인 코드의 git 커밋 (예전 코드가 응답하는지 휴대폰에서도 알 수 있게)."""
    import subprocess
    from importlib.metadata import PackageNotFoundError, version
    from pathlib import Path

    # bridge/__init__.py 에 기대지 않는다: 편집 가능 설치를 레포 밖(launchd 작업 폴더=홈)에서 쓰면
    # bridge 가 네임스페이스 패키지로 잡혀 __init__.py 가 실행되지 않는다 (실측: 재시작 루프)
    try:
        __version__ = version("voice-bridge")
    except PackageNotFoundError:
        __version__ = "0.0.0"
    from .proc import run_quiet

    try:
        h = run_quiet(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
                      text=True, timeout=5).stdout.strip()
    except Exception:
        h = ""
    return f"{__version__}+{h}" if h else __version__


BRIDGE_VERSION = _version()

INSTRUCTIONS = """\
이 서버는 한 머신의 Claude Code 세션에 명령을 넣고 결과를 받는다.
세션 종류 (list_sessions 의 kind):
- bridge: bridge 가 띄운 세션 — 명령 가능
- background: 사용자가 claude --bg -n "이름" --remote-control "이름" 으로 띄운 세션 — 명령 가능 (권한 모드가 허용 목록일 때)
- desktop: 데스크톱 앱·다른 터미널 세션 — 보기 전용. 명령을 넣을 수 없으니 reason 대로 안내
목록을 읽어 줄 때는 명령 불가 세션도 이름과 reason 을 함께 짧게 알린다. warning 이 있으면 그것부터 알린다.

규칙:
1. 세션 지정은 사용자가 명시적으로 한다. 추론해서 고르지 말 것.
2. list_sessions 로 후보를 보여주고 "○○ 세션, 폴더 △△ - 맞습니까?" 로 확인. 예 → select_session 으로 고정, 아니오 → 다시 조회.
   permission_mode 가 auto 인 세션이면 고정할 때 "자동 모드 세션입니다" 라고 한 번 알린다.
3. 고정 후에는 send_command / get_result 로만 진행. 세션 변경은 사용자가 다시 지정할 때만.
4. 결과는 나오는 즉시 이 대화에서 보고한다 (사용자가 "어떻게 됐어?" 라고 묻게 하지 말 것):
   - send_command 는 결과가 나올 때까지(최대 약 50초) 기다렸다가 결과를 함께 돌려준다. final=true 면 바로 요약해 보고.
   - final=false(아직 작업 중)면 "작업 중입니다. 끝나면 바로 알려드릴게요" 라고 짧게 말하고, 사용자에게 묻지 말고 곧바로
     get_result(request_id, wait=true) 를 final=true 가 될 때까지 반복 호출한 뒤 결과를 보고한다.
   - 상태가 '승인 대기' 면 "승인이 필요합니다. Code 탭에서 확인해 주세요" 라고 알리고, 계속 get_result(wait=true) 로
     기다렸다가 승인 뒤 결과를 바로 보고한다. 대신 승인하지 않는다.
   - 사용자가 다른 말을 하면 기다리기를 멈추고 그 말을 따른다.
5. send_command 가 거부되면 상태만 알리고 재시도·큐잉하지 말 것.
6. auto 모드 세션에서 되돌리기 어려운 작업(삭제·배포·전송 등)을 요청받으면 실행 전에 한 번 더 확인한다.
   권한 모드·권한 설정을 바꾸라는 요청은 전달하지 말고 Code 탭에서 직접 하라고 안내한다.
7. prompt_mismatch 가 있으면 결과를 확정하지 말고 Code 탭에서 확인하라고 안내한다.
8. '!' 나 '/' 로 시작하는 명령은 받지 않는다 (말로 풀어서 요청).
9. 사용자가 "멈춰" 하면 interrupt.
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
        """이 머신의 모든 Claude Code 세션 (이름, 작업 폴더, 호스트명, 상태: 대기/작업 중/승인 대기, 종류, 명령 가능 여부)."""
        sessions = await call(manager.list_sessions)
        d = {"ok": True, "host": manager.config.host_id, "sessions": sessions,
             "presets": sorted(manager.config.sessions), "bridge_version": BRIDGE_VERSION}
        if manager.agents.error:
            # claude 가 없거나 오래돼 목록 조회 실패 → 빈 목록만 주면 "세션이 없다"로 오해한다
            d["warning"] = f"이 머신의 세션 목록을 읽지 못했습니다: {manager.agents.error}"
        return d

    @mcp.tool()
    async def select_session(name: str) -> dict:
        """대상 세션 1개를 서버 측에 고정한다 (이름 또는 session_id). 사용자에게 확인을 받은 뒤에만 호출.
        보기 전용(desktop) 세션은 고정할 수 없다."""
        r = await call(manager.select_session, name)
        return r if r.get("ok") is False else {"ok": True, "selected": r}

    @mcp.tool()
    async def send_command(text: str, wait: bool = True) -> dict:
        """고정된 세션에 명령(프롬프트)을 주입한다. 세션이 대기 상태가 아니면 거부하고 상태를 돌려준다.
        wait=true(기본)면 결과가 나오거나 승인이 필요해질 때까지 최대 약 50초 기다렸다가 결과를 함께 돌려준다.
        final=false 면 get_result(request_id, wait=true) 로 계속 기다린다."""
        def send_command_():
            return manager.send_command(text, wait)
        send_command_.__name__ = "send_command"
        return await call(send_command_)

    @mcp.tool()
    async def get_result(request_id: str, wait: bool = False) -> dict:
        """요청 ID 의 진행 상태(완료/작업 중/승인 대기/중단됨)와, 끝났으면 최종 응답.
        wait=true 면 결과가 나오거나 새로 승인이 필요해질 때까지 최대 약 50초 기다렸다가 돌려준다 (결과를 바로 보고할 때)."""
        def get_result_():
            return manager.wait_result(request_id) if wait else manager.get_result(request_id)
        get_result_.__name__ = "get_result"
        return await call(get_result_)

    @mcp.tool()
    async def interrupt() -> dict:
        """고정된 세션의 현재 작업을 중단한다 (Esc 주입). 사용자가 '멈춰' 라고 할 때."""
        return await call(manager.interrupt)

    @mcp.tool()
    async def start_session(name: str) -> dict:
        """설정에 등록된 프리셋 이름으로 RC 세션을 띄운다 (임의 폴더 불가). 목록은 list_sessions 의 presets."""
        def start_session_():
            return manager.start_session(name).info()
        start_session_.__name__ = "start_session"
        r = await call(start_session_)
        return r if r.get("ok") is False else {"ok": True, "session": r}

    return mcp


def build_app(manager: SessionManager, token):
    cfg: BridgeConfig = manager.config
    mcp = build_mcp(manager)
    # Host/Origin 검사는 TokenAuthMiddleware 가 한다 (라이브러리 검사는 "*.trycloudflare.com" 같은 와일드카드를 못 씀)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security, host=cfg.bind)
    return TokenAuthMiddleware(app, token, allowed_hosts=cfg.allowed_hosts)
