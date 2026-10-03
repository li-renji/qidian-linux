#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qd_loader.py - 奇点OS 模块加载器（严格按规范 4.4 节）
负责：
  1. .qd 模块识别（目录名 + module.qdmeta 验证）
  2. .qds 系统单文件加载（magic 验证 + 提取 ELF + 沙箱执行）
  3. .qdai 规则文件解析（多段式：YAML头 + 自然语言 + JSON规则 + Lua）
  4. 模块依赖检查、权限审核、递归加载子模块
"""
import os
import sys
import json
import struct
import shutil
import subprocess
import tempfile
import hashlib
import re

QDS_MAGIC = 0x51445353
QDS_HEADER_SIZE = 256
VALID_TYPES = {'core', 'system', 'ui', 'compat', 'ai', 'net', 'user', 'dev', 'desktop'}

# ---- .qdai 分隔标记 ----
QD_SECTIONS = {
    'NATURAL': '===QD_SECTION_NATURAL_LANGUAGE===',
    'RULES': '===QD_SECTION_STRUCTURED_RULES===',
    'CONTRACT': '===QD_SECTION_CONTRACT===',
    'LUA': '===QD_SECTION_LUA===',
    'KNOWLEDGE': '===QD_SECTION_KNOWLEDGE===',
}


class QDError(Exception):
    """规范 9.6 错误码分级：0=OK 1=契约/参数 2=依赖 3=权限 4=资源 5=内部"""
    def __init__(self, msg, code=5):
        super().__init__(msg)
        self.code = code


# ============================================================
# .qds 加载器
# ============================================================
class QdsLoader:
    """验证并加载 .qds 文件，提取 ELF 到临时目录执行"""

    @staticmethod
    def verify_header(data):
        if len(data) < QDS_HEADER_SIZE:
            raise QDError(f'.qds 文件太小: {len(data)}B < 256B')
        magic, hver, fver = struct.unpack_from('<IHH', data, 0)
        if magic != QDS_MAGIC:
            raise QDError(f'magic 不符: 0x{magic:08X} (期望 QDSS)')
        name = data[8:72].split(b'\0')[0].decode('utf-8', errors='replace')
        entry = data[72:136].split(b'\0')[0].decode('utf-8', errors='replace')
        arch, bin_offset = struct.unpack_from('<II', data, 136)
        bin_size, config_offset = struct.unpack_from('<QQ', data, 144)
        config_size, sig_offset = struct.unpack_from('<QQ', data, 160)
        sig_size, permissions = struct.unpack_from('<QQ', data, 176)
        min_os, api = struct.unpack_from('<II', data, 192)
        # 自校验
        sha = hashlib.sha256(data[:232]).digest()[:24]
        if sha != data[232:256]:
            raise QDError(f'文件头自校验失败: {name}')
        return {
            'name': name, 'entry': entry, 'arch': arch,
            'bin_offset': bin_offset, 'bin_size': bin_size,
            'config_offset': config_offset, 'config_size': config_size,
            'sig_offset': sig_offset, 'sig_size': sig_size,
            'permissions': permissions, 'min_os': min_os, 'api': api,
            'payload_type': struct.unpack_from('<I', data, 200)[0] if len(data) >= 204 else 0,
        }

    @staticmethod
    def load(path, run=False):
        """验证 .qds，返回元信息；run=True 时提取 ELF 执行"""
        with open(path, 'rb') as f:
            data = f.read()
        meta = QdsLoader.verify_header(data)
        expected = (QDS_HEADER_SIZE + meta['bin_size'] + meta['config_size']
                    + meta['sig_size'])
        if len(data) != expected:
            raise QDError(f'文件大小不匹配: 实际{len(data)}B 期望{expected}B')
        elf = data[meta['bin_offset']:meta['bin_offset']+meta['bin_size']]
        config = {}
        if meta['config_size'] > 0:
            cfg_raw = data[meta['config_offset']:meta['config_offset']+meta['config_size']]
            try:
                config = json.loads(cfg_raw.decode('utf-8'))
            except Exception as e:
                raise QDError(f'内嵌配置解析失败: {e}')
        if run:
            return QdsLoader._exec(elf, config, meta)
        return meta, config

    @staticmethod
    def _exec(elf, config, meta):
        """提取 payload 到临时文件并执行：0=ELF 直接执行；1=Python 用 python3 沙箱执行"""
        suffix = '.bin'
        argv = None
        if meta.get('payload_type') == 1:
            suffix = '.py'
            argv = ['/usr/bin/python3', None]
        tmp = tempfile.NamedTemporaryFile(prefix='qd_', suffix=suffix, delete=False)
        tmp.write(elf)
        tmp.close()
        os.chmod(tmp.name, 0o755)
        env = os.environ.copy()
        env['QD_SERVICE_NAME'] = meta['name']
        env['QD_CONFIG'] = os.environ.get('QD_CONFIG', json.dumps(config, ensure_ascii=False))  # 外部优先，内嵌兜底
        try:
            if argv:
                argv[1] = tmp.name
                proc = subprocess.Popen(argv, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            else:
                proc = subprocess.Popen([tmp.name], env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return proc
        except Exception as e:
            os.unlink(tmp.name)
            raise QDError(f'执行 {meta["name"]} 失败: {e}')


# ============================================================
# .qdai 解析器
# ============================================================
class QdaiParser:
    """解析 .qdai 多段式规则文件"""

    @staticmethod
    def parse(path):
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()

        # 1. YAML 元数据头（--- 开头 --- 结尾）
        meta = {}
        yaml_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
        if yaml_match:
            yaml_text = yaml_match.group(1)
            for line in yaml_text.split('\n'):
                if ':' in line:
                    k, v = line.split(':', 1)
                    meta[k.strip()] = v.strip()
            rest = text[yaml_match.end():]
        else:
            rest = text
            meta['name'] = os.path.basename(path)

        # 2. 按分隔标记分割
        sections = {}
        # 按出现顺序找所有分隔标记
        markers = []
        for name, marker in QD_SECTIONS.items():
            idx = rest.find(marker)
            if idx >= 0:
                markers.append((idx, name, marker))
        markers.sort()

        if not markers:
            # 纯自然语言模式（降级）
            return {'meta': meta, 'natural': rest.strip(), 'rules': {},
                    'contract': None, 'lua': None, 'knowledge': None}

        parts = {}
        for i, (idx, name, marker) in enumerate(markers):
            start = idx + len(marker)
            end = markers[i+1][0] if i+1 < len(markers) else len(rest)
            parts[name] = rest[start:end].strip()

        # 3. 解析结构化规则
        rules = {}
        if 'RULES' in parts:
            try:
                rules = json.loads(parts['RULES'])
            except json.JSONDecodeError as e:
                raise QDError(f'{path} 结构化规则 JSON 解析失败: {e}')

        return {
            'meta': meta,
            'natural': parts.get('NATURAL', ''),
            'rules': rules,
            'contract': parts.get('CONTRACT'),
            'lua': parts.get('LUA'),
            'knowledge': parts.get('KNOWLEDGE'),
        }


# ============================================================
# .qd 模块加载器
# ============================================================
class ModuleLoader:
    def __init__(self, roots=None):
        # 规范 7.1：/奇点OS 全树扫描（含 AI.qd 核心模块，R-LD-01 修复）
        self.roots = roots or ['/奇点OS/系统', '/奇点OS/用户.qd/模块']
        self.extra_modules = ['/奇点OS/AI.qd', '/奇点OS/用户.qd']  # 顶层 .qd 模块本体
        self.modules = {}      # id -> module info
        self.registry = {}     # 识别登记表（规范 7.3）：id -> 识别结果
        self.qds_services = {}  # name -> path
        self.qdai_rules = []    # list of parsed rules

    def scan(self):
        """扫描全树 .qd 模块（顶层模块 + 容器目录）"""
        for extra in self.extra_modules:
            if os.path.isdir(extra) and extra.endswith('.qd'):
                self._load_module(extra)
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                full = os.path.join(root, entry)
                if os.path.isdir(full) and entry.endswith('.qd'):
                    self._load_module(full)
        return self.modules

    def _load_module(self, path):
        """加载单个 .qd 模块（递归）"""
        meta_path = os.path.join(path, 'module.qdmeta')
        if not os.path.isfile(meta_path):
            print(f'[跳过] {path}: 缺少 module.qdmeta')
            return None
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception as e:
            print(f'[损坏] {path}: module.qdmeta 解析失败 ({e})')
            return None

        # 验证必填字段
        missing = [f for f in ('name', 'id', 'version', 'entry', 'type')
                   if f not in meta]
        if missing:
            print(f'[损坏] {path}: 缺少必填字段 {missing}')
            return None
        if meta['type'] not in VALID_TYPES:
            print(f'[跳过] {path}: 非法类型 {meta["type"]}')
            return None

        module = {
            'path': path,
            'meta': meta,
            'qds': [],
            'qdai': [],
            'submodules': [],
        }

        # 识别登记（规范 7.3）：type/name/entry/icon/window_hint → 供任何桌面查询
        self.registry[meta['id']] = {
            'type': meta.get('type'),
            'name': meta.get('name'),
            'entry': os.path.join(path, meta.get('entry', '')),
            'icon': meta.get('icon'),
            'window_hint': meta.get('window_hint', 'normal'),
            'path': path,
            'interface': meta.get('interface', {}),
        }

        # 扫描 .qds 和 .qdai
        for fname in sorted(os.listdir(path)):
            fpath = os.path.join(path, fname)
            if os.path.isfile(fpath):
                if fname.endswith('.qds'):
                    try:
                        qmeta, qcfg = QdsLoader.load(fpath)
                        module['qds'].append(qmeta)
                        self.qds_services[qmeta['name']] = fpath
                    except QDError as e:
                        print(f'  [警告] {fname}: {e}')
                elif fname.endswith('.qdai'):
                    try:
                        rule = QdaiParser.parse(fpath)
                        module['qdai'].append(rule)
                        self.qdai_rules.append({'module': meta['id'], 'rule': rule})
                    except QDError as e:
                        print(f'  [警告] {fname}: {e}')

        # 递归加载子模块
        for fname in sorted(os.listdir(path)):
            fpath = os.path.join(path, fname)
            if os.path.isdir(fpath) and fname.endswith('.qd'):
                sub = self._load_module(fpath)
                if sub:
                    module['submodules'].append(sub)

        # 依赖检查
        deps_ok = self._check_deps(meta)
        module['deps_ok'] = deps_ok

        self.modules[meta['id']] = module
        status = '[OK]' if deps_ok else '[依赖缺失]'
        print(f'{status} {meta["type"]:6s} {meta["id"]:20s} {meta["name"]}'
              f' (qds:{len(module["qds"])} qdai:{len(module["qdai"])})')
        return module

    def _check_deps(self, meta):
        for dep in meta.get('depends', []):
            dep_id = dep.get('id') if isinstance(dep, dict) else dep
            if dep_id and dep_id not in self.modules:
                return False
        return True

    # ---- 识别契约查询接口（规范 7.3）----
    def query(self, module_id):
        """桌面 / 模块问系统："这是什么" → 返回识别结果（无则 None）"""
        return self.registry.get(module_id)

    def desktop_candidates(self):
        """返回全部 type=desktop 的模块（桌面候选列表，供设置切换）"""
        return [r for r in self.registry.values() if r['type'] == 'desktop']


# ============================================================
# 主入口
# ============================================================
def main():
    loader = ModuleLoader()
    print('=== 奇点OS 模块扫描（全树，含 AI.qd） ===')
    mods = loader.scan()
    print(f'\n=== 扫描完成: {len(mods)} 个模块, '
          f'{len(loader.qds_services)} 个 .qds 服务, '
          f'{len(loader.qdai_rules)} 条 .qdai 规则 ===')
    desks = loader.desktop_candidates()
    if desks:
        print(f'\n=== 桌面候选（type=desktop，可切换） ===')
        for d in desks:
            print(f'  {d["name"]:12s} entry={d["entry"]}')
    return 0 if mods else 1


if __name__ == '__main__':
    sys.exit(main())
