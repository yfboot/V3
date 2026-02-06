#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立脚本：向私有 Nexus 批量上传 .tgz，全部覆盖（已存在则先删后传）。flow 不调用，仅需时手动执行。

使用前请在下方「私有仓库占位」中填写 NEXUS_REGISTRY / NEXUS_USERNAME / NEXUS_PASSWORD；
也可通过命令行 --base-url、--repository、--username、--password 覆盖。
"""

import os
import re
import sys
import time
import argparse
from pathlib import Path
from urllib.parse import urlparse
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

# ========== 私有仓库占位（请填写后使用，或通过命令行参数覆盖） ==========
NEXUS_REGISTRY = ""   # 例如 http://localhost:8081/repository/npm-hosted
NEXUS_USERNAME = ""
NEXUS_PASSWORD = ""


def _default_base_url_and_repo():
    reg = (NEXUS_REGISTRY or "").strip().rstrip("/")
    base_url = repo = ""
    if reg:
        parsed = urlparse(reg)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        parts = (parsed.path or "").strip("/").split("/")
        repo = parts[-1] if parts else ""
    return base_url, repo


BASE_URL, REPOSITORY = _default_base_url_and_repo()
USERNAME = (NEXUS_USERNAME or "").strip()
PASSWORD = (NEXUS_PASSWORD or "").strip()

PACKAGES_PATH = "./packages"
UPLOAD_LOG = "logs/publish.log"
MAX_WORKERS = 50
TIMEOUT = 60


def parse_args():
    p = argparse.ArgumentParser(description="批量上传 .tgz 到 Nexus npm 仓库，全部覆盖")
    p.add_argument("--base-url", "-u", default=BASE_URL, help="Nexus 地址")
    p.add_argument("--repository", "-r", default=REPOSITORY, help="仓库名称")
    p.add_argument("--username", default=USERNAME, help="用户名")
    p.add_argument("--password", default=PASSWORD, help="密码")
    p.add_argument("--packages-path", "-p", default=PACKAGES_PATH, help=".tgz 所在目录")
    p.add_argument("--workers", "-w", type=int, default=MAX_WORKERS, help="并发上传数")
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


def upload_one(base_url, repository, auth, filepath, timeout):
    """
    上传单个 .tgz，已存在则先删后传（覆盖）。返回 (filename, 'success'|'overwritten'|'failure', message)。
    """
    url = f"{base_url.rstrip('/')}/service/rest/v1/components?repository={repository}"
    filename = filepath.name
    name, version = parse_tgz_name(filename)

    try:
        with open(filepath, "rb") as f:
            files = {"npm.asset": (filename, f, "application/gzip")}
            r = requests.post(url, auth=auth, files=files, timeout=timeout)
        if r.status_code in (200, 201, 204):
            return (filename, "success", None)
        if r.status_code == 400 and "does not allow updating" in (r.text or ""):
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
    base_url = (args.base_url or "").strip().rstrip("/")
    repo = (args.repository or "").strip()
    if not base_url or not repo:
        print("未配置私有仓库：请在脚本内填写 NEXUS_REGISTRY，或使用 --base-url 与 --repository 参数。")
        sys.exit(1)
    auth = (args.username, args.password)
    packages_path = Path(args.packages_path)

    files = collect_tgz_files(packages_path)
    if not files:
        print(f"在 {packages_path} 下未找到任何 .tgz 文件")
        return

    total = len(files)
    print("============================================")
    print("  Nexus npm 包批量上传（全部覆盖）")
    print("============================================")
    print(f"  地址: {base_url}")
    print(f"  仓库: {repo}")
    print(f"  包数: {total}")
    print(f"  并发: {args.workers}")
    print("============================================\n")

    os.makedirs(os.path.dirname(UPLOAD_LOG) or ".", exist_ok=True)
    success_count = 0
    overwritten_count = 0
    failed = []
    bar_width = 40

    def progress_bar(done, total, ok_count, overwritten, failed_count):
        pct = (done * 100) // total if total else 0
        filled = (done * bar_width) // total if total else 0
        bar = "#" * filled + " " * (bar_width - filled)
        return f"\r  [{bar}] {pct}% | 已上传: {ok_count + overwritten} 覆盖: {overwritten} 失败: {failed_count} | {done}/{total}"

    t0 = time.time()
    done = 0

    with open(UPLOAD_LOG, "w", encoding="utf-8") as log:
        log.write(f"# Nexus npm 上传日志（全部覆盖）\n# 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.write(f"# 地址: {base_url}\n# 仓库: {repo}\n# 总数: {total}\n\n")

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(upload_one, base_url, repo, auth, fp, TIMEOUT): fp for fp in files}
            for f in as_completed(futures):
                filename, status, msg = f.result()
                done += 1
                if status == "success":
                    success_count += 1
                    log.write(f"OK   {filename}\n")
                elif status == "overwritten":
                    overwritten_count += 1
                    log.write(f"OVERWRITE {filename}\n")
                else:
                    failed.append((filename, msg))
                    log.write(f"FAIL {filename}  {msg}\n")
                print(progress_bar(done, total, success_count, overwritten_count, len(failed)), end="", flush=True)

    elapsed = time.time() - t0
    print(progress_bar(total, total, success_count, overwritten_count, len(failed)))

    print("\n============================================")
    print("  上传完成")
    print("============================================")
    print(f"  已上传: {success_count + overwritten_count}（成功 {success_count}，覆盖 {overwritten_count}）")
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
