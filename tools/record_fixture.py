#!/usr/bin/env python3
"""실제 transcript 를 테스트 샘플로 '녹화'한다. 구조(type/subtype/stop_reason/tool_use id 등)는 보존하고
내용 문자열은 가린다.

    python tools/record_fixture.py <src.jsonl> <dst.jsonl> [--until-line N] [--keep-text]
"""
from __future__ import annotations

import argparse
import json

KEEP_KEYS = {"type", "subtype", "stop_reason", "id", "tool_use_id", "name", "role", "uuid", "parentUuid",
             "timestamp", "isSidechain", "isMeta", "isApiErrorMessage", "sessionId", "model", "promptId",
             "permissionMode", "is_error", "durationMs", "version", "entrypoint", "isCompactSummary"}


def redact(v, key=None, keep_text=False):
    if isinstance(v, dict):
        return {k: redact(x, k, keep_text) for k, x in v.items()
                if k not in ("signature", "toolUseResult", "wireToolInputs", "serverClassifierContext", "usage")}
    if isinstance(v, list):
        return [redact(x, key, keep_text) for x in v]
    if isinstance(v, str) and key not in KEEP_KEYS and not keep_text:
        if v.startswith("[Request interrupted"):
            return v
        return f"<{key or 'str'}:{len(v)}>"
    return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--until-line", type=int)
    ap.add_argument("--keep-text", action="store_true")
    a = ap.parse_args()
    lines = open(a.src, encoding="utf-8").read().splitlines()
    if a.until_line:
        lines = lines[: a.until_line]
    with open(a.dst, "w", encoding="utf-8") as f:
        for ln in lines:
            d = json.loads(ln)
            if d.get("type") in ("attachment", "file-history-snapshot", "bridge-session", "cost-state"):
                continue
            f.write(json.dumps(redact(d, keep_text=a.keep_text), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
