#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qd_guard.py —— 模块调用隔离拦截器（隔离从“标记”升级为“拦截”）

机制：qdd 启动模块进程时用本包装器加载目标模块，先注入 sys.meta_path
钩子。任何 import 解析出的文件落在 /奇点OS/ 下、但不在本模块根目录内
（即跨模块伸手）→ 直接 ImportError 拦截。

豁免：AI.qd（QD_GUARD_ALLOW=ai）与白名单路径（QD_GUARD_WHITELIST）。
用法：QD_GUARD=1 python -m qd_guard <模块根目录> <模块entry>
"""
import os
import sys
import runpy

GUARD_ROOT = os.environ.get('QD_GUARD_ROOT', '/奇点OS')
ALLOW = os.environ.get('QD_GUARD_ALLOW', 'ai').split(',')   # 豁免模块id（AI.qd 核心豁免）
WHITELIST = os.environ.get('QD_GUARD_WHITELIST', '').split(',')
MOD_ROOT = None


class GuardFinder:
    """meta_path 钩子：拦截跨模块 import（白名单路径放行）"""
    def __init__(self, mod_root):
        self.mod_root = os.path.abspath(mod_root)

    def find_spec(self, name, path=None, target=None):
        # 只用默认 PathFinder 解析（不递归调用自己）
        spec = None
        import importlib.machinery
        for entry in (path if path else sys.path):
            spec = importlib.machinery.PathFinder.find_spec(name, [entry])
            if spec:
                break
        if spec and spec.origin and spec.origin not in ('built-in', 'frozen'):
            p = os.path.abspath(spec.origin)
            if p.startswith(os.path.abspath(GUARD_ROOT)):
                # 在本模块目录内 → 放行
                if p.startswith(self.mod_root):
                    return spec
                # 白名单路径 → 放行
                for wl in WHITELIST:
                    if wl and p.startswith(os.path.abspath(wl)):
                        return spec
                raise ImportError(
                    f'[qd-guard] 跨模块 import 被拦截: {name} ({p})'
                    f' —— 不在本模块 {self.mod_root} 内；模块间只有调用关系（走总线）')
        return spec


def main():
    if len(sys.argv) < 3:
        print('usage: qd_guard.py <模块根目录> <entry.py>', file=sys.stderr)
        sys.exit(2)
    mod_root, entry = sys.argv[1], sys.argv[2]
    # 豁免检查：模块根含豁免 id（如 ai）则不装拦截器
    mod_id = os.path.basename(mod_root)
    if any(a and a in mod_root for a in ALLOW):
        runpy.run_path(entry, run_name='__main__')
        return
    if os.environ.get('QD_GUARD', '1') == '1':
        sys.meta_path.insert(0, GuardFinder(mod_root))
    runpy.run_path(entry, run_name='__main__')


if __name__ == '__main__':
    main()
