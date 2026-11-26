"""本地数据服务（FastAPI，默认 :8899）：直连公开行情源 + 短 TTL 缓存。

为什么是本地服务
----------------
社区主流做法（REFERENCE.md 2026-11 结论）：**本地 Python 服务直连数据源 + 前端**，
Cloudflare 只做静态托管/快照兜底。免费源里的「秒级」上限就是客户端 3 秒轮询；
腾讯 ``qt.gtimg.cn`` CORS 开放、``web.ifzq.gtimg.cn`` 分时本机可用，新浪列表分页可用，
东财（push2）只在本机出口可用 → 实时抓取放本地最稳，云端 Worker 不再承担高频拉取。

端点（返回统一带 ``schema`` / ``ok``；数据源失败返回明确错误，绝不编造数据）
----------------------------------------------------------------------------
- ``GET /api/health``                        健康检查 ``{ok, time, ...}``
- ``GET /api/quote?codes=sh000001,sz399001`` 腾讯 ``qt.gtimg.cn`` 批量实时（GBK），缓存 2s
- ``GET /api/intraday?code=sh000001``        腾讯 ``web.ifzq.gtimg.cn`` 当日分时（242 点）
- ``GET /api/market-all?page=1&num=100``     新浪 ``Market_Center.getHQNodeData`` 全市场分页，缓存 60s
- ``GET /api/board``                         新浪行业板块（涨幅/领涨），缓存 60s

复用
----
- ``quant.data.market.MAJOR_INDICES``：指数代码/中文名（本模块再补科创50/中证500/中证1000）；
- 情绪分 / 涨跌家数等深度指标仍由云端 Worker 的 market-overview 提供，本服务只做"实时行情"，
  这样两端职责清晰：本地=秒级行情，云端=快照/历史/信号。

运行::

    python -m quant.local_server                 # 127.0.0.1:8899
    python -m quant.local_server --host 0.0.0.0 --port 8899

公网访问请配合 ``scripts/tunnel.sh``（cloudflared trycloudflare 临时隧道）；
详见 ``docs/LOCAL_SERVER.md``。

社区参考
--------
- Cloudflare Quick Tunnels（trycloudflare，无需账号）：
  https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/
- 九章·ALGOR ``server.py``（腾讯批量行情 GBK 解析同款思路）：
  https://github.com/Ksyniko/jiuzhang-algor
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:  # 复用数据层已有的指数名称定义；数据层不可用时用内置兜底
    from quant.data.market import MAJOR_INDICES as _MAJOR_INDICES
except Exception:  # pragma: no cover - 数据层导入失败不影响本地服务
    _MAJOR_INDICES = []

logger = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
SERVICE_NAME = "personal-quant-local"
VERSION = "1.0.0"
DEFAULT_PORT = 8899
DEFAULT_CODES = ["sh000001", "sz399001", "sz399006", "sh000300"]
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

#: 缓存 TTL（秒）
QUOTE_TTL = 2.0
INTRADAY_TTL = 5.0
MARKET_ALL_TTL = 60.0
BOARD_TTL = 60.0
MARKET_COUNT_TTL = 300.0

#: 指数代码 → 中文名（云端 Worker intradayName 同口径；优先复用 data.market.MAJOR_INDICES）
_INDEX_NAMES: dict[str, str] = {code: name for code, name in _MAJOR_INDICES}
_INDEX_NAMES.update(
    {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
        "sh000300": "沪深300",
        "sh000688": "科创50",
        "sh000905": "中证500",
        "sh000852": "中证1000",
    }
)
INDEX_CODES: list[str] = list(_INDEX_NAMES.keys())

#: 腾讯 qt.gtimg.cn 返回：v_sh000001="1~上证指数~000001~3476.88~...";
_QUOTE_LINE_RE = re.compile(r'v_((?:sh|sz|bj)\d{6})="([^"]*)"')

#: 新浪行业板块 JS：var S_Finance_bankuai_sinaindustry = {...};
_SINA_BOARD_RE = re.compile(
    r"var\s+S_Finance_bankuai_sinaindustry\s*=\s*(\{.*\})\s*;?\s*$", re.DOTALL
)

#: 支持的指数代码格式（分时接口只允许指数，避免误请求个股）
_CODE_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$")
_SIX_RE = re.compile(r"^\d{6}$")

_STARTED_AT = time.monotonic()


# ---------------------------------------------------------------------------
# 错误 / 缓存 / HTTP 小工具
# ---------------------------------------------------------------------------
class DataSourceError(RuntimeError):
    """上游数据源不可用（超时/限流/解析失败）。API 返回明确错误，不编造。"""

    def __init__(self, source: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.source = source
        self.message = message
        self.status_code = status_code


class TTLCache:
    """线程安全的短 TTL 内存缓存（免费源防限流的最小实现，参考 dsh-stock-terminal）。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, value = item
            if time.monotonic() < expires:
                return value
            self._data.pop(key, None)
            return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)

    def get_or_set(self, key: str, ttl: float, factory: Callable[[], Any]) -> tuple[Any, bool]:
        """返回 ``(value, from_cache)``；miss 时调用 factory 并写入缓存。"""
        cached = self.get(key)
        if cached is not None:
            return cached, True
        value = factory()
        self.set(key, value, ttl)
        return value, False

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            alive = sum(1 for expires, _ in self._data.values() if expires > now)
            return {"entries": len(self._data), "alive": alive}

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_cache = TTLCache()
_client_lock = threading.Lock()
_client: httpx.Client | None = None


