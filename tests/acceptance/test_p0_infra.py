"""P0 준비 및 자동 체크 인프라 인수 테스트."""
import json
import sys
import tomllib

import httpx
import pytest

from tests.conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import notion_sync as ns  # noqa: E402


@pytest.mark.req("P0-1")
def test_repo_layout_and_pytest_env():
    for d in ("bridge", "tests/acceptance", "tools"):
        assert (ROOT / d).is_dir(), d
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("req(") for m in markers)


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
 <testcase classname="a" name="t1"><properties><property name="req" value="P1-1"/></properties></testcase>
 <testcase classname="a" name="t2"><properties><property name="req" value="P1-1"/></properties></testcase>
 <testcase classname="a" name="t3"><properties><property name="req" value="P1-2"/></properties><failure message="x"/></testcase>
 <testcase classname="a" name="t4"><properties><property name="req" value="P1-2"/></properties></testcase>
 <testcase classname="a" name="t5"><properties><property name="req" value="P1-3"/></properties><skipped message="live"/></testcase>
 <testcase classname="a" name="t6"><properties><property name="req" value="P1-4"/></properties></testcase>
 <testcase classname="a" name="t7"><properties><property name="req" value="P2-1"/></properties><error message="e"/></testcase>
 <testcase classname="a" name="t8"></testcase>
</testsuite></testsuites>"""


@pytest.fixture
def junit(tmp_path):
    p = tmp_path / "junit.xml"
    p.write_text(JUNIT)
    return p


def todo(bid, text, checked=False, children=False):
    return {"object": "block", "id": bid, "type": "to_do", "has_children": children,
            "to_do": {"rich_text": [{"plain_text": text}], "checked": checked}}


class FakeNotion:
    """블록 조회·to_do 갱신만 흉내 내는 Notion API."""

    def __init__(self):
        self.blocks = {
            "page": [
                {"object": "block", "id": "h", "type": "heading_2", "has_children": False, "heading_2": {}},
                todo("b11", "[P1-1] pty_runner: 기동·종료"),
                todo("b12", "[P1-2] 프롬프트 주입", checked=True),
                todo("b13", "[P1-3] Esc 중단", checked=True),
                todo("b14", "[P1-4] (수동) Code 탭에 보임"),
                todo("b21", "[P2-1] transcript 위치", checked=True, children=True),
                todo("b99", "태그 없는 항목"),
            ],
            "b21": [todo("b22", "[P2-2] 중첩 항목")],
        }
        self.patches = []
        self.page_size_seen = []

    def handler(self, req: httpx.Request):
        assert req.headers["authorization"] == "Bearer tok"
        assert req.headers["notion-version"]
        parts = req.url.path.strip("/").split("/")
        if req.method == "GET" and parts[-1] == "children":
            bid = parts[-2]
            items = self.blocks.get(bid, [])
            cursor = req.url.params.get("start_cursor")
            start = int(cursor) if cursor else 0
            page = items[start:start + 3]
            more = start + 3 < len(items)
            return httpx.Response(200, json={"results": page, "has_more": more,
                                             "next_cursor": str(start + 3) if more else None})
        if req.method == "PATCH":
            body = json.loads(req.content)
            assert set(body) == {"to_do"} and set(body["to_do"]) == {"checked"}  # 문구는 건드리지 않음
            self.patches.append((parts[-1], body["to_do"]["checked"]))
            return httpx.Response(200, json={})
        return httpx.Response(404)


@pytest.mark.req("P0-2")
def test_junit_to_plan(junit):
    plan = ns.decide(ns.parse_junit([junit]))
    assert plan == {"P1-1": True, "P1-2": False, "P1-4": True, "P2-1": False}
    assert "P1-3" not in plan  # skip 은 판단 보류


@pytest.mark.req("P0-2")
def test_sync_updates_only_checked_state(junit):
    fake = FakeNotion()
    client = httpx.Client(base_url=ns.NOTION_API, transport=httpx.MockTransport(fake.handler))
    plan = ns.decide(ns.parse_junit([junit]))
    changes = ns.sync(ns.Notion("tok", client), "page", plan)
    assert sorted(fake.patches) == [("b11", True), ("b12", False), ("b21", False)]
    # (수동) 항목은 테스트가 통과해도 건드리지 않음, 태그 없는 항목·중첩 항목도 규칙대로
    assert all(c["id"] != "P1-4" for c in changes)
    # 다시 돌리면(상태 반영 후) 변경 없음 → 멱등
    for bid, val in fake.patches:
        for blocks in fake.blocks.values():
            for b in blocks:
                if b["id"] == bid:
                    b["to_do"]["checked"] = val
    fake.patches.clear()
    ns.sync(ns.Notion("tok", client), "page", plan)
    assert fake.patches == []


@pytest.mark.req("P0-2")
def test_dry_run_makes_no_writes(junit):
    fake = FakeNotion()
    client = httpx.Client(base_url=ns.NOTION_API, transport=httpx.MockTransport(fake.handler))
    changes = ns.sync(ns.Notion("tok", client), "page", {"P1-1": True}, dry_run=True)
    assert changes and fake.patches == []


@pytest.mark.req("P0-2")
def test_missing_token_is_not_fatal(junit, monkeypatch, capsys):
    monkeypatch.setattr(ns, "load_token", lambda: None)
    assert ns.main([str(junit)]) == 0
    assert ns.main([str(junit), "--strict"]) == 1


@pytest.mark.req("P0-3")
def test_make_verify_runs_tests_then_sync():
    mk = (ROOT / "Makefile").read_text()
    body = mk.split("verify:")[1].split("\n\n")[0]
    assert "--junitxml" in body and "notion_sync.py" in body
    assert body.index("pytest") < body.index("notion_sync.py")


@pytest.mark.req("P0-3")
def test_stop_hook_runs_make_verify():
    path = ROOT / ".claude" / "settings.json"
    if not path.exists():  # 개인 설정이라 레포에 없다 (CI·새 클론) → 판단 보류
        pytest.skip("개인 Claude Code 훅 설정(.claude/settings.json)이 없음 — README 'Stop 훅 연결' 참고")
    settings = json.loads(path.read_text(encoding="utf-8"))
    cmds = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert any("make" in c and "verify" in c for c in cmds)
