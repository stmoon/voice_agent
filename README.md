# voice-bridge

휴대폰 음성 대화(모바일 Claude) → MCP → 원격 머신의 **Claude Code Remote Control 세션**에 명령 주입·결과 회수.
요구사항·진행 현황의 기준 문서: 노션 「🔌 음성 대화 - Claude Code 연동 (MCP 브리지)」.

```
모바일 Claude ──MCP(HTTPS, 토큰)──▶ bridge.py(머신마다 1개) ──PTY 주입──▶ claude --remote-control "<이름>"
                                          ▲                                   │
                                          └──── transcript JSONL + 훅 이벤트 ◀┘  (RC 릴레이 → 휴대폰 Code 탭)
```

## 구성

| 모듈 | 역할 |
|---|---|
| `bridge/pty_runner.py` | `claude` 를 의사 터미널로 기동·입력·종료 (macOS·Linux `pty`, Windows pywinpty) |
| `bridge/transcript.py` | `~/.claude/projects/*/<session_id>.jsonl` 파싱 → 상태(대기/작업 중/승인 대기)·요청별 최종 응답 |
| `bridge/hook_sink.py` | `--settings` 로 심는 훅(PermissionRequest/Notification/SessionStart…) → 사이드카 events.jsonl |
| `bridge/session_manager.py` | 세션 등록·고정·명령 주입·결과 회수·중단, 충돌 규칙 |
| `bridge/mcp_server.py` | MCP 도구 `list_sessions` `select_session` `send_command` `get_result` `interrupt` (+`start_session`) |
| `bridge/auth.py` | 토큰 인증(401), OS 보안 저장소(keyring) |
| `tools/notion_sync.py` | JUnit XML → 요구사항 ID 별 통과 여부 → 노션 체크박스 |
| `tools/tunnel_setup.py` | Cloudflare Tunnel 발급 (`<호스트ID>.bridge.<도메인>`) |
| `tools/install.py` | 설치 (Windows·macOS·Linux 공통) |

## 설치·실행

```bash
python3 tools/install.py                         # ~/.local/share/voice-bridge/venv
voice-bridge token init                          # MCP 토큰 → 키체인, 커넥터 URL 출력
python3 tools/tunnel_setup.py --domain <도메인>   # cloudflared tunnel login 선행 필요
voice-bridge serve --start "로그 플랫폼"
```

`~/.config/voice-bridge/config.toml`:

```toml
port = 8765
allowed_hosts = ["macmini.bridge.example.com"]
[sessions]                       # 음성으로 띄울 수 있는 프리셋 (claude 로 한 번 열어 신뢰해 둔 폴더만)
"로그 플랫폼" = "~/Project/logplatform"
```

커넥터 URL: `https://<호스트ID>.bridge.<도메인>/t/<토큰>/mcp` (헤더를 못 넣는 커넥터용).
헤더를 넣을 수 있으면 `Authorization: Bearer <토큰>` + `/mcp`. 현재 주소는 `voice-bridge url` 이 조립해 준다.

### 자동 실행 (macOS)

```bash
voice-bridge service install     # 로그인 시 자동 실행 + 죽으면 재시작 (LaunchAgent 2개: serve, tunnel)
voice-bridge service status
voice-bridge service uninstall
```

- `serve` 는 설정의 `autostart` 세션을 함께 띄운다. 로그: `~/Library/Logs/voice-bridge/`
- `tunnel` 은 cloudflared 임시 터널을 띄우고, **발급 주소가 이전과 다르면 맥 알림**을 띄운다
  (재부팅·터널 재시작 시 주소가 바뀜 → `voice-bridge url` 로 새 주소 확인 후 커넥터 갱신).
- 임시 터널은 `allowed_hosts = ["*.trycloudflare.com"]` 로 허용하면 주소가 바뀌어도 bridge 재시작이 필요 없다.

### claude.ai 커넥터 등록 시 주의 (실측)

- claude.ai 는 MCP 연결에 성공한 뒤에도 `/.well-known/oauth-*` 를 조회한다. 여기에 401 을 주면 OAuth 서버로 오인해
  `/register` 를 시도하다 "로그인 서비스에 등록할 수 없습니다" 로 실패한다 → bridge 는 OAuth 경로에 404 를 준다.
- 등록 후 **새 대화**에서 입력창의 커넥터 메뉴로 켜야 도구가 로드된다.

## 설계 결정 (PoC 결과 반영)

- **CLI 플래그**: 노션 문서의 `--rc` 는 실제로 `--remote-control [name]` (2.1.285 기준). 여기에
  `--session-id <uuid>` 를 함께 넘겨 bridge 가 transcript 파일을 처음부터 특정한다.
- **권한 모드 `default` 고정**: 이 머신의 기본값이 auto 라서 `--permission-mode default` 를 강제로 넘긴다.
  설정에서 `--permission-mode`·`--dangerously-skip-permissions` 를 넣으면 기동을 거부한다.
