import asyncio
import json
import os
from urllib.parse import urlparse, unquote
import re
import sys
import time

import aiohttp
import yaml

import config


class Emoji:
    """根据终端能力自动选择 emoji 或 ASCII 替代符。"""

    _MAP = {
        "✅": "[OK]", "❌": "[X]", "⚠️": "[!]", "🔍": "[?]",
        "📦": "[PKG]", "🎉": "[YAY]", "🔧": "[TOOL]", "⏱️": "[TIME]",
        "📊": "[STAT]", "📝": "[LOG]", "🚫": "[BLOCK]", "🔁": "[RETRY]",
    }

    def __init__(self):
        self.supports_emoji = self._detect()

    @staticmethod
    def _detect() -> bool:
        if getattr(sys, 'frozen', False):
            return False
        if config.IS_WIN:
            return bool(os.environ.get('WT_SESSION') or os.environ.get('TERM_PROGRAM'))
        return True

    def get(self, char: str) -> str:
        return char if self.supports_emoji else self._MAP.get(char, "[?]")


emoji = Emoji()

# ===== 配置（来自 config.py） =====
PACKAGES_PATH = "./packages"
MAX_RETRIES = 3
TIMEOUT = config.DOWNLOAD_TIMEOUT
CONCURRENT_LIMIT = config.DOWNLOAD_CONCURRENCY
CUSTOM_REGISTRY = config.DOWNLOAD_REGISTRY
DOWNLOAD_LOG = "logs/download.log"

# ===== 工具函数 =====
def replace_registry(url, use_custom=True):
    """将 tarball URL 的源替换为配置的镜像。
    通过提取 URL 路径部分重建，兼容任何来源（npmjs / npmmirror / 本地 127.0.0.1 等）。"""
    if not use_custom or "/-/" not in url:
        return url
    parsed = urlparse(url)
    return CUSTOM_REGISTRY.rstrip("/") + parsed.path

# ===== 安全路径处理 =====
def sanitize_path(path):
    """将非法路径字符替换为安全字符"""
    return re.sub(r'[<>:"/\\|?*\x00-\x1F()@]', '_', path)

def clean_package_url(url):
    """清理URL中的嵌套依赖信息"""
    # 基本npm包URL格式: registry/name/-/name-version.tgz
    parsed = urlparse(url)
    path = parsed.path
    
    # 如果没有嵌套括号，直接返回
    if '(' not in path and ')' not in path:
        return url
    
    try:
        # 提取主要部分
        # 对于 /pkg/-/pkg-1.0.0(dep1)(dep2).tgz 提取成 /pkg/-/pkg-1.0.0.tgz
        main_path = re.sub(r'(\([^()]*(?:\([^()]*\)[^()]*)*\))+\.tgz$', '.tgz', path)
        
        # 如果清理失败，尝试更复杂的方法
        if '(' in main_path:
            # 识别作用域包 /@scope/pkg/-/pkg-1.0.0(...)
            if '/@' in path and '/-/' in path:
                scope_end = path.find('/-/')
                if scope_end > 0:
                    scope_part = path[:scope_end]  # 例如 /@scope/pkg
                    file_part = path[scope_end+3:] # 例如 pkg-1.0.0(...).tgz
                    if '(' in file_part:
                        # 提取版本号
                        pkg_name = scope_part.split('/')[-1]
                        version_match = re.match(f'{pkg_name}-([0-9]+\\.[0-9]+\\.[0-9]+[^()]*)\\(', file_part)
                        if version_match:
                            version = version_match.group(1)
                            main_path = f"{scope_part}/-/{pkg_name}-{version}.tgz"
            else:
                # 处理普通包 /pkg/-/pkg-1.0.0(...)
                base_path_match = re.match(r'^(/[^/]+/-/[^/]+-\d+\.\d+\.\d+)', path)
                if base_path_match:
                    main_path = f"{base_path_match.group(1)}.tgz"
                else:
                    # 最后尝试
                    pkg_path = path.split('/-/')[0] if '/-/' in path else ''
                    file_name = os.path.basename(path)
                    version_match = re.match(r'.*?-(\d+\.\d+\.\d+[^()]*?)[\(\.]', file_name)
                    if version_match and pkg_path:
                        pkg_name = os.path.basename(pkg_path)
                        version = version_match.group(1)
                        main_path = f"{pkg_path}/-/{pkg_name}-{version}.tgz"
        
        # 重建URL
        cleaned_url = f"{parsed.scheme}://{parsed.netloc}{main_path}"
        if url != cleaned_url:
            print(f"{emoji.get('🔧')} 修正包URL: {url.split('/')[-1]} -> {cleaned_url.split('/')[-1]}")
        return cleaned_url
    except Exception as e:
        print(f"{emoji.get('⚠️')} URL清理失败，使用原始URL: {url}, 错误: {str(e)[:100]}")
        return url
        
