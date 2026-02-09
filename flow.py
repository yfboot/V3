#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主流程：按阶段串联各脚本，完成「下载 → 本地 registry 安装与补包」闭环。

阶段一：npm_package_download.py 按 lock 下载到 packages/；
        若下载日志中有异常 URL，由 download_from_log.py 补下到 packages/。
阶段二：本地 registry + 安装。保留 package-lock.json，将其中的 resolved 重写为本地 registry URL，
        使 npm 按 lock 全量从本地拉包；缺包时由 supplement_missing 补到 manual_packages/ 并重试。

flow 只做衔接与子进程/文件编排，具体解析与下载由各阶段脚本完成。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import subprocess
from pathlib import Path
from typing import List, Tuple

# 仅用 supplement_missing 的日志解析，不在此处实现任何下载/补包逻辑
import supplement_missing

# ================== 配置与路径 ==================
PYTHON = sys.executable
BASE_DIR = Path(__file__).resolve().parent
MAX_FIX_ROUNDS = 200
NPM_INSTALL_ARGS: List[str] = []  # 可追加如 "--legacy-peer-deps"


def read_local_config(base_dir: Path) -> Tuple[bool, int]:
    """从 config.local 读取：是否跳过阶段1、本地 registry 端口。"""
    from config_loader import load_config, get_local_registry_config
    cfg = load_config(base_dir)
    skip_phase1 = (cfg.get("SKIP_PHASE1") or "").strip() == "1"
    (port,) = get_local_registry_config(base_dir)
    return (skip_phase1, port)


