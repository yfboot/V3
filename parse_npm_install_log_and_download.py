#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
根据 logs/npm_install.log 解析缺包（404、ETARGET notarget 等），从公网查 tarball 并下载到 manual_packages/。
可单独运行，不依赖 flow 主流程。用法：
  python parse_npm_install_log_and_download.py [日志路径]
默认日志路径：logs/npm_install.log
"""

import sys
from pathlib import Path

# 与 flow 同目录，复用其解析与下载逻辑
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from flow import (
    BASE_DIR as FLOW_BASE,
    extract_404_from_npm_install_log,
    download_tarballs_with_names,
)


def main() -> int:
    log_path = FLOW_BASE / "logs" / "npm_install.log"
    if len(sys.argv) >= 2:
        log_path = Path(sys.argv[1])
    if not log_path.is_absolute():
        log_path = FLOW_BASE / log_path

    if not log_path.exists():
        print(f"日志不存在: {log_path}", flush=True)
        return 1

    missing = extract_404_from_npm_install_log(log_path)
    if not missing:
        print("日志中未解析到缺包（无 404 / Package not found / ETARGET notarget）。", flush=True)
        return 0

    print(f"解析到缺包 {len(missing)} 个：", flush=True)
    for name, rng in missing:
        print(f"  {name}@{rng}", flush=True)

    manual_dir = FLOW_BASE / "manual_packages"
    manual_dir.mkdir(parents=True, exist_ok=True)
    if any(manual_dir.iterdir()):
        import shutil
        shutil.rmtree(manual_dir)
        manual_dir.mkdir(parents=True, exist_ok=True)

    print("\n仅用 npm view 从官方 registry 取 tarball 并下载到 manual_packages/ ...", flush=True)
    supplemented = download_tarballs_with_names(missing, manual_dir)
    if not supplemented:
        print("未能解析或下载任何 tarball。", flush=True)
        return 2
    print(f"已下载 {len(supplemented)} 个到 {manual_dir}，可执行 publish.py 上传到私有仓库。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