# ===== 处理包URL =====
def add_url_to_download(urls_set, url):
    """添加URL到下载集合，处理URL格式问题"""
    # 清理嵌套依赖信息
    clean_url = clean_package_url(url)
    urls_set.add(clean_url)

# ===== 从 lock 中收集“有 resolved”的包名（用于排除已存在的 peer/optional） =====
def _npm_lock_resolved_names(lockfile_data):
    """返回 package-lock 中已有 resolved 的包名集合。
    支持嵌套路径：node_modules/a/node_modules/@scope/b -> 提取 @scope/b。
    """
    names = set()
    if 'packages' not in lockfile_data:
        return names
    for pkg_path, pkg_info in lockfile_data['packages'].items():
        if not pkg_path or not isinstance(pkg_info, dict) or 'resolved' not in pkg_info:
            continue
        # 取最后一段 node_modules/ 之后的部分作为包名
        # node_modules/a/node_modules/@scope/b -> @scope/b
        parts = pkg_path.split('node_modules/')
        name = parts[-1].strip('/') if parts else ''
        if name:
            names.add(name)
    return names


def _parse_version_tuple(version_str):
    """将 4.6.7 或 4.6.7-beta.1 解析为 (4, 6, 7) 用于比较，预发布只取数字部分。"""
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)', str(version_str).strip())
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)


def _version_satisfies_range(version_str, range_str):
    """判断 version 是否满足 npm semver range。
    支持: *, x, ^, ~, >=, >, <=, <, =, ||, 省略 minor/patch 简写(^4, >=2, 1.x)。
    """
    v = _parse_version_tuple(version_str)
    range_str = (range_str or '').strip()

    # 通配符
    if not range_str or range_str in ('*', 'x', 'X', 'latest'):
        return True

    # || 分隔：任一满足
    if '||' in range_str:
        return any(_version_satisfies_range(version_str, p.strip())
                   for p in range_str.split('||'))

    # 匹配一个条件: 可选前缀 + 主版本号[.次版本号[.补丁号]]
    m = re.match(
        r'^(\^|~|>=|>|<=|<|=)?\s*(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?',
        range_str,
    )
    if not m:
        return False

    prefix = m.group(1) or ''
    major = int(m.group(2))
    minor_raw, patch_raw = m.group(3), m.group(4)
    has_minor = minor_raw is not None and minor_raw not in ('x', 'X', '*')
    has_patch = patch_raw is not None and patch_raw not in ('x', 'X', '*')
    minor = int(minor_raw) if has_minor else 0
    patch = int(patch_raw) if has_patch else 0
    b = (major, minor, patch)

    def _ok():
        if prefix == '^':
            # ^major: >= major.0.0 < (major+1).0.0   (major > 0)
            # ^0.minor: >= 0.minor.0 < 0.(minor+1).0 (minor > 0)
            # ^0.0.patch: 精确匹配
            if major > 0:
                return v >= b and v[0] == major
            if has_minor and minor > 0:
                return v >= b and v[0] == 0 and v[1] == minor
            if has_patch:
                return v == b
            return v[0] == major
        if prefix == '~':
            # ~major.minor[.patch]: >= b < major.(minor+1).0
            # ~major: >= major.0.0 < (major+1).0.0
            if has_minor:
                return v >= b and v[0] == major and v[1] == minor
            return v >= b and v[0] == major
        if prefix == '>=':
            return v >= b
        if prefix == '>':
            return v > b
        if prefix == '<=':
            return v <= b
        if prefix == '<':
            return v < b
        if prefix == '=':
            return v == b
        # 无前缀
        if not has_minor:
            return v[0] == major          # "2" → 任意 2.x.x
        if not has_patch:
            return v[0] == major and v[1] == minor  # "1.2" → 任意 1.2.x
        return v == b                     # 精确匹配

    ok = _ok()
    # 空格分隔的后续条件（如 ">=1 <3"）：全部满足
    rest = range_str[m.end():].strip()
    if rest and ok:
        return _version_satisfies_range(version_str, rest)
    return ok


