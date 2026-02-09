"""
项目配置 + 平台初始化。各脚本顶部 import config 即可。
用户直接修改下方变量值来调整配置。
"""

import io
import os
import sys
from pathlib import Path

# ===== 项目配置（按需修改） =====

# 是否跳过阶段一（下载）。1=跳过，0=不跳过
SKIP_PHASE1 = 0

# 本地 registry 端口（npm install --registry http://127.0.0.1:端口）
LOCAL_REGISTRY_PORT = 4874

# 阶段一下载镜像（加速源），用于从 lock 批量下载 tgz
DOWNLOAD_REGISTRY = "https://registry.npmmirror.com"

# 补包公网源（npm view 查 tarball），阶段二缺包时使用
NPM_PUBLIC_REGISTRY = "https://registry.npmjs.org"

# 下载超时（秒）
DOWNLOAD_TIMEOUT = 30

# 并发下载数
DOWNLOAD_CONCURRENCY = 10

# ===== 平台常量（无需修改） =====

TOOLS_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOLS_DIR.parent  # 项目根目录（package.json 所在目录）
IS_WIN = sys.platform == "win32"
NPM = "npm.cmd" if IS_WIN else "npm"
CURL = "curl.exe" if IS_WIN else "curl"

# ===== 控制台编码（无需修改） =====

if IS_WIN:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True,
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True,
        )
    except Exception:
        pass
else:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)
        except Exception:
            pass
