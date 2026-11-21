"""配置加载与路径解析（Lead 维护）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取 YAML 配置，缺省用 config/default.yaml。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    with cfg_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def project_path(rel: str | Path) -> Path:
    """相对路径按项目根目录解析，绝对路径原样返回。"""
    p = Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT / p


def cfg_path(cfg: dict, key: str) -> Path:
    """按 'data.db' 这类点路径取配置值并解析为项目内绝对路径。"""
    node: Any = cfg
    for part in key.split("."):
        node = node[part]
    return project_path(node)
