#!/usr/bin/env python3
"""生成 Kaggle 内核（notebook + kernel-metadata.json）

两个内核
--------
- ``probe``：只探测环境（TF/JAX/PyTorch 版本、TPU 设备、内存磁盘），3 分钟内出结果；
- ``main`` ：完整流程（环境自检 → 装 TF-TPU（如缺）→ clone 仓库 → 抓数据 → 训练 → 打包产物）。

设计取舍：把重逻辑放在仓库 ``training/tpu/nn_tf.py`` 里（可本地 review/测试），
notebook 只做「环境 + 一条命令」，这样 Kaggle 上失败时看日志就能定位到具体脚本行。

用法
----
    python training/tpu/kernels.py --mode probe --out training/tpu/kernel_probe --user <kaggle 用户名>
    python training/tpu/kernels.py --mode main  --out training/tpu/kernel_main  --user <kaggle 用户名> --epochs 12 --folds 6
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

NB_METADATA = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.11"},
}


def _cell(kind: str, source: str) -> dict:
    lines = source.strip("\n").split("\n")
    src = [ln + "\n" for ln in lines[:-1]] + [lines[-1]]
    cell = {"cell_type": kind, "metadata": {}, "source": src}
    if kind == "code":
        cell.update({"execution_count": None, "outputs": []})
    return cell


def _notebook(cells: list[dict]) -> dict:
    return {"cells": cells, "metadata": NB_METADATA, "nbformat": 4, "nbformat_minor": 5}


PROBE_CELLS = [
    _cell("markdown", """
# TPU 环境探针（3 分钟）

只回答一个问题：**这台 Kaggle 机器上，TensorFlow / JAX / PyTorch 哪个版本、TPU 能不能拿到。**

结果写进 `env_probe.json`（随 Output 一起下载），用来决定正式训练用哪条路线。
"""),
    _cell("code", """
import json, platform, subprocess, sys, os, importlib, shutil

report = {
    "python": sys.version.split()[0],
    "platform": platform.platform(),
    "cpu_count": os.cpu_count(),
}
try:
    with open("/proc/meminfo") as f:
        report["mem_total_kb"] = int([l for l in f if l.startswith("MemTotal")][0].split()[1])
except Exception:
    pass
report["disk_free_gb"] = round(shutil.disk_usage("/").free / 1e9, 1)
report["kaggle_working_files"] = sorted(os.listdir("/kaggle/working")) if os.path.isdir("/kaggle/working") else []

def pkg_version(mod):
    try:
        m = importlib.import_module(mod)
        return getattr(m, "__version__", "unknown")
    except Exception as exc:
        return f"MISSING ({type(exc).__name__})"

report["packages"] = {m: pkg_version(m) for m in
                      ("tensorflow", "jax", "jaxlib", "torch", "torch_xla", "pandas", "numpy", "pyarrow", "lightgbm")}
print(json.dumps(report, ensure_ascii=False, indent=2))
"""),
    _cell("code", """
# TPU 探测：谁真的能拿到 8 核？
tpu = {"tensorflow": None, "jax": None}

try:
    import tensorflow as tf
    info = {"version": tf.__version__}
    try:
        resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu="local")
        tf.config.experimental_connect_to_cluster(resolver)
        tf.tpu.experimental.initialize_tpu_system(resolver)
        strategy = tf.distribute.TPUStrategy(resolver)
        info.update({"ok": True, "cores": strategy.num_replicas_in_sync,
                     "devices": [d.name for d in tf.config.list_logical_devices("TPU")]})
    except Exception as exc:
        info.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    tpu["tensorflow"] = info
