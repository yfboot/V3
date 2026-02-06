# 本地 npm 依赖闭环脚本（仅本地仓库，无私有 Nexus）

在外网或本机：按 `package-lock.json` 下载依赖到 `packages/` → 用本地 registry（`local_registry.py`）提供 packages + manual_packages → 执行 `npm install`，缺包时从公网补包到 `manual_packages/` 并重试，直到安装成功。

---

## 快速开始

1. **依赖**（Python 3 + 可联网）

   ```bash
   pip install -r requirements.txt
   ```

2. **配置**

   - 直接编辑 `config.local`，按需修改 `SKIP_PHASE1`、`LOCAL_REGISTRY_PORT`（默认 4874）

3. **放入依赖清单**

   - 将需要安装依赖的项目的 **package.json** 和 **package-lock.json** 放到本仓库根目录

4. **执行主流程**

   ```bash
   python flow.py
   ```

   首次运行会：下载依赖到 `packages/` → 将 lock 的 resolved 重写为本地 registry → 启动本地 registry，npm 按 lock 全量从本地安装；若日志中出现缺包，从公网补包到 `manual_packages/` 并重试，结束后恢复 lock。

---

## 配置说明（config.local）

| 项 | 说明 |
|----|------|
| `SKIP_PHASE1` | 阶段一：解析 package-lock.json 并下载依赖到 packages/。`1`=跳过，`0`=不跳过 |
| `LOCAL_REGISTRY_PORT` | 本地 registry 端口，默认 4874 |

---

## 流程概览（flow 串联各阶段）

| 阶段 | 说明 |
|------|------|
| **1** | `npm_package_download.py` 按 lock 下载到 `packages/` → `download_from_log.py` 从下载日志补异常 URL 到 `packages/` |
| **2** | 备份 lock，将其中 `resolved` 重写为本地 registry URL；启动 `local_registry.py`；循环：`npm install`（按 lock 全量从本地拉包）→ `supplement_missing.py` 解析缺包并下载到 `manual_packages/` → 若有新补包则重启 registry 再 install，直到无缺包；最后从备份恢复 lock |

日志：`logs/npm_install.log`、`logs/npm_package_download.log`、`logs/supplement_round.txt`（阶段二每轮待补/已补列表）、`logs/supplemented_packages.txt`（本次运行补包汇总）。

---

## 脚本一览（每脚本只负责一事，由 flow 串联）

| 脚本 | 职责 |
|------|------|
| **flow.py** | 主入口：按阶段调用各脚本，处理 lock 重命名/恢复与 registry 启停，不做具体解析/下载 |
| **npm_package_download.py** | 阶段一：按 package-lock.json 从外网下载 .tgz 到 `packages/` |
| **download_from_log.py** | 阶段一补充：从下载日志提取 URL 并下载到 `packages/` |
| **supplement_missing.py** | 阶段三：从 npm install 日志解析缺包，用 npm view 从公网下载到 `manual_packages/` |
| **local_registry.py** | 阶段三：将 packages/ + manual_packages/ 提供为本地 HTTP registry |
| **publish.py** | 独立：批量上传 .tgz 到私有 Nexus（flow 不调用） |

---

## 单独使用

- **只下载**：配置好 `package.json` + `package-lock.json` 后执行 `python npm_package_download.py`，产物在 `packages/`。
- **只启动本地 registry**：`python local_registry.py 4874`，再在项目目录执行 `npm install --registry http://127.0.0.1:4874`。

---

## 环境与依赖

- Python 3，`pip install -r requirements.txt`（含 `requests` 等）
- 运行 flow 阶段三需本机已安装 **npm**
- 本仓库不提交 `package.json`、`package-lock.json`、`packages/`、`manual_packages/`、`logs/`
