#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程调度.qds - 进程调度服务（功能.qd）
系统进程调度：列出/启动/停止奇点OS 管理的进程
对应 功能.qd/进程调度.qds（无兼容层，管理 Linux 原生进程）

功能:
  list [过滤词]     列出进程
  start <命令...>   启动进程
  stop <pid>        停止进程
  status            服务状态
"""
import os
import sys
import json
import subprocess
import signal
import argparse
import time
from datetime import datetime

STATUS_FILE = '/tmp/qd_sched_status.json'


def list_procs(filter_str=None):
    """列出进程"""
    ps = subprocess.run(['ps', 'aux'], capture_output=True, text=True).stdout
    lines = ps.splitlines()
    if not lines:
        print('(无进程)')
        return
    print(f"{'PID':>7} {'CPU%':>5} {'MEM%':>5} 命令")
    count = 0
    for line in lines[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        pid, cpu, mem = parts[1], parts[2], parts[3]
        cmd = parts[10]
        if filter_str and filter_str not in cmd:
            continue
        print(f'{pid:>7} {cpu:>5} {mem:>5} {cmd[:80]}')
        count += 1
    print(f'共 {count} 个进程')


def start_proc(cmd_list):
    """启动进程"""
    if not cmd_list:
        print('需要命令')
        return 1
    try:
        p = subprocess.Popen(cmd_list)
        print(f'[OK] 已启动 PID {p.pid}: {" ".join(cmd_list)}')
        return 0
    except Exception as e:
        print(f'[错误] 启动失败: {e}')
        return 1


def stop_proc(pid_str):
    """停止进程"""
    try:
        pid = int(pid_str)
    except ValueError:
        print(f'非法 PID: {pid_str}')
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
        print(f'[OK] 已发送停止信号 PID {pid}')
        return 0
    except ProcessLookupError:
        print(f'[错误] 进程不存在: {pid}')
        return 1
    except PermissionError:
        print(f'[错误] 无权限: {pid}')
        return 1


def status():
    st = {
        'service': '进程调度',
        'time': datetime.now().isoformat(),
        'pid': os.getpid(),
    }
    with open(STATUS_FILE, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    print(f'进程调度服务: 运行中 (PID {os.getpid()})')
    return 0


def main():
    parser = argparse.ArgumentParser(description='进程调度服务')
    sub = parser.add_subparsers(dest='cmd')
    p = sub.add_parser('list', help='列出进程')
    p.add_argument('filter', nargs='?', default=None)
    p = sub.add_parser('start', help='启动进程')
    p.add_argument('cmd', nargs='+')
    p = sub.add_parser('stop', help='停止进程')
    p.add_argument('pid')
    sub.add_parser('status', help='服务状态')
    args = parser.parse_args()

    if args.cmd == 'list':
        return list_procs(args.filter)
    if args.cmd == 'start':
        return start_proc(args.cmd)
    if args.cmd == 'stop':
        return stop_proc(args.pid)
    if args.cmd == 'status':
        return status()
    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
