# 内网 npm 依赖下载与上传指南

本文档说明如何在外网准备 npm 依赖包，上传到 内网Nexus 私服，并在内网完成离线安装。提供**多种实现方式**，可按环境与偏好选择。

---

## 方式一：本仓库脚本（推荐，无需先安装 node_modules）

**特点**：仅需 `package-lock.json`（或 pnpm-lock.yaml / yarn.lock），无需先执行 `npm install`。适合“只有锁文件、无 node_modules”的场景。

### 1. 下载依赖到本地

- 将 **package-lock.json**（或 pnpm-lock.yaml / yarn.lock）与 **npm_package_download.py**（或 npm_package_download.exe）放在**同一目录**，且**路径不含中文**。
- 在可联网机器上执行：

```bash
# 使用 Python 脚本（需安装 aiohttp、PyYAML，脚本可自动检测并安装）
python npm_package_download.py

# 可选：包含 peer 依赖，尽量下载全
python npm_package_download.py --include-peer

# 指定输出目录和镜像
python npm_package_download.py --output-dir ./packages --registry https://registry.npmmirror.com
```

或直接运行 **npm_package_download.exe**（Windows）。

- 完成后会在当前目录生成 **packages** 文件夹，内含所有 `.tgz` 包。
- 若有失败，会生成 **download_log.txt**，可按其中链接手动下载缺失包。

### 2. 补齐缺包（可选）

若 `npm install` 或 pack-util 报缺包（如 404），可将**完整终端输出**保存为日志，用本仓库的补齐脚本自动解析并下载：

```bash
# 示例：先保存安装日志
npm install --ignore-scripts > npm-install.log 2>&1

# 从日志提取缺包并下载到 packages/
node download-missing-packages.js --log npm-install.log --out ./packages --registry https://registry.npmmirror.com
```

报告会写入 **missing-packages-report.txt**。

### 3. 上传到 Nexus

- 将 **publish.py**（或 **publish.sh**）与 **packages** 目录拷贝到内网/外网机器（需安装 Python 和 `requests`：`pip install requests`）。**publish.sh** 为 Shell 入口，仅转发到 **publish.py**，新功能均在 **publish.py** 中实现。
- 在脚本内修改配置，或通过命令行指定 Nexus 地址、仓库名：

```bash
# 使用脚本内默认配置（编辑 publish.py 顶部 BASE_URL、REPOSITORY、USERNAME、PASSWORD）
python publish.py
# 或
./publish.sh

# 外网 Nexus
python publish.py --base-url http://外网Nexus:8081 --repository npm-test

# 内网 Nexus
python publish.py --base-url http://内网Nexus:8081 --repository npm-hosted --packages-path ./packages
```

- **已存在同版本依赖时**：默认**跳过**不上传（由脚本头 `SKIP_IF_EXISTS = True` 控制）；若需**覆盖**，可在 **publish.py** 顶部改为 `SKIP_IF_EXISTS = False`，或命令行加 `--overwrite`（覆盖会先删后传）。显式指定跳过可用 `--skip-existing`。
- **性能说明**：脚本不做“先查询再上传”，而是直接上传；仅当 Nexus 返回 400（does not allow updating）时视为已存在，因此不会多一轮请求。若改为先查再传，则会多一倍请求。
- 详细结果见 **logs/publish.log**（成功/失败/跳过/覆盖列表）。

### 4. 内网安装

- 配置 registry 指向 Nexus：

```bash
npm config set registry http://你的Nexus地址:端口/repository/你的仓库名/
```

- 若 `npm install` 报错，可二选一：
  - **方式 A**：直接删除 **package-lock.json**，再执行 `npm install --legacy-peer-deps`。
  - **方式 B**：在 package-lock.json 中把外网 registry 地址替换为上述 Nexus 地址，并将 **integrity** 字段替换为占位（如 `"fffffffun"`），再执行 `npm install --legacy-peer-deps`。

---

## 方式二：pack-util（需先在外网安装 node_modules）

**特点**：使用开源工具 [pack-util](https://github.com/vampirefan/pack-util)，从已安装的 **node_modules** 打包，输出到 **node_modules_pack**，再上传。支持 pnpm / yarn / npm。

### 1. 安装 pack-util

```bash
npm install -g @fanwang/pack-util
```

若 Windows 提示禁止运行脚本，请以管理员身份在 PowerShell 中执行：

```powershell
Set-ExecutionPolicy RemoteSigned
```

### 2. 外网准备依赖（npm 模式必读）

**重要**：选择 **npm** 时，pack-util 会执行 `npm list --all` 枚举依赖，因此**必须先在外网安装好 node_modules**，否则会报 `ELSPROBLEMS / UNMET DEPENDENCY` 导致打包失败。

建议流程：

```bash
cd your_project
npm config set registry https://registry.npmmirror.com
npm install --ignore-scripts   # 避免 husky 等 prepare 脚本失败影响安装
pack-util pack
# 选择包管理工具：pnpm | yarn | npm
```

- pnpm：根据 **pnpm-lock.yaml** 检索  
- yarn：根据 **yarn.lock** 检索  
- npm：根据 **npm list --all** 输出检索（故必须先 `npm install`）

完成后会在项目下生成 **node_modules_pack** 目录。

### 3. 上传到 Nexus

- 将 **node_modules_pack** 与项目一起拷贝到内网，在项目目录执行：

```bash
pack-util upload
```

按提示输入 Nexus 地址、仓库名、用户名、密码。若部分包上传失败，可查看提示后重试或手动上传。

### 4. 内网安装

- 先删除 **yarn.lock** / **package-lock.json** / **pnpm-lock.yaml** 之一（与使用的包管理器一致）。
- 配置 registry 并安装：

**PNPM**

```bash
pnpm config set registry http://xx.xx.xx.xx:xxxx/repository/localNpm/
pnpm install
```

**YARN**

```bash
yarn config set registry http://xx.xx.xx.xx:xxxx/repository/localNpm/
yarn install
```

**NPM**

```bash
npm config set registry http://xx.xx.xx.xx:xxxx/repository/localNpm/
npm install --save --legacy-peer-deps
```

---

## 其他脚本说明

| 脚本 | 作用 |
|------|------|
| **clear-repository.py** | 清空 Nexus 指定 npm 仓库中的所有包。**先拉取全部组件，展示总数量与两层依赖树（包 → 版本列表）**，用户确认后再**并行删除**。需安装 `requests`。配置在脚本内修改 `BASE_URL`、`REPOSITORY`、`USERNAME`、`PASSWORD`（base_url 可含上下文路径，如 `http://host:8081/nexus`），或使用参数：`--base-url`、`--repository`、`--username`、`--password`、`--workers`、`--yes`。 |
| **download-missing-packages.js** | 从 npm install / pack-util 的日志中解析缺包，解析版本并下载到 packages/，详见上文「补齐缺包」。 |

---

## 方式对比小结

| 项目 | 方式一（本仓库脚本） | 方式二（pack-util） |
|------|----------------------|----------------------|
| 是否需要先安装 node_modules | 否（仅需 lock 文件） | 是（npm 模式必须） |
| 下载/打包产物 | packages/*.tgz | node_modules_pack/*.tgz |
| 上传方式 | publish.py | pack-util upload |
| 依赖 | Python 或 exe；上传需 Python + requests | Node.js + 全局 pack-util |

按需选择：**只有 lock 文件、不想先装 node_modules** 用方式一；**已在外网装好 node_modules、习惯 pack-util** 用方式二。两种方式上传到 Nexus 后，内网安装步骤一致（配置 registry + `npm install --legacy-peer-deps` 或删除/替换 lock 文件）。
