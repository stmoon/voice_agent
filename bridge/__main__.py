"""voice-bridge CLI.

    voice-bridge serve [--start 이름 ...]     MCP 서버 실행 (+ 프리셋 세션 자동 기동)
    voice-bridge token init                   MCP 토큰 생성 → OS 보안 저장소 저장, 커넥터 URL 출력
    voice-bridge token set-notion             노션 통합 토큰을 보안 저장소에 저장 (표준입력으로 받음)
    voice-bridge info                         설정·호스트 정보
    voice-bridge url                          커넥터 주소 출력 (현재 터널 주소 + 키체인 토큰)
    voice-bridge add "이름" 폴더 [--start] [--autostart]   세션 프리셋 추가 (재시작 불필요)
    voice-bridge tunnel                       cloudflared 임시 터널 (주소가 바뀌면 맥 알림)
    voice-bridge service install|uninstall|status   로그인 시 자동 실행 (macOS launchd)
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
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
        print("[bridge] 종료 (세션 정리 완료)", file=sys.stderr)
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


def tunnel_hosts(cfg: BridgeConfig) -> list[str]:
    """커넥터에 쓸 외부 호스트: 실행 중인 임시 터널(cloudflared 메트릭 /quicktunnel) + 설정의 고정 호스트."""
    import json
    import urllib.request

    hosts = []
    for port in range(20241, 20246):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/quicktunnel", timeout=1) as r:
                h = json.loads(r.read() or b"{}").get("hostname")
                if h:
                    hosts.append(h)
        except Exception:
            continue
    from .service import host_file

    hf = host_file(cfg.state_dir)  # 자동 실행 터널이 기록한 최근 주소 (메트릭 조회 실패 대비)
    if not hosts and hf.exists() and hf.read_text().strip():
        hosts.append(hf.read_text().strip())
    hosts += [h for h in cfg.allowed_hosts if "*" not in h and h not in hosts]
    return hosts


def cmd_url(args, cfg: BridgeConfig) -> int:
    from .auth import MCP_TOKEN, TokenMissing, load_secret

    try:
        tok = load_secret(MCP_TOKEN)
    except TokenMissing as e:
        print(e, file=sys.stderr)
        return 1
    hosts = tunnel_hosts(cfg)
    if not hosts:
        print("실행 중인 터널이 없습니다. 먼저: cloudflared tunnel --url http://localhost:%d" % cfg.port, file=sys.stderr)
        return 2
    print("커넥터 주소 (claude.ai → 설정 → 커넥터 → 사용자 지정 커넥터 추가에 그대로 붙여넣기):")
    for h in hosts:
        print(f"  https://{h}/t/{tok}/mcp")
    return 0


def cmd_tunnel(args, cfg: BridgeConfig) -> int:
    from .service import run_tunnel

    return run_tunnel(cfg.port, cfg.state_dir)


def cmd_service(args, cfg: BridgeConfig) -> int:
    from . import service

    if args.action == "install":
        for p in service.install():
            print(f"등록: {p}")
        print(f"로그: {service.log_dir()}")
        if sys.platform == "win32":
            print("로그온할 때 자동으로 뜹니다 (작업 스케줄러 VoiceBridge-serve·tunnel, 꺼져 있으면 1분 안에 다시 시작).")
        elif sys.platform.startswith("linux"):
            print("로그아웃 뒤에도 돌게 하려면: loginctl enable-linger $USER")
        print("터널 주소가 정해지면(바뀌면) 알림이 뜹니다. 새 주소: voice-bridge url")
    elif args.action == "uninstall":
        for p in service.uninstall():
            print(f"해제: {p}")
    for label, st in service.status().items():
        print(f"{label}: {st}")
    return 0


def cmd_add(args, cfg: BridgeConfig) -> int:
    from pathlib import Path

    from .config import add_preset, claude_trusts

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"폴더가 없습니다: {folder}", file=sys.stderr)
        return 1
    add_preset(cfg.source or default_config_path(), args.name, str(folder), autostart=args.autostart)
    print(f"추가: \"{args.name}\" = {folder}" + ("  (자동 기동)" if args.autostart else ""))
    trusted = claude_trusts(str(folder))
    if trusted is False:
        print(f"주의: claude 가 아직 이 폴더를 신뢰하지 않았습니다. 한 번 실행해 '신뢰'를 고르세요:\n  cd {folder} && claude")
    if args.start:
        if trusted is False:
            print("폴더 신뢰 전이라 지금은 띄우지 않았습니다.")
            return 0
        import asyncio
        import json

        from mcp import Client

        from .auth import MCP_TOKEN, load_secret

        async def go():
            async with Client(f"http://127.0.0.1:{cfg.port}/t/{load_secret(MCP_TOKEN)}/mcp") as c:
                return json.loads((await c.call_tool("start_session", {"name": args.name})).content[0].text)

        try:
            r = asyncio.run(go())
        except Exception as e:
            print(f"실행 중인 브리지에 연결하지 못했습니다 ({type(e).__name__}). 휴대폰에서 '{args.name} 세션 띄워줘' 로 띄우세요.")
            return 0
        if r.get("ok"):
            print(f"세션 기동: {args.name} (상태 {r['session']['status_ko']})")
        else:
            print(f"기동 실패: {r.get('message')}")
    print(f"휴대폰에서: \"{args.name} 세션 띄워줘\" → \"{args.name} 세션으로 하자\"")
    return 0


def cmd_info(args, cfg: BridgeConfig) -> int:
    print(f"config    : {default_config_path()}")
    print(f"host_id   : {cfg.host_id}")
    print(f"listen    : {cfg.bind}:{cfg.port}")
    print(f"presets   : {', '.join(cfg.sessions) or '(없음)'}")
    print(f"autostart : {', '.join(cfg.autostart) or '(없음)'}")
    print(f"hosts     : {', '.join(cfg.allowed_hosts) or '(없음)'}")
    return 0


def _redirect_output(path: str) -> None:
    """stdout·stderr 를 파일로 (Windows pythonw 는 콘솔이 없어 출력이 사라지고 print 가 실패할 수 있다)."""
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="voice-bridge")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--start", action="append", help="기동할 프리셋 세션 이름 (반복 가능)")
    s.add_argument("--log", help="출력을 이 파일에 덧붙임 (창 없이 도는 자동 실행용)")
    t = sub.add_parser("token")
    t.add_argument("action", choices=["init", "set-notion"])
    sub.add_parser("info")
    sub.add_parser("url", help="커넥터 주소(토큰 포함) 출력")
    ad = sub.add_parser("add", help="세션 프리셋 추가 (브리지 재시작 불필요)")
    ad.add_argument("name", help="음성으로 부를 세션 이름")
    ad.add_argument("folder", help="작업 폴더")
    ad.add_argument("--autostart", action="store_true", help="브리지가 뜰 때 자동 기동")
    ad.add_argument("--start", action="store_true", help="실행 중인 브리지에 지금 바로 기동")
    tn = sub.add_parser("tunnel", help="cloudflared 임시 터널 실행 (주소가 바뀌면 알림)")
    tn.add_argument("--log", help="출력을 이 파일에 덧붙임 (창 없이 도는 자동 실행용)")
    sv = sub.add_parser("service", help="자동 실행 등록·해제·상태 (macOS launchd / Windows 작업 스케줄러 / Linux systemd)")
    sv.add_argument("action", choices=["install", "uninstall", "status"])
    args = p.parse_args(argv)
    if getattr(args, "log", None):
        _redirect_output(args.log)
    cfg = BridgeConfig.load()
    return {"serve": cmd_serve, "token": cmd_token, "info": cmd_info, "url": cmd_url, "add": cmd_add,
            "tunnel": cmd_tunnel, "service": cmd_service}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