def rewrite_lock_resolved_to_local(lock_path: Path, registry_url: str) -> None:
    """将 package-lock.json 中所有 resolved 改为本地 registry URL，使 npm 从本地全量安装。
    同时移除无效幽灵条目（无 version、resolved、integrity 的空壳），避免 npm 报 Invalid Version。"""
    registry_url = registry_url.rstrip("/")
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = data.get("packages") or {}
    # 先清理幽灵条目：无 version 且无 resolved/integrity 的条目对 npm 无意义，只会导致 Invalid Version
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
        print(f"  已移除 {len(phantom_keys)} 个无效幽灵条目（无 version/resolved/integrity）。", flush=True)
    for key, pkg in packages.items():
        if not isinstance(pkg, dict):
            continue
        if key == "":
            continue
        # 支持嵌套：node_modules/a/node_modules/@scope/name → @scope/name
        name = key.replace("\\", "/").split("node_modules/")[-1].strip("/")
        version = (pkg.get("version") or "").strip()
        if not version:
            continue  # 仍无版本的条目（如 link），跳过 resolved 重写
        if name.startswith("@"):
            if "/" not in name:
                continue  # 异常 lock 条目，如仅 @scope 无包名，跳过
            path_part = name.replace("/", "%2F")
            tarball_name = name.split("/", 1)[1] + f"-{version}.tgz"
        else:
            path_part = name
            tarball_name = f"{name}-{version}.tgz"
        pkg["resolved"] = f"{registry_url}/{path_part}/-/{tarball_name}"
    lock_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)
        except Exception:
            pass
    print("flow 开始执行 ...", flush=True)
    os.chdir(str(BASE_DIR))

    skip_phase1, local_registry_port = read_local_config(BASE_DIR)
    if "--ignore-scripts" not in NPM_INSTALL_ARGS:
        NPM_INSTALL_ARGS.append("--ignore-scripts")

    packages_dir = BASE_DIR / "packages"
    manual_dir = BASE_DIR / "manual_packages"
    download_log = BASE_DIR / "logs" / "npm_package_download.log"
    npm_install_log = BASE_DIR / "logs" / "npm_install.log"
    supplement_log_path = BASE_DIR / "logs" / "supplemented_packages.txt"
    # 阶段二补包：单一日志文件，先写入本轮待补列表，supplement_missing 读入并覆盖写入本轮已补列表
    supplement_round_log = BASE_DIR / "logs" / "supplement_round.txt"
    registry_url = f"http://127.0.0.1:{local_registry_port}"

    # ---------- 阶段一：下载到 packages/ ----------
    if not skip_phase1:
        print("Step1: 下载依赖到 packages/ ...", flush=True)
        subprocess.check_call([PYTHON, "npm_package_download.py"], cwd=str(BASE_DIR))
        subprocess.check_call([
            PYTHON, "download_from_log.py",
            "--log", str(download_log),
            "--output-dir", str(packages_dir),
        ], cwd=str(BASE_DIR))
    else:
        print("已跳过阶段1（下载 packages/）", flush=True)

    # ---------- 阶段二：本地 registry + npm install（按 lock 全量从本地拉包）与补包循环 ----------
    lock_path = BASE_DIR / "package-lock.json"
    lock_backup = BASE_DIR / "package-lock.json.backup_for_flow"
    lock_rewritten = False
    if lock_path.exists():
        shutil.copy2(lock_path, lock_backup)
        rewrite_lock_resolved_to_local(lock_path, registry_url)
        lock_rewritten = True
        print("Step2: 已备份 lock 并将 resolved 重写为本地 registry，npm 将按 lock 全量从本地安装。", flush=True)

    manual_dir.mkdir(parents=True, exist_ok=True)
    local_server_proc = subprocess.Popen(
        [PYTHON, "local_registry.py", "packages", "manual_packages", str(local_registry_port)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    print(f"Step2: 本地 registry 已启动（{registry_url}），packages/ + manual_packages/ 作为本地仓库。", flush=True)

    supplemented_this_run: set[Tuple[str, str]] = set()
    all_supplemented: List[Tuple[str, str]] = []
    npm_exe = "npm.cmd" if sys.platform == "win32" else "npm"
    cmd_install = [npm_exe, "install", "--registry", registry_url, *NPM_INSTALL_ARGS]

    try:
        for round_idx in range(1, MAX_FIX_ROUNDS + 1):
            print(f"Step3 (round {round_idx}): npm cache clean --force ...", flush=True)
            subprocess.run([npm_exe, "cache", "clean", "--force"], cwd=str(BASE_DIR), capture_output=True, timeout=60)
            print(f"Step3 (round {round_idx}): npm install（registry={registry_url}），输出写 {npm_install_log} ...", flush=True)
            code = run_cmd_to_file(cmd_install, BASE_DIR, npm_install_log, echo_stdout=False)

            # 由 supplement_missing 提供解析，flow 只做衔接
            missing = supplement_missing.extract_404_from_npm_install_log(npm_install_log)
            if not missing:
                if code != 0:
                    print(f"npm install 执行失败（退出码 {code}），请查看 {npm_install_log}。日志中可能有 Invalid Version 等错误。", flush=True)
                    return 1
                print("npm install 未检测到缺包，流程结束。", flush=True)
                if all_supplemented:
                    supplement_log_path.parent.mkdir(parents=True, exist_ok=True)
                    supplement_log_path.write_text(
                        "# 本次运行补包列表（缺包 -> 已保存于 manual_packages/）\n" +
                        "\n".join(f"{n}@{r}" for n, r in all_supplemented) + "\n",
                        encoding="utf-8"
                    )
                    print(f"本次补包共 {len(all_supplemented)} 个，已保存于 manual_packages/，列表见 {supplement_log_path}", flush=True)
                    for n, r in all_supplemented:
                        print(f"  - {n}@{r}", flush=True)
                return 0

            new_missing = [(n, r) for (n, r) in missing if (n, r) not in supplemented_this_run]
            if not new_missing:
                print("缺包均为本 run 已补过的包，重试一次 npm cache clean 后 npm install ...", flush=True)
                subprocess.run([npm_exe, "cache", "clean", "--force"], cwd=str(BASE_DIR), capture_output=True, timeout=60)
                code = run_cmd_to_file(cmd_install, BASE_DIR, npm_install_log, echo_stdout=False)
                missing = supplement_missing.extract_404_from_npm_install_log(npm_install_log)
                if not missing:
                    if code != 0:
                        print(f"npm install 执行失败（退出码 {code}），请查看 {npm_install_log}。", flush=True)
                        return 1
                    print("npm install 未检测到缺包，流程结束。", flush=True)
                    if all_supplemented:
                        supplement_log_path.parent.mkdir(parents=True, exist_ok=True)
                        supplement_log_path.write_text(
                            "# 本次运行补包列表（缺包 -> 已保存于 manual_packages/）\n" +
                            "\n".join(f"{n}@{r}" for n, r in all_supplemented) + "\n",
                            encoding="utf-8"
                        )
                        print(f"本次补包共 {len(all_supplemented)} 个，已保存于 manual_packages/，列表见 {supplement_log_path}", flush=True)
                    return 0
                new_missing = [(n, r) for (n, r) in missing if (n, r) not in supplemented_this_run]
            if not new_missing:
                print("以下依赖已在本 run 补包但安装仍报错，请检查 manual_packages/ 或重试。", flush=True)
                for n, r in missing:
                    print(f"  {n}@{r}", flush=True)
                return 5

            print(f"检测到缺包 {len(missing)} 个，其中本 run 未补过的 {len(new_missing)} 个，交由 supplement_missing 补包 ...", flush=True)
            for n, r in new_missing:
                print(f"  - {n}@{r}", flush=True)
            npm_install_log.parent.mkdir(parents=True, exist_ok=True)
            supplement_round_log.write_text("\n".join(f"{n}@{r}" for n, r in new_missing), encoding="utf-8")
            subprocess.check_call([
                PYTHON, "supplement_missing.py",
                "--log", str(npm_install_log),
                "--out-dir", str(manual_dir),
                "--base-dir", str(BASE_DIR),
                "--only-new-file", str(supplement_round_log),
                "--report-file", str(supplement_round_log),
            ], cwd=str(BASE_DIR))
            report_lines = [s.strip() for s in supplement_round_log.read_text(encoding="utf-8").splitlines() if s.strip()]
            for line in report_lines:
                if "@" not in line:
                    continue
                name, rng = line.rsplit("@", 1)
                name, rng = name.strip(), rng.strip()
                if not name or not rng:
                    continue
                t = (name, rng)
                supplemented_this_run.add(t)
                all_supplemented.append(t)
            if not report_lines:
                print("未能通过 supplement_missing 解析或下载任何 tarball，停止。", flush=True)
                return 3

            # 通知本地 registry 重新扫描目录（含新下载的 manual_packages），无需重启进程
            try:
                import urllib.request
                urllib.request.urlopen(
                    f"http://127.0.0.1:{local_registry_port}/-/rescan",
                    timeout=10,
                )
            except Exception as e:
                print(f"重新扫描 registry 失败: {e}，请检查本地 registry 是否在运行。", flush=True)
                return 6
            print("已重新扫描本地 registry（已含新补包），下一轮 npm install。", flush=True)

        print(f"已达到最大补包轮次 MAX_FIX_ROUNDS={MAX_FIX_ROUNDS}，仍有缺包，请检查日志 {npm_install_log}", flush=True)
        if all_supplemented:
            supplement_log_path.parent.mkdir(parents=True, exist_ok=True)
            supplement_log_path.write_text(
                "# 本次运行补包列表（未完全解决缺包，已保存于 manual_packages/）\n" +
                "\n".join(f"{n}@{r}" for n, r in all_supplemented) + "\n",
                encoding="utf-8"
            )
            print(f"本次已补包 {len(all_supplemented)} 个，已保存于 manual_packages/，列表见 {supplement_log_path}", flush=True)
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
            print("已恢复 package-lock.json（从备份）。", flush=True)


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as e:
        print(f"flow 异常: {e}", flush=True)
        import traceback
        traceback.print_exc()
        exit_code = 1
    raise SystemExit(exit_code)
