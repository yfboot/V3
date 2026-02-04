#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线依赖闭环流程（适合做 Skills 的编排入口）

流程（可联网机器 + 可访问 Nexus）：
1) 运行 npm_package_download.py 生成 packages/
2) 如 logs/npm_package_download.log 中有 404/异常 URL，则提取下载链接并补下到 packages/
3) 运行 publish.py 上传 packages/
4) 第三阶段（可单独跳过 1、2）：安装前将 package-lock.json 重命名为 .temp；循环执行：
   - 从私有 registry 执行 npm install，输出写入 logs/npm_install.log
   - 分析日志中的 404、缺包、版本不匹配（ETARGET notarget 等）
   - 仅用命令 npm view <包>@<版本范围> dist.tarball --registry=https://registry.npmjs.org 获取下载地址
   - 用 curl 下载到 manual_packages/，上传到私有仓库，再执行 npm install
   - 直到无缺包后恢复 package-lock.json

说明：补包时只使用官方 registry.npmjs.org，不使用镜像。
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

try:
    import requests
except Exception:
    print("请先安装 requests: pip install requests")
    raise


# ================== 可配置项（按需修改） ==================
PYTHON = sys.executable
BASE_DIR = Path(__file__).resolve().parent

# 补包时仅使用 npm 官方公网仓库，不使用镜像（避免镜像过时）
NPM_PUBLIC_REGISTRY: str = "https://registry.npmjs.org"
PUBLIC_REGISTRIES: List[str] = [NPM_PUBLIC_REGISTRY]
# 私有 registry、SKIP_PHASE1/2 从 config.local 读取（见 read_local_config()）

# npm install 额外参数（例如 "--legacy-peer-deps"）
NPM_INSTALL_ARGS: List[str] = []

# 最大补包轮次，避免无限循环
MAX_FIX_ROUNDS = 200


# ================== 工具函数 ==================
def read_local_config(base_dir: Path) -> Tuple[str, bool, bool]:
    """
    从 config.local 读取：私有 registry（由 NEXUS 四项推导）、是否跳过阶段1/2。
    返回 (private_registry, skip_phase1, skip_phase2)。私有 registry 未配置时再回退到 .npmrc。
    """
    from config_loader import get_private_registry, load_config
    private_registry = get_private_registry(base_dir)
    cfg = load_config(base_dir)
    skip_phase1 = (cfg.get("SKIP_PHASE1") or "").strip().lower() in ("1", "true", "yes", "on")
    skip_phase2 = (cfg.get("SKIP_PHASE2") or "").strip().lower() in ("1", "true", "yes", "on")
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


def run_cmd_to_file(cmd: List[str], cwd: Path, log_path: Path, echo_stdout: bool = True) -> int:
    """执行命令，实时写日志；echo_stdout=True 时同时输出到终端，False 时仅写 log（避免淹没终端）。"""
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


def safe_tarball_basename(package_name: str, version: str) -> str:
    """
    生成与 publish.py 解析约定一致的 .tgz 文件名。
    - 无 scope：name-version.tgz
    - 有 scope：@scope%2Fname-version.tgz（%2F 便于 publish 解析回 @scope/name）
    """
    name = (package_name or "").strip()
    ver = (version or "").strip()
    if not ver:
        return name + ".tgz"
    if name.startswith("@"):
        part = name.replace("/", "%2F")
    else:
        part = name
    return f"{part}-{ver}.tgz"


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