except Exception as exc:
    tpu["tensorflow"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

try:
    import jax
    tpu["jax"] = {"version": jax.__version__, "devices": [str(d) for d in jax.devices()],
                  "device_kind": [getattr(d, "device_kind", "?") for d in jax.devices()]}
except Exception as exc:
    tpu["jax"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

report["tpu"] = tpu
print(json.dumps(tpu, ensure_ascii=False, indent=2))
with open("/kaggle/working/env_probe.json", "w") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print("已写 /kaggle/working/env_probe.json")
"""),
]


def main_notebook(epochs: int, folds: int, baseline: bool, seq_len: int, start: str, zt_days: int,
                  symbols: str = "") -> dict:
    """生成主内核 notebook。参数用 __TOKEN__ 注入，避免 f-string 把 notebook 里的 {} 当占位符。"""
    baseline_arg = '["--baseline-lgbm"]' if baseline else "[]"
    symbols_arg = f'["--symbols", "{symbols}"]' if symbols else "[]"
    baseline_note = "，附带同折 LightGBM 对比" if baseline else ""
    cells = [
        _cell("markdown", """
# 个人量化 · Kaggle TPU 训练（GRU 序列模型，Walk-Forward）

用 **Kaggle 免费 TPU v5e-8** 跑 `training/tpu/nn_tf.py`：与 LightGBM 完全同口径的 30 特征 +
T+1 三分类标签，把过去 __SEQ_LEN__ 个交易日的特征窗口喂进 GRU(48) → MLP 头，
逐折训练、收集样本外预测。

- 预计 15~25 分钟（抓数据占一半）；数据抓取幂等，中断可直接重跑
- 产物：`out/nn_model.json`（前端/Worker 可复算的权重 JSON）、`out/nn_metrics.json`、`out/nn_oof.parquet`
- 想快速冒烟：把训练格里的 `--epochs 1 --max-folds 1` 打开

> ⚠️ 仅供学习研究，非投资建议。
"""),
        _cell("code", """
import importlib, json, platform, subprocess, sys
print("python", sys.version.split()[0], "|", platform.platform())
for mod in ("tensorflow", "jax", "torch", "pandas", "numpy", "pyarrow", "lightgbm"):
    try:
        m = importlib.import_module(mod)
        print(f"{mod:12s}", getattr(m, "__version__", "?"))
    except Exception as exc:
        print(f"{mod:12s} 缺失（{type(exc).__name__}）")
"""),
        _cell("code", """
# 镜像里没有 TensorFlow 时，从 libtpu 官方索引装 TPU 版
import importlib.util, subprocess, sys

if importlib.util.find_spec("tensorflow") is None:
    for version in ("2.19.0", "2.18.0", "2.17.0"):
        print(f"尝试 pip install tensorflow-tpu=={version} ...", flush=True)
        cmd = [sys.executable, "-m", "pip", "install", "-q", f"tensorflow-tpu=={version}",
               "--find-links", "https://storage.googleapis.com/libtpu-tf-releases/index.html"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and importlib.util.find_spec("tensorflow"):
            print("已安装", version)
            break
        print((r.stderr or r.stdout)[-1200:])
    else:
        raise RuntimeError("TensorFlow 安装失败：看上面 pip 输出")
import tensorflow as tf
print("TensorFlow", tf.__version__)
"""),
        _cell("code", """
import os, subprocess

REPO_DIR = "/kaggle/working/personal-quant"
REPO_URL = "https://github.com/xiongkong520/personal-quant.git"
if not os.path.isdir(REPO_DIR):
    subprocess.run(["git", "clone", "--depth", "1", REPO_URL, REPO_DIR], check=True)
os.chdir(REPO_DIR)
print(subprocess.run(["git", "log", "--oneline", "-1"], capture_output=True, text=True).stdout)
"""),
        _cell("markdown", """
## 第 1 步：抓数据（近 3 年前复权日线 + 最近 __ZT_DAYS__ 个交易日涨跌停池）

AKShare 首选、Baostock 兜底；两个免费源都连不上时退回 `--synthetic` 合成数据，
**那样只能验证管道跑得通，指标没有意义**（日志里会明确写 SYNTHETIC / 失败原因）。
"""),
        _cell("code", """
import os, subprocess, sys

def run(cmd):
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd="/kaggle/working/personal-quant").returncode

rc = run([sys.executable, "training/fetch_data.py", "--start", "__START__", "--zt-days", "__ZT_DAYS__"] + __SYMBOLS_ARG__)
data_ok = os.path.isfile("/kaggle/working/personal-quant/training/data/daily.parquet")
if rc != 0 or not data_ok:
    print("⚠ 真实数据抓取失败，退回合成数据（只验证管道，指标无意义）", flush=True)
    run([sys.executable, "training/fetch_data.py", "--synthetic"])
    os.environ["SYNTHETIC_DATA"] = "1"

import pandas as pd
d = pd.read_parquet("/kaggle/working/personal-quant/training/data/daily.parquet")
print("daily=", d.shape, "symbols=", d["symbol"].nunique(), "dates=", d["trade_date"].nunique(),
      "range=", d["trade_date"].min(), "~", d["trade_date"].max())
"""),
        _cell("markdown", """
## 第 2 步：TPU 训练（__FOLDS__ 折 Walk-Forward，__EPOCHS__ epochs/折__BASELINE_NOTE__）

脚本自己找 TPU（8 核数据并行）；找不到 TPU 会回退 CPU，并明确写进日志和 `nn_metrics.json` 的 device 字段。
"""),
        _cell("code", """
import subprocess, sys

cmd = [sys.executable, "-u", "training/tpu/nn_tf.py",
           "--out-dir", "/kaggle/working/out",
           "--epochs", "__EPOCHS__", "--max-folds", "__FOLDS__", "--seq-len", "__SEQ_LEN__"] + __BASELINE_ARG__
print("$", " ".join(cmd), flush=True)
rc = subprocess.run(cmd, cwd="/kaggle/working/personal-quant").returncode
print("训练退出码:", rc)
if rc != 0:
    raise SystemExit(rc)
"""),
        _cell("code", """
import json, os, shutil
m = json.load(open("/kaggle/working/out/nn_metrics.json"))
wf = m["walk_forward"]
print("设备:", m["device"]["accelerator"], "| 副本数:", m["device"]["num_replicas"], "| TF", m["device"]["tensorflow"])
print("OOF n=", wf["n"], "acc=", round(wf["accuracy"], 4), "logloss=", round(wf["logloss"], 4),
      "win_rate=", wf["win_rate"], "brier=", round(wf["brier"], 4))
print("模型大小 MB:", m["model_size_mb"], "| parity:", m["parity_max_abs_diff"], "| 耗时 s:", m["elapsed_secs"])
if m.get("baseline", {}).get("blend"):
    b = m["baseline"]
    print("同折 LightGBM acc=", round(b["lgbm"]["accuracy"], 4), "→ 融合 w=", b["blend"]["weight_nn"],
          "acc=", round(b["blend"]["accuracy"], 4), "logloss=", round(b["blend"]["logloss"], 4))

for name in ("nn_model.json", "nn_metrics.json", "nn_oof.parquet", "env.json"):
    src = os.path.join("/kaggle/working/out", name)
    if os.path.isfile(src):
        shutil.copy(src, "/kaggle/working/" + name)
shutil.make_archive("/kaggle/working/nn_artifacts", "zip", "/kaggle/working/out")
print("产物已打包 /kaggle/working/nn_artifacts.zip")
"""),
    ]
    replacements = {
        "__SEQ_LEN__": str(seq_len),
        "__EPOCHS__": str(epochs),
        "__FOLDS__": str(folds),
        "__START__": start,
        "__ZT_DAYS__": str(zt_days),
        "__SYMBOLS_ARG__": symbols_arg,
        "__BASELINE_ARG__": baseline_arg,
        "__BASELINE_NOTE__": baseline_note,
    }
    for cell in cells:
        cell["source"] = [replacements.get(line, line) if False else line for line in cell["source"]]
        joined = "".join(cell["source"])
        for key, value in replacements.items():
            joined = joined.replace(key, value)
        lines = joined.split("\n")
        cell["source"] = [ln + "\n" for ln in lines[:-1]] + [lines[-1]]
    return _notebook(cells)


def kernel_metadata(user: str, slug: str, title: str, code_file: str) -> dict:
    return {
        "id": f"{user}/{slug}",
        "title": title,
        "code_file": code_file,
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": False,
        "enable_tpu": True,
        "enable_internet": True,
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }


def write_kernel(out_dir: Path, notebook: dict, metadata: dict, code_file: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / code_file).write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "kernel-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 Kaggle 内核目录")
    ap.add_argument("--mode", choices=["probe", "main"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--user", default="")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--seq-len", type=int, default=20)
    ap.add_argument("--start", default="2024-06-01")
    ap.add_argument("--zt-days", type=int, default=30)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--baseline", action="store_true")
    args = ap.parse_args(argv)

    user = args.user or "USERNAME_NOT_SET"
    out = Path(args.out)
    if args.mode == "probe":
        slug = args.slug or "quant-tpu-probe"
        nb, code_file = _notebook(PROBE_CELLS), "env_probe.ipynb"
        meta = kernel_metadata(user, slug, "Quant TPU env probe", code_file)
    else:
        slug = args.slug or "quant-tpu-gru"
        nb = main_notebook(args.epochs, args.folds, args.baseline, args.seq_len, args.start, args.zt_days, args.symbols)
        code_file = "tpu_nn.ipynb"
        meta = kernel_metadata(user, slug, "Quant TPU GRU walk-forward", code_file)
    write_kernel(out, nb, meta, code_file)
    print(f"已生成 {out}（id={meta['id']}, enable_tpu={meta['enable_tpu']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
