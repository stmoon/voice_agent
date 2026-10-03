"""사용자용 명령: 음성으로 쓸 세션 만들기·목록·멈추기, 한 번에 설정하기.

    voice-bridge setup                                   토큰(없을 때만) + 자동 실행 + 터널 대기 + 커넥터 주소
    voice-bridge session new 이름 [폴더] [--chrome] [--approve]
    voice-bridge session list
    voice-bridge session stop 이름
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from .config import BridgeConfig, claude_trusts


def _agents(cfg: BridgeConfig) -> list[dict]:
    from .agents import list_agents
    from .pty_runner import child_env

    return list_agents(cfg.claude_bin, env=child_env()) or []


def session_new(cfg: BridgeConfig, name: str, folder: str | None, chrome: bool = False, approve: bool = False,
                wait: float = 40.0) -> int:
    """백그라운드 세션(claude --bg --remote-control)을 띄우고 RC 연결·명령 가능 여부까지 확인한다."""
    from .proc import run_quiet
    from .pty_runner import child_env

    name = name.strip()
    if not name:
        print("세션 이름이 비었습니다.", file=sys.stderr)
        return 1
    path = Path(folder or ".").expanduser().resolve()
    if not path.is_dir():
        print(f"폴더가 없습니다: {path}", file=sys.stderr)
        return 1
    if claude_trusts(str(path)) is False:
        print(f"claude 가 아직 이 폴더를 신뢰하지 않았습니다. 먼저 한 번 실행해 '신뢰'를 고르세요:\n  cd \"{path}\" && claude")
        return 1
    same = [a for a in _agents(cfg) if a.get("name") == name]
    if same:
        print(f"'{name}' 세션이 이미 있습니다 ({', '.join(a.get('id') or a['sessionId'][:8] for a in same)}). "
              f"다시 만들려면: voice-bridge session stop \"{name}\"")
        return 1
    argv = [*cfg.claude_bin, "--bg", "-n", name, "--remote-control", name]
    if chrome:
        argv.append("--chrome")
    if approve:
        argv += ["--permission-mode", "default"]
    r = run_quiet(argv, env=child_env(), cwd=str(path), text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "backgrounded" not in out:
        print(f"세션을 띄우지 못했습니다:\n{out.strip()}", file=sys.stderr)
        return 1
    print(out.strip().splitlines()[0])
    return _report_ready(cfg, name, wait)


def _report_ready(cfg: BridgeConfig, name: str, wait: float) -> int:
    """RC 연결과 명령 가능 여부를 기다렸다가 알려 준다 (같은 이름을 짧게 여러 번 등록하면 RC 생성이 실패할 수 있음)."""
    import tempfile

    from .session_manager import SessionManager

    m = SessionManager(BridgeConfig(**{**cfg.__dict__, "state_dir": Path(tempfile.mkdtemp())}))
    end = time.time() + wait
    d = None
    while time.time() < end:
        d = next((x for x in m.list_sessions() if x["name"] == name), None)
        if d and (d["commandable"] or "Remote Control" in (d.get("reason") or "") and time.time() > end - wait / 2):
            break
        time.sleep(2)
    m.shutdown()
    if d and d["commandable"]:
        print(f"준비됨: '{name}' ({d.get('permission_mode_ko')}) — 휴대폰에서 \"{name} 세션으로 하자\"")
        return 0
    print(f"아직 음성 명령을 받을 수 없습니다: {(d or {}).get('reason') or 'RC 연결 대기 중'}\n"
          f"잠시 뒤 voice-bridge session list 로 다시 확인하세요. RC 가 계속 안 붙으면 멈춘 뒤 잠깐 기다렸다가 한 번만 다시 만드세요.")
    return 2


def _bridge_list(cfg: BridgeConfig) -> list[dict] | None:
    """실행 중인 브리지에 목록을 묻는다 (브리지가 띄운 세션까지 정확히 나옴). 브리지가 없으면 None."""
    import asyncio
    import json as _json

    from .auth import MCP_TOKEN, TokenMissing, load_secret

    try:
        tok = load_secret(MCP_TOKEN)
    except (TokenMissing, Exception):
        return None

    async def go():
        from mcp import Client

        async with Client(f"http://127.0.0.1:{cfg.port}/t/{tok}/mcp") as c:
            r = _json.loads((await c.call_tool("list_sessions", {})).content[0].text)
            return r.get("sessions")

    try:
        return asyncio.run(asyncio.wait_for(go(), timeout=15))
    except Exception:
        return None


def session_list(cfg: BridgeConfig, ask_bridge: bool = True) -> int:
    import tempfile

    from .session_manager import SessionManager

    rows = _bridge_list(cfg) if ask_bridge else None
    if rows is None:
        m = SessionManager(BridgeConfig(**{**cfg.__dict__, "state_dir": Path(tempfile.mkdtemp())}))
        try:
            rows = m.list_sessions()
        finally:
            m.shutdown()
    if not rows:
        print("실행 중인 세션이 없습니다.")
    for d in rows:
        if d["kind"] == "desktop":
            print(f"- {d['name']}  [{d['kind_ko']}] {d['status_ko']} · 보기 전용")
            continue
        mark = "가능" if d["commandable"] else "불가"
        print(f"- {d['name']}  [{d['kind_ko']}] {d['status_ko']} · 음성 명령 {mark}"
              + (f" · {d.get('permission_mode_ko')}" if d.get("permission_mode") else "")
              + (f"\n    {d['reason']}" if not d["commandable"] and d.get("reason") else ""))
    return 0


def session_stop(cfg: BridgeConfig, name: str) -> int:
    from .proc import run_quiet
    from .pty_runner import child_env

    targets = [a for a in _agents(cfg) if a.get("name") == name and a.get("kind") == "background" and a.get("id")]
    if not targets:
        print(f"'{name}' 이라는 백그라운드 세션이 없습니다. (데스크톱 앱 세션은 앱에서 닫으세요)")
        return 1
    for a in targets:
        r = run_quiet([*cfg.claude_bin, "stop", a["id"]], env=child_env(), text=True, timeout=60)
        print((r.stdout or r.stderr or "").strip() or f"stopped {a['id']}")
    return 0


def setup(cfg: BridgeConfig, wait: float = 90.0) -> int:
    """처음 한 번: 토큰(없을 때만) → 자동 실행 등록 → 터널 주소가 나올 때까지 대기 → 커넥터 주소 출력."""
    from . import service
    from .auth import MCP_TOKEN, TokenMissing, generate_mcp_token, load_secret

    try:
        load_secret(MCP_TOKEN)
        print("[1/3] 접속 토큰: 이미 있음 (그대로 사용)")
    except TokenMissing:
        generate_mcp_token()
        print("[1/3] 접속 토큰: 새로 만들어 OS 보안 저장소에 저장")
    if not shutil.which("cloudflared"):
        hint = "winget install Cloudflare.cloudflared" if sys.platform == "win32" else (
            "brew install cloudflared" if sys.platform == "darwin" else "https://pkg.cloudflare.com 의 cloudflared 패키지")
        print(f"cloudflared 가 없습니다. 설치 후 (새 터미널에서) 다시 실행하세요: {hint}", file=sys.stderr)
        return 2
    for p in service.install():
        print(f"[2/3] 자동 실행 등록: {p}")
    from .__main__ import tunnel_hosts

    print("[3/3] 터널 주소를 기다리는 중…")
    end = time.time() + wait
    while time.time() < end and not tunnel_hosts(cfg):
        time.sleep(2)
    from .__main__ import cmd_url

    rc = cmd_url(None, cfg)
    if rc == 0:
        print("\n이 주소를 claude.ai → 설정 → 커넥터 → 사용자 지정 커넥터 추가에 붙여 넣으세요 (주소에 비밀 토큰 포함).")
    return rc

