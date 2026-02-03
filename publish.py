#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nexus npm 包批量上传脚本

默认行为：不跳过，直接上传（Nexus 若允许覆盖会返回 200，再传即覆盖）。
“已存在则跳过”需使用 --skip-existing：会先请求 Nexus 查询该包是否已存在，存在则不上传（多一轮请求）。
部分 Nexus 配置为允许覆盖，再传时返回 200 而非 400，故仅靠 400 无法实现“存在则跳过”，必须用 --skip-existing 时才会先查再跳。
"""

import os
import re
import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows 控制台 UTF-8
if sys.platform == "win32":
    import io
    import locale
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    enc = locale.getpreferredencoding()
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding=enc, errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding=enc, errors="replace")

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)

from config_loader import get_nexus_config

# ========== 配置（优先从 config.local 读取 Nexus，可被命令行覆盖） ==========
_SCRIPT_DIR = Path(__file__).resolve().parent
_NEXUS_BASE, _NEXUS_REPO, _NEXUS_USER, _NEXUS_PASS = get_nexus_config(_SCRIPT_DIR)
BASE_URL = _NEXUS_BASE
REPOSITORY = _NEXUS_REPO
USERNAME = _NEXUS_USER
PASSWORD = _NEXUS_PASS

PACKAGES_PATH = "./packages"
UPLOAD_LOG = "logs/publish.log"
MAX_WORKERS = 50
TIMEOUT = 60

# 当仓库中已存在同版本依赖时：False=直接上传/覆盖（默认），True=先查存在则跳过（需多一轮请求）
SKIP_IF_EXISTS = False


def parse_args():
    p = argparse.ArgumentParser(description="批量上传 .tgz 到 Nexus npm 仓库（支持外网/内网）")
    p.add_argument("--base-url", "-u", default=BASE_URL, help="Nexus 地址")
    p.add_argument("--repository", "-r", default=REPOSITORY, help="仓库名称")
    p.add_argument("--username", default=USERNAME, help="用户名")
    p.add_argument("--password", default=PASSWORD, help="密码")
    p.add_argument("--packages-path", "-p", default=PACKAGES_PATH, help=".tgz 所在目录")
    p.add_argument("--workers", "-w", type=int, default=MAX_WORKERS, help="并发上传数")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--skip-existing", action="store_true", help="已存在则跳过（先查 Nexus 再决定是否上传）")
    g.add_argument("--overwrite", action="store_true", help="已存在则覆盖（先删后传）；默认即为覆盖/直接上传")
    return p.parse_args()


def collect_tgz_files(packages_path):
    """收集目录下所有 .tgz 文件路径"""
    path = Path(packages_path)
    if not path.is_dir():
        return []
    return sorted(path.glob("**/*.tgz"), key=lambda p: p.name)


# 从 .tgz 文件名解析包名与版本，如 lodash-4.17.21.tgz -> (lodash, 4.17.21)，@babel/core-7.0.0.tgz -> (@babel/core, 7.0.0)
_TGZ_NAME_VERSION = re.compile(r"^(.+)-(\d+\.\d+\.\d+(?:[-.]\w+)*)\.tgz$", re.IGNORECASE)


def parse_tgz_name(filename):
    """返回 (name, version) 或 (None, None)。name 中的 %2f 会还原为 /。"""
    m = _TGZ_NAME_VERSION.match(filename)
    if not m:
        return (None, None)
    name = m.group(1).replace("%2f", "/").replace("%2F", "/")
    return (name, m.group(2))


def find_component_id(base_url, repository, auth, name, version, timeout):
    """在仓库中按 name/version 查找组件 id，未找到返回 None。"""
    url = f"{base_url.rstrip('/')}/service/rest/v1/components"
    params = {"repository": repository}
    while True:
        r = requests.get(url, auth=auth, params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        for item in data.get("items") or []:
            n = (item.get("name") or "").strip()
            g = (item.get("group") or "").strip()
            if g:
                n = f"{g}/{n}" if n else g
            if n == name and (item.get("version") or "").strip() == version:
                return item.get("id")
        token = data.get("continuationToken")
        if not token:
            return None
        params = {"repository": repository, "continuationToken": token}


def delete_component(base_url, component_id, auth, timeout):
    """删除指定 id 的组件，成功返回 True。"""
    url = f"{base_url.rstrip('/')}/service/rest/v1/components/{component_id}"
    try:
        r = requests.delete(url, auth=auth, timeout=timeout)
        return r.status_code in (200, 204)
    except Exception:
        return False


def upload_one(base_url, repository, auth, filepath, timeout, skip_if_exists):
    """
    上传单个 .tgz。返回 (filename, 'success'|'skipped'|'overwritten'|'failure', message)。
    skip_if_exists 为 True 时：先查 Nexus 是否已有该包版本，有则返回 skipped（真正跳过）。
    否则直接 POST；200 为 success；400 且 "does not allow updating" 时先删后传（overwritten）。
    """
    url = f"{base_url.rstrip('/')}/service/rest/v1/components?repository={repository}"
    filename = filepath.name
    name, version = parse_tgz_name(filename)

    # 需要“存在则跳过”时，先查是否已存在，存在则直接跳过（不依赖 400）
    if skip_if_exists and name and version:
        cid = find_component_id(base_url, repository, auth, name, version, timeout)
        if cid:
            return (filename, "skipped", "already exists")

    try:
        with open(filepath, "rb") as f:
            files = {"npm.asset": (filename, f, "application/gzip")}
            r = requests.post(url, auth=auth, files=files, timeout=timeout)
        if r.status_code in (200, 201, 204):
            return (filename, "success", None)
        if r.status_code == 400 and "does not allow updating" in (r.text or ""):
            # 未走“先查跳过”时，400 表示已存在且不允许覆盖，则先删后传
            if not name or not version:
                return (filename, "failure", "already exists, cannot parse name/version for overwrite")
            cid = find_component_id(base_url, repository, auth, name, version, timeout)
            if not cid:
                return (filename, "failure", "already exists, component id not found for overwrite")
            if not delete_component(base_url, cid, auth, timeout):
                return (filename, "failure", "already exists, delete failed for overwrite")
            with open(filepath, "rb") as f2:
                files2 = {"npm.asset": (filename, f2, "application/gzip")}
                r2 = requests.post(url, auth=auth, files=files2, timeout=timeout)
            if r2.status_code in (200, 201, 204):
                return (filename, "overwritten", None)
            return (filename, "failure", f"overwrite re-upload HTTP {r2.status_code} {r2.text[:200]}")
        return (filename, "failure", f"HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        return (filename, "failure", str(e))


def main():
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    auth = (args.username, args.password)
    packages_path = Path(args.packages_path)
    if args.overwrite:
        skip_if_exists = False
    elif args.skip_existing:
        skip_if_exists = True
    else:
        skip_if_exists = SKIP_IF_EXISTS

    files = collect_tgz_files(packages_path)
    if not files:
        print(f"在 {packages_path} 下未找到任何 .tgz 文件")
        return

    total = len(files)
    print("============================================")
    print("  Nexus npm 包批量上传（publish）")
    print("============================================")
    print(f"  地址: {base_url}")
    print(f"  仓库: {args.repository}")
    print(f"  包数: {total}")
    print(f"  并发: {args.workers}")
    print(f"  已存在: {'跳过（先查后传）' if skip_if_exists else '直接上传/覆盖'}")
    print("============================================\n")

    os.makedirs(os.path.dirname(UPLOAD_LOG) or ".", exist_ok=True)
    success_count = 0
    skipped_count = 0
    overwritten_count = 0
    failed = []
    bar_width = 40

    def progress_bar(done, total, success, skipped, overwritten, failed_count):
        pct = (done * 100) // total if total else 0
        filled = (done * bar_width) // total if total else 0
        bar = "#" * filled + " " * (bar_width - filled)
        return f"\r  [{bar}] {pct}% | 成功: {success} 跳过: {skipped} 覆盖: {overwritten} 失败: {failed_count} | {done}/{total}"

    t0 = time.time()
    done = 0

    # 每次运行覆盖日志文件，不追加；仅保留本次执行的完整日志
    with open(UPLOAD_LOG, "w", encoding="utf-8") as log:
        log.write(f"# Nexus npm 上传日志\n# 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.write(f"# 地址: {base_url}\n# 仓库: {args.repository}\n# 总数: {total}\n# 已存在: {'跳过（先查后传）' if skip_if_exists else '直接上传/覆盖'}\n\n")

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(upload_one, base_url, args.repository, auth, fp, TIMEOUT, skip_if_exists): fp
                for fp in files
            }
            for f in as_completed(futures):
                filename, status, msg = f.result()
                done += 1
                if status == "success":
                    success_count += 1
                    log.write(f"OK   {filename}\n")
                elif status == "skipped":
                    skipped_count += 1
                    log.write(f"SKIP {filename}  {msg}\n")
                elif status == "overwritten":
                    overwritten_count += 1
                    log.write(f"OVERWRITE {filename}\n")
                else:
                    failed.append((filename, msg))
                    log.write(f"FAIL {filename}  {msg}\n")
                print(progress_bar(done, total, success_count, skipped_count, overwritten_count, len(failed)), end="", flush=True)

    elapsed = time.time() - t0
    print(progress_bar(total, total, success_count, skipped_count, overwritten_count, len(failed)))

    print("\n============================================")
    print("  上传完成")
    print("============================================")
    print(f"  成功: {success_count}")
    print(f"  跳过: {skipped_count}（仓库中已存在）")
    if overwritten_count:
        print(f"  覆盖: {overwritten_count}")
    print(f"  失败: {len(failed)}")
    print(f"  耗时: {elapsed:.1f} 秒")
    print(f"  日志: {UPLOAD_LOG}")
    if failed:
        print("\n失败列表:")
        for fn, msg in failed[:30]:
            print(f"    {fn}  {msg}")
        if len(failed) > 30:
            print(f"    ... 共 {len(failed)} 条，详见 {UPLOAD_LOG}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(0)
    except requests.RequestException as e:
        print(f"请求错误: {e}")
        if getattr(e, "response", None) is not None:
            print(f"  状态码: {e.response.status_code}")
            print(f"  响应: {e.response.text[:500]}")
        sys.exit(1)
