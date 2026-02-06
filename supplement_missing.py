#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段三补包：从 npm install 日志解析缺包（404 / not found / notarget / lacks tarball），
用 npm view 从公网取 tarball 并下载到 manual_packages/。

职责：仅做「安装日志 → 缺包列表 → 下载到目录」；不启停 registry、不执行 npm install。
用法：python supplement_missing.py --log PATH --out-dir DIR [--report-file PATH] [--base-dir DIR]
  --report-file：将本轮补包列表写入该文件（每行 name@range），供 flow 读取。
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)

NPM_PUBLIC_REGISTRY = "https://registry.npmjs.org"


def extract_404_from_npm_install_log(log_path: Path) -> List[Tuple[str, str]]:
    """从 npm install 日志解析缺包，返回 [(包名, 版本范围), ...]。"""
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    found: List[Tuple[str, str]] = []
    for m in re.finditer(r"404\s+[^']*'([^']+)@([^']+)'\s+is not in this registry", text, re.I):
        found.append((m.group(1).strip(), m.group(2).strip().rstrip(".")))
    for m in re.finditer(r"Package\s+'([^']+)'\s+not found", text, re.I):
        found.append((m.group(1).strip(), "latest"))
    for m in re.finditer(r"notarget\s+No matching version found for\s+(.+?)@(\S+)", text, re.I):
        found.append((m.group(1).strip(), m.group(2).strip().rstrip(".")))
    for m in re.finditer(r"Package\s+([^\s]+)\s+lacks\s+tarball\s+version\s+(\S+)", text, re.I):
        found.append((m.group(1).strip().rstrip("."), m.group(2).strip().rstrip(".")))
    seen = set()
    out: List[Tuple[str, str]] = []
    for name, rng in found:
        k = f"{name}@{rng}"
        if k in seen:
            continue
        seen.add(k)
        out.append((name, rng))
    return out


def safe_tarball_basename(package_name: str, version: str) -> str:
    name = (package_name or "").strip()
    ver = (version or "").strip()
    if not ver:
        return name + ".tgz"
    part = name.replace("/", "%2F") if name.startswith("@") else name
    return f"{part}-{ver}.tgz"


def get_tarball_via_npm_view(
    name: str, range_spec: str, base_dir: Path, verbose: bool = True
) -> Optional[Tuple[str, str]]:
    """用 npm view 从公网获取 tarball URL 与版本。"""
    range_spec = (range_spec or "").strip().rstrip(".")
    if name == "@tootallnate/once" and (
        not range_spec or range_spec in ("1", "1.") or re.match(r"^\d+\.?$", range_spec)
    ):
        range_spec = "2"
    spec = f"{name}@{range_spec}" if range_spec else name
    npm_exe = "npm.cmd" if sys.platform == "win32" else "npm"
    reg = f"--registry={NPM_PUBLIC_REGISTRY}"
    try:
        out = subprocess.run(
            [npm_exe, "view", spec, "dist.tarball", reg],
            capture_output=True,
            encoding="utf-8",
            timeout=25,
            cwd=str(base_dir),
        )
        if verbose:
            print(f"  执行: {npm_exe} view {spec} dist.tarball {reg}", flush=True)
        if out.returncode != 0 or not out.stdout or not out.stdout.strip():
            if verbose:
                print(f"  失败: 退出码 {out.returncode}, stderr: {(out.stderr or '')[:150]}", flush=True)
            return None
        url = out.stdout.strip()
        if not url.startswith("http"):
            return None
        out2 = subprocess.run(
            [npm_exe, "view", spec, "version", reg],
            capture_output=True,
            encoding="utf-8",
            timeout=25,
            cwd=str(base_dir),
        )
        version = (out2.stdout or "").strip() if out2.returncode == 0 else ""
        if not version:
            version = "unknown"
        if verbose:
            print(f"  得到: version={version}, tarball={url[:70]}...", flush=True)
        return (url, version)
    except Exception as e:
        if verbose:
            print(f"  异常: {e}", flush=True)
        return None


def download_via_curl(url: str, dest: Path, timeout: int = 60) -> bool:
    curl_exe = "curl.exe" if sys.platform == "win32" else "curl"
    cmd = [curl_exe, "-L", "-s", "-S", "-o", str(dest), "--connect-timeout", "15", "--max-time", str(timeout), url]
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout + 10)
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except Exception:
        return False


