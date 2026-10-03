#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qd_hotload.py - 奇点OS 模块热加载守护（规范 4.4 热加载节）
轮询监听 .qd/.qds/.qdai 文件变化：
  - .qdai 变化 → 通知 ai_service 重载规则（UNIX socket hot_reload_qdai）
  - .qds/.qd 变化 → 调用 qd_loader 重新扫描模块

用法:
  python3 qd_hotload.py daemon    # 守护模式
  python3 qd_hotload.py once      # 单次扫描（供测试）
"""
import os
import sys
import json
import time
import socket
import signal
import hashlib
import argparse
from datetime import datetime

QD_ROOT = '/奇点OS'
WATCH_ROOTS = [
    os.path.join(QD_ROOT, '系统'),
    os.path.join(QD_ROOT, 'AI.qd'),
    os.path.join(QD_ROOT, '用户.qd'),
]
IPC_SOCKET = '/run/qd/qd_ai.sock'
LOG_FILE = '/tmp/qd_hotload.log'
POLL_INTERVAL = 2.0
WATCH_EXTS = ('.qd', '.qds', '.qdai', '.qdmeta')

_log_fh = None


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        if _log_fh:
            _log_fh.write(line + '\n')
            _log_fh.flush()
    except Exception:
        pass


def collect_snapshots():
    """扫描所有监控根目录下的 .qd/.qds/.qdai/.qdmeta 文件：path -> (mtime, size)"""
    snap = {}
    for root in WATCH_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过 .git 与缓存
            dirnames[:] = [d for d in dirnames
                           if d not in ('.git', '__pycache__', '缓存', 'tmp')]
            for fn in filenames:
                if fn.endswith(WATCH_EXTS):
                    full = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(full)
                        snap[full] = (st.st_mtime, st.st_size)
                    except OSError:
                        pass
    return snap


def notify_ai(cmd, payload=None):
    """通过 UNIX socket 通知 ai_service"""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(IPC_SOCKET)
        req = {'cmd': cmd}
        if payload:
            req.update(payload)
        s.sendall(json.dumps(req).encode('utf-8'))
        resp = s.recv(4096).decode('utf-8', errors='replace')
        s.close()
        return resp
    except Exception as e:
        log(f'[notify] ai_service 通知失败: {e}')
        return None


def rescan_modules():
    """调用 qd_loader 重新扫描模块"""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
        from qd_loader import ModuleLoader
        loader = ModuleLoader(roots=[os.path.join(QD_ROOT, '系统'),
                                     os.path.join(QD_ROOT, '用户.qd', '模块')])
        mods = loader.scan()
        return len(mods)
    except Exception as e:
        log(f'[rescan] 模块重扫失败: {e}')
        return -1


def run_once():
    """单次扫描（测试用）：立即重载一次"""
    changed = collect_snapshots()
    log(f'[once] 扫描完成: {len(changed)} 个文件')
    nmods = rescan_modules()
    resp = notify_ai('hot_reload_qdai')
    log(f'[once] 模块重扫: {nmods} 个, ai 重载响应: {resp}')
    return 0


def daemon():
    global _log_fh
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    _log_fh = open(LOG_FILE, 'a', encoding='utf-8')
    log('=== qd_hotload 热加载守护启动 ===')
    log(f'监控根: {", ".join(WATCH_ROOTS)}')

    last = collect_snapshots()
    log(f'初始快照: {len(last)} 个文件')
    last_ai_reload = time.time()
    running = True

    def on_signal(sig, frame):
        nonlocal running
        running = False
        sys.exit(0)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    while running:
        time.sleep(POLL_INTERVAL)
        try:
            cur = collect_snapshots()
            changed_qdai = []
            changed_other = []

            # 新增 / 修改
            for path, (mt, sz) in cur.items():
                old = last.get(path)
                if old is None:
                    (changed_qdai if path.endswith('.qdai')
                     else changed_other).append(path)
                elif old != (mt, sz):
                    (changed_qdai if path.endswith('.qdai')
                     else changed_other).append(path)
            # 删除
            for path in last:
                if path not in cur:
                    (changed_qdai if path.endswith('.qdai')
                     else changed_other).append(f'(删除) {path}')

            if changed_qdai or changed_other:
                now = time.time()
                log(f'[检测] 变化: qdai={len(changed_qdai)} 其他={len(changed_other)}')
                for p in (changed_qdai + changed_other)[:8]:
                    log(f'        {p}')

                # 防抖：2 秒内合并多次变化
                if now - last_ai_reload >= 1.0:
                    if changed_qdai:
                        resp = notify_ai('hot_reload_qdai')
                        log(f'[重载] ai 规则重载 → {resp if resp else "(无响应)"}')
                    if changed_other:
                        nmods = rescan_modules()
                        log(f'[重载] 模块重扫 → {nmods} 个模块')
                    last_ai_reload = now

            last = cur
        except Exception as e:
            log(f'[错误] {e}')
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description='qd_hotload 热加载守护')
    parser.add_argument('cmd', nargs='?', default='daemon',
                        choices=['daemon', 'once', 'status'])
    args = parser.parse_args()
    if args.cmd == 'once':
        return run_once()
    if args.cmd == 'status':
        print(f'qd_hotload 日志: {LOG_FILE}')
        if os.path.exists(LOG_FILE):
            print(open(LOG_FILE, encoding='utf-8').read()[-800:])
        return 0
    daemon()
    return 0


if __name__ == '__main__':
    sys.exit(main())
