#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
slo_measure.py —— 奇点OS SLO 测量框架（规范 20.1）
测量项：
  1. shm 回环延迟（/dev/shm/qd_ipc 写+读，P50/P99）
  2. socket RPC ping 延迟（UNIX socket /run/qd/qd_ai.sock，P50/P99）
  3. qds 冷启动（qd-run.py 加载一个简单 .qds，10 次平均）
  4. 桌面空闲 CPU（xfdesktop/xfwm4/shell 5s 采样均值）
  5. 双框架启动（systemd-analyze blame：qd-os 与 ai-qd）
输出：JSON 到 stdout 与 /奇点OS/运行/logs/slo_report.json
幂等：重复执行只覆盖报告，不改变系统状态。
"""
import os
import sys
import json
import time
import socket
import statistics
import subprocess
import glob

RUN_DIR = '/奇点OS/运行'
LOG_DIR = os.path.join(RUN_DIR, 'logs')
REPORT = os.path.join(LOG_DIR, 'slo_report.json')

# 规范 20.1 红线
RED = {
    'shm_p99_ms': 5.0,          # shm P99 ≤ 5ms
    'socket_p99_ms': 50.0,       # socket P99 ≤ 50ms
    'qds_cold_start_ms': 10.0,   # qds 冷启动 ≤ 10ms（原生化目标，Python 版预期不达标）
    'dual_framework_boot_s': 30.0,  # 双框架启动 ≤ 30s
    'idle_cpu_pct': 3.0,         # 桌面空闲 CPU ≤ 3%
}

SHM_BASE = '/dev/shm/qd_ipc'
IPC_SOCK = '/run/qd/qd_ai.sock'
QDS_SAMPLE = '/奇点OS/用户.qd/工具.qd/眼睛.qds'


def _pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def measure_shm(n=200):
    """shm 回环：写一条小消息再读回，计 write+read 总耗时。"""
    sys.path.insert(0, RUN_DIR)
    try:
        import shm_ipc
    except Exception as e:
        return {'ok': False, 'error': 'shm_ipc import 失败: ' + str(e)}
    key = 'slo_probe'
    samples = []
    for i in range(n):
        t0 = time.perf_counter()
        shm_ipc.write_msg(key, {'seq': i, 'ping': 1})
        shm_ipc.read_msg(key)
        samples.append((time.perf_counter() - t0) * 1000.0)
    # 清理探测段
    p = shm_ipc.shm_path(key)
    try:
        os.unlink(p)
    except OSError:
        pass
    return {
        'ok': True, 'iterations': n,
        'p50_ms': round(_pct(samples, 50), 3),
        'p99_ms': round(_pct(samples, 99), 3),
        'min_ms': round(min(samples), 3),
        'max_ms': round(max(samples), 3),
        'redline_ms': RED['shm_p99_ms'],
        'pass': _pct(samples, 99) <= RED['shm_p99_ms'],
    }


def measure_socket(n=200):
    """UNIX socket RPC ping：连接+发送+接收，每次新建连接。"""
    samples = []
    ok_count = 0
    for i in range(n):
        try:
            t0 = time.perf_counter()
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(IPC_SOCK)
            s.sendall(json.dumps({'cmd': 'ping'}).encode('utf-8'))
            s.recv(65536)
            s.close()
            samples.append((time.perf_counter() - t0) * 1000.0)
            ok_count += 1
        except Exception:
            pass
    if not samples:
        return {'ok': False, 'error': 'socket ping 全部失败（服务未运行？）',
                'socket': IPC_SOCK}
    return {
        'ok': True, 'iterations': ok_count,
        'p50_ms': round(_pct(samples, 50), 3),
        'p99_ms': round(_pct(samples, 99), 3),
        'min_ms': round(min(samples), 3),
        'max_ms': round(max(samples), 3),
        'redline_ms': RED['socket_p99_ms'],
        'pass': _pct(samples, 99) <= RED['socket_p99_ms'],
    }


def measure_qds_cold_start(n=10):
    """qds 冷启动：qd-run.py 加载一个简单 .qds 并跑 status。"""
    samples = []
    for i in range(n):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, os.path.join(RUN_DIR, 'qd-run.py'),
                        QDS_SAMPLE, 'status'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        'ok': True, 'iterations': n,
        'avg_ms': round(statistics.mean(samples), 3),
        'p50_ms': round(_pct(samples, 50), 3),
        'max_ms': round(max(samples), 3),
        'sample': QDS_SAMPLE,
        'redline_ms': RED['qds_cold_start_ms'],
        'pass': statistics.mean(samples) <= RED['qds_cold_start_ms'],
    }


def _proc_cpu_snapshot(pids):
    """读 /proc/<pid>/stat 的 utime+stime（clock tick）。"""
    out = {}
    for pid in pids:
        try:
            with open('/proc/%d/stat' % pid) as f:
                parts = f.read().split()
            # comm 含括号，字段 14/15 是 utime/stime（1-based），索引 13/14
            out[pid] = int(parts[13]) + int(parts[14])
        except Exception:
            out[pid] = None
    return out


def measure_idle_cpu(window=5.0):
    """采样 xfdesktop / xfwm4 / shell 的 CPU 占用，window 秒内均值（%）。"""
    targets = ['xfdesktop', 'xfwm4', 'xfce4-panel']
    pids = []
    for name in targets:
        try:
            r = subprocess.run(['pgrep', '-x', name], capture_output=True, text=True)
            for line in r.stdout.split():
                if line.strip().isdigit():
                    pids.append(int(line.strip()))
        except Exception:
            pass
    if not pids:
        return {'ok': False, 'error': '未找到桌面进程（xfdesktop/xfwm4/xfce4-panel）',
                'targets': targets}
    hz = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
    snap0 = _proc_cpu_snapshot(pids)
    t0 = time.perf_counter()
    time.sleep(window)
    elapsed = time.perf_counter() - t0
    snap1 = _proc_cpu_snapshot(pids)
    total_cpu = 0.0
    detail = {}
    for pid in pids:
        a, b = snap0.get(pid), snap1.get(pid)
        if a is None or b is None:
            continue
        pct = (b - a) / hz / elapsed * 100.0
        total_cpu += pct
        detail[pid] = round(pct, 2)
    return {
        'ok': True, 'window_s': window,
        'pids': detail,
        'total_cpu_pct': round(total_cpu, 2),
        'redline_pct': RED['idle_cpu_pct'],
        'pass': total_cpu <= RED['idle_cpu_pct'],
    }


def measure_boot():
    """双框架启动耗时：systemd-analyze blame。"""
    try:
        r = subprocess.run(['systemd-analyze', 'blame'], capture_output=True,
                           text=True, timeout=15)
        lines = r.stdout.splitlines()
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    found = {}
    total = None
    for ln in lines:
        for svc in ('qd-os.service', 'ai-qd.service'):
            if svc in ln:
                found[svc] = ln.strip()
    try:
        r2 = subprocess.run(['systemd-analyze'], capture_output=True, text=True, timeout=10)
        total = r2.stdout.strip()
    except Exception:
        pass
    # 解析秒数
    def _sec(s):
        # 形如 "       1.091s qd-os.service"
        parts = s.split()
        for tok in parts:
            if tok.endswith('s'):
                t = tok.rstrip('s')
                try:
                    return float(t)
                except ValueError:
                    return None
        return None
    qdos = _sec(found.get('qd-os.service', ''))
    aiqd = _sec(found.get('ai-qd.service', ''))
    boot_sum = sum(x for x in (qdos, aiqd) if x is not None)
    return {
        'ok': True,
        'qd_os_service': found.get('qd-os.service'),
        'ai_qd_service': found.get('ai-qd.service'),
        'qd_os_s': qdos,
        'ai_qd_s': aiqd,
        'boot_sum_s': round(boot_sum, 3),
        'systemd_analyze': total,
        'redline_s': RED['dual_framework_boot_s'],
        'pass': boot_sum <= RED['dual_framework_boot_s'],
    }


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    report = {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'host': os.uname().nodename,
        'redlines_ms': RED,
        'metrics': {
            'shm_loopback': measure_shm(),
            'socket_rpc': measure_socket(),
            'qds_cold_start': measure_qds_cold_start(),
            'desktop_idle_cpu': measure_idle_cpu(),
            'dual_framework_boot': measure_boot(),
        },
    }
    # 汇总
    checks = []
    for k, v in report['metrics'].items():
        if isinstance(v, dict) and v.get('ok') and 'pass' in v:
            checks.append({'metric': k, 'pass': v['pass']})
    report['summary'] = {
        'checks': checks,
        'all_pass': all(c['pass'] for c in checks) if checks else False,
        'failed': [c['metric'] for c in checks if not c['pass']],
    }
    with open(REPORT, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
