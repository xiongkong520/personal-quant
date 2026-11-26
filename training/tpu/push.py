#!/usr/bin/env python3
"""Kaggle TPU 一键驱动：登录检查 → 生成内核 → 推送 → 轮询 → 取回产物

前置：这台机器能上 Kaggle（梯子），并且已经登录一次（二选一）
    python3 -m kaggle auth login --no-launch-browser     # OAuth：浏览器授权一次
    export KAGGLE_API_TOKEN=xxxx                          # 或 Kaggle 设置页的 API Token

常用
----
    python training/tpu/push.py auth                       # 看看登录状态 / 剩余配额
    python training/tpu/push.py run --mode probe           # 探针：3 分钟内告诉我们 TPU 能不能用
    python training/tpu/push.py run --mode main            # 正式训练（推送→等待→下载→回填）
    python training/tpu/push.py status --mode main --watch # 只看状态
    python training/tpu/push.py fetch --mode main          # 只下载产物并回填仓库

产物回填
--------
- ``model/nn_model.json``            ← 上线用的权重 JSON（前端/Worker 可复算）
- ``training/tpu/last_run.json``     ← 最近一次跑的完整指标（含设备、折、parity）
- ``training/tpu/last_env.json``     ← 设备/环境快照
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TPU_DIR = Path(__file__).resolve().parent
REPO_DIR = TPU_DIR.parent.parent
KAGGLE_JSON = Path.home() / ".kaggle" / "kaggle.json"
DEFAULT_DIRS = {"probe": TPU_DIR / "kernel_probe", "main": TPU_DIR / "kernel_main"}
DEFAULT_SLUGS = {"probe": "quant-tpu-env-probe", "main": "quant-tpu-gru-walk-forward"}


def kaggle(*args: str, timeout: int = 300, check: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "kaggle", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def resolve_user(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if os.environ.get("KAGGLE_USERNAME"):
        return os.environ["KAGGLE_USERNAME"]
    if KAGGLE_JSON.is_file():
        try:
            return json.loads(KAGGLE_JSON.read_text()).get("username", "")
        except Exception:  # noqa: BLE001
            pass
    out = kaggle("config", "view")
    for line in (out.stdout or "").splitlines():
        if "username" in line.lower() and ":" in line:
            value = line.split(":", 1)[1].strip()
            if value and value != "None":
                return value
    return ""


def cmd_auth(args) -> int:
    out = kaggle("config", "view")
    print((out.stdout or "").strip() or (out.stderr or "").strip())
    quota = kaggle("quota")
    text = (quota.stdout or "") + (quota.stderr or "")
    print("—— 配额 ——")
    print(text.strip()[:1500] or "（quota 命令无输出）")
    return 0 if "未登录" not in text and "Authentication required" not in text else 1


def cmd_build(args) -> int:
    user = resolve_user(args.user)
    if not user:
        print("找不到 Kaggle 用户名：先登录（python3 -m kaggle auth login --no-launch-browser）或传 --user")
        return 2
    out_dir = Path(args.dir or DEFAULT_DIRS[args.mode])
    cmd = [
        sys.executable, str(TPU_DIR / "kernels.py"),
        "--mode", args.mode, "--out", str(out_dir), "--user", user,
        "--slug", args.slug or DEFAULT_SLUGS[args.mode],
        "--epochs", str(args.epochs), "--folds", str(args.folds),
    ]
    if args.baseline:
        cmd.append("--baseline")
    r = subprocess.run(cmd, capture_output=True, text=True)
    print((r.stdout or "").strip())
    if r.returncode != 0:
        print((r.stderr or "").strip())
    return r.returncode


def cmd_push(args) -> int:
    if cmd_build(args) != 0:
        return 2
    out_dir = Path(args.dir or DEFAULT_DIRS[args.mode])
    r = kaggle("kernels", "push", "-p", str(out_dir), timeout=600)
    text = (r.stdout or "") + (r.stderr or "")
    print(text.strip()[:2000])
    return 0 if r.returncode == 0 else 2


def cmd_status(args) -> int:
    slug = _slug(args)
    watched = 0
    while True:
        r = kaggle("kernels", "status", slug)
        text = ((r.stdout or "") + (r.stderr or "")).strip()
        print(f"[{time.strftime('%H:%M:%S')}] {text[:400]}", flush=True)
        if not args.watch:
            return 0
        watched += 1
        if any(k in text for k in ("COMPLETE", "ERROR", "CANCEL", "FAILED")):
            return 0
        if watched > 240:
            print("轮询超过 240 次，放弃（可用 status 再查）")
            return 0
        time.sleep(args.interval)


def cmd_fetch(args) -> int:
    slug = _slug(args)
    dest = Path(args.dest or (TPU_DIR / "kaggle_out" / args.mode))
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    r = kaggle("kernels", "output", slug, "-p", str(dest), timeout=600)
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    print(text[-1500:])
    if r.returncode != 0:
        return 2
    return _install_artifacts(dest, args.mode)


def _install_artifacts(dest: Path, mode: str) -> int:
    print("—— 下载到的文件 ——")
    for p in sorted(dest.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(dest)}  {p.stat().st_size / 1024:.1f} KB")
    if mode == "probe":
        probe = next(dest.rglob("env_probe.json"), None)
        if probe:
            print("—— 探针结果 ——")
            print(probe.read_text()[:4000])
        return 0

    model_src = next(iter(dest.rglob("nn_model.json")), None)
    metrics_src = next(iter(dest.rglob("nn_metrics.json")), None)
    env_src = next(iter(dest.rglob("env.json")), None)
    if metrics_src is None:
        print("没拿到 nn_metrics.json：看 Kaggle 日志（kaggle kernels output 里的 .log）")
        return 2
    metrics = json.loads(metrics_src.read_text())
    if model_src is not None:
        target = REPO_DIR / "model" / "nn_model.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(model_src, target)
        print(f"已回填 {target.relative_to(REPO_DIR)}（{target.stat().st_size / 1024:.1f} KB）")
    shutil.copy(metrics_src, TPU_DIR / "last_run.json")
    if env_src is not None:
        shutil.copy(env_src, TPU_DIR / "last_env.json")
    wf = metrics.get("walk_forward", {})
    print(
        f"\n设备 {metrics.get('device', {}).get('accelerator')} | "
        f"OOF n={wf.get('n')} acc={wf.get('accuracy')} logloss={wf.get('logloss')} "
        f"win_rate={wf.get('win_rate')} | parity {metrics.get('parity_max_abs_diff')}"
    )
    return 0


def cmd_run(args) -> int:
    if getattr(args, "push", True):
        rc = cmd_push(args)
        if rc != 0:
            return rc
    time.sleep(5)
    rc = cmd_status(args)
    if rc != 0:
        return rc
    if not args.watch:
        return 0
    return cmd_fetch(args)


def _slug(args) -> str:
    if args.slug:
        return args.slug if "/" in args.slug else f"{resolve_user(args.user)}/{args.slug}"
    return f"{resolve_user(args.user)}/{DEFAULT_SLUGS[args.mode]}"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Kaggle TPU 驱动")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, need_mode: bool = True):
        if need_mode:
            p.add_argument("--mode", choices=["probe", "main"], default="main")
        p.add_argument("--user", default=None)
        p.add_argument("--slug", default=None)
        p.add_argument("--dir", default=None)

    p_auth = sub.add_parser("auth", help="登录/配额检查")
    common(p_auth, need_mode=False)
    p_auth.set_defaults(func=cmd_auth)

    p_build = sub.add_parser("build", help="只生成内核目录")
    common(p_build)
    p_build.add_argument("--epochs", type=int, default=12)
    p_build.add_argument("--folds", type=int, default=6)
    p_build.add_argument("--baseline", action="store_true")
    p_build.set_defaults(func=cmd_build)

    p_push = sub.add_parser("push", help="推送（并运行）内核")
    common(p_push)
    p_push.add_argument("--epochs", type=int, default=12)
    p_push.add_argument("--folds", type=int, default=6)
    p_push.add_argument("--baseline", action="store_true")
    p_push.set_defaults(func=cmd_push)

    p_status = sub.add_parser("status", help="查询运行状态")
    common(p_status)
    p_status.add_argument("--watch", action="store_true")
    p_status.add_argument("--interval", type=int, default=30)
    p_status.set_defaults(func=cmd_status)

    p_fetch = sub.add_parser("fetch", help="下载产物并回填仓库")
    common(p_fetch)
    p_fetch.add_argument("--dest", default=None)
    p_fetch.set_defaults(func=cmd_fetch)

    p_run = sub.add_parser("run", help="推送 → 等待 → 下载 → 回填")
    common(p_run)
    p_run.add_argument("--epochs", type=int, default=12)
    p_run.add_argument("--folds", type=int, default=6)
    p_run.add_argument("--baseline", action="store_true")
    p_run.add_argument("--watch", action="store_true", default=True)
    p_run.add_argument("--interval", type=int, default=30)
    p_run.add_argument("--dest", default=None)
    p_run.add_argument("--push", action="store_true", default=True)
    p_run.set_defaults(func=cmd_run)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
