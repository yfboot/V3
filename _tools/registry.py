#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将指定目录下的 .tgz 当作 npm 本地仓库提供 HTTP 服务（类似 Maven .m2/repository）。

可单独运行（不依赖 flow.py）：
   python registry.py [目录1] [目录2] ... [端口]
   python registry.py [端口]
   - 目录为相对本脚本所在目录的路径，可多个，会合并索引。
   - 最后一个参数若为数字则视为端口。
   - 仅传端口时，默认目录为：packages。
   - 默认端口：4874。
   - 示例：python registry.py 4874
   启动后在项目根执行：npm install --registry http://127.0.0.1:4874
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

import config  # noqa: F401

_TGZ_NAME_VERSION = re.compile(r"^(.+)-(\d+\.\d+\.\d+(?:[-.]\w+)*)\.tgz$", re.IGNORECASE)


def parse_tgz_name(filename: str) -> tuple:
    m = _TGZ_NAME_VERSION.match(filename)
    if not m:
        return (None, None)
    name = m.group(1).replace("%2f", "/").replace("%2F", "/")
    return (name, m.group(2))


def scan_packages_dir(root: Path) -> dict:
    index = {}
    if not root.is_dir():
        return index
    for fp in root.glob("**/*.tgz"):
        name, ver = parse_tgz_name(fp.name)
        if name and ver:
            index[(name, ver)] = fp
    return index


def build_packument(index: dict, base_url: str, package_name: str) -> dict:
    """构建 packument；包名匹配忽略大小写；scoped 包同时匹配完整名与后半段名（如 pro-field）。"""
    package_name = (package_name or "").replace("%2F", "/").replace("%2f", "/")
    package_name_lower = package_name.lower()
    unscoped_lower = package_name.split("/", 1)[1].lower() if package_name.startswith("@") and "/" in package_name else None
    versions = {}
    for (n, v), path in index.items():
        n_lower = (n or "").lower()
        if n_lower != package_name_lower and (unscoped_lower is None or n_lower != unscoped_lower):
            continue
        if package_name.startswith("@"):
            rest = package_name.split("/", 1)[1]
            tarball_name = f"{rest}-{v}.tgz"
            path_part = package_name.replace("/", "%2F")
        else:
            tarball_name = f"{package_name}-{v}.tgz"
            path_part = package_name
        tarball_url = f"{base_url.rstrip('/')}/{path_part}/-/{tarball_name}"
        # npm/arborist 需要每个版本对象里有 name/version，否则可能出现 Invalid Version:（空版本）的问题
        versions[v] = {
            "name": package_name,
            "version": v,
            "dist": {"tarball": tarball_url},
        }
    if not versions:
        return {}
    # 给一个最基本的 dist-tags，避免部分 npm 逻辑拿不到 latest
    try:
        latest = sorted(versions.keys())[-1]
    except Exception:
        latest = next(iter(versions.keys()))
    # 顶层 version 为 latest，避免 npm 报 Invalid Version:（空）
    if not latest:
        return {}
    return {
        "name": package_name,
        "version": latest,
        "versions": versions,
        "dist-tags": {"latest": latest},
    }


class LocalRegistryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = unquote(self.path).split("?")[0].strip("/")
        index = self.server.package_index  # type: ignore
        base_url = self.server.base_url  # type: ignore

        # 重新扫描 packages 目录，使新下载的包生效（无需重启进程）
        if path == "-/rescan":
            roots = getattr(self.server, "package_roots", [])  # type: ignore
            new_index = {}
            for root in roots:
                if root.is_dir():
                    for k, v in scan_packages_dir(root).items():
                        new_index[k] = v
            self.server.package_index = new_index  # type: ignore
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"rescan ok, {len(new_index)} packages\n".encode("utf-8"))
            return

        parts = path.split("/-/", 1)
        if len(parts) == 2:
            prefix, tarball_name = parts[0].strip("/"), parts[1].strip()
            package_name = prefix.replace("%2F", "/").replace("%2f", "/")
            m = re.match(r"^(.+)-(\d+\.\d+\.\d+(?:[-.]\w+)*)\.tgz$", tarball_name, re.I)
            if m:
                ver = m.group(2)
                # 包名匹配忽略大小写；scoped 包同时匹配完整名与后半段名
                want_lower = package_name.lower()
                want_unscoped = package_name.split("/", 1)[1].lower() if package_name.startswith("@") and "/" in package_name else None
                filepath = None
                for (n, v), fp in index.items():
                    n_lower = (n or "").lower()
                    if v != ver:
                        continue
                    if n_lower == want_lower or (want_unscoped is not None and n_lower == want_unscoped):
                        filepath = fp
                        break
                if filepath is not None and filepath.exists():
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Length", str(filepath.stat().st_size))
                    self.end_headers()
                    try:
                        with filepath.open("rb") as f:
                            while True:
                                chunk = f.read(1024 * 256)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        return
                    return
            self.send_error(404)
            return

        package_name = path.replace("%2F", "/").replace("%2f", "/").strip()
        if not package_name:
            self.send_error(404)
            return
        pack = build_packument(index, base_url, package_name)
        if not pack:
            self.send_error(404)
            return
        body = json.dumps(pack).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return

    def do_HEAD(self):
        self.do_GET()

class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """屏蔽客户端中途断开导致的噪声堆栈（npm 并发/取消请求时很常见）。"""

    def handle_error(self, request, client_address):  # pragma: no cover
        exc_type, _, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return
        return super().handle_error(request, client_address)


def main():
    base_dir = Path(__file__).resolve().parent
    argv = sys.argv[1:]
    port_arg = 4874
    dirs_arg = ["packages"]
    if argv:
        if argv[-1].isdigit():
            port_arg = int(argv[-1])
            dirs_arg = argv[:-1] if len(argv) > 1 else ["packages"]
        else:
            dirs_arg = list(argv)
    index = {}
    for d in dirs_arg:
        root = (base_dir / d).resolve()
        if root.is_dir():
            for k, v in scan_packages_dir(root).items():
                index[k] = v
        else:
            print(f"跳过不存在的目录: {root}", file=sys.stderr)
    print(f"已扫描 {len(index)} 个包版本，目录: {dirs_arg}", flush=True)
    host = "127.0.0.1"
    base_url = f"http://{host}:{port_arg}"
    # npm 会并发拉取包，使用多线程 server 可显著降低连接中断/拒绝风险
    try:
        server = QuietThreadingHTTPServer((host, port_arg), LocalRegistryHandler)  # type: ignore
        server.daemon_threads = True  # type: ignore[attr-defined]
    except Exception:
        server = HTTPServer((host, port_arg), LocalRegistryHandler)
    roots = [base_dir.resolve() / d for d in dirs_arg]
    server.package_index = index  # type: ignore
    server.package_roots = roots  # type: ignore
    server.base_url = base_url  # type: ignore
    print(f"本地 registry 已启动: {base_url}", flush=True)
    print("npm install --registry " + base_url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    server.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    main()