def get_tarball_via_npm_view(name: str, range_spec: str, verbose: bool = True) -> Optional[Tuple[str, str]]:
    """
    仅使用 npm view 从官方 registry 获取 tarball 地址和版本（你提供的命令方式）。
    命令: npm view <name>@<range> dist.tarball --registry=https://registry.npmjs.org
          npm view <name>@<range> version --registry=https://registry.npmjs.org
    返回 (tarball_url, version) 或 None。
    """
    range_spec = (range_spec or "").strip().rstrip(".")  # 去掉末尾点，避免 npm view 404（如 ^7.29.0.）
    spec = f"{name}@{range_spec}" if range_spec else name
    npm_exe = "npm.cmd" if sys.platform == "win32" else "npm"
    reg = f"--registry={NPM_PUBLIC_REGISTRY}"
    try:
        out = subprocess.run(
            [npm_exe, "view", spec, "dist.tarball", reg],
            capture_output=True,
            encoding="utf-8",
            timeout=25,
            cwd=str(BASE_DIR),
        )
        if verbose:
            print(f"  执行: {npm_exe} view {spec} dist.tarball {reg}", flush=True)
        if out.returncode != 0 or not out.stdout or not out.stdout.strip():
            if verbose:
                print(f"  失败: 退出码 {out.returncode}, stderr: {(out.stderr or '').strip()[:150]}", flush=True)
            return None
        url = out.stdout.strip()
        if not url.startswith("http"):
            if verbose:
                print(f"  失败: 输出非 URL", flush=True)
            return None
        out2 = subprocess.run(
            [npm_exe, "view", spec, "version", reg],
            capture_output=True,
            encoding="utf-8",
            timeout=25,
            cwd=str(BASE_DIR),
        )
        version = (out2.stdout or "").strip() if out2.returncode == 0 else ""
        if not version:
            version = "unknown"
        if verbose:
            print(f"  得到: version={version}, tarball={url[:70]}...", flush=True)
        return (url, version)
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        if verbose:
            print(f"  异常: {e}", flush=True)
        return None


def download_via_curl(url: str, dest: Path, timeout: int = 60) -> bool:
    """使用 curl 将 url 下载到 dest。"""
    curl_exe = "curl.exe" if sys.platform == "win32" else "curl"
    cmd = [curl_exe, "-L", "-s", "-S", "-o", str(dest), "--connect-timeout", "15", "--max-time", str(timeout), url]
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout + 10)
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return False


