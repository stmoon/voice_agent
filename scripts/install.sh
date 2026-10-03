#!/usr/bin/env bash
# voice-bridge 한 번에 설치 (macOS·Linux): 설치 → 토큰 → 자동 실행 → 커넥터 주소 출력
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 이상이 필요합니다. (macOS: brew install python@3.12)"; exit 1
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "claude CLI 가 필요합니다: https://claude.com/claude-code  (설치 뒤 claude /login)"; exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  if [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    echo "cloudflared 설치 중 (brew)…"; brew install cloudflared
  else
    echo "cloudflared 를 설치한 뒤 다시 실행하세요: https://pkg.cloudflare.com"; exit 1
  fi
fi

"$PY" tools/install.py
VB="$HOME/.local/share/voice-bridge/venv/bin/voice-bridge"
"$VB" setup
echo
echo "다음: 휴대폰에서 부를 세션을 만드세요"
echo "  voice-bridge session new \"이름\" ~/작업폴더"
