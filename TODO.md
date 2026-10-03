# TODO

기준 문서: 노션 「🔌 음성 대화 - Claude Code 연동 (MCP 브리지)」 (`[P단계-번호]` 는 노션 체크리스트 ID)

## 확인 필요 (실기)

- [ ] **Windows PC 에서 실제 사용** — 진행 중 (2026-10-04, Windows 11 한국어, claude 2.1.286).
      CI Windows 실패 1건(터널 테스트의 가짜 Popen `_handle`)은 수정 → push 후 CI 재확인
  - [ ] `scripts\install.ps1` 로 설치 → 작업 스케줄러 등록이 일반 권한으로 되는지, 창 없이 뜨는지, 꺼지면 1분 안에 다시 뜨는지
  - [x] `claude --bg` / `claude attach` / `claude agents --json` — 한글 이름·경로, 브리지의 attach 약 4초, 화면 하단 모드 판정,
        attach 를 닫아도 세션 유지, `stop`·`rm` 확인 (Remote Control 연결은 아래 휴대폰 흐름에서)
  - [ ] ConPTY 를 거친 Esc(중단)·Ctrl+U(입력창 비우기)·Enter — Ctrl+U/Backspace 지우기(한 줄·여러 줄·화면 폭을 넘는 줄),
        한글 입력, 여러 줄 붙여넣기(중간 제출 없음) 확인. Esc 중단은 남음
  - [ ] 한글 폴더·세션 이름, npm 설치본(`claude.cmd`)과 공식 설치본(`claude.exe`) 둘 다 — 한글 이름·폴더, `claude.exe` 확인.
        `claude.cmd` 는 남음
  - [ ] 휴대폰에서 세션 목록 → 명령 → 결과 → 승인 흐름
- [ ] **auto 모드 세션**(`위키자동`)을 휴대폰에서 명령 → 승인 없이 결과가 오는지
- [ ] **브라우저 세션**(`--chrome`)을 휴대폰에서 명령 → 크롬 조작·요약이 되는지, 확인 규칙(구매·전송 전 "진행할까요?")이 지켜지는지
- [ ] 결과 즉시 보고: 50초를 넘는 작업에서 휴대폰 Claude 가 사용자에게 묻지 않고 계속 기다렸다가 보고하는지 (음성 모드 포함)

## 노션 체크리스트 남은 항목

- [ ] `[P0-3]` Claude Code Stop 훅 / `make verify` 에 notion_sync 연결 — `.claude/settings.json` 에 훅 추가(직접). docs/DESIGN.md 참고
- [ ] `[P7-2]` 두 번째 머신 추가 후 머신별 세션 목록 구분 확인 (Windows PC 로 하면 위 실기 확인과 겸함)
- [ ] `[P7-3]` (수동) 머신이 늘 때 커넥터를 하나의 MCP 엔드포인트로 묶을지 결정
- [ ] `[P8-1]`~`[P8-6]` 적용 사례: 코러스 전자결재 (Windows 머신, Playwright, 자격 증명 관리자)
  - 조회 스크립트만 그 프로젝트 `permissions.allow` 에 넣어야 "조회는 자동" 규칙이 지켜진다 (승인 스크립트는 제외)

## 개선

- [ ] **고정 주소** — 임시 터널은 재시작 때 주소가 바뀌어 커넥터를 다시 등록해야 함. named tunnel(`tools/tunnel_setup.py`) 또는
      Tailscale Funnel 로 고정
- [ ] **노션 자동 체크 실가동** — 노션 내부 통합 토큰 발급·페이지 연결 → `voice-bridge token set-notion` (지금은 수동 반영)
- [ ] 세션 고정이 브리지 전체에서 공유됨 → 새 대화에서도 이전 고정이 남는다. 대화(또는 시간) 단위로 풀기
- [ ] 책상에서 `claude attach` 로 같은 세션에 붙어 입력 중일 때 음성 명령이 오면 입력창 비우기가 그 내용을 지울 수 있음
      → 입력 줄이 비어 있지 않으면 거부
- [ ] 가짜 claude 의 attach 를 실제처럼 "세션 본체 + 붙는 클라이언트" 로 분리 (승인 뒤 백그라운드 진행 등 재현력)
- [ ] Windows: ConPTY 출력량으로 `quiet_rate` 재측정(지금 값은 macOS 실측). (Shift+Tab 모드 전환 테스트는 가짜 claude 가
      ReadConsoleInputW 로 키를 읽게 바꿔 Windows 에서도 돈다)
- [ ] `plan` 권한 모드 지원 여부 (승인창 문구 실측 필요)
- [ ] RC 끊김 기록 문구(`Remote Control disconnected …`) 외 다른 끊김 형태가 있는지 확인
- [ ] 노션 본문의 도구 표가 5개로 되어 있음 (실제 7개: + `start_session`, `get_result(wait)`) — 갱신 여부 결정

## 기록

- [ ] 이번 결정들(생각 중 중단 처리, OAuth 경로 404, auto 모드 허용, 결과 즉시 보고, Windows 포팅 방식)을 위키에 남길지 결정
