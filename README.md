# 内网 npm 依赖闭环脚本

在外网机器上：按 `package-lock.json` 下载依赖 → 上传到私有 Nexus → 项目里用私有源执行 `npm install`，缺包时自动补包并重试，直到安装成功。

---

## Skills 编排说明（标准入口）

- **编排入口**：`flow.py`（唯一主流程，适合作为 Skills 的编排脚本）。
- **执行方式**：`python flow.py`；依赖与配置见下方「快速开始」。
- **配置**：全部通过 `config.local` 管理（模板 `config.template`），敏感信息不写进脚本，符合规范。
- **子脚本**：由 flow 按阶段调用 `npm_package_download.py`、`publish.py`；也可单独运行各脚本。

---

## 快速开始

1. **依赖**（Python 3 + 可联网）

   ```bash
   pip install -r requirements.txt
   ```

2. **配置**

   - 复制 `config.template` 为 `config.local`（该文件不提交）
   - 在 `config.local` 中填写 `PRIVATE_REGISTRY=`（你的 Nexus npm 仓库地址，与 publish 上传目标一致）

3. **放入依赖清单**

   - 将需要安装依赖的项目的 **package.json** 和 **package-lock.json** 放到本仓库根目录（本仓库不提交这两文件，由使用方从业务项目复制）

4. **执行主流程**

   ```bash
   python flow.py
   ```

   首次运行会：下载依赖到 `packages/` → 上传到 Nexus → 在本目录执行 `npm install --registry <私有地址>`；若出现 404 缺包，会自动从外网解析并下载到 `manual-packages/`、上传后重试，直到无 404 或达到最大轮次。

---

## 配置说明（config.local）

| 项 | 说明 |
|----|------|
| `PRIVATE_REGISTRY` | 私有 npm 仓库地址（flow 中 `npm install` 用），一般与 Nexus 仓库地址一致 |
| `NEXUS_BASE_URL` | Nexus 服务地址，publish、clear_repository 共用 |
| `NEXUS_REPOSITORY` | Nexus 仓库名 |
| `NEXUS_USERNAME` / `NEXUS_PASSWORD` | Nexus 登录账号 |
| `SKIP_PHASE1` | `true` 时跳过「下载 packages/」（首次跑完后可设为 true 只做补包循环） |
| `SKIP_PHASE2` | `true` 时跳过「上传 packages/」 |

支持 `0/1`、`false/true` 等，不区分大小写。

---

## 流程概览（flow.py）

| 阶段 | 说明 |
|------|------|
| **1** | 用 `npm_package_download.py` 根据 lock 从外网镜像下载 .tgz 到 `packages/`；若有下载异常 URL 会从日志补下 |
| **2** | 用 `publish.py` 将 `packages/` 上传到 Nexus |
| **3** | 循环：临时改写 lock 中 resolved 为私有地址 → `npm install` → 若 404 则解析缺包、从外网下到 `manual-packages/`、上传 → 再 install；结束后恢复 lock 原内容 |

日志：`logs/npm_install.log`、`logs/publish.log`、`logs/npm_package_download.log`。

---

## 脚本一览

| 脚本 | 作用 |
|------|------|
| **flow.py** | 主入口，串联 下载 → 上传 → npm install 循环（含自动补包） |
| **npm_package_download.py** | 按 package-lock.json 从外网镜像下载 .tgz 到 `packages/`，可选 `--include-peer`、`--output-dir`、`--registry` |
| **publish.py** | 将指定目录下 .tgz 批量上传到 Nexus，支持 `--packages-path`、`--base-url`、`--repository` 等；默认直接覆盖，需「已存在则跳过」时用 `--skip-existing` |
| **clear_repository.py** | 清空 Nexus 指定 npm 仓库（先列数量与依赖树，确认后删除）；删除后建议在 Nexus 界面执行「重建索引」 |

publish、clear_repository 的 Nexus 地址与账号从 **config.local** 读取（与 flow 共用）；也可用命令行参数覆盖。

---

## 单独使用

- **只下载不跑完整 flow**：配置好 `package.json` + `package-lock.json` 后执行 `python npm_package_download.py`，产物在 `packages/`。
- **只上传**：`python publish.py --packages-path ./packages`（可按需加 `--base-url`、`--repository` 等）。

---

## 环境与依赖

- Python 3，`pip install -r requirements.txt`（含 `requests`、`aiohttp`、`PyYAML`）
- 运行 `flow.py` 的 Step3 需本机已安装 **npm**（且能执行 `npm install`）
- 本仓库不提交 `package.json`、`package-lock.json`、`config.local`、`packages/`、`manual-packages/`、`logs/`，使用前从业务项目复制依赖清单并填写 config.local
