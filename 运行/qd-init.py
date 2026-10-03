#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qd-init.py - 奇点OS 初始化服务（Linux 魔改版）
替代自研 qd-init 的系统服务执行层入口，基于 systemd 底座。

启动流程（对应规范 2.3）：
  1. 挂载/校验系统目录
  2. 启动模块加载器（扫描 .qd 模块）
  3. 启动 AI.qd 中枢（策略层）
  4. 启动系统服务（进程调度/shm_ipc/microsrv）
  5. 等待就绪，报告状态

用法:
  python3 qd-init.py start    # 启动奇点OS 框架
  python3 qd-init.py status   # 查看状态
  python3 qd-init.py stop     # 停止框架
"""
import os
import sys
import json
import time
import signal
import subprocess
import argparse
from datetime import datetime

QD_ROOT = '/奇点OS'
SYSTEM_DIR = os.path.join(QD_ROOT, '系统')
AI_DIR = os.path.join(QD_ROOT, 'AI.qd')
USER_DIR = os.path.join(QD_ROOT, '用户.qd')
TMP_DIR = '/tmp/qd'
STATUS_FILE = '/tmp/qd_status.json'

# 必须存在的顶层目录（规范 2.2）
TOP_DIRS = [
    os.path.join(SYSTEM_DIR, '功能.qd'),
    os.path.join(SYSTEM_DIR, '界面.qd'),
    os.path.join(SYSTEM_DIR, '虚拟化.qd'),
    os.path.join(SYSTEM_DIR, '微服务.qd'),
    AI_DIR,
    os.path.join(USER_DIR, '应用'),
    os.path.join(USER_DIR, '模块'),
    os.path.join(USER_DIR, '文档'),
    os.path.join(USER_DIR, '下载'),
    os.path.join(USER_DIR, '工具.qd'),
    TMP_DIR,
]


class QDInit:
    def __init__(self):
        self.running = False
        self.procs = {}  # 服务名 -> Popen
        self.services = []

    def ensure_dirs(self):
        """确保规范要求的顶层目录存在"""
        os.makedirs(TMP_DIR, exist_ok=True)
        for d in TOP_DIRS:
            os.makedirs(d, exist_ok=True)
        print('[目录] 奇点OS 目录结构已确认')

    def start(self):
        self.ensure_dirs()
        self.running = True
        print('========================================')
        print('  奇点OS 系统服务 (Linux 魔改版)')
        print('========================================')

        # 1. 模块系统
        print('\n[1/5] 启动模块系统...')
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from qd_loader import ModuleLoader
        loader = ModuleLoader(roots=[SYSTEM_DIR, os.path.join(USER_DIR, '模块')])
        mods = loader.scan()
        print(f'      {len(mods)} 个模块已加载')

        # 2. AI.qd 中枢（由 ai-qd.service 独立管理；此处检测避免重复启动）
        print('\n[2/5] 启动 AI.qd 中枢...')
        import subprocess as _sp
        try:
            _existing = _sp.run(['pgrep', '-f', 'ai_service.py daemon'],
                                capture_output=True, text=True)
            _pids = [x for x in _existing.stdout.split() if x]
        except Exception:
            _pids = []
        if _pids:
            print(f'      [ai_service] 已在运行 (PID {",".join(_pids)})，跳过重复启动')
            self.procs['ai_service'] = None
        else:
            ai_proc = self._start_service(
                'ai_service',
                [sys.executable, os.path.join(os.path.dirname(__file__), 'ai_service.py'), 'daemon'],
            )
        time.sleep(1)

        # 3. shm_ipc（共享内存 IPC）
        print('\n[3/5] 启动 shm_ipc 服务...')
        self._start_service(
            'shm_ipc',
            [sys.executable, os.path.join(os.path.dirname(__file__), 'shm_ipc.py'), 'daemon'],
        )

        # 4. microsrv（微服务）
        print('\n[4/5] 启动 microsrv 微服务...')
        self._start_service(
            'microsrv',
            [sys.executable, os.path.join(os.path.dirname(__file__), 'microsrv.py'), 'daemon'],
        )

        # 5. 就绪
        print('\n[5/5] 奇点OS 框架就绪')
        self._write_status('running')
        self._print_status()

    def _start_service(self, name, cmd):
        """启动子服务并监控"""
        log = open(os.path.join(TMP_DIR, f'{name}.log'), 'a', encoding='utf-8')
        try:
            p = subprocess.Popen(cmd, stdout=log, stderr=log,
                                 start_new_session=True)
            self.procs[name] = p
            self.services.append(name)
            print(f'      [{name}] PID={p.pid}')
            return p
        except Exception as e:
            print(f'      [{name}] 启动失败: {e}')
            return None

    def _write_status(self, state):
        status = {
            'state': state,
            'time': datetime.now().isoformat(),
            'services': {k: (v is not None and v.poll() is None) for k, v in self.procs.items()},
            'pid': os.getpid(),
        }
        with open(STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(status, f, ensure_ascii=False, indent=2)

    def _print_status(self):
        print()
        print('--- 服务状态 ---')
        for name, p in self.procs.items():
            state = '外部管理' if p is None else ('运行中' if p.poll() is None else f'已退出(code={p.poll()})')
            print(f'  {name:12s} {state}')
        print(f'  状态文件: {STATUS_FILE}')

    def stop(self):
        self.running = False
        print('停止奇点OS 框架服务...')
        for name, p in list(self.procs.items()):
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    p.terminate()
                print(f'  [{name}] 已停止')
        self._write_status('stopped')
        print('已停止')

    def status(self):
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                st = json.load(f)
            print(f'奇点OS 状态: {st["state"]}')
            print(f'启动时间: {st["time"]}')
            for name, running in st['services'].items():
                print(f'  {name:12s} {"运行中" if running else "已停止"}')
        else:
            print('奇点OS 框架未启动')
        # 检查进程
        for name in ['ai_service', 'shm_ipc', 'microsrv']:
            out = subprocess.run(['pgrep', '-f', name], capture_output=True, text=True)
            if out.stdout.strip():
                print(f'  实际进程 {name}: {out.stdout.strip().splitlines()}')


def main():
    parser = argparse.ArgumentParser(description='奇点OS 初始化服务')
    parser.add_argument('cmd', choices=['start', 'stop', 'status', 'restart'])
    args = parser.parse_args()
    init = QDInit()
    if args.cmd == 'start':
        init.start()
    elif args.cmd == 'stop':
        init.stop()
    elif args.cmd == 'status':
        init.status()
    elif args.cmd == 'restart':
        init.stop()
        time.sleep(1)
        init.start()


if __name__ == '__main__':
    main()
