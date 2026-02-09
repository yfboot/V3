#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主流程：按阶段串联各脚本，完成「下载 → 本地 registry 安装与补包」闭环。

阶段一：download.py 按 lock 下载到 packages/；若日志中有失败 URL 自动补下。
阶段二：本地 registry + 安装。保留 package-lock.json，将其中的 resolved 重写为本地 registry URL，
        使 npm 按 lock 全量从本地拉包；缺包时由 supplement.py 补到 packages/ 并重试。

flow 只做衔接与子进程/文件编排，具体解析与下载由各阶段脚本完成。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import subprocess
from pathlib import Path
from typing import List, Tuple

import requests

import config
import supplement

# ================== 常量 ==================
PYTHON = sys.executable
BASE_DIR = config.BASE_DIR
TOOLS_DIR = config.TOOLS_DIR
MAX_FIX_ROUNDS = 200
NPM_INSTALL_ARGS: List[str] = []  # 可追加如 "--legacy-peer-deps"


# ================== 下载日志重试 ==================
def _to_mirror_url(url: str) -> str:
    """将任意来源的 tarball URL 转为配置的镜像地址，确保重试使用加速源。"""
    if "/-/" not in url:
        return url
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return config.DOWNLOAD_REGISTRY.rstrip("/") + parsed.path


