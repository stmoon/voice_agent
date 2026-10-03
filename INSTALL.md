# 설치

## 준비물

| 항목 | macOS | Windows | Linux |
|---|---|---|---|
| Claude Code **2.1.285 이상** (로그인까지) | `claude /login` | 같음 + Git for Windows | 같음 |
| Python 3.11 이상 | `brew install python@3.12` | `winget install Python.Python.3.12` | 배포판 패키지 |
| cloudflared | 설치 스크립트가 brew 로 설치 | 설치 스크립트가 winget 으로 설치 | https://pkg.cloudflare.com |
| claude.ai 계정 | 사용자 지정 커넥터를 추가할 수 있는 계정 | | |

- `claude` 가 PATH 에 있어야 한다. 오래된 CLI(`agents --json`·`--bg`·`attach` 없음)는 `claude update` 로 올린다.
  `voice-bridge info` 가 찾은 claude 의 경로·버전과 문제를 알려 준다.
- Windows 공식 설치본은 `%USERPROFILE%\.local\bin\claude.exe` 에 깔린다. 그 폴더가 PATH 에 없으면 브리지가 claude 를
  찾지 못한다(세션 목록이 비고 `warning` 이 붙음) → PATH 에 넣거나 설정의 `claude_bin` 에 전체 경로를 적는다.

## 한 번에 설치

```bash
git clone https://github.com/stmoon/voice_agent.git && cd voice_agent
./scripts/install.sh
```

Windows (PowerShell):

```powershell
git clone https://github.com/stmoon/voice_agent.git; cd voice_agent
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

스크립트가 하는 일:

1. `tools/install.py` — 전용 가상환경에 설치, 기본 설정 파일 생성, `~/.local/bin/voice-bridge` 실행 링크
2. `voice-bridge setup`
   - 접속 토큰을 OS 보안 저장소에 저장 (macOS 키체인 / Windows 자격 증명 관리자 / Linux Secret Service). 이미 있으면 그대로 쓴다
   - 자동 실행 등록 (아래 표), cloudflared 임시 터널 시작
   - 터널 주소가 나오면 **커넥터 주소**(`https://○○.trycloudflare.com/t/<토큰>/mcp`) 출력

`voice-bridge` 가 안 잡히면 `~/.local/bin` 을 PATH 에 넣거나 전체 경로로 실행한다
(macOS·Linux `~/.local/share/voice-bridge/venv/bin/voice-bridge`, Windows `%LOCALAPPDATA%\voice-bridge\venv\Scripts\voice-bridge.exe`).

## 커넥터 등록

1. claude.ai(웹) → 설정 → 커넥터 → **사용자 지정 커넥터 추가** → 이름은 자유, 주소는 `voice-bridge url` 출력 그대로. OAuth 칸은 비워 둔다
2. 휴대폰 Claude 앱에서 **새 대화**를 열고 입력창의 커넥터 메뉴에서 켠다
3. 커넥터를 등록한 계정과 휴대폰 앱의 계정이 같아야 한다

## 세션 종류

| 종류 | 만드는 법 | 음성 명령 |
|---|---|---|
| 백그라운드 세션 | `voice-bridge session new "이름" 폴더` (= `claude --bg -n 이름 --remote-control 이름`) | 가능 |
| 브리지 세션 | 설정의 프리셋: `voice-bridge add "이름" 폴더 --start` | 가능 |
| 데스크톱 앱·터미널 세션 | Claude 데스크톱 앱 Code 탭 등 | 보기 전용 (입력 통로가 없음) |

- 처음 쓰는 폴더는 그 폴더에서 `claude` 를 한 번 실행해 "신뢰"를 골라 둔다
- 권한 모드는 승인 필요(`--approve`) 또는 auto(이 머신의 기본값) 만 음성 명령을 받는다. acceptEdits·권한 우회 모드 세션은 거부
- git 저장소 안의 백그라운드 세션은 파일을 고칠 때 `.claude/worktrees/<이름>` 작업 트리에서 고칠 수 있다
- 책상에서도 `claude attach <id>` 로 같은 세션을 쓸 수 있다 (`claude agents` 로 id 확인)
- 같은 이름의 세션을 짧은 시간에 여러 번 만들면 Remote Control 등록이 실패할 수 있다. 멈춘 뒤 잠깐 기다렸다가 한 번만 만든다

## 설정 파일

