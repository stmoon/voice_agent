"""Claude Code 훅 → 사이드카 이벤트 파일 기록기.

bridge 가 세션을 띄울 때 --settings 로 이 모듈을 PermissionRequest / Notification /
SessionStart / Stop 훅에 연결한다. 표준입력의 훅 JSON 에 수신 시각(_ts)을 붙여
events.jsonl 에 한 줄로 덧붙인다.

아무것도 출력하지 않고 0 으로 끝난다 → 권한 결정에 관여하지 않음(승인은 반드시 사람이).
"""
from __future__ import annotations

import json
import sys
import time

HOOK_EVENTS = ("SessionStart", "PermissionRequest", "Notification", "Stop", "UserPromptSubmit")


def build_settings(python: str, events_file: str) -> dict:
    # 세션 cwd 는 임의 폴더라 패키지 import 에 기대지 않고 이 파일을 직접 실행.
    # 경로는 '/' 로: Windows 의 cmd·Git Bash 어느 쪽이 훅을 실행해도 따옴표 안 역슬래시 해석 문제가 없다.
    from pathlib import Path

    def q(p) -> str:
        return '"' + Path(p).absolute().as_posix() + '"'  # resolve() 는 venv 심볼릭 링크를 따라가므로 쓰지 않음

    cmd = f"{q(python)} {q(__file__)} {q(events_file)}"
    return {"hooks": {ev: [{"hooks": [{"type": "command", "command": cmd}]}] for ev in HOOK_EVENTS}}


def read_events(path) -> list[dict]:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {"raw": data}
    data["_ts"] = time.time()
    with open(argv[1], "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
