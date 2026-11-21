"""命令行入口：fetch / train / signal / backtest / paper / serve / export。

模块实现由各模块负责，CLI 只做参数解析与调度（延迟导入）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from quant.config import cfg_path, load_config


def _symbols(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def cmd_fetch(args: argparse.Namespace) -> int:
    from quant.data.fetch import sync_daily, sync_minute
    from quant.data.store import init_db

    cfg = load_config(args.config)
    init_db(cfg_path(cfg, "data.db"))
    stats = sync_daily(_symbols(args.symbols), args.start, args.end, cfg)
    print(json.dumps({"daily": stats}, ensure_ascii=False))
    if args.minute:
        m = sync_minute(_symbols(args.symbols), cfg["data"]["minute_period"], cfg)
        print(json.dumps({"minute": m}, ensure_ascii=False))
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    from quant.model.train import train

    cfg = load_config(args.config)
    metrics = train(_symbols(args.symbols) if args.symbols else cfg["symbols"], cfg)
    print(json.dumps({"metrics": metrics}, ensure_ascii=False, indent=2))
    return 0


def cmd_signal(args: argparse.Namespace) -> int:
    from quant.data.store import init_db, write_signal
    from quant.model.predict import generate_signal

    cfg = load_config(args.config)
    db = cfg_path(cfg, "data.db")
    init_db(db)
    symbols = _symbols(args.symbols) if args.symbols else cfg["symbols"]
    signals = [generate_signal(s, cfg) for s in symbols]

    # 落库：模拟盘（paper advance）只读 signals 表；单条失败不中断
    written = 0
    for sig in signals:
        try:
            write_signal(sig, db)
            written += 1
        except Exception as exc:  # noqa: BLE001 - 集成时容错
            print(f"[warn] 信号落库失败 {sig.get('symbol')}: {exc}", file=sys.stderr)

    out = Path(args.out) if args.out else cfg_path(cfg, "data.dir") / "reports" / "signals.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(signals), "written_to_db": written, "out": str(out)}, ensure_ascii=False))
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from quant.backtest.engine import run_backtest

    cfg = load_config(args.config)
    result = run_backtest(args.start, args.end, cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    from quant.paper.engine import advance, init_account, snapshot

    cfg = load_config(args.config)
    db = cfg_path(cfg, "data.db")
    if args.init_cash:
        init_account(float(args.init_cash), db)
    snap = advance(args.advance, cfg) if args.advance else snapshot(cfg)
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from quant.api.server import run

    cfg = load_config(args.config)
    run(port=args.port, cfg=cfg)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from quant.export_cloud import export_to_cloudflare

    cfg = load_config(args.config)
    result = export_to_cloudflare(cfg)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quant", description="个人量化工作台 CLI")
    p.add_argument("--config", default=None, help="配置文件路径（默认 config/default.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="拉取行情数据")
    f.add_argument("--symbols", default="600519,000001,300750")
    f.add_argument("--start", default="2022-01-01")
    f.add_argument("--end", default=None)
    f.add_argument("--minute", action="store_true", help="同时拉分钟线")
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser("train", help="训练并导出模型")
    t.add_argument("--symbols", default=None)
    t.add_argument("--horizon", default="1d")
    t.set_defaults(func=cmd_train)

    s = sub.add_parser("signal", help="生成最新信号")
    s.add_argument("--symbols", default=None)
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_signal)

    b = sub.add_parser("backtest", help="回测并生成报告")
    b.add_argument("--start", default=None)
    b.add_argument("--end", default=None)
    b.set_defaults(func=cmd_backtest)

    pa = sub.add_parser("paper", help="模拟盘")
    pa.add_argument("--init-cash", type=float, default=None)
    pa.add_argument("--advance", type=int, default=0, help="推进交易日数")
    pa.set_defaults(func=cmd_paper)

    sv = sub.add_parser("serve", help="本地 API + 看板")
    sv.add_argument("--port", type=int, default=8787)
    sv.set_defaults(func=cmd_serve)

    ex = sub.add_parser("export", help="导出/推送到 Cloudflare")
    ex.add_argument("--target", default="cloudflare")
    ex.set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ImportError as exc:  # 模块未实现时给出清晰提示
        print(f"[未实现] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
