# voice-bridge

## INTRODUCTION

휴대폰 Claude에게 **말로** 시키면, 내 컴퓨터의 Claude Code 세션이 그 일을 하고 **결과가 그 대화에 바로 돌아온다.**

```
휴대폰 (음성) ── Claude 앱 ── 커넥터 ──▶ voice-bridge (내 컴퓨터) ──▶ Claude Code 세션들
                    ▲                                                     │
                    └──────────────── 결과 요약 ◀──────────────────────────┘
                                       (승인은 휴대폰 Code 탭에서)
```

- 컴퓨터에 띄워 둔 세션들을 보고 골라서, "테스트 돌려줘", "이 페이지 요약해줘"처럼 말로 일을 시킨다
- 결과는 끝나는 대로 명령을 보낸 대화에 바로 보고된다 (다시 물어볼 필요 없음)
- 파일 수정 같은 위험한 작업은 휴대폰 Code 탭에서 사람이 승인한다. "멈춰"로 중단할 수 있다
- 크롬을 말로 조작하는 세션도 만들 수 있다 (Claude in Chrome)
- macOS 에서 쓰고 있고, Windows·Linux 는 자동 테스트까지 통과했다 (실기 확인은 [TODO.md](TODO.md))

## HOW TO USE

### 1. 설치 — 처음 한 번

```bash
./scripts/install.sh
```

Windows 는 `powershell -ExecutionPolicy Bypass -File scripts\install.ps1`

마지막에 나오는 **커넥터 주소**를 claude.ai → 설정 → 커넥터 → **사용자 지정 커넥터 추가**에 붙여 넣는다.
(주소에 비밀 토큰이 들어 있으니 공유하지 않는다. 준비물·자세한 설치·문제 해결은 [INSTALL.md](INSTALL.md))

### 2. 세션 만들기 — 일 시킬 폴더마다

```bash
voice-bridge session new "위키" ~/Project/LLMWiki
voice-bridge session new "브라우저" sessions/browser --chrome     # 레포 폴더에서: 크롬 조작용
```

| 명령 | 하는 일 |
|---|---|
| `voice-bridge session new "이름" 폴더` | 세션 만들기 (`--chrome` 크롬 조작, `--approve` 작업마다 승인) |
| `voice-bridge session list` | 세션 목록과 음성 명령 가능 여부 |
| `voice-bridge session stop "이름"` | 세션 멈추기 |

### 3. 휴대폰에서 말하기

Claude 앱에서 **새 대화**를 열고 입력창의 커넥터 메뉴에서 이 커넥터를 켠 뒤:

| 말하기 | 결과 |
|---|---|
| "음성 브리지 세션 목록 보여줘" | 세션 목록 |
| "위키 세션으로 하자" → "응" | 그 세션으로 고정 |
| "테스트 돌려줘" | 실행하고, 끝나면 결과를 바로 알려 준다 |
| "멈춰" | 작업 중단 |

"승인이 필요합니다"라고 하면 **휴대폰 Code 탭**에서 승인한다.

### 컴퓨터에 "음성 브리지 주소가 바뀌었습니다" 알림이 뜨면

```bash
voice-bridge url
```

나온 주소로 claude.ai 커넥터 주소를 바꾼다. (재부팅 등으로 임시 터널 주소가 바뀌었을 때)
