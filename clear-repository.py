#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nexus npm 仓库清空脚本（clear repository）
先拉取全部组件，展示总数量与两层依赖树，确认后再并行删除。

说明：本脚本通过 REST API 按 component ID 删除，会删除组件及其 tarball，但 Nexus 内
部可能仍保留 npm「包级元数据」（即“某包有哪些版本”的索引）。若删除后不重建索引就重新
上传，可能出现 500 "Package X lacks tarball version Y"（元数据里仍记录版本 Y，但
tarball 已无）。删除完成后请在 Nexus 管理界面对该仓库执行「重建索引」或相关 Repair 任务。
"""

import os
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows 控制台 UTF-8
if sys.platform == "win32":
    import io
    import locale
    import os
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

from pathlib import Path
from config_loader import get_nexus_config

_SCRIPT_DIR = Path(__file__).resolve().parent
_NEXUS_BASE, _NEXUS_REPO, _NEXUS_USER, _NEXUS_PASS = get_nexus_config(_SCRIPT_DIR)
BASE_URL = _NEXUS_BASE
REPOSITORY = _NEXUS_REPO
USERNAME = _NEXUS_USER
PASSWORD = _NEXUS_PASS

DELETE_LOG = "logs/clear-repository.log"
MAX_DELETE_WORKERS = 20
TIMEOUT = 30


def parse_args():
    p = argparse.ArgumentParser(description="清空 Nexus npm 仓库：先列数量与依赖树，确认后并行删除")
    p.add_argument("--base-url", "-u", default=BASE_URL, help="Nexus 地址（可含上下文路径）")
    p.add_argument("--repository", "-r", default=REPOSITORY, help="仓库名称")
    p.add_argument("--username", default=USERNAME, help="用户名")
    p.add_argument("--password", default=PASSWORD, help="密码")
    p.add_argument("--workers", "-w", type=int, default=MAX_DELETE_WORKERS, help="并行删除数")
    p.add_argument("--yes", "-y", action="store_true", help="跳过确认，直接删除")
    return p.parse_args()


def get_session(auth):
    s = requests.Session()
    s.auth = auth
    s.headers["accept"] = "application/json"
    s.timeout = TIMEOUT
    return s


def fetch_all_components(base_url, repository, auth):
    """拉取所有组件（分页），返回 list[dict]"""
    url = f"{base_url.rstrip('/')}/service/rest/v1/components"
    params = {"repository": repository}
    out = []
    with get_session(auth) as s:
        while True:
            r = s.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            for item in items:
                out.append(item)
            token = data.get("continuationToken")
            if not token or not items:
                break
            params = {"repository": repository, "continuationToken": token}
    return out


def build_tree(items):
    """两层结构：包名 -> [版本列表]，并返回 (id -> item) 映射"""
    tree = {}
    id_to_item = {}
    for item in items:
        cid = item.get("id")
        if not cid:
            continue
        id_to_item[cid] = item
        group = (item.get("group") or "").strip()
        name = (item.get("name") or "").strip()
        version = (item.get("version") or "").strip()
        if group:
            pkg = f"{group}/{name}" if name else group
        else:
            pkg = name or "(unknown)"
        if pkg not in tree:
            tree[pkg] = []
        tree[pkg].append((version, cid))
    return tree, id_to_item


def delete_one(base_url, component_id, auth):
    """删除一个组件，返回 (component_id, success, message)"""
    url = f"{base_url.rstrip('/')}/service/rest/v1/components/{component_id}"
    try:
        r = requests.delete(url, auth=auth, timeout=TIMEOUT)
        if r.status_code in (200, 204):
            return (component_id, True, None)
        return (component_id, False, f"HTTP {r.status_code}")
    except Exception as e:
        return (component_id, False, str(e))


def main():
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    auth = (args.username, args.password)

    print("============================================")
    print("  清空 Nexus npm 仓库（clear repository）")
    print("============================================")
    print(f"  地址: {base_url}")
    print(f"  仓库: {args.repository}")
    print("============================================\n")

    print("正在获取组件列表（分页）...")
    t0 = time.time()
    items = fetch_all_components(base_url, args.repository, auth)
    elapsed = time.time() - t0
    print(f"  获取完成: 共 {len(items)} 个组件，耗时 {elapsed:.1f} 秒\n")

    if not items:
        print("仓库中没有任何组件，无需删除。")
        return

    _, id_to_item = build_tree(items)
    total_count = len(items)

    if not args.yes:
        confirm = input(f"确认删除以上全部组件？共 {total_count} 个组件。(yes/no): ").strip().lower()
        if confirm != "yes":
            print("已取消。")
            return

    ids = list(id_to_item.keys())
    total_ids = len(ids)
    print(f"\n开始并行删除（workers={args.workers}）...")
    t0 = time.time()
    success_count = 0
    failed = []
    bar_width = 40

    def progress_bar(done, total):
        pct = (done * 100) // total if total else 0
        filled = (done * bar_width) // total if total else 0
        bar = "#" * filled + " " * (bar_width - filled)
        return f"\r  [{bar}] {pct}% ({done}/{total})"

    log_dir = os.path.dirname(DELETE_LOG)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    with open(DELETE_LOG, "w", encoding="utf-8") as log:
        log.write(f"# Nexus 删除日志\n# 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n# 仓库: {args.repository}\n# 总数: {total_ids}\n\n")

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(delete_one, base_url, cid, auth): cid for cid in ids}
            done = 0
            for f in as_completed(futures):
                cid, ok, msg = f.result()
                done += 1
                print(progress_bar(done, total_ids), end="", flush=True)
                if ok:
                    success_count += 1
                    log.write(f"OK  {cid}\n")
                else:
                    failed.append((cid, msg))
                    log.write(f"FAIL {cid}  {msg}\n")

    elapsed = time.time() - t0
    print(progress_bar(total_ids, total_ids))

    print("\n============================================")
    print("  删除完成")
    print("============================================")
    print(f"  成功: {success_count}")
    print(f"  失败: {len(failed)}")
    print(f"  耗时: {elapsed:.1f} 秒")
    print(f"  日志: {DELETE_LOG}")
    if failed:
        print("\n失败列表:")
        for cid, msg in failed[:20]:
            print(f"    {cid}  {msg}")
        if len(failed) > 20:
            print(f"    ... 共 {len(failed)} 条，详见 {DELETE_LOG}")

    print("\n正在查询删除后仓库组件数量...")
    try:
        remaining = fetch_all_components(base_url, args.repository, auth)
        print(f"  删除后仓库中组件数量: {len(remaining)} 个")
    except Exception as e:
        print(f"  查询失败: {e}")

    print("\n【重要】npm 仓库建议：删除后请在 Nexus 管理界面对该仓库执行「重建索引」")
    print("  （Repository → 选择仓库 → 菜单/Admin → Rebuild index 或 Repair index）")
    print("  否则再次上传时可能报 500 \"Package X lacks tarball version Y\"。")
    print()
    print("若重建索引仍无效，可再去 Nexus 管理界面执行以下操作，以彻底对齐组件库与 blob：")
    print("  Settings → System → Tasks → 新建 → 分别新建与执行下面两个任务：")
    print("    • Repair - Reconcile component database from blob store")
    print("    • Repair - Rebuild npm metadata")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(0)
    except requests.RequestException as e:
        print(f"请求错误: {e}")
        if getattr(e, "response", None) is not None and e.response is not None:
            print(f"  状态码: {e.response.status_code}")
            print(f"  响应: {e.response.text[:500]}")
        sys.exit(1)