def _pick_best_version(versions_dict, range_str):
    """从 versions 的 key 中选一个满足 range 的最高版本。"""
    candidates = []
    for ver in versions_dict:
        if _version_satisfies_range(ver, range_str):
            candidates.append((_parse_version_tuple(ver), ver))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ===== 从 lock 中收集“未带 resolved”的依赖（仅 npm lock v2/v3） =====
def collect_missing_peer_optional_from_lock(lockfile_data, existing_urls):
    """
    从 package-lock.json 的 packages 里收集所有 peerDependencies、optionalDependencies、dependencies
    中出现的依赖；若该依赖在 lock 中没有自己的 resolved 条目（或未出现在 existing_urls 中），
    则视为缺失，返回 [(name, range), ...]。这样即使 lock 因 npm 版本/安装方式未生成某包的
    resolved，只要某包声明了该依赖，也会被补下（如 @types/event-emitter）。
    existing_urls: 当前已收集到的 tarball URL 列表，用于解析出已存在的包名，避免重复。
    """
    if 'packages' not in lockfile_data:
        return []
    resolved_names = _npm_lock_resolved_names(lockfile_data)
    for url in (existing_urls or []):
        try:
            name, _ = extract_package_info(url)
            if name:
                resolved_names.add(name)
        except Exception:
            pass
    missing = []
    seen = set()
    # 同时检查 dependencies，避免因 lock 未包含某包 resolved 而漏下（如部分 npm/install 场景）
    for pkg_path, pkg_info in lockfile_data['packages'].items():
        if not isinstance(pkg_info, dict):
            continue
        for key in ('peerDependencies', 'optionalDependencies', 'dependencies'):
            deps = pkg_info.get(key)
            if not isinstance(deps, dict):
                continue
            for dep_name, range_spec in deps.items():
                dep_name = (dep_name or '').strip()
                if not dep_name or dep_name in resolved_names:
                    continue
                # lock 里 dependencies 的值可能是版本或 range 字符串
                spec_str = (range_spec if isinstance(range_spec, str) else str(range_spec or 'latest')).strip()
                spec = (dep_name, spec_str)
                if spec in seen:
                    continue
                seen.add(spec)
                missing.append(spec)
    return missing


