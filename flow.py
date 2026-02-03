#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线依赖闭环流程（适合做 Skills 的编排入口）

流程（可联网机器 + 可访问 Nexus）：
1) 运行 npm_package_download.py 生成 packages/
2) 如 logs/npm_package_download.log 中有 404/异常 URL，则提取下载链接并补下到 packages/
3) 运行 publish.py 上传 packages/
4) 循环执行 npm install（registry 指向私有库），将输出覆盖写入 logs/npm_install.log
   - 若出现 404，则解析缺包 -> 从外网 registry 解析 tarball -> 下载到 manual-packages/
   - 上传 manual-packages/ -> 再次 npm install
   - 直到不再出现 404 为止

说明：
- 本脚本不会修改你的 npm_package_download.py / publish.py 的行为，只做编排与补齐。
- npm_install.log 写入 logs/，每次覆盖，不追加。
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import requests
except Exception:
    print("请先安装 requests: pip install requests")
    raise


# ================== 可配置项（按需修改） ==================
PYTHON = sys.executable
BASE_DIR = Path(__file__).resolve().parent

# 外网/镜像 registry：补包时解析 tarball 用（可提交）
PUBLIC_REGISTRY = "https://registry.npmmirror.com"
# 私有 registry、SKIP_PHASE1/2 从 config.local 读取（见 read_local_config()）

# npm install 额外参数（例如 "--legacy-peer-deps"）
NPM_INSTALL_ARGS: List[str] = []

# 最大补包轮次，避免无限循环
MAX_FIX_ROUNDS = 10


# ================== 工具函数 ==================
def read_local_config(base_dir: Path) -> Tuple[str, bool, bool]:
    """
    从 config.local 或 .npmrc 读取：私有 registry、是否跳过阶段1、是否跳过阶段2。
    返回 (private_registry, skip_phase1, skip_phase2)。无 config 时默认 ( "", False, False )。
    """
    private_registry = ""
    skip_phase1 = False
    skip_phase2 = False
    config_local = base_dir / "config.local"
    if config_local.exists():
        for line in config_local.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("PRIVATE_REGISTRY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'").rstrip("/")
                if value:
                    private_registry = value
            elif line.startswith("SKIP_PHASE1="):
                v = line.split("=", 1)[1].strip().lower()
                skip_phase1 = v in ("1", "true", "yes", "on")
            elif line.startswith("SKIP_PHASE2="):
                v = line.split("=", 1)[1].strip().lower()
                skip_phase2 = v in ("1", "true", "yes", "on")
    if not private_registry:
        npmrc_path = base_dir / ".npmrc"
        if npmrc_path.exists():
            for line in npmrc_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("registry="):
                    private_registry = line.split("=", 1)[1].strip().rstrip("/")
                    break
    return (private_registry, skip_phase1, skip_phase2)


# 正则：匹配 "resolved": "http(s)://...任意" 的 URL 部分，用于快速替换 origin，不解析整份 JSON
_RESOLVED_URL_RE = re.compile(r'"resolved":\s*"(https?://[^"]+)"')


def _rewrite_resolved_origin_in_text(content: str, private_base: str) -> Tuple[str, int]:
    """在 lock 文件文本中把 resolved URL 的 origin 替换为 private_base，返回 (新内容, 替换条数)。"""
    if not private_base:
        return content, 0
    changed = 0

    def repl(m: re.Match) -> str:
        nonlocal changed
        url = m.group(1)
        if url.startswith(private_base):
            return m.group(0)
        parsed = urlparse(url)
        new_url = private_base + (parsed.path or "/")
        changed += 1
        return '"resolved": "' + new_url + '"'

    new_content = _RESOLVED_URL_RE.sub(repl, content)
    return new_content, changed


def run_cmd_to_file(cmd: List[str], cwd: Path, log_path: Path) -> int:
    """执行命令，实时写日志并输出到终端，避免 npm 等子进程在重定向时缓冲导致无反应。"""
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
            print(line, end="")
            sys.stdout.flush()
        return p.wait()


def extract_urls_from_download_log(log_path: Path) -> List[str]:
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    # npm_package_download.log 里有：下载链接: <url>
    urls = re.findall(r"下载链接:\s*(https?://\S+)", text)
    # 去重保序
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
    name = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1F]', "_", name)
    return name


def download_urls(urls: Iterable[str], out_dir: Path, timeout: int = 60) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        fn = safe_filename_from_url(url)
        dst = out_dir / fn
        if dst.exists() and dst.stat().st_size > 0:
            continue
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        with dst.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def npm_pkg_doc_url(registry: str, name: str) -> str:
    registry = registry.rstrip("/")
    if name.startswith("@") and "/" in name:
        scope, pkg = name.split("/", 1)
        return f"{registry}/{scope}%2F{pkg}"
    return f"{registry}/{name}"