def _http_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=httpx.Timeout(8.0, connect=5.0),
                follow_redirects=True,
                headers={"user-agent": BROWSER_UA, "accept": "*/*"},
            )
        return _client


def _get_bytes(
    url: str,
    *,
    source: str,
    referer: str | None = None,
    attempts: int = 2,
    timeout: float = 8.0,
) -> bytes:
    """GET 原始字节（GBK 接口自行 decode）。失败重试一次后抛 ``DataSourceError``。"""
    headers = {"referer": referer} if referer else None
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = _http_client().get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:  # noqa: BLE001 - 上游异常统一封装
            last = exc
            if attempt + 1 < attempts:
                time.sleep(0.25)
    raise DataSourceError(source, f"{source} 请求失败: {last}") from last


def _get_text(
    url: str,
    *,
    source: str,
    encoding: str = "utf-8",
    referer: str | None = None,
    attempts: int = 2,
) -> str:
    raw = _get_bytes(url, source=source, referer=referer, attempts=attempts)
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError as exc:  # pragma: no cover - 编码名固定
        raise DataSourceError(source, f"{source} 编码 {encoding} 不支持: {exc}") from exc


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today_shanghai() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _f(value: Any) -> float | None:
    """宽松转 float（空串/破折号/无效值 → None，不编造 0）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "—", "null", "None"}:
        return None
    try:
        out = float(text)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _i(value: Any) -> int | None:
    out = _f(value)
    return int(out) if out is not None else None


def normalize_code(raw: str) -> str:
    """``600519`` → ``sh600519``；``sh000001`` 原样返回；非法抛 ValueError。"""
    code = str(raw or "").strip().lower()
    if _CODE_RE.match(code):
        return code
    if _SIX_RE.match(code):
        if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
            return "sh" + code
        if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
            return "sz" + code
        if code.startswith(("4", "8", "9")):
            return "bj" + code
        return "sh" + code
    raise ValueError(f"非法代码: {raw!r}（需如 600519 / sh000001 / sz399001）")


def _split_codes(raw: str | None, default: list[str] | None = None) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return list(default or DEFAULT_CODES)
    out: list[str] = []
    for piece in re.split(r"[,\s]+", text):
        if not piece:
            continue
        code = normalize_code(piece)
        if code not in out:
            out.append(code)
    if not out:
        raise ValueError("codes 不能为空")
    if len(out) > 60:
        raise ValueError("一次最多 60 个代码（腾讯批量接口上限）")
    return out


# ---------------------------------------------------------------------------
# 数据源解析（纯函数，便于排查/复用）
# ---------------------------------------------------------------------------
def _parse_tencent_time(raw: str) -> str:
    """``20261126161427`` → ``2026-11-26 16:14:27``（格式异常原样返回）。"""
    text = str(raw or "").strip()
    if len(text) >= 14 and text[:14].isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}:{text[12:14]}"
    return text


def _quote_amount(fields: list[str]) -> float | None:
    """成交额（元）：优先取 ``price/volume/amount`` 组合字段的第三段（精确到元），
    否则退回字段 37 的「万元」值 ×10000。"""
    if len(fields) > 35 and "/" in fields[35]:
        parts = fields[35].split("/")
        if len(parts) >= 3:
            exact = _f(parts[2])
            if exact is not None:
                return round(exact, 2)
    amount_wan = _f(fields[37]) if len(fields) > 37 else None
    return round(amount_wan * 10000.0, 2) if amount_wan is not None else None


def parse_tencent_quote(text: str) -> list[dict[str, Any]]:
    """腾讯批量行情文本 → 行情 dict 列表（字段按社区实测索引解析）。"""
    items: list[dict[str, Any]] = []
    for match in _QUOTE_LINE_RE.finditer(text):
        code, raw = match.group(1), match.group(2)
        fields = raw.split("~")
        if len(fields) < 40:
            continue
        price = _f(fields[3])
        prev_close = _f(fields[4])
        if price is None or price <= 0:
            # 停牌/退市/无报价：不编造价格，交给调用方过滤
            continue
        pct = _f(fields[32])
        if pct is None and prev_close:
            pct = round((price / prev_close - 1.0) * 100.0, 4)
        amount = _quote_amount(fields)
        items.append(
            {
                "code": code,
                "name": fields[1].strip(),
                "price": round(price, 4),
                "prev_close": prev_close,
                "open": _f(fields[5]),
                "high": _f(fields[33]),
                "low": _f(fields[34]),
                "change": _f(fields[31]),
                "pct_chg": pct,
                "amount": amount,
                "volume": _i(fields[6]),
                "time": _parse_tencent_time(fields[30]),
            }
        )
    return items


def fetch_quotes(codes: list[str]) -> dict[str, Any]:
    """批量实时行情：腾讯 ``qt.gtimg.cn``（GBK），按 50 个/批请求后合并。"""
    chunks = [codes[i : i + 50] for i in range(0, len(codes), 50)]
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    source = "qt.gtimg.cn"
    for chunk in chunks:
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk) + "&_=" + str(int(time.time() * 1000))
        try:
            text = _get_text(url, source=source, encoding="gbk", referer="https://gu.qq.com/")
        except DataSourceError as exc:
            errors.append(str(exc))
            continue
        parsed = parse_tencent_quote(text)
        items.extend(parsed)
    if not items and errors:
        raise DataSourceError(source, "；".join(errors))
    by_code = {item["code"]: item for item in items}
    ordered = [by_code[code] for code in codes if code in by_code]
    missing = [code for code in codes if code not in by_code]
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "source": source,
        "codes": codes,
        "count": len(ordered),
        "items": ordered,
        "missing": missing,
        "updated_at": now_iso(),
        "degraded": bool(errors),
    }
    if errors:
        result["errors"] = errors
    return result


def parse_tencent_intraday(code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """腾讯 ``web.ifzq.gtimg.cn`` minute JSON → 分时 payload（契约形状对齐云端 /api/intraday）。"""
    node = ((payload.get("data") or {}).get(code) or {}) if isinstance(payload, dict) else {}
    inner = node.get("data") or {}
    rows = inner.get("data") or []
    raw_date = str(inner.get("date") or "")
    if not rows:
        raise DataSourceError("web.ifzq.gtimg.cn", f"分时数据为空: {code}", status_code=503)
    if len(raw_date) >= 8 and raw_date[:8].isdigit():
        trade_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    else:
        trade_date = today_shanghai()
    qt_fields = ((node.get("qt") or {}).get(code) or []) if isinstance(node.get("qt"), dict) else []
    name = _INDEX_NAMES.get(code) or (str(qt_fields[1]).strip() if len(qt_fields) > 1 else code)
    pre_close = _f(qt_fields[4]) if len(qt_fields) > 4 else None
    quote_price = _f(qt_fields[3]) if len(qt_fields) > 3 else None

    is_index = code in _INDEX_NAMES
    points: list[dict[str, Any]] = []
    prev_cum_volume = 0.0
    for line in rows:
        parts = str(line).strip().split()
        if len(parts) < 2:
            continue
        raw_time = parts[0]
        price = _f(parts[1])
        if price is None:
            continue
        cum_volume = _f(parts[2]) if len(parts) > 2 else None
        cum_amount = _f(parts[3]) if len(parts) > 3 else None
        minute_volume = None
        vwap = None
        if cum_volume is not None:
            minute_volume = max(0, int(cum_volume - prev_cum_volume))
            prev_cum_volume = cum_volume
        if not is_index and cum_volume and cum_volume > 0 and cum_amount and cum_amount > 0:
            # 个股（volume 单位=手）：累计成交额 / (累计成交量×100) = 分时均价（VWAP）
            vwap = round(cum_amount / (cum_volume * 100.0), 4)
        time_text = f"{raw_time[0:2]}:{raw_time[2:4]}" if len(raw_time) == 4 else raw_time
        points.append(
            {
                "time": time_text,
                "price": price,
                "avg_price": vwap,
                "volume": minute_volume,
                "cum_volume": int(cum_volume) if cum_volume is not None else None,
                "cum_amount": round(cum_amount, 2) if cum_amount is not None else None,
            }
        )
    if not points:
        raise DataSourceError("web.ifzq.gtimg.cn", f"分时数据解析为空: {code}", status_code=503)
    last_price = quote_price if quote_price is not None and quote_price > 0 else points[-1]["price"]
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "source": "web.ifzq.gtimg.cn",
        "code": code,
        "name": name,
        "trade_date": trade_date,
        "pre_close": pre_close,
        "last_price": last_price,
        "count": len(points),
        "points": points,
        "updated_at": now_iso(),
        "stale": trade_date != today_shanghai(),
    }


def fetch_intraday(code: str) -> dict[str, Any]:
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + code
    text = _get_text(url, source="web.ifzq.gtimg.cn", encoding="utf-8", referer="https://gu.qq.com/")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataSourceError("web.ifzq.gtimg.cn", f"分时 JSON 解析失败: {exc}") from exc
    parsed = parse_tencent_intraday(code, payload)
    # 与云端 /api/intraday 同形状：外层元信息 + data{code,name,trade_date,pre_close,points}
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "source": parsed["source"],
        "code": code,
        "trade_date": parsed["trade_date"],
        "updated_at": parsed["updated_at"],
        "stale": parsed["stale"],
        "count": parsed["count"],
        "data": {
            "code": code,
            "name": parsed["name"],
            "trade_date": parsed["trade_date"],
            "pre_close": parsed["pre_close"],
            "points": [
                {"time": point["time"], "price": point["price"], "avg_price": point["avg_price"], "volume": point["volume"]}
                for point in parsed["points"]
            ],
        },
    }


# ---------------------------------------------------------------------------
# 新浪：全市场分页 + 行业板块
# ---------------------------------------------------------------------------
SINA_HQ_BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
SINA_NODES = {"hs_a": "沪深A股", "sh_a": "沪市A股", "sz_a": "深市A股", "bj_a": "北交所"}
SINA_MARKET_SORTS = {"symbol", "changepercent", "amount", "turnoverratio", "trade", "volume"}


def _sina_total_count(node: str) -> int | None:
    url = f"{SINA_HQ_BASE}/Market_Center.getHQNodeStockCount?node={node}"
    try:
        text = _get_text(url, source="sina_market_center", encoding="utf-8", referer="https://finance.sina.com.cn/")
    except DataSourceError:
        return None
    text = text.strip().strip('"').strip()
    try:
        return int(text)
    except ValueError:
        return None


def fetch_market_all(
    page: int = 1, num: int = 100, node: str = "hs_a", sort: str = "symbol", asc: int = 1
) -> dict[str, Any]:
    params = {
        "page": page,
        "num": num,
        "sort": sort,
        "asc": 1 if asc else 0,
        "node": node,
        "symbol": "",
        "_s_r_a": "page",
    }
    url = f"{SINA_HQ_BASE}/Market_Center.getHQNodeData?{urlencode(params)}"
    text = _get_text(url, source="sina_market_center", encoding="utf-8", referer="https://finance.sina.com.cn/")
    try:
        rows = json.loads(text) if text.strip() else []
    except json.JSONDecodeError as exc:
        raise DataSourceError("sina_market_center", f"全市场列表 JSON 解析失败: {exc}") from exc
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise DataSourceError("sina_market_center", "全市场列表返回结构异常（非数组）")
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if not code or not symbol:
            continue
        items.append(
            {
                "symbol": symbol,
                "code": code,
                "name": str(row.get("name") or "").strip(),
                "price": _f(row.get("trade")),
                "prev_close": _f(row.get("settlement")),
                "open": _f(row.get("open")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "change": _f(row.get("pricechange")),
                "pct_chg": _f(row.get("changepercent")),
                "volume": _i(row.get("volume")),
                "amount": _f(row.get("amount")),
                "turnover": _f(row.get("turnoverratio")),
                "ticktime": str(row.get("ticktime") or ""),
            }
        )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "source": "sina_market_center",
        "node": node,
        "node_label": SINA_NODES.get(node, node),
        "page": page,
        "num": num,
        "sort": sort,
        "asc": bool(asc),
        "count": len(items),
        "items": items,
        "updated_at": now_iso(),
    }


def parse_sina_boards(text: str) -> tuple[list[dict[str, Any]], str]:
    """新浪行业板块 JS（GBK）→ 板块列表 + 数据时间。"""
    match = _SINA_BOARD_RE.search(text)
    if not match:
        raise DataSourceError("sina_industry", "行业板块数据解析失败（未找到 JS 变量）")
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DataSourceError("sina_industry", f"行业板块 JSON 解析失败: {exc}") from exc
    items: list[dict[str, Any]] = []
    for code, value in raw.items():
        fields = str(value).split(",")
        if len(fields) < 13:
            continue
        items.append(
            {
                "code": code,
                "name": fields[1].strip(),
                "count": _i(fields[2]),
                "avg_price": _f(fields[3]),
                "avg_change": _f(fields[4]),
                "pct_chg": _f(fields[5]),
                "volume": _i(fields[6]),
                "amount": _f(fields[7]),
                "leader": {
                    "code": fields[8].strip(),
                    "name": fields[12].strip(),
                    "pct_chg": _f(fields[9]),
                    "price": _f(fields[10]),
                    "change": _f(fields[11]),
                },
            }
        )
    items.sort(key=lambda item: item.get("pct_chg") if item.get("pct_chg") is not None else -1e9, reverse=True)
    return items, now_iso()


def fetch_boards(limit: int = 30, sort: str = "pct_chg") -> dict[str, Any]:
    url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    text = _get_text(url, source="sina_industry", encoding="gbk", referer="https://finance.sina.com.cn/")
    items, updated_at = parse_sina_boards(text)
    if sort == "amount":
        items.sort(key=lambda item: item.get("amount") or 0.0, reverse=True)
    elif sort != "pct_chg":
        raise ValueError("sort 只支持 pct_chg / amount")
    if limit and limit > 0:
        items = items[: int(limit)]
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "source": "sina_industry",
        "type": "industry",
        "count": len(items),
        "items": items,
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title="个人量化工作台 · 本地数据服务",
        description=(
            "本地直连公开行情源（腾讯/新浪）的只读数据服务，供前端 3 秒轮询；"
            "研究用途，不构成投资建议。"
        ),
        version=VERSION,
        docs_url="/docs",
        redoc_url=None,
    )
    # 允许 Cloudflare Pages / 任意静态页跨域调用本地隧道
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.exception_handler(DataSourceError)
    def _data_source_error(_request: Any, exc: DataSourceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "schema": SCHEMA_VERSION,
                "ok": False,
                "error": "data_unavailable",
                "source": exc.source,
                "message": exc.message,
                "updated_at": now_iso(),
            },
            headers={"cache-control": "no-store"},
        )

    try:  # FastAPI 参数校验错误也统一成 {ok:false,error:"invalid_params"} 形状
        from fastapi.exceptions import RequestValidationError

        @app.exception_handler(RequestValidationError)
        def _validation_error(_request: Any, exc: RequestValidationError) -> JSONResponse:
            return JSONResponse(
                status_code=400,
                content={
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "error": "invalid_params",
                    "message": "请求参数不合法",
                    "detail": exc.errors(),
                    "updated_at": now_iso(),
                },
            )
    except Exception:  # pragma: no cover - FastAPI 版本差异时保持默认 422
        pass

    @app.exception_handler(ValueError)
    def _value_error(_request: Any, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "schema": SCHEMA_VERSION,
                "ok": False,
                "error": "invalid_params",
                "message": str(exc),
                "updated_at": now_iso(),
            },
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """健康检查：不访问上游，仅返回本地状态（供隧道/前端探活）。"""
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "service": SERVICE_NAME,
            "version": VERSION,
            "time": now_iso(),
            "pid": os.getpid(),
            "uptime_sec": round(time.monotonic() - _STARTED_AT, 1),
            "cache": _cache.stats(),
            "endpoints": [
                "/api/health",
                "/api/quote?codes=sh000001,sz399001",
                "/api/intraday?code=sh000001",
                "/api/market-all?page=1&num=100",
                "/api/board",
            ],
            "cache_ttl_sec": {
                "quote": QUOTE_TTL,
                "intraday": INTRADAY_TTL,
                "market_all": MARKET_ALL_TTL,
                "board": BOARD_TTL,
            },
        }

    @app.get("/api/quote")
    def quote(codes: str | None = Query(None, description="逗号分隔代码，支持 600519/sh600519")) -> dict[str, Any]:
        """腾讯批量实时报价（GBK 解码），缓存 2s。"""
        code_list = _split_codes(codes)
        cache_key = "quote:" + ",".join(code_list)
        payload, cached = _cache.get_or_set(cache_key, QUOTE_TTL, lambda: fetch_quotes(code_list))
        payload = dict(payload)
        payload["cached"] = cached
        return payload

    @app.get("/api/intraday")
    def intraday(code: str = Query("sh000001", description="指数代码，如 sh000001 / sz399001")) -> dict[str, Any]:
        """腾讯当日分时（242 点，时间/价格/分时量/均价），缓存 5s。"""
        target = normalize_code(code)
        if not target.startswith(("sh0", "sz399")):
            # 分时接口仅对指数稳定；个股走 /api/kline（云端）或后续扩展
            raise ValueError("分时接口当前只支持指数代码（如 sh000001 / sz399001 / sz399006 / sh000300）")
        payload, cached = _cache.get_or_set(
            "intraday:" + target, INTRADAY_TTL, lambda: fetch_intraday(target)
        )
        payload = dict(payload)
        payload["cached"] = cached
        return payload

    @app.get("/api/market-all")
    def market_all(
        page: int = Query(1, ge=1, le=200, description="页码（从 1 开始）"),
        num: int = Query(100, ge=20, le=100, description="每页条数（20–100）"),
        node: str = Query("hs_a", description="hs_a / sh_a / sz_a / bj_a"),
        sort: str = Query("symbol", description="symbol/changepercent/amount/turnoverratio/trade/volume"),
        asc: int = Query(1, ge=0, le=1, description="1=升序，0=降序"),
    ) -> dict[str, Any]:
        """新浪全市场行情分页（5580 只），缓存 60s。"""
        if node not in SINA_NODES:
            raise ValueError(f"node 只支持 {sorted(SINA_NODES)}")
        if sort not in SINA_MARKET_SORTS:
            raise ValueError(f"sort 只支持 {sorted(SINA_MARKET_SORTS)}")
        cache_key = f"market-all:{node}:{page}:{num}:{sort}:{asc}"
        total_key = "market-all-total:" + node

        def build() -> dict[str, Any]:
            payload = fetch_market_all(page=page, num=num, node=node, sort=sort, asc=asc)
            total, _ = _cache.get_or_set(
                total_key, MARKET_COUNT_TTL, lambda: _sina_total_count(node)
            )
            payload["total"] = total
            return payload

        payload, cached = _cache.get_or_set(cache_key, MARKET_ALL_TTL, build)
        payload = dict(payload)
        payload["cached"] = cached
        return payload

    @app.get("/api/board")
    def board(
        limit: int = Query(30, ge=1, le=100, description="返回板块数"),
        sort: str = Query("pct_chg", description="pct_chg / amount"),
    ) -> dict[str, Any]:
        """新浪行业板块（涨幅 / 领涨股），缓存 60s。"""
        cache_key = f"board:{limit}:{sort}"
        payload, cached = _cache.get_or_set(cache_key, BOARD_TTL, lambda: fetch_boards(limit=limit, sort=sort))
        payload = dict(payload)
        payload["cached"] = cached
        return payload

    @app.get("/")
    def index() -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "service": SERVICE_NAME,
            "version": VERSION,
            "time": now_iso(),
            "docs": "/docs",
            "hint": "行情接口见 /docs；公网访问请用 scripts/tunnel.sh 起 cloudflared 隧道",
        }

    return app


app = create_app()


def run(host: str = "127.0.0.1", port: int = DEFAULT_PORT, log_level: str = "info") -> None:
    """启动本地服务（uvicorn）。"""
    import uvicorn

    print(f"[local-server] {SERVICE_NAME} v{VERSION} → http://{host}:{int(port)}/docs")
    print("[local-server] 公网隧道：bash scripts/tunnel.sh（详见 docs/LOCAL_SERVER.md）")
    uvicorn.run(app, host=host, port=int(port), log_level=log_level)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quant-local-server",
        description="本地行情数据服务（FastAPI，直连腾讯/新浪；研究用途）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1；隧道只需本地）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 8899）")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别")
    args = parser.parse_args(argv)
    run(host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
