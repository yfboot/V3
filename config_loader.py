#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 config.local 读取公共配置，供 flow.py、publish.py、clear_repository.py 使用。
敏感信息不写在各脚本内，统一在 config.local 配置（模板见 config.template）。
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


def get_nexus_config(base_dir: Path) -> Tuple[str, str, str, str]:
    """
    从 config.local 读取 Nexus 配置。
    返回 (base_url, repository, username, password)，未配置的项为空字符串。
    """
    cfg = load_config(base_dir)
    base_url = (cfg.get("NEXUS_BASE_URL") or "").strip().rstrip("/")
    repository = (cfg.get("NEXUS_REPOSITORY") or "").strip()
    username = (cfg.get("NEXUS_USERNAME") or "").strip()
    password = (cfg.get("NEXUS_PASSWORD") or "").strip()
    return (base_url, repository, username, password)