- **환경 변수**: Claude Code 안에서 bridge 를 띄우면 `CLAUDE_CODE_CHILD_SESSION` 등이 상속되어 자식 transcript 저장이
  꺼진다. 자식 env 에서 세션 표식만 제거하고 `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` 을 준다.
- **기동 대화상자**: 온보딩 안내("Try the new fullscreen renderer?" 등)는 Esc(=거절)로 닫는다.
  **폴더 신뢰 확인은 자동 수락하지 않는다** → `folder_not_trusted` 오류. 미리 그 폴더에서 claude 를 한 번 열어 신뢰할 것.
  화면을 읽는 곳은 이 기동 단계뿐이고, 결과·상태는 transcript 로만 판단한다.
- **승인 대기 판별**: JSONL 만으로는 "도구 실행 중" 과 "승인 대기" 가 구분되지 않는다(둘 다 결과 없는 tool_use).
  bridge 가 `--settings` 로 PermissionRequest/Notification 훅을 심어 사이드카에 기록하고, 결과 없는 tool_use 이후
  권한 이벤트가 있으면 승인 대기로 본다. 훅은 아무것도 출력하지 않으므로 권한 결정에 관여하지 않는다.
- **세션 시작 방식(P3-4)**: `start_session(name)` 을 제공하되 **config 에 등록된 프리셋 이름만** 허용
  (음성 경로로 임의 폴더 지정 불가). 로컬에서는 `serve --start` 로도 기동.
- **여러 줄 명령**: bracketed paste 로 넣어 중간 줄바꿈이 제출되지 않게 한다.
- **중단(Esc) 동작 — 실측 2.1.285**:
  - 응답 출력 도중 중단 → 부분 응답 + `[Request interrupted by user]` 가 transcript 에 남는다.
  - **생각 중(출력 전) 중단 → transcript 에 아무것도 남지 않고, 프롬프트가 입력창에 되돌아온다.**
    그냥 다음 명령을 타이핑하면 되돌아온 문장 뒤에 이어 붙어 **중단한 명령이 다시 실행된다**(실측으로 재현).
  - 대응 1: 주입 전에 항상 입력창을 비운다 — `(Ctrl+U, Backspace)` 반복. 빈 입력·한 줄·여러 줄·화면 폭을 넘는 줄 모두
    실측 확인. Esc 한 번은 안 지워지고, Esc 두 번은 빈 입력에서 되감기 메뉴를 열어 쓰지 않는다.
  - 대응 2: 끝나지 않은 턴 뒤에 다음 프롬프트가 오면 그 턴은 '중단됨'.
  - 대응 3: 마지막 턴이 끝나지 않았는데 TUI 가 조용하면(4초간 출력 150자/초 미만, transcript 변화 없음) '중단됨'.
    실측 출력량: 작업 중 500~1100자/초(스피너), 대기 0~50자/초. **결과 없는 tool_use 가 있으면 이 규칙을 쓰지 않는다**
    (권한 대화상자도 화면이 정적이라 조용하다).
- **주입 안전장치**: 화면 맨 뒤가 입력 대기 안내(`? for shortcuts`)가 아니고 확인·권한 대화상자(`Do you want to proceed?`,
  `Esc to cancel` 등)가 떠 있으면 주입을 거부한다(`not_at_prompt`). 대화상자에 명령+Enter 가 들어가면 기본 선택(승인)이
  눌릴 수 있기 때문.
- **RC 연결 확인**: RC 가 붙으면 CLI 가 transcript 에 `system/bridge_status` 레코드로 Code 탭 세션 URL 을 남긴다.
  `list_sessions` 가 이를 `rc_url` 로 돌려준다 (`null` 이면 RC 미연결).
- **기록된 프롬프트 검증**: `get_result` 는 transcript 에 기록된 프롬프트가 보낸 것과 다르면 `prompt_mismatch` 를 표시한다.
- **요청 ↔ 턴 매핑**: 주입 직전 transcript 레코드 수(offset)를 요청에 저장, 그 뒤 처음 시작된 턴이 해당 요청.
  대기 상태에서만 주입하므로 모호하지 않다.

## 테스트·노션 자동 체크

```bash
make test           # 전체 (live 제외)
make verify         # 테스트 → reports/junit.xml → 노션 체크박스 갱신
make verify-live    # 실제 claude 를 띄우는 live 테스트 포함 (로그인 필요)
```

- 테스트의 `@pytest.mark.req("P1-2")` ↔ 노션 체크박스 `[P1-2]`
- 전부 통과 → 체크, 하나라도 실패 → 해제, skip 섞임·테스트 없음·`(수동)` → 변경 안 함
- 노션 토큰: 내부 통합을 만들어 해당 페이지에 연결한 뒤 `voice-bridge token set-notion` (키체인 저장)

### Stop 훅 연결 (직접 설치 필요)

`.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{ "hooks": [{ "type": "command",
      "command": "cd \"$CLAUDE_PROJECT_DIR\" && make -s verify > reports/verify.log 2>&1; exit 0",
      "timeout": 300 }] }]
  }
}
```