def download_tarballs_with_names(
    entries: List[Tuple[str, str]], out_dir: Path, base_dir: Path, timeout: int = 60
) -> List[Tuple[str, str]]:
    """将缺包列表用 npm view 取 tarball 并下载到 out_dir，返回成功下载的 [(name, range), ...]。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    supplemented: List[Tuple[str, str]] = []
    for name, rng in entries:
        print(f"缺包: {name}@{rng}", flush=True)
        pair = get_tarball_via_npm_view(name, rng, base_dir, verbose=True)
        if not pair:
            continue
        url, version = pair
        fn = safe_tarball_basename(name, version)
        dst = out_dir / fn
        if dst.exists() and dst.stat().st_size > 0:
            print(f"  已存在: {fn}", flush=True)
            supplemented.append((name, rng))
            continue
        print(f"  下载: curl -o {fn}", flush=True)
        if not download_via_curl(url, dst, timeout=timeout):
            print("  curl 失败，改用 requests 下载 ...", flush=True)
            try:
                r = requests.get(url, stream=True, timeout=timeout)
                r.raise_for_status()
                with dst.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            except Exception as e:
                print(f"  下载失败: {e}", flush=True)
                continue
        if dst.exists() and dst.stat().st_size > 0:
            print(f"  已写入: {dst}", flush=True)
            supplemented.append((name, rng))
        else:
            print("  下载失败", flush=True)
    return supplemented


def run(
    log_path: Path,
    out_dir: Path,
    base_dir: Path,
    only_new: Optional[List[Tuple[str, str]]] = None,
    timeout: int = 60,
) -> List[Tuple[str, str]]:
    """
    解析 log 中的缺包，下载到 out_dir。若提供 only_new，则只下载该子集（用于 flow 去重）。
    返回本轮成功下载的 [(name, range), ...]（含已存在而跳过的）。
    """
    missing = extract_404_from_npm_install_log(log_path)
    if not missing:
        return []
    to_download = only_new if only_new is not None else missing
    if not to_download:
        return []
    return download_tarballs_with_names(to_download, out_dir, base_dir, timeout=timeout)


def _parse_name_range(line: str) -> Tuple[str, str]:
    """解析 'name@range' 或 '@scope/name@version'，从最后一个 @ 分割（scoped 包名内可含 @）。"""
    if "@" not in line:
        return (line.strip(), "")
    name, rng = line.strip().rsplit("@", 1)
    return (name.strip(), rng.strip())


def parse_only_new_file(path: Path) -> List[Tuple[str, str]]:
    """读取每行 name@range，返回 [(name, range), ...]。scoped 包为 @scope/name@version，用 rsplit 解析。"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "@" in line:
            name, rng = _parse_name_range(line)
            if name and rng:
                out.append((name, rng))
    return out


def main():
    import argparse
    p = argparse.ArgumentParser(description="从 npm install 日志补包到 manual_packages")
    p.add_argument("--log", "-l", default="logs/npm_install.log", help="npm install 日志路径")
    p.add_argument("--out-dir", "-o", default="manual_packages", help="补包输出目录")
    p.add_argument("--base-dir", "-b", default=".", help="工作目录（npm view 的 cwd）")
    p.add_argument("--report-file", "-r", default="", help="将本轮补包列表写入该文件，每行 name@range")
    p.add_argument("--only-new-file", default="", help="仅补这些包（每行 name@range），不填则补日志中全部缺包")
    args = p.parse_args()
    base_dir = Path(args.base_dir).resolve()
    log_path = base_dir / args.log if not Path(args.log).is_absolute() else Path(args.log)
    out_dir = base_dir / args.out_dir if not Path(args.out_dir).is_absolute() else Path(args.out_dir)
    only_new = None
    if args.only_new_file:
        only_new_path = base_dir / args.only_new_file if not Path(args.only_new_file).is_absolute() else Path(args.only_new_file)
        only_new = parse_only_new_file(only_new_path)
        if not only_new:
            if args.report_file:
                report = base_dir / args.report_file if not Path(args.report_file).is_absolute() else Path(args.report_file)
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text("", encoding="utf-8")
            return 0
    supplemented = run(log_path, out_dir, base_dir, only_new=only_new)
    if args.report_file:
        report = base_dir / args.report_file if not Path(args.report_file).is_absolute() else Path(args.report_file)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(f"{n}@{r}" for n, r in supplemented) + ("\n" if supplemented else ""), encoding="utf-8")
    return 0 if supplemented or not extract_404_from_npm_install_log(log_path) else 2  # 2 = 有缺包但未补到


if __name__ == "__main__":
    raise SystemExit(main())