def retry_failed_from_log(log_path: Path, out_dir: Path):
    """从下载日志中提取失败 URL，转为镜像地址后重新下载到 out_dir。"""
    if not log_path.exists():
        return
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    raw_urls = list(dict.fromkeys(re.findall(r"下载链接:\s*(https?://\S+)", text)))
    if not raw_urls:
        return
    urls = [_to_mirror_url(u) for u in raw_urls]
    print(f"  从日志提取 {len(urls)} 条失败 URL，转为镜像地址后补下到 {out_dir} ...", flush=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        fn = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", url.split("?")[0].rstrip("/").split("/")[-1])
        if not fn.endswith(".tgz"):
            fn += ".tgz"
        dst = out_dir / fn
        if dst.exists() and dst.stat().st_size > 0:
            continue
        try:
            r = requests.get(url, stream=True, timeout=60)
            r.raise_for_status()
            with dst.open("wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            print(f"  跳过: {url[:60]}... {e}", flush=True)


# ================== lock 重写 ==================
def rewrite_lock_resolved_to_local(lock_path: Path, registry_url: str) -> None:
    """将 package-lock.json 中所有 resolved 改为本地 registry URL。
    同时移除无效幽灵条目（无 version/resolved/integrity 的空壳）。"""
    registry_url = registry_url.rstrip("/")
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = data.get("packages") or {}
    phantom_keys = [
        key for key, pkg in packages.items()
        if isinstance(pkg, dict) and key != ""
        and not (pkg.get("version") or "").strip()
        and not pkg.get("resolved")
        and not pkg.get("integrity")
        and not pkg.get("link")
    ]
    for key in phantom_keys:
        del packages[key]
    if phantom_keys:
        print(f"  已移除 {len(phantom_keys)} 个无效幽灵条目。", flush=True)
    for key, pkg in packages.items():
        if not isinstance(pkg, dict) or key == "":
            continue
        name = key.replace("\\", "/").split("node_modules/")[-1].strip("/")
        version = (pkg.get("version") or "").strip()
        if not version:
            continue
        if name.startswith("@"):
            if "/" not in name:
                continue
            path_part = name.replace("/", "%2F")
            tarball_name = name.split("/", 1)[1] + f"-{version}.tgz"
        else:
            path_part = name
            tarball_name = f"{name}-{version}.tgz"
        pkg["resolved"] = f"{registry_url}/{path_part}/-/{tarball_name}"
    lock_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ================== 命令执行 ==================
def run_cmd_to_file(cmd: List[str], cwd: Path, log_path: Path, echo_stdout: bool = True) -> int:
    """执行命令并实时写入日志文件。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as f:
        f.write(f"# cmd: {' '.join(cmd)}\n# time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.flush()
        p = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            shell=False, encoding="utf-8", errors="replace", bufsize=1
        )
        for line in iter(p.stdout.readline, ""):
            f.write(line)
            f.flush()
            if echo_stdout:
                print(line, end="")
                sys.stdout.flush()
        return p.wait()


# ================== 补包汇总日志 ==================
def _write_supplement_total(path: Path, items: List[Tuple[str, str]], finished: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# 本次补包列表\n" if finished else "# 本次补包列表（未完全解决）\n"
    path.write_text(header + "\n".join(f"{n}@{r}" for n, r in items) + "\n", encoding="utf-8")
    print(f"本次补包共 {len(items)} 个，列表见 {path}", flush=True)
    for n, r in items:
        print(f"  - {n}@{r}", flush=True)


# ================== 主流程 ==================
def main() -> int:
    print("flow 开始执行 ...", flush=True)
    os.chdir(str(BASE_DIR))

    if "--ignore-scripts" not in NPM_INSTALL_ARGS:
        NPM_INSTALL_ARGS.append("--ignore-scripts")

    packages_dir = BASE_DIR / "packages"
    download_log = BASE_DIR / "logs" / "download.log"
    npm_install_log = BASE_DIR / "logs" / "npm_install.log"
    supplement_total_log = BASE_DIR / "logs" / "supplement_total.log"
    supplement_round_log = BASE_DIR / "logs" / "supplement_round.log"
    local_registry_port = config.LOCAL_REGISTRY_PORT
    registry_url = f"http://127.0.0.1:{local_registry_port}"

    # ---------- 阶段一：下载到 packages/ ----------
    if not config.SKIP_PHASE1:
        print("Step1: 下载依赖到 packages/ ...", flush=True)
        subprocess.check_call([PYTHON, str(TOOLS_DIR / "download.py")], cwd=str(BASE_DIR))
        retry_failed_from_log(download_log, packages_dir)
    else:
        print("Step1: 已跳过（下载 packages/）。", flush=True)

    # ---------- 阶段二：本地 registry + npm install + 补包循环 ----------
    lock_path = BASE_DIR / "package-lock.json"
    lock_backup = BASE_DIR / "package-lock.json.backup_for_flow"
    lock_rewritten = False
    if lock_path.exists():
        shutil.copy2(lock_path, lock_backup)
        rewrite_lock_resolved_to_local(lock_path, registry_url)
        lock_rewritten = True
        print("Step2: 已备份 lock 并将 resolved 重写为本地 registry。", flush=True)

    packages_dir.mkdir(parents=True, exist_ok=True)
    local_server_proc = subprocess.Popen(
        [PYTHON, str(TOOLS_DIR / "registry.py"), "packages", str(local_registry_port)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    print(f"Step2: 本地 registry 已启动（{registry_url}）。", flush=True)

    supplemented_this_run: set[Tuple[str, str]] = set()
    all_supplemented: List[Tuple[str, str]] = []
    npm_exe = config.NPM
    cmd_install = [npm_exe, "install", "--registry", registry_url, *NPM_INSTALL_ARGS]

    try:
        for round_idx in range(1, MAX_FIX_ROUNDS + 1):
            print(f"Step3 (round {round_idx}): npm install ...", flush=True)
            code = run_cmd_to_file(cmd_install, BASE_DIR, npm_install_log, echo_stdout=False)

            missing = supplement.extract_404_from_npm_install_log(npm_install_log)
            if not missing:
                if code != 0:
                    print(f"npm install 失败（退出码 {code}），详见 {npm_install_log}。", flush=True)
                    return 1
                print("npm install 成功，未检测到缺包。", flush=True)
                if all_supplemented:
                    _write_supplement_total(supplement_total_log, all_supplemented)
                return 0

            new_missing = [(n, r) for (n, r) in missing if (n, r) not in supplemented_this_run]
            if not new_missing:
                print("缺包均已补过，重试 npm install ...", flush=True)
                code = run_cmd_to_file(cmd_install, BASE_DIR, npm_install_log, echo_stdout=False)
                missing = supplement.extract_404_from_npm_install_log(npm_install_log)
                if not missing:
                    if code != 0:
                        print(f"npm install 失败（退出码 {code}），详见 {npm_install_log}。", flush=True)
                        return 1
                    print("npm install 成功，未检测到缺包。", flush=True)
                    if all_supplemented:
                        _write_supplement_total(supplement_total_log, all_supplemented)
                    return 0
                new_missing = [(n, r) for (n, r) in missing if (n, r) not in supplemented_this_run]
            if not new_missing:
                print("以下依赖已补包但安装仍报错，请检查 packages/ 或重试：", flush=True)
                for n, r in missing:
                    print(f"  {n}@{r}", flush=True)
                return 5

            print(f"检测到缺包 {len(missing)} 个，其中未补过 {len(new_missing)} 个：", flush=True)
            for n, r in new_missing:
                print(f"  - {n}@{r}", flush=True)
            supplement_round_log.parent.mkdir(parents=True, exist_ok=True)
            supplement_round_log.write_text(
                "\n".join(f"{n}@{r}" for n, r in new_missing), encoding="utf-8"
            )
            subprocess.check_call([
                PYTHON, str(TOOLS_DIR / "supplement.py"),
                "--log", str(npm_install_log),
                "--out-dir", str(packages_dir),
                "--base-dir", str(BASE_DIR),
                "--only-new-file", str(supplement_round_log),
                "--report-file", str(supplement_round_log),
            ], cwd=str(BASE_DIR))
            report_lines = [
                s.strip() for s in supplement_round_log.read_text(encoding="utf-8").splitlines()
                if s.strip()
            ]
            for line in report_lines:
                if "@" not in line:
                    continue
                name, rng = line.rsplit("@", 1)
                name, rng = name.strip(), rng.strip()
                if name and rng:
                    supplemented_this_run.add((name, rng))
                    all_supplemented.append((name, rng))
            if not report_lines:
                print("未能补到任何 tarball，停止。", flush=True)
                return 3

            try:
                import urllib.request
                urllib.request.urlopen(
                    f"http://127.0.0.1:{local_registry_port}/-/rescan", timeout=10,
                )
            except Exception as e:
                print(f"重新扫描 registry 失败: {e}", flush=True)
                return 6
            print("已重新扫描本地 registry，下一轮 npm install。", flush=True)

        print(f"已达最大轮次 {MAX_FIX_ROUNDS}，仍有缺包，详见 {npm_install_log}", flush=True)
        if all_supplemented:
            _write_supplement_total(supplement_total_log, all_supplemented, finished=False)
        return 4
    finally:
        if local_server_proc is not None:
            try:
                local_server_proc.terminate()
                local_server_proc.wait(timeout=3)
            except Exception:
                try:
                    local_server_proc.kill()
                except Exception:
                    pass
            print("已停止本地 registry。", flush=True)
        if lock_rewritten and lock_backup.exists():
            shutil.copy2(lock_backup, lock_path)
            try:
                lock_backup.unlink()
            except Exception:
                pass
            print("已恢复 package-lock.json。", flush=True)


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as e:
        print(f"flow 异常: {e}", flush=True)
        import traceback
        traceback.print_exc()
        exit_code = 1
    raise SystemExit(exit_code)
