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
헤더를 넣을 수 있으면 `Authorization: Bearer <토큰>` + `/mcp`.

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
