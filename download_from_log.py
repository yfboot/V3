#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段一补充：从「下载日志」中提取失败/异常 URL，下载到指定目录（packages/）。

职责：仅做「日志 → URL 列表 → 下载到目录」，供 flow 在阶段一后调用。
用法：python download_from_log.py [--log PATH] [--output-dir DIR]
"""

import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)


def extract_urls_from_download_log(log_path: Path):
    """从 npm_package_download.log 中提取「下载链接: <url>」。"""
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    urls = re.findall(r"下载链接:\s*(https?://\S+)", text)
    seen = set()
    out = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def safe_filename_from_url(url: str) -> str:
    name = url.split("?")[0].rstrip("/").split("/")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    if not name or name == "." or name == "..":
        name = "unknown"
    return name


def download_urls(urls, out_dir: Path, timeout: int = 60):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        fn = safe_filename_from_url(url)
        if not fn.endswith(".tgz"):
            fn = fn + ".tgz"
        dst = out_dir / fn
        if dst.exists() and dst.stat().st_size > 0:
            continue
        try:
            r = requests.get(url, stream=True, timeout=timeout)
            r.raise_for_status()
        except Exception as e:
            print(f"  跳过下载失败: {url[:60]}... {e}", flush=True)
            continue
        with dst.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def main():
    import argparse
    p = argparse.ArgumentParser(description="从下载日志提取 URL 并下载到目录")
    p.add_argument("--log", "-l", default="logs/npm_package_download.log", help="下载日志路径")
    p.add_argument("--output-dir", "-o", default="packages", help="输出目录")
    args = p.parse_args()
    base = Path(__file__).resolve().parent
    log_path = base / args.log if not Path(args.log).is_absolute() else Path(args.log)
    out_dir = base / args.output_dir if not Path(args.output_dir).is_absolute() else Path(args.output_dir)
    urls = extract_urls_from_download_log(log_path)
    if not urls:
        print("日志中未发现待补下载链接。")
        return 0
    print(f"从日志提取 {len(urls)} 条 URL，下载到 {out_dir} ...")
    download_urls(urls, out_dir)
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
