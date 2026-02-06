# 完整需求描述

请按以下需求理解并实现/修改 **flow 的「仅本地仓库」运行方式**。全程**不使用私有 Nexus**，依赖只落在本地目录，第三阶段安装与补包均基于项目内的**本地 registry**。

---

## 一、目标与约束

**目标：**

- 第一次执行 **flow.py** 时：**不跳过阶段一**，执行下载逻辑，把依赖下载到**本地仓库目录**（即项目内的 `packages/`）。
- **第三阶段** 完全不用私有仓库：不向 Nexus 上传、安装时也不从私有仓库拉包；改为使用**本地 registry**：`npm install --registry http://127.0.0.1:<端口>`，由本地 HTTP 服务提供 `packages/` 与 `manual_packages/` 下的 .tgz。
- 若 **npm install** 报缺包（404 等），则用 `npm view` 从公网（https://registry.npmjs.org）解析 tarball，**下载到本地仓库目录**（如 `manual_packages/`），然后重启本地 registry，再执行 npm install，循环直到无缺包或达到最大轮次。**不上传私有仓库**。

**约束：**

- 阶段一：必须执行，下载结果只写入本地目录（如 `packages/`），不依赖 Nexus。
- 阶段二：在「仅本地仓库」模式下**不执行**「上传到 Nexus」；即阶段二可跳过或在本模式下自动跳过，保证依赖只留在本地（packages/ 等）。
- 阶段三：仅使用本地 registry + 本地目录补包，不调用 publish、不访问 NEXUS_*。

---

## 二、配置要求

- **config.local**（或 config.template 复制为 config.local）中需支持：
  - **SKIP_PHASE1**：首次完整跑时设为 **false**，即不跳过阶段一，执行下载逻辑。
  - **SKIP_PHASE2**：在仅本地模式下设为 **true**，或由「是否仅本地」自动决定，使**不上传 packages/ 到 Nexus**。
  - **INSTALL_FROM_LOCAL**：设为 **true**，表示第三阶段用本地 registry 安装，不用私有仓库。
  - **LOCAL_REGISTRY_PORT**：本地 registry 端口，如 4874。
- 当 INSTALL_FROM_LOCAL=true 时，**不要求** 填写 NEXUS_REGISTRY/NEXUS_USERNAME/NEXUS_PASSWORD；阶段三不读取或使用它们。

---

## 三、三阶段行为（仅本地仓库模式）

### 阶段一（不跳过）

- 执行 **npm_package_download.py**（或当前项目中等价的「根据 package.json/package-lock 下载依赖」逻辑）。
- 将所有下载的 .tgz 写入**本地仓库目录**，即项目下的 **packages/**（或配置项指定的目录）；不写入 Nexus。
- 若存在「从 npm_install 或下载日志中提取 404/异常 URL 并补下」的逻辑，补下的包同样只写入 packages/（或同一本地目录）。

### 阶段二（在仅本地模式下不执行上传）

- 当 INSTALL_FROM_LOCAL=true（或等价配置）时：**不执行**「将 packages/ 上传到 Nexus」；即不调用 publish.py 或任何向 NEXUS_REGISTRY 上传的逻辑。
- 可通过 SKIP_PHASE2=true 显式跳过，或在代码中根据 INSTALL_FROM_LOCAL 自动跳过阶段二的上传步骤，使依赖仅保留在本地 packages/。

### 阶段三（本地 registry + 本地补包）

- **安装前**：若存在 package-lock.json，先重命名为 package-lock.json.temp，安装结束后再恢复（以便从本地 registry 解析依赖）。
- **本地 registry**：
  - 启动本地 HTTP 服务（如 **local_registry.py**）：扫描 **packages/** 与 **manual_packages/** 下所有 .tgz，提供 npm registry 兼容的接口（packument + tarball 下载），监听 127.0.0.1:&lt;LOCAL_REGISTRY_PORT&gt;；目录由命令行或配置传入（当前项目的 packages 与 manual_packages）。
- **安装**：在项目根执行 `npm install --registry http://127.0.0.1:<LOCAL_REGISTRY_PORT>`，将标准输出/错误写入 **logs/npm_install.log**。
- **缺包处理**：
  - 从 logs/npm_install.log 中解析 404、缺包、notarget、lacks tarball 等，得到缺包列表 [(name, version_or_range), ...]。
  - 对每个缺包：用 `npm view <name>@<version_or_range> dist.tarball --registry=https://registry.npmjs.org` 取 tarball URL，用 `npm view ... version` 取解析后的 version；若包名为 @tootallnate/once 且版本范围残缺（如 1、1.），可规范为 2 再查。
  - 使用 curl（或 requests）将 tarball **下载到本地仓库目录** **manual_packages/**，文件名格式：scoped 包为 @scope%2Fname-version.tgz，否则 name-version.tgz。
  - **不**调用 publish、**不**上传到 Nexus。
  - 重启本地 registry（重新扫描 packages/ 与 manual_packages/），再执行 npm install；循环直到无缺包或达到最大轮次（如 200）。
- **结束后**：恢复 package-lock.json，停止本地 registry 进程，并可选输出本次补包列表（写入 manual_packages/ 或 logs/）。

---

## 四、本地 registry 与缺包逻辑说明

- **本地 registry**：多目录 .tgz 扫描（如 packages/、manual_packages/），提供 packument（GET /&lt;package_name&gt;）与 tarball 下载（GET /&lt;path&gt;/-/&lt;tarball_name&gt;），tarball URL 格式符合 npm 约定；监听 127.0.0.1:&lt;端口&gt;。
- **缺包解析**：从 logs/npm_install.log 用正则匹配 404、Package not found、notarget、lacks tarball 等，得到 (name, version_or_range) 列表。
- **补包方式**：仅用公网 registry.npmjs.org + 下载到 manual_packages/，不上传 Nexus；重启本地 registry 后重试 install。

---

## 五、预期一次完整运行

1. **配置**：SKIP_PHASE1=false，SKIP_PHASE2=true（或由 INSTALL_FROM_LOCAL 自动跳过阶段二），INSTALL_FROM_LOCAL=true，LOCAL_REGISTRY_PORT=4874；可不填 Nexus 账号密码。
2. **执行**：`python flow.py`。
3. **结果**：阶段一将依赖下载到 packages/；阶段二不上传；阶段三用本地 registry 安装，缺包自动下载到 manual_packages/ 并重试，直到 node_modules 安装完成，全程无私有仓库参与。

请根据以上描述，检查并实现/修改 flow.py、config 读取、以及（若需要）本地 registry 服务（如 local_registry.py）的集成，使「第一次跑 flow、仅用本地仓库」的流程完整可用。
