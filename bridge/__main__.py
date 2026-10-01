"""voice-bridge CLI.

    voice-bridge serve [--start 이름 ...]     MCP 서버 실행 (+ 프리셋 세션 자동 기동)
    voice-bridge token init                   MCP 토큰 생성 → OS 보안 저장소 저장, 커넥터 URL 출력
    voice-bridge token set-notion             노션 통합 토큰을 보안 저장소에 저장 (표준입력으로 받음)
    voice-bridge info                         설정·호스트 정보
"""
from __future__ import annotations

import argparse
import getpass
import logging
import signal
import sys

from .config import BridgeConfig, default_config_path


def cmd_serve(args, cfg: BridgeConfig) -> int:
    import uvicorn

    from .auth import MCP_TOKEN, RedactTokenFilter, load_secret
    from .mcp_server import build_app
    from .session_manager import BridgeError, SessionManager

    token = load_secret(MCP_TOKEN)  # 없으면 여기서 실패 (평문 대체 없음)
    if not _port_free(cfg.bind, cfg.port):
        print(f"[bridge] {cfg.bind}:{cfg.port} 를 이미 다른 프로세스가 쓰고 있습니다 (bridge 가 이미 실행 중인지 확인). "
              "세션을 띄우지 않고 종료합니다.", file=sys.stderr)
        return 2
    manager = SessionManager(cfg)
    names = list(dict.fromkeys([*cfg.autostart, *(args.start or [])]))  # 설정 autostart + --start, 중복 제거
    for name in names:
        try:
            s = manager.start_session(name)
            print(f"[bridge] 세션 기동: {s.name} ({s.cwd})", file=sys.stderr)
        except BridgeError as e:
            print(f"[bridge] 세션 기동 실패 {name}: {e.message}", file=sys.stderr)
            if e.code == "unknown_preset":
                print(f"[bridge] 등록된 프리셋: {', '.join(cfg.sessions) or '(없음)'}  — 설정: {default_config_path()}",
                      file=sys.stderr)
    app = build_app(manager, token)

    def _stop(*_):
        manager.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    # Config 생성 시점에 uvicorn 이 로깅을 설정하므로 그 뒤에 토큰 가림 필터를 붙인다
    config = uvicorn.Config(app, host=cfg.bind, port=cfg.port, log_level="info")
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(RedactTokenFilter())
    try:
        uvicorn.Server(config).run()
    finally:
        manager.shutdown()
    return 0


def _port_free(host: str, port: int) -> bool:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        # uvicorn 과 같게: 방금 끈 서버의 TIME_WAIT 연결은 무시하고, 실제로 LISTEN 중인 것만 '사용 중'으로 본다
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def cmd_token(args, cfg: BridgeConfig) -> int:
    from .auth import NOTION_TOKEN, generate_mcp_token, store_secret

    if args.action == "init":
        tok = generate_mcp_token()
        print("MCP 토큰을 OS 보안 저장소에 저장했습니다. 커넥터 등록용 (한 번만 표시):")
        host = cfg.allowed_hosts[0] if cfg.allowed_hosts else f"{cfg.host_id}.bridge.<도메인>"
        print(f"  https://{host}/t/{tok}/mcp")
        return 0
    if args.action == "set-notion":
        val = getpass.getpass("노션 내부 통합 토큰: ").strip()
        if not val:
            print("비어 있음", file=sys.stderr)
            return 1
        store_secret(NOTION_TOKEN, val)
        print("저장했습니다.")
        return 0
    return 1


def cmd_info(args, cfg: BridgeConfig) -> int:
    print(f"config    : {default_config_path()}")
    print(f"host_id   : {cfg.host_id}")
    print(f"listen    : {cfg.bind}:{cfg.port}")
    print(f"presets   : {', '.join(cfg.sessions) or '(없음)'}")
    print(f"autostart : {', '.join(cfg.autostart) or '(없음)'}")
    print(f"hosts     : {', '.join(cfg.allowed_hosts) or '(없음)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="voice-bridge")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--start", action="append", help="기동할 프리셋 세션 이름 (반복 가능)")
    t = sub.add_parser("token")
    t.add_argument("action", choices=["init", "set-notion"])
    sub.add_parser("info")
    args = p.parse_args(argv)
    cfg = BridgeConfig.load()
    return {"serve": cmd_serve, "token": cmd_token, "info": cmd_info}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
