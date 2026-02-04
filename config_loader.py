#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 config.local 读取公共配置，供 flow.py、publish.py、clear_repository.py 使用。
敏感信息不写在各脚本内，统一在 config.local 配置（模板见 config.template）。
"""

from pathlib import Path
from typing import Tuple
from urllib.parse import urlparse


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
    返回 (base_url, repository, username, password)。base_url 与 repository 由 NEXUS_REGISTRY 解析得出。
    """
    cfg = load_config(base_dir)
    registry = (cfg.get("NEXUS_REGISTRY") or "").strip().rstrip("/")
    username = (cfg.get("NEXUS_USERNAME") or "").strip()
    password = (cfg.get("NEXUS_PASSWORD") or "").strip()
    base_url = ""
    repository = ""
    if registry:
        parsed = urlparse(registry)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        parts = (parsed.path or "").strip("/").split("/")
        repository = parts[-1] if parts else ""
    return (base_url, repository, username, password)


def get_private_registry(base_dir: Path) -> str:
    """从 config.local 读取 NEXUS_REGISTRY，供 flow.py 中 npm install 使用。"""
    cfg = load_config(base_dir)
    return (cfg.get("NEXUS_REGISTRY") or "").strip().rstrip("/")
