#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qd-run.py —— QDSS 容器直接执行器（binfmt_misc 解释器）
内核识别 .qds 的 SSDQ magic 后，调用本脚本：
  1. 用 qd_loader 验证容器（magic/大小/自校验）
  2. 提取 payload 到临时文件
  3. exec 替换自身进程执行（python 用 python3，ELF 直接 exec）
用法（由 binfmt_misc 调用，也可手动）：
  qd-run.py <容器路径> [参数...]
"""
import os
import sys
import tempfile

sys.path.insert(0, '/奇点OS/运行')


def main():
    if len(sys.argv) < 2:
        print('用法: qd-run.py <容器.qds> [参数...]', file=sys.stderr)
        return 1

    path = sys.argv[1]
    args = sys.argv[2:]

    try:
        from qd_loader import QdsLoader
        meta, config = QdsLoader.load(path)
    except Exception as e:
        print(f'[qd-run] 容器加载失败 {path}: {e}', file=sys.stderr)
        return 5

    # 提取 payload
    with open(path, 'rb') as f:
        data = f.read()
    elf = data[meta['bin_offset']:meta['bin_offset'] + meta['bin_size']]

    suffix = '.py' if meta.get('payload_type') == 1 else '.bin'
    fd, tmp = tempfile.mkstemp(prefix='qd_run_', suffix=suffix)
    with os.fdopen(fd, 'wb') as f:
        f.write(elf)
    os.chmod(tmp, 0o755)

    env = os.environ.copy()
    env['QD_SERVICE_NAME'] = meta['name']
    env['QD_MODULE_DIR'] = os.path.dirname(os.path.abspath(path))
    import json
    env['QD_CONFIG'] = json.dumps({'args': args})

    try:
        if meta.get('payload_type') == 1:
            os.execvpe('/usr/bin/python3', ['python3', tmp] + args, env)
        else:
            os.execvpe(tmp, [tmp] + args, env)
    except Exception as e:
        print(f'[qd-run] 执行失败: {e}', file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 5


if __name__ == '__main__':
    sys.exit(main())