위치: macOS·Linux `~/.config/voice-bridge/config.toml`, Windows `%APPDATA%\voice-bridge\config.toml` (`VC_CONFIG` 로 변경 가능)

| 항목 | 기본값 | 설명 |
|---|---|---|
| `port` | 8765 | 브리지가 듣는 로컬 포트 |
| `allowed_hosts` | `["*.trycloudflare.com"]` | 외부 주소(Host) 허용. 고정 도메인이면 그 주소 |
| `allowed_permission_modes` | `["default", "auto"]` | 음성 명령을 받을 세션 권한 모드 (이 둘만 가능) |
| `session_permission_mode` | `"default"` | 브리지가 직접 띄우는 세션의 모드 |
| `wait_timeout` | 50 | 결과를 한 번에 기다리는 최대 초 (Cloudflare 100초 제한보다 짧게) |
| `autostart` | `[]` | 브리지가 뜰 때 함께 띄울 프리셋 이름 |
| `claude_bin` | `"claude"` | claude 실행 파일 (PATH 에 없으면 전체 경로) |
| `[sessions]` | — | 프리셋: `"이름" = "폴더"` (`voice-bridge add` 로 추가) |

## 자동 실행

| OS | 방식 | 다시 띄우기 | 로그 |
|---|---|---|---|
| macOS | launchd `com.voicebridge.serve` / `.tunnel` | 10초 뒤 | `~/Library/Logs/voice-bridge/` |
| Windows | 작업 스케줄러 `VoiceBridge-serve` / `-tunnel` (로그온, 창 없이) | 1분 안 | `%LOCALAPPDATA%\voice-bridge\logs` |
| Linux | systemd 사용자 서비스 `voice-bridge-serve` / `-tunnel` | 10초 뒤 | `~/.local/state/voice-bridge/logs` |

```bash
voice-bridge service status
voice-bridge service uninstall     # 해제 (다시 등록: voice-bridge service install)
```

Linux 에서 로그아웃 뒤에도 돌게 하려면 `loginctl enable-linger $USER`.

## 터널 주소

- 기본은 cloudflared **임시 터널**이라 재부팅·터널 재시작 때 주소가 바뀐다. 바뀌면 데스크톱 알림이 뜨고,
  `voice-bridge url` 로 새 주소를 확인해 커넥터 주소를 바꾼다
- 주소를 고정하려면 Cloudflare 계정·도메인으로 named tunnel 을 쓴다: `python3 tools/tunnel_setup.py --domain <도메인>`
  (`cloudflared tunnel login` 먼저). 만들어진 주소를 `allowed_hosts` 에 넣는다

## 삭제

```bash
voice-bridge service uninstall
rm -rf ~/.local/share/voice-bridge ~/.local/bin/voice-bridge ~/.config/voice-bridge ~/.local/state/voice-bridge
```

토큰은 OS 보안 저장소의 `voice-bridge` 항목(`mcp-token`)을 지운다. claude.ai 커넥터도 삭제한다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 휴대폰이 "도구가 연결되어 있지 않다"고 함 | 새 대화에서 커넥터를 켰는지, 커넥터를 등록한 계정이 맞는지 |
| 커넥터 추가 시 "로그인 서비스에 등록할 수 없습니다" | 주소를 `voice-bridge url` 출력 그대로 넣었는지, 브리지가 실행 중인지 |
| 세션이 목록에 있는데 명령 불가 | `voice-bridge session list` 의 이유: RC 꺼짐(다시 만들기), 허용되지 않는 권한 모드 |
| 아무 응답이 없음 | `voice-bridge service status`, 로그 폴더, 알림(주소가 바뀌었는지) |
| 세션이 "기동 실패" (폴더 신뢰) | 그 폴더에서 `claude` 를 한 번 실행해 신뢰 |
| 목록이 비고 "세션 목록을 읽지 못했습니다" | `voice-bridge info` 로 claude 경로·버전 확인 (PATH, 2.1.285 이상) |

## 개발

- 구성·설계 결정·실측 기록: [docs/DESIGN.md](docs/DESIGN.md)
- 테스트: `make test` (가짜 claude), `make verify-live` (실제 claude, 로그인 필요). CI 는 macOS·Windows·Linux
- Windows 에서 테스트를 직접 돌릴 때는 `set PYTHONUTF8=1` 후 `python -m pytest -q` (한국어 Windows 에서도 통과)
- 요구사항·진행 현황 기준 문서: 노션 「🔌 음성 대화 - Claude Code 연동 (MCP 브리지)」
