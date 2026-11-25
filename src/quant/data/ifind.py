"""同花顺 iFinD HTTP API 接入（免费版，官方数据源）。

认证链路：refresh_token（长期）→ access_token（7 天有效，最多 20 IP）
接口：
- 实时行情 POST /api/v1/real_time_quotation
- 历史行情 POST /api/v1/history_quotes
- 高频序列（分钟）POST /api/v1/high_frequency
- 交易日历 POST /api/v1/get_trade_dates

免费额度（月）：实时 300 万 / 历史 100 万 / 日内快照 200 万 / 基础数据 60 万
免费年限：历史行情最近 5 年、日内快照最近 3 个月、高频最近 1 年。

refresh_token 获取：iFinD 网页版「超级命令 → 账号信息」查看；
本地放在 /root/.quant_secrets/ifind.env（QUANT_IFIND_REFRESH_TOKEN=...）或环境变量。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

BASE = "https://quantapi.51ifind.com/api/v1"
SECRET_FILE = Path("/root/.quant_secrets/ifind.env")
TOKEN_CACHE = Path("data/cache/ifind_token.json")

HISTORY_INDICATORS = "preClose,open,high,low,close,volume,amount,changeRatio,turnoverRatio"
REALTIME_INDICATORS = "latest,open,high,low,preClose,changeRatio,volume,amount,turnoverRatio"


def _read_refresh_token() -> str:
    token = os.environ.get("QUANT_IFIND_REFRESH_TOKEN", "").strip()
    if token:
        return token
    if SECRET_FILE.is_file():
        for line in SECRET_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("QUANT_IFIND_REFRESH_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "缺少 iFinD refresh_token：设置环境变量 QUANT_IFIND_REFRESH_TOKEN "
        "或写入 /root/.quant_secrets/ifind.env"
    )


def get_access_token(force: bool = False) -> str:
    """refresh_token 换 access_token；本地缓存 6 天（access_token 7 天过期）。"""
    if not force and TOKEN_CACHE.is_file():
        try:
            cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            if float(cached.get("expires_at", 0)) > time.time() + 3600:
                return str(cached["access_token"])
        except (OSError, ValueError, KeyError):
            pass
    resp = httpx.post(
        f"{BASE}/get_access_token",
        headers={"Content-Type": "application/json", "refresh_token": _read_refresh_token()},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errorcode") != 0:
        raise RuntimeError(f"iFinD access_token 获取失败：{payload.get('errmsg') or payload}")
    token = str(payload["data"]["access_token"])
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(
        json.dumps({"access_token": token, "expires_at": time.time() + 6 * 86400}),
        encoding="utf-8",
    )
    return token


def to_thscode(symbol: str) -> str:
    """sh600519 / 600519 / 000001.SZ → iFinD 代码（600519.SH / 000001.SZ）。"""
    raw = symbol.strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        code, exchange = raw[2:], raw[:2].upper()
    elif "." in raw:
        code, exchange = raw.split(".", 1)
        exchange = exchange.upper()
    else:
        code = raw
        exchange = "SH" if code.startswith(("6", "9", "5")) else "SZ"
    return f"{code}.{exchange}"


def _call(endpoint: str, body: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    token = get_access_token()
    resp = httpx.post(
        f"{BASE}/{endpoint}",
        headers={"Content-Type": "application/json", "access_token": token},
        json=body,
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errorcode") not in (0, None):
        raise RuntimeError(f"iFinD {endpoint} 失败：{payload.get('errmsg') or payload}")
    return payload


def _tables_to_frame(payload: dict[str, Any]) -> pd.DataFrame:
    """tables: [{thscode, time: [...], table: {指标: [...]}}] → 长表。"""
    rows: list[dict[str, Any]] = []
    for table in payload.get("tables") or []:
        thscode = str(table.get("thscode", ""))
        times = table.get("time") or []
        data = table.get("table") or {}
        if not isinstance(data, dict):
            continue
        length = len(times) or max((len(v) for v in data.values() if isinstance(v, list)), default=0)
        for idx in range(length):
            row: dict[str, Any] = {"thscode": thscode, "time": times[idx] if idx < len(times) else None}
            for key, values in data.items():
                if isinstance(values, list) and idx < len(values):
                    row[key] = values[idx]
            rows.append(row)
    return pd.DataFrame(rows)


def fetch_history(
    codes: list[str],
    start: str,
    end: str,
    indicators: str = HISTORY_INDICATORS,
    cps: int = 2,
) -> pd.DataFrame:
    """历史行情（日频，CPS=2 前复权分红再投）。"""
    body = {
        "codes": ",".join(to_thscode(code) for code in codes),
        "indicators": indicators,
        "startdate": start,
        "enddate": end,
        "functionpara": {"CPS": str(cps), "Fill": "Blank", "Interval": "D"},
    }
    frame = _tables_to_frame(_call("history_quotes", body))
    if frame.empty:
        return frame
    frame["symbol"] = frame["thscode"].str.split(".").str[0]
    frame["trade_date"] = pd.to_datetime(frame["time"]).dt.strftime("%Y-%m-%d")
    return frame


def fetch_realtime(codes: list[str], indicators: str = REALTIME_INDICATORS) -> pd.DataFrame:
    """实时行情（免费版实时行情 300 万/月）。"""
    frame = _tables_to_frame(_call("real_time_quotation", {"codes": ",".join(to_thscode(c) for c in codes), "indicators": indicators}))
    if frame.empty:
        return frame
    frame["symbol"] = frame["thscode"].str.split(".").str[0]
    return frame


def fetch_intraday(symbol: str, start: str, end: str, interval: str = "1") -> pd.DataFrame:
    """分钟级高频序列（免费版最近 1 年；日内快照最近 3 个月）。"""
    body = {
        "codes": to_thscode(symbol),
        "indicators": "open,high,low,close,volume,amount,avgPrice",
        "starttime": start,
        "endtime": end,
        "functionpara": {"Interval": interval, "Fill": "Original"},
    }
    frame = _tables_to_frame(_call("high_frequency", body))
    if not frame.empty:
        frame["symbol"] = symbol
    return frame


if __name__ == "__main__":
    # 自检：打印 token 与前一日历史行情
    token = get_access_token()
    print("access_token 获取成功：", token[:12], "...")
    frame = fetch_history(["600519", "000001"], "2026-11-01", "2026-11-24")
    print(frame.tail(4).to_string())
