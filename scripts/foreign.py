"""境外信息代理（本机 → Cloudflare Worker → 境外站点）。

用法（命令行）：
    python scripts/foreign.py get https://raw.githubusercontent.com/xiongkong520/personal-quant/main/README.md
    python scripts/foreign.py post https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent '{"contents":[...]}'

Python 调用：
    from scripts.foreign import foreign_get, foreign_post
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

WORKER = os.environ.get("QUANT_WORKER_URL", "https://quant-board-9y2.pages.dev")
SECRET_FILE = Path("/root/.quant_secrets/cloudflare.env")


def _token() -> str:
    token = os.environ.get("QUANT_INGEST_TOKEN", "").strip()
    if token:
        return token
    if SECRET_FILE.is_file():
        for line in SECRET_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("QUANT_INGEST_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("缺少 QUANT_INGEST_TOKEN")


def foreign_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """通过 Worker 代理 GET 境外 URL，返回 {status, body, content_type}。"""
    resp = httpx.post(
        f"{WORKER}/api/proxy",
        headers={"x-ingest-token": _token(), "content-type": "application/json"},
        json={"url": url, "method": "GET", "headers": headers or {}},
        timeout=60,
    )
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"代理失败：{payload}")
    return payload


def foreign_post(url: str, body: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """通过 Worker 代理 POST 境外 URL。"""
    resp = httpx.post(
        f"{WORKER}/api/proxy",
        headers={"x-ingest-token": _token(), "content-type": "application/json"},
        json={"url": url, "method": "POST", "headers": headers or {}, "body": body},
        timeout=90,
    )
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"代理失败：{payload}")
    return payload


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "get"
    url = sys.argv[2] if len(sys.argv) > 2 else "https://www.google.com"
    if action == "post":
        body = sys.argv[3] if len(sys.argv) > 3 else "{}"
        print(json.dumps(foreign_post(url, body), ensure_ascii=False)[:800])
    else:
        result = foreign_get(url)
        print("status:", result.get("status"), "| type:", result.get("content_type"))
        print(str(result.get("body", ""))[:500])
