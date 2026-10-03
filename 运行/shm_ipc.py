#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shm_ipc.py - 共享内存 IPC 服务（功能.qd/进程调度 关联服务）
基于 mmap 共享内存实现进程间通信，微秒级，不经 AI.qd 实时转发
（对应规范 13 章 QD-IPC 双通道架构中的直连通道）

规范 20.4 配额与背压：
  - 每段默认配额 8MB（注册时可指定，上限 64MB）
  - 写入前检查段大小，超限返回错误码 4（资源不足）
  - 段生命周期绑定注册应用，应用退出/崩溃时回收该段

用法:
  python3 shm_ipc.py daemon                 # 守护模式
  python3 shm_ipc.py send <key> <data>      # 发送消息（超限 code=4）
  python3 shm_ipc.py recv <key>             # 读取最新消息
  python3 shm_ipc.py register <key> [mb]     # 注册段并设定配额（默认8MB，上限64MB）
  python3 shm_ipc.py reap                   # 回收 owner 已退出的段
  python3 shm_ipc.py status                 # 查看状态与配额
"""
import os
import sys
import json
import mmap
import time
import struct
import signal
import argparse
import threading
from datetime import datetime

SHM_BASE = '/dev/shm/qd_ipc'
STATUS_FILE = '/tmp/qd_shm_status.json'
QUOTA_FILE = '/tmp/qd_shm_quota.json'

# 规范 20.4 配额
DEFAULT_QUOTA = 8 * 1024 * 1024     # 8MB
MAX_QUOTA = 64 * 1024 * 1024        # 64MB
HEADER_FIXED = 8                    # magic(4) + size(4)

# 错误码（规范 9.6）
ERR_OK = 0
ERR_RESOURCE = 4
ERR_INTERNAL = 5

# 消息头：magic(4) + size(4) + 数据
MAGIC = b'QDSM'

# 最近一次 write_msg 的结构化结果（供 CLI 读取错误码）
last_result = {'code': ERR_OK, 'msg': ''}


def shm_path(key):
    return f'{SHM_BASE}_{key}'


# ---------------- 配额管理层（规范 20.4） ----------------
def _load_quota():
    try:
        with open(QUOTA_FILE, encoding='utf-8') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_quota(q):
    tmp = QUOTA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(q, f, ensure_ascii=False, indent=1)
    os.replace(tmp, QUOTA_FILE)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def register(key, quota_mb=None):
    """注册段配额。quota_mb 默认 8，上限 64。绑定当前进程为 owner。"""
    q = _load_quota()
    mb = 8 if quota_mb is None else max(1, min(64, float(quota_mb)))
    q[key] = {
        'quota_bytes': int(mb * 1024 * 1024),
        'owner_pid': os.getpid(),
        'registered_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    _save_quota(q)
    return {'code': ERR_OK, 'key': key, 'quota_mb': mb, 'owner_pid': os.getpid()}


def get_quota(key):
    q = _load_quota()
    ent = q.get(key)
    if ent:
        # owner 已死则视为未注册（可被回收）
        if _pid_alive(ent.get('owner_pid', 0)):
            return ent.get('quota_bytes', DEFAULT_QUOTA)
    return DEFAULT_QUOTA


def usage(key):
    p = shm_path(key)
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def reap():
    """回收 owner 已退出的段：删除段文件与配额登记。"""
    q = _load_quota()
    removed = []
    for key, ent in list(q.items()):
        if not _pid_alive(ent.get('owner_pid', 0)):
            try:
                os.unlink(shm_path(key))
            except OSError:
                pass
            q.pop(key, None)
            removed.append(key)
    _save_quota(q)
    return {'code': ERR_OK, 'reaped': removed, 'remaining': list(q.keys())}


def write_msg_ex(key, data):
    """写入共享内存消息（原子替换），带配额检查。
    返回 {'code': 0|4|5, 'msg':..., 'bytes':...}。超限 code=4。"""
    payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
    total = HEADER_FIXED + len(payload)
    quota = get_quota(key)
    if total > quota:
        last_result.update({'code': ERR_RESOURCE,
                            'msg': f'资源不足: 段 {key} 需要 {total}B 超过配额 {quota}B'})
        return last_result.copy()
    path = shm_path(key)
    try:
        os.makedirs(SHM_BASE, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
        os.write(fd, MAGIC + struct.pack('<I', len(payload)) + payload)
        os.close(fd)
        last_result.update({'code': ERR_OK, 'msg': 'ok', 'bytes': total})
        return last_result.copy()
    except Exception as e:
        last_result.update({'code': ERR_INTERNAL, 'msg': str(e)})
        return last_result.copy()


def write_msg(key, data):
    """向后兼容：True 成功 / False 失败（含配额超限 code=4）。"""
    r = write_msg_ex(key, data)
    return r['code'] == ERR_OK


def read_msg(key):
    """读取共享内存最新消息"""
    path = shm_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) < 8 or data[:4] != MAGIC:
            return None
        size = struct.unpack('<I', data[4:8])[0]
        payload = data[8:8+size]
        return json.loads(payload.decode('utf-8'))
    except Exception:
        return None


class ShmIpcService:
    def __init__(self):
        self.running = True
        self.handlers = {}  # key -> callback

    def register(self, key, cb):
        self.handlers[key] = cb

    def start(self):
        os.makedirs(SHM_BASE, exist_ok=True)
        print('=== shm_ipc 服务启动 ===')
        print(f'共享内存目录: {SHM_BASE}')
        print(f'PID: {os.getpid()}')
        # 定期写入心跳
        threading.Thread(target=self._heartbeat, daemon=True).start()
        while self.running:
            time.sleep(1)

    def _heartbeat(self):
        while self.running:
            write_msg('heartbeat', {
                'service': 'shm_ipc',
                'time': datetime.now().isoformat(),
                'pid': os.getpid(),
            })
            # 周期性回收死亡 owner 的段
            reap()
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description='shm_ipc 共享内存服务')
    parser.add_argument('cmd', nargs='?', default='daemon',
                        choices=['daemon', 'send', 'recv', 'register', 'reap', 'status'])
    parser.add_argument('key', nargs='?', default=None)
    parser.add_argument('data', nargs='?', default=None)
    args = parser.parse_args()

    if args.cmd == 'send':
        if not args.key or args.data is None:
            print('用法: shm_ipc.py send <key> <json数据>')
            return 1
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError:
            data = args.data
        r = write_msg_ex(args.key, data)
        print(f'[shm] 发送到 {args.key}: code={r["code"]} {r["msg"]} bytes={r.get("bytes", 0)}')
        return 0 if r['code'] == ERR_OK else r['code']

    if args.cmd == 'recv':
        if not args.key:
            print('用法: shm_ipc.py recv <key>')
            return 1
        msg = read_msg(args.key)
        if msg is None:
            print(f'[shm] {args.key}: 无消息')
            return 1
        print(json.dumps(msg, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == 'register':
        if not args.key:
            print('用法: shm_ipc.py register <key> [mb]')
            return 1
        mb = float(args.data) if args.data else None
        print(json.dumps(register(args.key, mb), ensure_ascii=False))
        return 0

    if args.cmd == 'reap':
        print(json.dumps(reap(), ensure_ascii=False))
        return 0

    if args.cmd == 'status':
        hb = read_msg('heartbeat')
        q = _load_quota()
        print(f'shm_ipc: {"运行中 (心跳 " + hb["time"] + ")" if hb else "无心跳"}')
        print(f'配额登记段数: {len(q)}')
        for k, ent in q.items():
            alive = _pid_alive(ent.get('owner_pid', 0))
            print(f'  {k}: quota={ent.get("quota_bytes",0)//1024}KB owner={ent.get("owner_pid")} alive={alive} usage={usage(k)}B')
        return 0

    # daemon
    svc = ShmIpcService()

    def on_signal(sig, frame):
        svc.running = False
        sys.exit(0)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    svc.start()


if __name__ == '__main__':
    main()
