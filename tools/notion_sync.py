#!/usr/bin/env python3
"""테스트 결과(JUnit XML) → 요구사항 ID 별 통과 여부 → 노션 페이지 체크박스 갱신.

규칙
- 체크박스 문구의 `[P1-2]` 태그 ↔ 테스트의 `@pytest.mark.req("P1-2")`
- 해당 ID 테스트가 1개 이상 있고 전부 통과 → 체크
- 하나라도 실패 → 체크 해제 (회귀 표시)
- 테스트가 없거나 건너뜀(skip)이 섞여 있으면 → 건드리지 않음
- `(수동)` 항목은 자동 변경 대상 아님
- to_do 블록의 `checked` 만 바꾸고 문구는 수정하지 않음

토큰: OS 보안 저장소(keyring) 의 voice-bridge/notion-token. 없으면 경고만 하고 0 으로 끝냄(--strict 면 1).

    python tools/notion_sync.py reports/junit.xml [--dry-run] [--page <id>]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

DEFAULT_PAGE = "3ea2f90c5d40819087e0ef1ca0421d20"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
ID_RE = re.compile(r"\[(P\d+-\d+)\]")

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"


# ---------- JUnit ----------

def parse_junit(paths: list[Path]) -> dict[str, list[str]]:
    """ID → 테스트 결과 목록."""
    out: dict[str, list[str]] = {}
    for p in paths:
        root = ET.parse(p).getroot()
        for tc in root.iter("testcase"):
            ids = [pr.get("value") for pr in tc.iter("property") if pr.get("name") == "req"]
            if not ids:
                continue
            if tc.find("failure") is not None or tc.find("error") is not None:
                res = FAILED
            elif tc.find("skipped") is not None:
                res = SKIPPED
            else:
                res = PASSED
            for rid in ids:
                out.setdefault(rid, []).append(res)
    return out


def decide(results: dict[str, list[str]]) -> dict[str, bool]:
    """ID → 목표 체크 상태. 결정 불가(테스트 없음·skip 포함)는 빠짐."""
    plan: dict[str, bool] = {}
    for rid, rs in results.items():
        if not rs:
            continue
        if FAILED in rs:
            plan[rid] = False
        elif all(r == PASSED for r in rs):
            plan[rid] = True
    return plan


# ---------- Notion ----------

def _plain(rich: list[dict]) -> str:
    return "".join(r.get("plain_text") or r.get("text", {}).get("content", "") for r in rich)


class Notion:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.c = client or httpx.Client(base_url=NOTION_API, timeout=30)
        self.h = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                  "Content-Type": "application/json"}

    def children(self, block_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = self.c.get(f"/blocks/{block_id}/children", headers=self.h, params=params)
            r.raise_for_status()
            d = r.json()
            out.extend(d.get("results", []))
            if not d.get("has_more"):
                return out
            cursor = d.get("next_cursor")

    def todos(self, page_id: str) -> list[dict]:
        """페이지 안 to_do 블록 전부 (중첩 포함)."""
        found, stack = [], [page_id]
        while stack:
            for b in self.children(stack.pop()):
                if b.get("type") == "to_do":
                    found.append(b)
                if b.get("has_children") and b.get("type") not in ("child_page", "child_database"):
                    stack.append(b["id"])
        return found

    def set_checked(self, block_id: str, checked: bool) -> None:
        r = self.c.patch(f"/blocks/{block_id}", headers=self.h, json={"to_do": {"checked": checked}})
        r.raise_for_status()


def todo_items(blocks: list[dict]) -> list[dict]:
    items = []
    for b in blocks:
        text = _plain(b["to_do"].get("rich_text", []))
        m = ID_RE.search(text)
        if not m:
            continue
        items.append({"block_id": b["id"], "id": m.group(1), "text": text,
                      "manual": "(수동)" in text, "checked": bool(b["to_do"].get("checked"))})
    return items


def sync(notion: Notion, page_id: str, plan: dict[str, bool], dry_run: bool = False) -> list[dict]:
    changes = []
    for it in todo_items(notion.todos(page_id)):
        if it["manual"] or it["id"] not in plan:
            continue
        want = plan[it["id"]]
        if want != it["checked"]:
            changes.append({"id": it["id"], "checked": want, "block_id": it["block_id"]})
            if not dry_run:
                notion.set_checked(it["block_id"], want)
    return changes


def load_token() -> str | None:
    try:
        import keyring

        return keyring.get_password("voice-bridge", "notion-token")
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("junit", nargs="+", type=Path)
    ap.add_argument("--page", default=DEFAULT_PAGE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan-only", action="store_true", help="노션에 접속하지 않고 ID별 목표 상태만 출력")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)

    paths = [p for p in a.junit if p.exists()]
    plan = decide(parse_junit(paths))
    if a.plan_only:
        print(json.dumps(plan, ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    token = load_token()
    if not token:
        print("[notion_sync] 노션 토큰 없음 (voice-bridge token set-notion). 계획만 출력:", file=sys.stderr)
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1 if a.strict else 0
    changes = sync(Notion(token), a.page, plan, dry_run=a.dry_run)
    for c in changes:
        print(f"[notion_sync] {c['id']} → {'체크' if c['checked'] else '해제'}{' (dry-run)' if a.dry_run else ''}")
    if not changes:
        print("[notion_sync] 변경 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
