#!/usr/bin/env python3
"""npm 依赖本地闭环工具入口。配置项见 _tools/config.py。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_tools"))

from flow import main  # noqa: E402

try:
    exit_code = main()
except Exception as e:
    print(f"flow 异常: {e}", flush=True)
    import traceback
    traceback.print_exc()
    exit_code = 1
raise SystemExit(exit_code)