def parse_ver_tuple(v: str) -> Tuple[int, int, int]:
    m = re.match(r"^(\\d+)\\.(\\d+)\\.(\\d+)", v.strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def pick_best_version(versions: Iterable[str], range_str: str) -> Optional[str]:
    range_str = (range_str or "").strip()
    m = re.match(r"^(\\^|>=|~)?\\s*(\\d+\\.\\d+\\.\\d+[^\\s]*)", range_str)
    if not m:
        return None
    prefix, base = m.group(1), m.group(2)
    b = parse_ver_tuple(base)

    def ok(ver: str) -> bool:
        v = parse_ver_tuple(ver)
        if prefix == "^":
            # 简化实现：^x.y.z => x 相同，且 >= base
            return v >= b and v[0] == b[0]
        if prefix == ">=":
            return v >= b
        if prefix == "~":
            return v >= b and (v[0], v[1]) == (b[0], b[1])
        return v == b

    cand = [(parse_ver_tuple(v), v) for v in versions if ok(v)]
    if not cand:
        return None
    cand.sort(key=lambda x: x[0], reverse=True)
    return cand[0][1]


def resolve_tarball(registry: str, name: str, range_spec: str) -> Optional[str]:
    doc = requests.get(npm_pkg_doc_url(registry, name), timeout=20)
    if doc.status_code != 200:
        return None
    data = doc.json()
    versions = data.get("versions") or {}
    if not versions:
        return None
    range_spec = (range_spec or "latest").strip()
    if range_spec == "latest":
        v = (data.get("dist-tags") or {}).get("latest")
    else:
        v = pick_best_version(versions.keys(), range_spec) or (data.get("dist-tags") or {}).get("latest")
    if not v or v not in versions:
        return None
    return ((versions[v].get("dist") or {}).get("tarball") or "").strip() or None


def extract_404_from_npm_install_log(log_path: Path) -> List[Tuple[str, str]]:
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    found: List[Tuple[str, str]] = []

    # npm error 404  '@types/event-emitter@^0.3.3' is not in this registry.
    for m in re.finditer(r"404\\s+[^']*'([^']+)@([^']+)'\\s+is not in this registry", text, re.I):
        found.append((m.group(1).strip(), m.group(2).strip()))

    # npm error ... Package '@types/event-emitter' not found
    for m in re.finditer(r"Package\\s+'([^']+)'\\s+not found", text, re.I):
        found.append((m.group(1).strip(), "latest"))

    # 去重保序
    seen = set()
    out: List[Tuple[str, str]] = []
    for name, rng in found:
        k = f"{name}@{rng}"
        if k in seen:
            continue
        seen.add(k)
        out.append((name, rng))
    return out


def main() -> int:
    os.chdir(str(BASE_DIR))

    private_registry, skip_phase1, skip_phase2 = read_local_config(BASE_DIR)
    if not private_registry:
        print("未找到私有 registry：请复制 config.template 为 config.local 并填写 PRIVATE_REGISTRY=，或于 .npmrc 中设置 registry=")
        return 2

    packages_dir = BASE_DIR / "packages"
    manual_dir = BASE_DIR / "manual-packages"
    download_log = BASE_DIR / "logs" / "npm_package_download.log"
    npm_install_log = BASE_DIR / "logs" / "npm_install.log"

    # 1) download（可跳过）
    if not skip_phase1:
        print("Step1: 下载依赖到 packages/ ...")
        subprocess.check_call([PYTHON, "npm_package_download.py"], cwd=str(BASE_DIR))
        extra_urls = extract_urls_from_download_log(download_log)
        if extra_urls:
            print(f"Step1.1: 检测到下载异常 URL {len(extra_urls)} 条，补下到 packages/ ...")
            download_urls(extra_urls, packages_dir)
    else:
        print("已跳过阶段1（下载 packages/）")

    # 2) publish packages（可跳过）
    if not skip_phase2:
        print("Step2: 上传 packages/ 到 Nexus ...")
        subprocess.check_call([PYTHON, "publish.py", "--packages-path", str(packages_dir)], cwd=str(BASE_DIR))
    else:
        print("已跳过阶段2（上传 packages/）")

    # 3) npm install loop：备份 lock 内容，每轮仅做一次正则替换后写回，用后恢复，避免 JSON 解析大文件且不改变磁盘上的外网 URL
    lock_path = BASE_DIR / "package-lock.json"
    lock_backup: Optional[str] = None
    if lock_path.exists():
        lock_backup = lock_path.read_text(encoding="utf-8", errors="replace")
    private_base = private_registry.rstrip("/")

    try:
        for round_idx in range(1, MAX_FIX_ROUNDS + 1):
            # 在备份文本上做正则替换后临时写回，不解析 JSON
            if lock_backup and private_base:
                rewritten, n = _rewrite_resolved_origin_in_text(lock_backup, private_base)
                if n:
                    lock_path.write_text(rewritten, encoding="utf-8")
            print(f"Step3 (round {round_idx}): npm install（registry={private_registry}）...")
            npm_exe = "npm.cmd" if sys.platform == "win32" else "npm"
            cmd = [npm_exe, "install", "--registry", private_registry, *NPM_INSTALL_ARGS]
            code = run_cmd_to_file(cmd, BASE_DIR, npm_install_log)

            missing = extract_404_from_npm_install_log(npm_install_log)
            if not missing:
                print("npm install 未检测到 404，流程结束。")
                return 0 if code == 0 else 1

            print(f"检测到 404 缺包 {len(missing)} 个，开始下载到 manual-packages/ ...")
            if manual_dir.exists():
                shutil.rmtree(manual_dir)
            manual_dir.mkdir(parents=True, exist_ok=True)

            tarballs: List[str] = []
            for name, rng in missing:
                tb = resolve_tarball(PUBLIC_REGISTRY, name, rng)
                if not tb:
                    print(f"  无法解析 tarball: {name}@{rng}")
                    continue
                tarballs.append(tb)

            if not tarballs:
                print("未能解析到任何 tarball，停止。请检查网络/registry 或手动补包。")
                return 3

            download_urls(tarballs, manual_dir)

            print("上传 manual-packages/ 到 Nexus ...")
            subprocess.check_call([PYTHON, "publish.py", "--packages-path", str(manual_dir)], cwd=str(BASE_DIR))

        print(f"已达到最大补包轮次 MAX_FIX_ROUNDS={MAX_FIX_ROUNDS}，仍有 404，请检查日志 {npm_install_log}")
        return 4
    finally:
        # 恢复原始 lock，保证 Phase1 的 npm_package_download 仍按外网 URL 下载
        if lock_backup is not None and lock_path.exists():
            lock_path.write_text(lock_backup, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

