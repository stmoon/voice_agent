#!/usr/bin/env python3
"""Cloudflare Tunnel 발급 스크립트 (머신 1대 = 터널 1개).

서브도메인 규칙:  <호스트ID>.bridge.<도메인>
    호스트ID = 호스트명 소문자, [a-z0-9-] 외 문자는 '-', 앞뒤 '-' 제거, 최대 63자
터널 이름:        vc-<호스트ID>

    python tools/tunnel_setup.py --domain example.com            # 실행
    python tools/tunnel_setup.py --domain example.com --dry-run  # 명령만 출력

사전 조건: cloudflared 설치, `cloudflared tunnel login` 1회 (브라우저 인증, 사람이 직접).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def host_label(hostname: str) -> str:
    s = hostname.split(".")[0].lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")[:63].strip("-")
    if not s or not LABEL_RE.match(s):
        raise ValueError(f"호스트명으로 서브도메인을 만들 수 없음: {hostname!r}")
    return s


def fqdn(hostname: str, domain: str) -> str:
    domain = domain.strip(".").lower()
    if not re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", domain):
        raise ValueError(f"도메인 형식 오류: {domain!r}")
    return f"{host_label(hostname)}.bridge.{domain}"


def tunnel_name(hostname: str) -> str:
    return f"vc-{host_label(hostname)}"


def config_yaml(name: str, host: str, port: int, cred_file: str) -> str:
    return (
        f"tunnel: {name}\n"
        f"credentials-file: {cred_file}\n"
        f"ingress:\n"
        f"  - hostname: {host}\n"
        f"    service: http://127.0.0.1:{port}\n"
        f"  - service: http_status:404\n"
    )


def plan(hostname: str, domain: str, port: int, cf_dir: Path) -> dict:
    name = tunnel_name(hostname)
    host = fqdn(hostname, domain)
    cfg = cf_dir / f"{name}.yml"
    return {
        "name": name,
        "host": host,
        "config_path": cfg,
        "commands": [
            ["cloudflared", "tunnel", "create", name],
            ["cloudflared", "tunnel", "route", "dns", name, host],
        ],
        "run": ["cloudflared", "tunnel", "--config", str(cfg), "run", name],
    }


def main(argv=None) -> int:
    import socket

    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--hostname", default=socket.gethostname())
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cf-dir", type=Path, default=Path.home() / ".cloudflared")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    p = plan(a.hostname, a.domain, a.port, a.cf_dir)
    print(f"터널: {p['name']}  →  https://{p['host']}  →  127.0.0.1:{a.port}")
    if a.dry_run:
        for c in p["commands"]:
            print("  $ " + " ".join(c))
        print(f"  (설정 파일) {p['config_path']}")
        print("  $ " + " ".join(p["run"]))
        return 0
    if not shutil.which("cloudflared"):
        print("cloudflared 가 없습니다. 설치 후 `cloudflared tunnel login` 을 먼저 하세요.", file=sys.stderr)
        return 2
    if not (a.cf_dir / "cert.pem").exists():
        print("`cloudflared tunnel login` 이 필요합니다 (브라우저에서 직접 인증).", file=sys.stderr)
        return 2
    for c in p["commands"]:
        print("$ " + " ".join(c))
        r = subprocess.run(c)
        if r.returncode != 0 and "create" in c:
            print("  (이미 있는 터널이면 무시하고 계속)")
    creds = sorted(a.cf_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    cred = str(creds[0]) if creds else str(a.cf_dir / f"{p['name']}.json")
    p["config_path"].write_text(config_yaml(p["name"], p["host"], a.port, cred))
    print(f"설정 저장: {p['config_path']}")
    print(f"bridge 설정(config.toml)에 추가: allowed_hosts = [\"{p['host']}\"]")
    print("실행: " + " ".join(p["run"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