# ===== 通过 registry 将 (name, range) 解析为 tarball URL =====
async def resolve_spec_to_tarball_url(session, name, range_spec, registry):
    """请求 registry 包元数据，解析 range 得到具体版本，返回 tarball URL；失败返回 None。"""
    registry = registry.rstrip('/')
    try:
        if name.startswith('@'):
            scope, pkg_name = name.split('/', 1)
            pkg_url = f"{registry}/{scope}%2F{pkg_name}"
        else:
            pkg_url = f"{registry}/{name}"
        async with session.get(pkg_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    versions = data.get('versions') or {}
    if not versions:
        return None
    range_str = (range_spec or '').strip()
    if not range_str:
        # 无版本约束时，不下载（避免拉取不兼容的 latest 版本）
        return None
    version = _pick_best_version(versions, range_str)
    if not version:
        # range 无法匹配任何已发布版本，放弃而非 fallback 到 latest（防止版本冲突）
        return None
    if version not in versions:
        return None
    dist = versions[version].get('dist') or {}
    tarball = dist.get('tarball')
    if not tarball or not tarball.startswith('http'):
        return None
    return replace_registry(tarball)


# ===== 提取依赖函数 =====
def extract_npm_urls(lockfile_data):
    urls = set()

    def recurse_deps(deps, is_npm7=False):
        if not isinstance(deps, dict):
            return
            
        for name, info in deps.items():
            if isinstance(info, dict):
                # 处理npm7+格式 (package-lock.json v2+)
                if is_npm7 and 'resolved' in info:
                    resolved_url = info['resolved']
                    if resolved_url.startswith('http'):
                        add_url_to_download(urls, replace_registry(resolved_url))
                
                # 处理常规依赖
                if 'resolved' in info and info['resolved'].startswith('http'):
                    add_url_to_download(urls, replace_registry(info['resolved']))
                    
                # 处理子依赖
                if 'dependencies' in info:
                    recurse_deps(info['dependencies'], is_npm7)
                    
                # 处理require节点
                if 'requires' in info and not is_npm7:
                    # npm <= 6 有时会将依赖放在requires节点
                    for req_name, req_version in info['requires'].items():
                        # 尝试在父节点找resolved URL
                        for parent_name, parent_info in deps.items():
                            if isinstance(parent_info, dict) and parent_name == req_name and 'resolved' in parent_info:
                                add_url_to_download(urls, replace_registry(parent_info['resolved']))

    # 处理 package-lock.json v2 (npm 7+) 格式
    if 'packages' in lockfile_data:
        is_npm7 = True
        for pkg_path, pkg_info in lockfile_data['packages'].items():
            if pkg_path == '':  # 跳过根包
                continue
                
            if isinstance(pkg_info, dict) and 'resolved' in pkg_info:
                resolved_url = pkg_info['resolved']
                if resolved_url.startswith('http'):
                    add_url_to_download(urls, replace_registry(resolved_url))

    # 处理传统 package-lock.json 格式
    if 'dependencies' in lockfile_data:
        recurse_deps(lockfile_data['dependencies'], 'packages' in lockfile_data)
        
    if 'devDependencies' in lockfile_data:
        recurse_deps(lockfile_data['devDependencies'], 'packages' in lockfile_data)
    if 'optionalDependencies' in lockfile_data:
        recurse_deps(lockfile_data['optionalDependencies'], 'packages' in lockfile_data)
        
    return list(urls)

def extract_pnpm_urls(lockfile_data):
    urls = set()
    workspace_packages = set()
    
    # 尝试读取 pnpm-workspace.yaml 以获取工作区信息
    try:
        if os.path.exists('pnpm-workspace.yaml'):
            with open('pnpm-workspace.yaml', encoding='utf-8') as f:
                workspace_data = yaml.safe_load(f)
                if workspace_data and 'packages' in workspace_data:
                    for pattern in workspace_data['packages']:
                        # 记录可能的工作区前缀
                        if pattern.endswith('/*'):
                            workspace_packages.add(pattern[:-2])
            print(f"{emoji.get('✅')} 已识别工作区目录: {', '.join(workspace_packages)}")
    except Exception as e:
        print(f"{emoji.get('⚠️')} 读取 pnpm-workspace.yaml 失败: {str(e)}")
    
    # 判断是否为工作区包
    def is_workspace_package(pkg_name, version_info):
        # 直接检查版本是否为 workspace: 或 link: 开头
        if isinstance(version_info, str) and (version_info.startswith('workspace:') or version_info.startswith('link:')):
            return True
            
        # 检查复杂对象的 specifier 和 version 字段
        if isinstance(version_info, dict):
            specifier = version_info.get('specifier', '')
            version = version_info.get('version', '')
            
            if (isinstance(specifier, str) and specifier.startswith('workspace:')) or \
               (isinstance(version, str) and version.startswith('link:')):
                return True
                
        return False
    
    # 处理包直接URL或构造URL
    def add_package_url(pkg_name, version, resolved=None):
        # 已经有明确的resolved URL
        if resolved and resolved.startswith('http'):
            add_url_to_download(urls, replace_registry(resolved))
            return True
            
        # 跳过workspace packages和本地链接
        if isinstance(version, str):
            # 检查明确的workspace或link前缀
            if version.startswith('link:') or version.startswith('workspace:'):
                print(f"{emoji.get('⚠️')} 跳过工作区包：{pkg_name}@{version}")
                return False
                
            # 检查包是否在工作区路径中
            for workspace in workspace_packages:
                if version.startswith(f"link:{workspace}/"):
                    print(f"{emoji.get('⚠️')} 跳过工作区包：{pkg_name}@{version}")
                    return False
            
        # 处理scoped包名 (@scope/package)
        if pkg_name.startswith('@'):
            try:
                scope, name = pkg_name.split('/', 1)
                # 构建完整URL，保留作用域
                url = f"{CUSTOM_REGISTRY}/{pkg_name}/-/{name}-{version}.tgz"
            except Exception as e:
                print(f"{emoji.get('⚠️')} 处理作用域包出错: {pkg_name}@{version}, 错误: {str(e)}")
                # 降级处理
                url = f"{CUSTOM_REGISTRY}/{pkg_name}/-/{pkg_name.split('/')[-1]}-{version}.tgz"
        else:
            url = f"{CUSTOM_REGISTRY}/{pkg_name}/-/{pkg_name}-{version}.tgz"
            
        add_url_to_download(urls, url)
        return True

    # 递归处理packages部分
    def process_packages():
        if 'packages' not in lockfile_data:
            return
            
        for path, pkg_info in lockfile_data['packages'].items():
            # 忽略根包
            if path == '':
                continue
                
            # 提取包名和版本号
            if path.startswith('node_modules/'):
                pkg_name = path.replace('node_modules/', '')
            else:
                pkg_name = path
                
            # 跳过工作区包
            if pkg_info and is_workspace_package(pkg_name, pkg_info):
                print(f"{emoji.get('⚠️')} 跳过工作区包：{pkg_name}@{str(pkg_info).split(',')[0] if isinstance(pkg_info, dict) else pkg_info}")
                continue
                
            # 处理已解析的URL
            if pkg_info and isinstance(pkg_info, dict):
                version = pkg_info.get('version')
                resolved = pkg_info.get('resolved')
                
                # 如果有明确的resolved字段，直接使用它
                if resolved:
                    add_package_url(pkg_name, version, resolved)
                elif version:
                    # 清理版本号中的括号内容
                    if isinstance(version, str):
                        # 跳过工作区链接
                        if version.startswith('link:') or version.startswith('workspace:'):
                            print(f"{emoji.get('⚠️')} 跳过工作区包：{pkg_name}@{version}")
                            continue
                            
                        version_match = re.match(r'^([^()]+)', version)
                        if version_match:
                            version = version_match.group(1).strip()
                    add_package_url(pkg_name, version)
    
    # 递归处理依赖部分
    def process_dependencies(deps_dict):
        if not deps_dict or not isinstance(deps_dict, dict):
            return
            
        for pkg_name, info in deps_dict.items():
            # 跳过工作区包
            if is_workspace_package(pkg_name, info):
                print(f"{emoji.get('⚠️')} 跳过工作区包：{pkg_name}@{str(info).split(',')[0] if isinstance(info, dict) else info}")
                continue
                
            if isinstance(info, dict):
                version = info.get('version')
                resolved = info.get('resolved')
                
                if version or resolved:
                    add_package_url(pkg_name, version, resolved)
                
                # 递归处理子依赖
                if 'dependencies' in info:
                    process_dependencies(info['dependencies'])
                    
            elif isinstance(info, str):
                # 跳过工作区包
                if info.startswith('workspace:') or info.startswith('link:'):
                    print(f"{emoji.get('⚠️')} 跳过工作区包：{pkg_name}@{info}")
                    continue
                # 简单的版本字符串
                version_match = re.match(r'^([^()]+)', info)
                version = version_match.group(1).strip() if version_match else info
                add_package_url(pkg_name, version)

    # 处理主要依赖部分
    if 'importers' in lockfile_data:
        for path, importer in lockfile_data['importers'].items():
            if isinstance(importer, dict):
                # 处理正常依赖
                if 'dependencies' in importer:
                    process_dependencies(importer['dependencies'])
                    
                # 处理开发依赖
                if 'devDependencies' in importer:
                    process_dependencies(importer['devDependencies'])
                    
                if 'optionalDependencies' in importer:
                    process_dependencies(importer['optionalDependencies'])
    
    # 处理顶级dependencies
    if 'dependencies' in lockfile_data:
        process_dependencies(lockfile_data['dependencies'])
        
    if 'devDependencies' in lockfile_data:
        process_dependencies(lockfile_data['devDependencies'])
    if 'optionalDependencies' in lockfile_data:
        process_dependencies(lockfile_data['optionalDependencies'])
        
    # 处理packages部分
    process_packages()

    return list(urls)

def extract_yarn_urls(lockfile_data):
    urls = set()
    
    # 匹配yarn.lock中的URL
    resolved_pattern = re.compile(r'"resolved"\s+"(https?://[^"]+)"')
    registry_pattern = re.compile(r'"registry"\s+"(https?://[^"]+)"')
    version_pattern = re.compile(r'"version"\s+"([^"]+)"') 
    name_pattern = re.compile(r'^([@a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)?)')
    
    lines = lockfile_data.splitlines()
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        
        # 检查是否为包声明行
        if line and line.endswith(':'):
            pkg_match = name_pattern.search(line)
            if pkg_match:
                pkg_name = pkg_match.group(1).strip('"\'')
                
                # 查找此包的version和resolved
                resolved_url = None
                version = None
                j = i + 1
                
                # 扫描到下一个包声明前或文件结束
                while j < len(lines) and not (lines[j].strip() and lines[j].strip().endswith(':')):
                    resolved_match = resolved_pattern.search(lines[j])
                    if resolved_match:
                        resolved_url = resolved_match.group(1)
                        if resolved_url.startswith('http'):
                            add_url_to_download(urls, replace_registry(resolved_url))
                            
                    # 如果没有resolved但有version和registry，尝试构造URL
                    if not resolved_url:
                        version_match = version_pattern.search(lines[j])
                        if version_match:
                            version = version_match.group(1)
                            
                        registry_match = registry_pattern.search(lines[j])
                        if registry_match and version:
                            registry = registry_match.group(1).rstrip('/')
                            # 构造可能的URL，正确处理作用域包
                            if pkg_name.startswith('@'):
                                scope, name = pkg_name.split('/', 1)
                                potential_url = f"{registry}/{pkg_name}/-/{name}-{version}.tgz"
                            else:
                                potential_url = f"{registry}/{pkg_name}/-/{pkg_name}-{version}.tgz"
                            add_url_to_download(urls, replace_registry(potential_url))
                            
                    j += 1
                
                # 如果版本号存在但没有resolved URL或registry，尝试使用默认registry
                if version and not resolved_url:
                    if pkg_name.startswith('@'):
                        scope, name = pkg_name.split('/', 1)
                        add_url_to_download(urls, f"{CUSTOM_REGISTRY}/{pkg_name}/-/{name}-{version}.tgz")
                    else:
                        add_url_to_download(urls, f"{CUSTOM_REGISTRY}/{pkg_name}/-/{pkg_name}-{version}.tgz")
                
                i = j - 1  # 调整主循环索引
        
        i += 1
        
    return list(urls)

# ===== 提取包名和版本号 =====
def extract_package_info(url):
    """从URL中提取包名和版本号"""
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        
        # 尝试提取包名
        if '/-/' in path:
            # 格式: /pkg/-/pkg-1.0.0.tgz 或 /@scope/pkg/-/pkg-1.0.0.tgz
            parts = path.split('/-/')
            pkg_part = parts[0].strip('/')
            
            # 提取版本号
            file_name = os.path.basename(path)
            if pkg_part.startswith('@'):
                # 作用域包 @scope/pkg
                scope, name = pkg_part.split('/', 1)
                version_match = re.search(f'{name}-([0-9]+\\.[0-9]+\\.[0-9]+[^)]*?)(\\.tgz|$)', file_name)
            else:
                # 普通包
                name = pkg_part
                version_match = re.search(f'{name}-([0-9]+\\.[0-9]+\\.[0-9]+[^)]*?)(\\.tgz|$)', file_name)
                
            if version_match:
                version = version_match.group(1)
                return pkg_part, version
        
        # 备用方法：直接从文件名猜测
        file_name = os.path.basename(path)
        name_version_match = re.match(r'(.+?)-([0-9]+\.[0-9]+\.[0-9]+[^)]*?)(\.tgz|$)', file_name)
        if name_version_match:
            name = name_version_match.group(1)
            version = name_version_match.group(2)
            
            # 检查是否为作用域包
            if '/-/' in path and '@' in path:
                parts = path.split('/-/')
                if parts and parts[0].strip('/'):
                    return parts[0].strip('/'), version
            
            return name, version

    except Exception:
        pass

    # 无法提取时返回文件名
    return os.path.basename(unquote(url)), "未知版本"

# ===== 下载函数 =====
async def download_file(session, url, semaphore):
    """使用信号量限制并发下载数量"""
    # 确保URL格式正确
    url = clean_package_url(url)
    mirror_url = replace_registry(url)
    official_url = url.replace(CUSTOM_REGISTRY, "https://registry.npmjs.org")

    async with semaphore:  # 使用信号量控制并发
        for attempt in range(MAX_RETRIES):
            try:
                current_url = mirror_url if attempt < MAX_RETRIES - 1 else official_url
                parsed = urlparse(current_url)
                file_name = os.path.basename(unquote(parsed.path))
                # 确保文件名安全，直接保存到目标文件夹
                safe_file_name = sanitize_path(file_name)
                file_path = os.path.join(PACKAGES_PATH, safe_file_name)

                # 确保目标目录存在
                os.makedirs(PACKAGES_PATH, exist_ok=True)

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0 Safari/537.36"
                }

                async with session.get(current_url, timeout=TIMEOUT, headers=headers) as response:
                    response.raise_for_status()
                    with open(file_path, 'wb') as f:
                        while True:
                            chunk = await response.content.read(8192)  # 增大读取块大小
                            if not chunk:
                                break
                            f.write(chunk)

                return None
            except aiohttp.ClientResponseError as e:
                if e.status == 404 and mirror_url != official_url:
                    # 如果是404错误，立即尝试官方源
                    print(f"{emoji.get('⚠️')} {mirror_url} 未找到, 尝试官方源 {official_url}")
                    mirror_url = official_url
                    continue
                elif attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                else:
                    print(f"{emoji.get('❌')} 下载失败 ({e.status}): {current_url}")
                    return (url, official_url, f"HTTP {e.status}")
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    print(f"{emoji.get('🔁')} 第 {attempt+1} 次失败，正在重试：{current_url}，错误: {str(e)[:100]}")
                    await asyncio.sleep(1)
                else:
                    print(f"{emoji.get('❌')} 下载失败：{current_url} → 可尝试手动下载：{official_url}")
                    print(f"   错误信息: {str(e)[:100]}")
                    return (url, official_url, str(e)[:500])

