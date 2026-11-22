#!/usr/bin/env python3
"""市场状态 + 行情主线 + 仓位建议报告（研究用途，不构成投资建议）。

等价于 task-7 的独立入口：拉取指数/情绪/板块数据，生成
``data/reports/regime.json``（schema=1）并打印人读摘要。

Example:
    python scripts/regime_report.py
    python scripts/regime_report.py --out data/reports/regime.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quant.config import cfg_path, load_config  # noqa: E402
from quant.data.regime import build_regime_snapshot  # noqa: E402

_KIND_LABEL = {"industry": "行业", "concept": "概念"}


def _fmt(value, spec: str = ".2f", suffix: str = "") -> str:
    """None 安全格式化。"""
    if value is None:
        return "N/A"
    try:
        return format(float(value), spec) + suffix
    except (TypeError, ValueError):
        return str(value)


def summary_lines(snapshot: dict) -> list[str]:
    """把 regime.json 摘要成终端可读的若干行。"""
    state = snapshot.get("market_state") or {}
    trend = snapshot.get("trend") or {}
    emotion = snapshot.get("emotion") or {}
    position = snapshot.get("position") or {}
    leaders = snapshot.get("leaders") or []

    lines: list[str] = []
    lines.append(
        f"市场状态：{state.get('label', 'N/A')}（{state.get('state', 'N/A')}）"
        f"状态分 {_fmt(state.get('score'), '.1f')}/100"
    )
    lines.append(
        f"趋势：{trend.get('index', 'N/A')} 收盘 {_fmt(trend.get('close'))}"
        f"，MA5/20/60 = {_fmt(trend.get('ma5'))} / {_fmt(trend.get('ma20'))} / {_fmt(trend.get('ma60'))}"
        f"（{trend.get('ma_state', 'N/A')}）"
        f"，近5日 {_fmt(trend.get('ret5'), '+.2f')}%，近20日 {_fmt(trend.get('ret20'), '+.2f')}%"
    )
    lines.append(
        f"情绪：{_fmt(emotion.get('score'), '.1f')}（{emotion.get('label', 'N/A')}）"
        f"，涨跌宽度 {_fmt((emotion.get('breadth') or 0) * 100, '.1f')}%"
        f"，涨停 {emotion.get('limit_up', 0)} / 跌停 {emotion.get('limit_down', 0)}"
        f"，成交额较20日均值 {_fmt(trend.get('amount_ratio'), '.2f')} 倍"
    )
    if leaders:
        lines.append(f"行情主线 Top{min(3, len(leaders))}：")
        for idx, leader in enumerate(leaders[:3], 1):
            lines.append(
                f"  {idx}. {leader.get('name', 'N/A')}"
                f"（{_KIND_LABEL.get(leader.get('kind'), leader.get('kind', 'N/A'))}）"
                f" 当日 {_fmt(leader.get('pct_1d'), '+.2f')}%"
                f" / 5日 {_fmt(leader.get('pct_5d'), '+.2f')}%"
                f" / 净流入 {_fmt(leader.get('net_inflow'))} 亿"
                f" / 连续强势 {leader.get('days', 0)} 天"
                f" / {leader.get('tag', 'N/A')}"
                f" / 综合分 {_fmt(leader.get('score'), '.1f')}"
            )
    else:
        lines.append("行情主线：板块数据缺失（partial），仅输出大盘状态")

    lines.append(f"仓位建议：{position.get('range', 'N/A')}（{position.get('label', 'N/A')}）")
    lines.append(f"  {position.get('rationale', '')}")
    for trigger in position.get("triggers") or []:
        lines.append(f"  · {trigger}")
    lines.append(f"免责声明：{position.get('disclaimer', '研究用途，不构成投资建议')}")

    quality = snapshot.get("data_quality") or {}
    lines.append(f"数据质量：{quality.get('status', snapshot.get('status', 'N/A'))}")
    for source, label in (quality.get("sources") or {}).items():
        lines.append(f"  数据源 {source}: {label}")
    for warning in quality.get("warnings") or []:
        lines.append(f"  [warn] {warning}")
    for error in quality.get("errors") or []:
        lines.append(f"  [error] {error}")
    lines.append(f"trade_date={snapshot.get('trade_date')} asof={snapshot.get('asof')}")
    return lines


def _write_json(path: Path, payload: dict) -> None:
    """原子写：临时文件 + os.replace，避免半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成市场状态/行情主线/仓位建议报告（研究用途，不构成投资建议）"
    )
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--out", default=None, help="输出 JSON 路径，默认 data/reports/regime.json")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    snapshot = build_regime_snapshot(cfg)

    out = Path(args.out) if args.out else cfg_path(cfg, "data.dir") / "reports" / "regime.json"
    _write_json(out, snapshot)
    for line in summary_lines(snapshot):
        print(line)
    print(f"已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
