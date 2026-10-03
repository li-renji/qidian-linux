#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
算力调度.qds - 算力调度服务（功能.qd）
基于 CPU 亲和性(taskset) + 优先级(renice) 的进程算力分配
对应规范：功能.qd/算力调度（进程调度之上的算力层）

用法:
  python3 算力调度.py list                列出服务进程与算力状态
  python3 算力调度.py set-cpu <pid> <cpus> 设置 CPU 亲和性 (taskset -pc 0,1 <pid>)
  python3 算力调度.py set-nice <pid> <v>   设置优先级 (-20..19)
  python3 算力调度.py policy <name>       应用预设策略 ai-first|balanced|system
  python3 算力调度.py stats               全局 CPU/内存统计
  python3 算力调度.py status              调度器状态
"""
import os
import sys
import json
import time
import subprocess
import argparse
from datetime import datetime

STATUS_FILE = '/tmp/qd_sched_status.json'
CORE_SERVICES = {
    'ai_service': 'AI 框架（最高优先）',
    'qd_hotload': '热加载守护',
    'shm_ipc': '共享内存 IPC',
    'microsrv': '微服务',
    'qd_shell': '桌面外壳',
}
# AI 服务绑核（大核优先策略：绑前两个 CPU）
AI_PRIORITY_NICE = -5


def _ps():
    r = subprocess.run(['ps', 'aux'], capture_output=True, text=True).stdout
    return r.splitlines()


def _nice_of(pid):
    try:
        r = subprocess.run(['ps', '-o', 'ni=', '-p', str(pid)],
                           capture_output=True, text=True).stdout.strip()
        return r
    except Exception:
        return '?'


def _cpu_affinity(pid):
    try:
        r = subprocess.run(['taskset', '-pc', str(pid)],
                           capture_output=True, text=True).stdout.strip()
        return r.split(':')[-1].strip() if ':' in r else r
    except Exception:
        return 'all'


def list_procs():
    """列出核心服务算力状态"""
    rows = []
    for line in _ps()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        pid, cpu, mem = parts[1], parts[2], parts[3]
        cmd = parts[10]
        for name in CORE_SERVICES:
            if name in cmd:
                rows.append({
                    'service': name,
                    'pid': pid,
                    'cpu%': cpu,
                    'mem%': mem,
                    'nice': _nice_of(pid),
                    'affinity': _cpu_affinity(pid),
                })
                break
    return rows


def set_cpu(pid, cpus):
    """设置 CPU 亲和性"""
    try:
        r = subprocess.run(['taskset', '-pc', cpus, str(pid)],
                           capture_output=True, text=True, timeout=5)
        return {'code': 0 if r.returncode == 0 else 5,
                'msg': (r.stdout + r.stderr).strip()}
    except Exception as e:
        return {'code': 5, 'msg': str(e)}


def set_nice(pid, value):
    """设置优先级"""
    try:
        r = subprocess.run(['renice', str(value), '-p', str(pid)],
                           capture_output=True, text=True, timeout=5)
        return {'code': 0 if r.returncode == 0 else 5,
                'msg': (r.stdout + r.stderr).strip()}
    except Exception as e:
        return {'code': 5, 'msg': str(e)}


def policy(name):
    """预设调度策略"""
    rows = list_procs()
    results = []
    if name == 'ai-first':
        # AI 框架最高优先：nice -5，绑前 2 核；其他恢复默认
        for r in rows:
            if r['service'] == 'ai_service':
                results.append({'service': 'ai_service',
                                'action': set_nice(r['pid'], AI_PRIORITY_NICE)})
                results.append({'service': 'ai_service',
                                'action': set_cpu(r['pid'], '0,1')})
            elif r['service'] in ('shm_ipc', 'microsrv', 'qd_hotload'):
                results.append({'service': r['service'],
                                'action': set_nice(r['pid'], 0)})
    elif name == 'balanced':
        for r in rows:
            if r['service'] in CORE_SERVICES:
                results.append({'service': r['service'],
                                'action': set_nice(r['pid'], 0)})
    elif name == 'system':
        # 桌面/窗口优先
        for r in rows:
            if r['service'] == 'qd_shell':
                results.append({'service': r['service'],
                                'action': set_nice(r['pid'], -3)})
            elif r['service'] == 'ai_service':
                results.append({'service': 'ai_service',
                                'action': set_nice(r['pid'], 0)})
    else:
        return {'code': 1, 'msg': f'未知策略: {name}（支持 ai-first/balanced/system）'}
    _write_status(name)
    return {'code': 0, 'msg': f'策略 {name} 已应用', 'detail': results}


def stats():
    """全局 CPU/内存统计"""
    try:
        r = subprocess.run(['top', '-bn1'],
                           capture_output=True, text=True, timeout=10).stdout
        lines = r.splitlines()
        cpu_line = next((l for l in lines if l.startswith('%Cpu')), '')
        mem_line = next((l for l in lines if l.startswith('MiB Mem')), '')
        return {'cpu': cpu_line.strip(), 'mem': mem_line.strip()}
    except Exception as e:
        return {'error': str(e)}


def _write_status(policy_applied):
    st = {
        'service': '算力调度',
        'policy': policy_applied,
        'time': datetime.now().isoformat(timespec='seconds'),
    }
    with open(STATUS_FILE, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description='算力调度服务')
    parser.add_argument('cmd', nargs='?', default='list',
                        choices=['list', 'set-cpu', 'set-nice', 'policy', 'stats', 'status'])
    parser.add_argument('args', nargs='*')
    args = parser.parse_args()

    if args.cmd == 'list':
        rows = list_procs()
        print(json.dumps({'code': 0, 'data': {'services': rows, 'count': len(rows)}},
                         ensure_ascii=False, indent=1))
    elif args.cmd == 'set-cpu' and len(args.args) >= 2:
        print(json.dumps(set_cpu(args.args[0], args.args[1]), ensure_ascii=False))
    elif args.cmd == 'set-nice' and len(args.args) >= 2:
        print(json.dumps(set_nice(args.args[0], args.args[1]), ensure_ascii=False))
    elif args.cmd == 'policy' and len(args.args) >= 1:
        print(json.dumps(policy(args.args[0]), ensure_ascii=False, indent=1))
    elif args.cmd == 'stats':
        print(json.dumps(stats(), ensure_ascii=False, indent=1))
    elif args.cmd == 'status':
        if os.path.exists(STATUS_FILE):
            print(open(STATUS_FILE, encoding='utf-8').read())
        else:
            print('算力调度: 未应用策略')
    else:
        print(json.dumps({'code': 1, 'msg': '参数不足（set-cpu <pid> <cpus> / set-nice <pid> <v>）'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