# ===== 主程序 =====
async def main():
    # 立即输出，避免用户误以为卡住（配合 line_buffering 或 flush）
    print("NPM 依赖下载工具 正在启动...", flush=True)

    print(f"""
{emoji.get("📦")} NPM 依赖下载工具
============================
{emoji.get("✅")} 包保存目录: {PACKAGES_PATH}
{emoji.get("✅")} 并发下载数: {CONCURRENT_LIMIT}
{emoji.get("✅")} 下载超时秒: {TIMEOUT}
{emoji.get("✅")} 镜像源地址: {CUSTOM_REGISTRY}
============================
    """, flush=True)

    extract_func = None
    data = None

    # 确保下载目录存在
    os.makedirs(PACKAGES_PATH, exist_ok=True)
    
    print(f"{emoji.get('🔍')} 检测锁文件...", flush=True)

    if os.path.exists('package-lock.json'):
        print(f"{emoji.get('✅')} 检测到 npm 锁文件 (package-lock.json)", flush=True)
        print("正在读取 package-lock.json（文件较大时可能需要几秒）...", flush=True)
        try:
            with open("package-lock.json", encoding='utf-8') as f:
                data = json.load(f)
            extract_func = extract_npm_urls
        except json.JSONDecodeError:
            print(f"{emoji.get('❌')} package-lock.json 格式错误!")
            return
    elif os.path.exists('pnpm-lock.yaml'):
        print(f"{emoji.get('✅')} 检测到 pnpm 锁文件 (pnpm-lock.yaml)")
        try:
            with open("pnpm-lock.yaml", encoding='utf-8') as f:
                data = yaml.safe_load(f)
            extract_func = extract_pnpm_urls
        except yaml.YAMLError:
            print(f"{emoji.get('❌')} pnpm-lock.yaml 格式错误!")
            return
    elif os.path.exists('yarn.lock'):
        print(f"{emoji.get('✅')} 检测到 yarn 锁文件 (yarn.lock)")
        try:
            with open("yarn.lock", encoding='utf-8') as f:
                data = f.read()
            extract_func = extract_yarn_urls
        except Exception as e:
            print(f"{emoji.get('❌')} yarn.lock 读取错误: {str(e)}")
            return
    else:
        print(f"{emoji.get('❌')} 错误: 未找到 package-lock.json, pnpm-lock.yaml 或 yarn.lock!")
        return

    print(f"{emoji.get('📦')} 解析依赖...")
    urls = list(extract_func(data))
    
    # 从 package-lock 中补下未带 resolved 的 peer/optional 依赖（仅 npm lock）
    missing_specs = []
    if extract_func is extract_npm_urls and isinstance(data, dict) and 'packages' in data:
        missing_specs = collect_missing_peer_optional_from_lock(data, urls)
        if missing_specs:
            print(f"{emoji.get('🔍')} 发现 lock 中未带 resolved 的 peer/optional 依赖 {len(missing_specs)} 个，将向 registry 解析并加入下载列表")
    
    # 移除重复URL并排序以提高稳定性（补下 peer-from-lock 的 URL 在下面 session 内合并）
    unique_urls = sorted(set(urls))
    total_count = len(unique_urls)
    
    if total_count == 0:
        print(f"{emoji.get('⚠️')} 没有找到任何依赖项。可能是锁文件格式不支持或没有依赖记录。")
        return

    # 创建信号量以限制并发下载数量
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    conn = aiohttp.TCPConnector(limit=CONCURRENT_LIMIT)
    
    print(f"{emoji.get('🔍')} 准备下载 {total_count} 个包...")
    print(f"{emoji.get('⏱️')} 开始下载...")
    
    start_time = asyncio.get_event_loop().time()
    failed_downloads = []
    
    async with aiohttp.ClientSession(timeout=timeout, connector=conn) as session:
        # 若有 lock 中缺失的 peer/optional，先向 registry 解析为 tarball URL 并合并
        if missing_specs:
            resolved_peer_urls = []
            for name, range_spec in missing_specs:
                u = await resolve_spec_to_tarball_url(session, name, range_spec, CUSTOM_REGISTRY)
                if u:
                    resolved_peer_urls.append(u)
                    print(f"{emoji.get('✅')} 解析 peer/optional: {name}@{range_spec} -> {u.split('/')[-1]}")
                else:
                    print(f"{emoji.get('⚠️')} 无法解析: {name}@{range_spec}")
            if resolved_peer_urls:
                unique_urls = sorted(set(unique_urls) | set(resolved_peer_urls))
                total_count = len(unique_urls)
        
        tasks = [download_file(session, url, semaphore) for url in unique_urls]
        
        # 分批处理任务并显示进度
        completed = 0
        for i, batch in enumerate(range(0, len(tasks), 10)):
            batch_tasks = tasks[batch:batch+10]
            batch_results = await asyncio.gather(*batch_tasks)
            
            # 更新进度
            completed += len(batch_tasks)
            progress = (completed / total_count) * 100
            print(f"进度: {completed}/{total_count} ({progress:.1f}%)")
            
            # 收集失败的下载 (url, official_url, error_info)
            for result in batch_results:
                if result is not None:
                    failed_downloads.append(result)
    
    end_time = asyncio.get_event_loop().time()
    duration = end_time - start_time
    
    success_count = total_count - len(failed_downloads)
    percent = (success_count / total_count) * 100

    print(f"\n{emoji.get('📊')} 下载结果报告：")
    print(f"总共下载：{total_count} 个包")
    print(f"成功：{success_count} 个包 ({percent:.1f}%)")
    print(f"耗时：{duration:.1f} 秒")

    # 仅将 404/异常/错误写入日志，正常下载不写入；每次运行覆盖
    log_dir = os.path.dirname(DOWNLOAD_LOG)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    with open(DOWNLOAD_LOG, "w", encoding="utf-8") as log_file:
        if failed_downloads:
            log_file.write(f"# 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_file.write(f"# 失败数: {len(failed_downloads)}\n\n")
            formatted_failures = []
            for item in failed_downloads:
                url, official_url = item[0], item[1]
                error_info = item[2] if len(item) >= 3 else "未知错误"
                pkg_name, version = extract_package_info(url)
                formatted_failures.append((pkg_name, version, official_url, error_info))
            for pkg_name, version, url, error_info in sorted(formatted_failures, key=lambda x: x[0].lower()):
                log_file.write(f"### {pkg_name}@{version}\n")
                log_file.write(f"错误: {error_info}\n")
                log_file.write(f"下载链接: {url}\n")
                log_file.write(f"命令行: curl -L \"{url}\" -o \"{os.path.basename(url)}\"\n\n")
        # 无失败时不写入任何内容，文件为空

    if not failed_downloads:
        print(f"{emoji.get('🎉')} 全部下载成功！")
    else:
        print(f"{emoji.get('✅')} 成功下载: {success_count} 个包")
        print(f"{emoji.get('❌')} 下载失败: {len(failed_downloads)} 个包")
        print(f"\n{emoji.get('🚫')} 失败的包列表（请尝试手动下载）：")
        for i, item in enumerate(failed_downloads, start=1):
            url, official_url = item[0], item[1]
            error_info = item[2] if len(item) >= 3 else ""
            pkg_name, version = extract_package_info(url)
            print(f"{i}. {pkg_name}@{version}  {error_info}")
            print(f"   → 下载链接: {official_url}")
        print(f"\n{emoji.get('📝')} 404/错误日志已写入：{DOWNLOAD_LOG}")

if __name__ == '__main__':
    try:
        if config.IS_WIN:
            try:
                os.system("title NPM包下载工具")
            except Exception:
                pass
        print("\n" + "=" * 40)
        print("     NPM 依赖包下载工具")
        print("=" * 40 + "\n")
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{emoji.get('⚠️')} 用户中断，程序退出")
    except Exception as e:
        print(f"\n{emoji.get('❌')} 程序出错: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        if getattr(sys, 'frozen', False):
            try:
                print("\n按任意键退出...")
                input()
            except Exception:
                try:
                    os.system("pause")
                except Exception:
                    pass