def download_tarballs_with_names(
    entries: List[Tuple[str, str]], out_dir: Path, timeout: int = 60
) -> List[Tuple[str, str]]:
    """
    第三阶段补包：仅用 npm view 获取 tarball，下载到 out_dir（manual_packages），保存为规范文件名。
    只使用官方 registry.npmjs.org。返回成功下载的 [(name, range), ...]。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    supplemented: List[Tuple[str, str]] = []
    for name, rng in entries:
        print(f"缺包: {name}@{rng}", flush=True)
        pair = get_tarball_via_npm_view(name, rng, verbose=True)
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
            print(f"  curl 失败，改用 requests 下载 ...", flush=True)
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
            print(f"  下载失败", flush=True)
    return supplemented


def npm_pkg_doc_url(registry: str, name: str) -> str:
    registry = registry.rstrip("/")
    if name.startswith("@") and "/" in name:
        scope, pkg = name.split("/", 1)
        return f"{registry}/{scope}%2F{pkg}"
    return f"{registry}/{name}"


def parse_ver_tuple(v: str) -> Tuple[int, int, int]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v.strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def version_satisfies_range(version: str, range_str: str) -> bool:
    """判断 version 是否满足 range_str（^x.y.z / ~x.y.z / >=x.y.z / x.y.z）。"""
    range_str = (range_str or "").strip()
    if not range_str or range_str.lower() == "latest":
        return True
    m = re.match(r"^(\^|>=|~)?\s*(\d+\.\d+\.\d+[^\s]*)", range_str)
    if not m:
        return True
    prefix, base = m.group(1), m.group(2)
    b = parse_ver_tuple(base)
    v = parse_ver_tuple(version)
    if prefix == "^":
        return v >= b and v[0] == b[0]
    if prefix == ">=":
        return v >= b
    if prefix == "~":
        return v >= b and (v[0], v[1]) == (b[0], b[1])
    return v == b


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


def resolve_tarball(registry: str, name: str, range_spec: str) -> Optional[Tuple[str, str]]:
    """从指定 registry 查包元数据，按版本范围解析出 (tarball URL, version)。"""
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
    url = ((versions[v].get("dist") or {}).get("tarball") or "").strip() or None
    return (url, v) if url else None


def resolve_tarball_from_public(name: str, range_spec: str) -> Optional[str]:
    """按 PUBLIC_REGISTRIES 顺序从公网查包，取 tarball 链接。保留兼容，仅返回 URL。"""
    pair = resolve_tarball_from_public_with_version(name, range_spec)
    return pair[0] if pair else None


def resolve_tarball_from_public_with_version(name: str, range_spec: str) -> Optional[Tuple[str, str]]:
    """按 PUBLIC_REGISTRIES 顺序从公网查包，返回 (tarball URL, version) 或 None。"""
    for registry in PUBLIC_REGISTRIES:
        pair = resolve_tarball(registry, name, range_spec)
        if pair:
            return pair
    return None


def extract_404_from_npm_install_log(log_path: Path) -> List[Tuple[str, str]]:
    """
    从 npm install 日志中解析「缺包」：404、Package not found、ETARGET notarget（No matching version found）。
    仅上述几类视为缺包；日志中的 "ERESOLVE overriding peer dependency" / "Could not resolve dependency: peer X"
    为 peer 依赖警告，不表示包不存在，不纳入缺包列表。
    返回 [(包名, 版本范围), ...]。
    """
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    found: List[Tuple[str, str]] = []

    # npm error 404  '@types/event-emitter@^0.3.3' is not in this registry.
    for m in re.finditer(r"404\s+[^']*'([^']+)@([^']+)'\s+is not in this registry", text, re.I):
        rng = m.group(2).strip().rstrip(".")
        found.append((m.group(1).strip(), rng))

    # npm error ... Package '@types/event-emitter' not found
    for m in re.finditer(r"Package\s+'([^']+)'\s+not found", text, re.I):
        found.append((m.group(1).strip(), "latest"))

    # npm error notarget No matching version found for @babel/plugin-transform-named-capturing-groups-regex@^7.29.0.
    for m in re.finditer(r"notarget\s+No matching version found for\s+(.+?)@(\S+)", text, re.I):
        rng = m.group(2).strip().rstrip(".")  # 去掉句末的点，否则 npm view 会 404（如 ^7.29.0.）
        found.append((m.group(1).strip(), rng))

    # Nexus 500：私有库已有该包但缺某版本，如 "Package @mapbox/node-pre-gyp lacks tarball version 2.0.3"
    for m in re.finditer(r"Package\s+([^\s]+)\s+lacks\s+tarball\s+version\s+(\S+)", text, re.I):
        name = m.group(1).strip().rstrip(".")
        ver = m.group(2).strip().rstrip(".")
        found.append((name, ver))

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


def extract_lacks_tarball_from_log(log_path: Path) -> List[Tuple[str, str]]:
    """从 publish 或 install 日志中解析 Nexus 500：Package X lacks tarball version Y。返回 [(name, version), ...]。"""
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    found: List[Tuple[str, str]] = []
    for m in re.finditer(r"Package\s+([^\s]+)\s+lacks\s+tarball\s+version\s+(\S+)", text, re.I):
        name = m.group(1).strip().rstrip(".")
        ver = m.group(2).strip().rstrip(".")
        found.append((name, ver))
    seen = set()
    out: List[Tuple[str, str]] = []
    for t in found:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def main() -> int:
    # 确保标准输出/错误立即显示（避免缓冲导致运行后无输出）
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)
        except Exception:
            pass
    print("flow 开始执行 ...", flush=True)
    os.chdir(str(BASE_DIR))

    private_registry, skip_phase1, skip_phase2 = read_local_config(BASE_DIR)
    if not private_registry:
        print("未找到私有 registry：请复制 config.template 为 config.local 并填写 NEXUS_REGISTRY，或于 .npmrc 中设置 registry=", flush=True)
        return 2

    packages_dir = BASE_DIR / "packages"
    manual_dir = BASE_DIR / "manual_packages"
    download_log = BASE_DIR / "logs" / "npm_package_download.log"
    npm_install_log = BASE_DIR / "logs" / "npm_install.log"

    # 1) download（可跳过）
    if not skip_phase1:
        print("Step1: 下载依赖到 packages/ ...", flush=True)
        subprocess.check_call([PYTHON, "npm_package_download.py"], cwd=str(BASE_DIR))
        extra_urls = extract_urls_from_download_log(download_log)
        if extra_urls:
            print(f"Step1.1: 检测到下载异常 URL {len(extra_urls)} 条，补下到 packages/ ...", flush=True)
            download_urls(extra_urls, packages_dir)
    else:
        print("已跳过阶段1（下载 packages/）", flush=True)

    # 2) publish packages（可跳过）
    if not skip_phase2:
        print("Step2: 上传 packages/ 到 Nexus ...", flush=True)
        subprocess.check_call([PYTHON, "publish.py", "--packages-path", str(packages_dir)], cwd=str(BASE_DIR))
    else:
        print("已跳过阶段2（上传 packages/）", flush=True)

    # 3) 安装前重命名 package-lock.json；循环：install -> 分析缺包 -> 仅对「本 run 未补过的」用 npm view 取 tarball -> 追加下载到 manual_packages（不删已有）-> 上传私有仓库 -> 再 install；直到无缺包。上传后不删除 manual_packages，结束时输出本次所有缺包列表。
    lock_path = BASE_DIR / "package-lock.json"
    lock_temp = BASE_DIR / "package-lock.json.temp"
    lock_renamed = False
    if lock_path.exists():
        lock_path.rename(lock_temp)
        lock_renamed = True
        print("Step3: 已临时重命名 package-lock.json -> package-lock.json.temp，从私有库解析依赖。", flush=True)

    supplemented_this_run: set[Tuple[str, str]] = set()  # 本 run 已下载并上传过的，不再重复下载
    all_supplemented: List[Tuple[str, str]] = []  # 本次运行所有补包，用于最后汇总
    supplement_log_path = BASE_DIR / "logs" / "supplemented_packages.txt"

    try:
        for round_idx in range(1, MAX_FIX_ROUNDS + 1):
            print(f"Step3 (round {round_idx}): npm cache clean --force ...", flush=True)
            npm_exe = "npm.cmd" if sys.platform == "win32" else "npm"
            subprocess.run([npm_exe, "cache", "clean", "--force"], cwd=str(BASE_DIR), capture_output=True, timeout=60)
            print(f"Step3 (round {round_idx}): npm install（registry={private_registry}），输出仅写 {npm_install_log} ...", flush=True)
            cmd = [npm_exe, "install", "--registry", private_registry, *NPM_INSTALL_ARGS]
            code = run_cmd_to_file(cmd, BASE_DIR, npm_install_log, echo_stdout=False)

            missing = extract_404_from_npm_install_log(npm_install_log)
            if not missing:
                print("npm install 未检测到缺包，流程结束。", flush=True)
                if all_supplemented:
                    supplement_log_path.parent.mkdir(parents=True, exist_ok=True)
                    with supplement_log_path.open("w", encoding="utf-8") as f:
                        f.write("# 本次运行补包列表（缺包 -> 已下载并上传至私有库）\n")
                        for n, r in all_supplemented:
                            f.write(f"{n}@{r}\n")
                    print(f"本次补包共 {len(all_supplemented)} 个，已保存于 manual_packages/，列表见 {supplement_log_path}", flush=True)
                    for n, r in all_supplemented:
                        print(f"  - {n}@{r}", flush=True)
                return 0 if code == 0 else 1

            new_missing = [(n, r) for (n, r) in missing if (n, r) not in supplemented_this_run]
            if not new_missing:
                print("缺包列表均为本 run 已上传的包，重试一次 npm cache clean --force 后 npm install ...", flush=True)
                subprocess.run([npm_exe, "cache", "clean", "--force"], cwd=str(BASE_DIR), capture_output=True, timeout=60)
                code = run_cmd_to_file(cmd, BASE_DIR, npm_install_log, echo_stdout=False)
                missing = extract_404_from_npm_install_log(npm_install_log)
                if not missing:
                    print("npm install 未检测到缺包，流程结束。", flush=True)
                    if all_supplemented:
                        supplement_log_path.parent.mkdir(parents=True, exist_ok=True)
                        with supplement_log_path.open("w", encoding="utf-8") as f:
                            f.write("# 本次运行补包列表\n")
                            for n, r in all_supplemented:
                                f.write(f"{n}@{r}\n")
                        print(f"本次补包共 {len(all_supplemented)} 个，已保存于 manual_packages/，列表见 {supplement_log_path}", flush=True)
                    return 0 if code == 0 else 1
                new_missing = [(n, r) for (n, r) in missing if (n, r) not in supplemented_this_run]
            if not new_missing:
                print("以下依赖已上传至私有库但安装仍报错，请检查 Nexus 或重试；manual_packages/ 中已保留已下载的包。", flush=True)
                for n, r in missing:
                    print(f"  {n}@{r}", flush=True)
                return 5

            print(f"检测到缺包 {len(missing)} 个，其中本 run 未补过的 {len(new_missing)} 个，仅对未补过的用 npm view 下载并追加到 manual_packages/ ...", flush=True)
            for n, r in new_missing:
                print(f"  - {n}@{r}", flush=True)
            manual_dir.mkdir(parents=True, exist_ok=True)
            # npm install 的详细输出只写日志，不刷屏终端；补包/上传等进度仍打终端
            # 不删除 manual_packages，每轮只追加新包，保留已上传的

            supplemented = download_tarballs_with_names(new_missing, manual_dir)
            if not supplemented:
                print("未能通过 npm view 解析或下载任何 tarball，停止。", flush=True)
                return 3
            supplemented_this_run.update(supplemented)
            all_supplemented.extend(supplemented)

            print("上传 manual_packages/ 到私有仓库（目录保留，不删除）...", flush=True)
            publish_log = BASE_DIR / "logs" / "publish.log"
            subprocess.check_call([PYTHON, "publish.py", "--packages-path", str(manual_dir)], cwd=str(BASE_DIR))
            lacks = extract_lacks_tarball_from_log(publish_log)
            if lacks:
                extra = [(n, v) for (n, v) in lacks if (n, v) not in supplemented_this_run]
                if extra:
                    print(f"Nexus 报缺某版本 tarball，补包 {len(extra)} 个并重新上传：", flush=True)
                    for n, v in extra:
                        print(f"  - {n}@{v}", flush=True)
                    supp2 = download_tarballs_with_names(extra, manual_dir)
                    supplemented_this_run.update(supp2)
                    all_supplemented.extend(supp2)
                    if supp2:
                        subprocess.check_call([PYTHON, "publish.py", "--packages-path", str(manual_dir)], cwd=str(BASE_DIR))

        print(f"已达到最大补包轮次 MAX_FIX_ROUNDS={MAX_FIX_ROUNDS}，仍有缺包，请检查日志 {npm_install_log}", flush=True)
        if all_supplemented:
            supplement_log_path.parent.mkdir(parents=True, exist_ok=True)
            with supplement_log_path.open("w", encoding="utf-8") as f:
                f.write("# 本次运行补包列表（未完全解决缺包）\n")
                for n, r in all_supplemented:
                    f.write(f"{n}@{r}\n")
            print(f"本次已补包 {len(all_supplemented)} 个，已保存于 manual_packages/，列表见 {supplement_log_path}", flush=True)
        return 4
    finally:
        if lock_renamed and lock_temp.exists():
            lock_temp.rename(lock_path)
            print("已恢复 package-lock.json。", flush=True)


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as e:
        print(f"flow.py 异常: {e}", flush=True)
        import traceback
        traceback.print_exc()
        exit_code = 1
    raise SystemExit(exit_code)

