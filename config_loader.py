#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 config.local 读取配置，供 flow.py 使用（仅本地仓库，无私有 Nexus）。
"""

from pathlib import Path
from typing import Tuple


def load_config(base_dir: Path) -> dict:
    """读取 config.local，返回键值对（键大写，值已 strip）。"""
    cfg = {}
    path = base_dir / "config.local"
    if not path.exists():
        return cfg
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip().upper()] = v.strip().strip("'\"").rstrip("/")
    return cfg


def get_local_registry_config(base_dir: Path) -> Tuple[int]:
    """从 config.local 读取本地 registry 端口，默认 4874。"""
    cfg = load_config(base_dir)
    try:
        port = int((cfg.get("LOCAL_REGISTRY_PORT") or "4874").strip())
    except ValueError:
        port = 4874
    return (port,)
