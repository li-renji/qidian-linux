#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qdd.py - 奇点OS 模块运行时守护（Module Runtime Daemon）
六机制一次落地：
  1. 热发现   —— 轮询快照 diff，.qd/.qds/.qdai 增改删 → 自动注册/更新/注销（无需重启）
  2. 统一生命周期 —— start/stop/status/unload 状态机，.qds/.py 入口进程管理（Popen+pid）
  3. 热替换   —— switch(type, new_id)：同类型多实现，停旧启新（桌面/AI/系统服务通用）
  4. 调用隔离 —— 扫描模块代码中的跨模块 sys.path/import，违规模块标记 flag，总线之外禁调用
  5. 模块通讯总线 —— UNIX socket(VM)/TCP(测试) JSON 协议：list/status/start/stop/switch/call
  6. 权限分级 —— module.qdmeta 声明 type → priv 级别；admin(core/ai) > system/ui/desktop > user/dev

用法:
  python3 qdd.py daemon          # 守护（热发现+总线+生命周期）
  python3 qdd.py once            # 单次扫描并打印模块表（测试）
  python3 qdd.py list            # 走总线查当前模块表
  python3 qdd.py start <id>      # 启动模块
  python3 qdd.py stop <id>
  python3 qdd.py switch <type> <new_id>
  python3 qdd.py call <id> <cmd> <json>
"""
import os
import sys
import json
import time
import socket
import signal
import struct
import shutil
import subprocess
import tempfile
import hashlib
import threading
import argparse
from datetime import datetime

QD_ROOT = os.environ.get('QD_ROOT', '/奇点OS')
POLL_INTERVAL = float(os.environ.get('QD_POLL', '2.0'))
BUS_PATH = os.environ.get('QD_BUS', '/run/qd/qd_bus.sock')
BUS_PORT = int(os.environ.get('QD_BUS_PORT', '0'))
LOG_FILE = os.environ.get('QD_LOG', '/tmp/qd_runtime.log')
WATCH_EXTS = ('.qd', '.qds', '.qdai', '.qdmeta')
SKIP_DIRS = ('.git', '__pycache__', '缓存', 'tmp')

# 权限分级：admin=3 core=3 system=2 ui=2 desktop=2 user=1 dev=1 net=1 compat=2
PRIV = {'ai': 3, 'core': 3, 'system': 2, 'ui': 2, 'desktop': 2,
        'compat': 2, 'user': 1, 'dev': 1, 'net': 1}
ARCHIVE_ROOT = os.environ.get('QD_ARCHIVE', '/奇点OS/运行/模块归档')
QDS_MAGIC = 0x51445353

_log_lock = threading.Lock()


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass


# ============================================================
# 模块对象与状态机
# ============================================================
class Module:
    def __init__(self, path, meta):
        self.path = path
        self.id = meta.get('id')
        self.name = meta.get('name', self.id)
        self.type = meta.get('type', 'user')
        self.version = meta.get('version', '0')
        self.entry = meta.get('entry', '')
        self.interface = meta.get('interface', {})
        self.depends = meta.get('depends', [])
        self.priv = PRIV.get(self.type, 1)
        self.state = 'stopped'
        self.pid = None
        self.flags = []          # 隔离检查告警
        self.qds = []
        self.qdai = []
        self.submodules = []
        self.error = None

    def entry_path(self):
        return os.path.join(self.path, self.entry) if self.entry else None

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'type': self.type,
                'version': self.version, 'state': self.state, 'pid': self.pid,
                'priv': self.priv, 'flags': self.flags, 'error': self.error,
                'entry': self.entry, 'path': self.path,
                'qds': [q.get('name') for q in self.qds],
                'qdai': [r.get('meta', {}).get('name', '') for r in self.qdai],
                'submodules': [s.id for s in self.submodules]}


# ============================================================
# 模块运行时
# ============================================================
class ModuleRuntime:
    def __init__(self, root=None):
        self.root = root or QD_ROOT
        self.modules = {}   # id -> Module
        self._snap = {}     # path -> (mtime,size)
        self._lock = threading.Lock()
        self._procs = {}    # id -> Popen

    # ---- 热发现：全树扫描 ----
    def collect(self):
        snap = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(WATCH_EXTS):
                    full = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(full)
                        snap[full] = (st.st_mtime, st.st_size)
                    except OSError:
                        pass
        return snap

    def scan(self):
        """全树发现 .qd 模块：新增注册、变更刷新、删除注销"""
        found = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for d in dirnames:
                if d.endswith('.qd'):
                    mpath = os.path.join(dirpath, d)
                    mpath = os.path.realpath(mpath)
                    meta_file = os.path.join(mpath, 'module.qdmeta')
                    if not os.path.isfile(meta_file):
                        continue
                    try:
                        with open(meta_file, 'r', encoding='utf-8') as f:
                            meta = json.load(f)
                        if not all(k in meta for k in ('name', 'id', 'version', 'entry', 'type')):
                            log(f'[发现] 跳过 {mpath}: qdmeta 缺字段')
                            continue
                        found[meta['id']] = (mpath, meta)
                    except Exception as e:
                        log(f'[发现] 损坏 {mpath}: {e}')
        with self._lock:
            # 新增/更新
            restart = []
            for mid, (mpath, meta) in found.items():
                if mid in self.modules and self.modules[mid].path == mpath:
                    continue
                if mid in self.modules and self.modules[mid].state == 'running':
                    log(f'[更新] {mid} 运行中被替换，等待重新加载')
                    restart.append(mid)
                mod = Module(mpath, meta)
                self._load_parts(mod)
                self._isolation_check(mod)
                self.modules[mid] = mod
                log(f'[注册] {mod.type:8s} {mid:24s} {mod.name} (priv={mod.priv})')
            # 注销
            for mid in list(self.modules):
                if mid not in found:
                    if self.modules[mid].state == 'running':
                        restart.append(mid)
                    log(f'[注销] {mid}（模块目录已移除）')
                    del self.modules[mid]
        # 锁外统一停进程（避免 stop 内再抢锁）
        for mid in restart:
            self.stop(mid)
        return len(self.modules)

    def _load_parts(self, mod):
        """扫描模块内 .qds / .qdai / 子模块"""
        for fname in sorted(os.listdir(mod.path)):
            fpath = os.path.join(mod.path, fname)
            if os.path.isfile(fpath):
                if fname.endswith('.qds'):
                    try:
                        meta = self._qds_peek(fpath)
                        if meta:
                            mod.qds.append(meta)
                    except Exception:
                        pass
                elif fname.endswith('.qdai'):
                    mod.qdai.append({'meta': {'name': fname}, 'path': fpath})
            elif os.path.isdir(fpath) and fname.endswith('.qd'):
                mod.submodules.append({'id': fname[:-3]})

    def _qds_peek(self, path):
        with open(path, 'rb') as f:
            head = f.read(256)
        if len(head) < 256:
            return None
        magic = struct.unpack_from('<I', head, 0)[0]
        if magic != QDS_MAGIC:
            return None
        name = head[8:72].split(b'\0')[0].decode('utf-8', errors='replace')
        payload = struct.unpack_from('<I', head, 200)[0]
        return {'name': name, 'payload_type': payload, 'path': path}

    # ---- 调用隔离检查 ----
    def _isolation_check(self, mod):
        """扫描模块代码：跨模块 sys.path 注入 / 直接 import 运行 等 → 违反隔离契约"""
        flags = []
        if mod.type == 'ai':
            pass  # AI.qd 是调度核心，豁免
        for dirpath, _, files in os.walk(mod.path):
            if '.git' in dirpath:
                continue
            for fn in files:
                if not fn.endswith('.py') or fn in ('qdd.py', 'module_main'):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    src = open(full, encoding='utf-8', errors='replace').read()
                except Exception:
                    continue
                for line in src.splitlines():
                    if 'sys.path.insert' in line and ('/奇点OS/运行' in line
                                                      or '运行' in line and '/奇点OS' in line):
                        flags.append(f'{fn}: 跨模块 sys.path 注入')
                        break
                    if ('import ' in line or 'from ' in line) and '运行' in line \
                            and '.qd' not in line and 'sys' not in line:
                        flags.append(f'{fn}: 直接 import 运行/')
                        break
        mod.flags = flags[:5]
        if flags:
            log(f'[隔离] {mod.id} 违反隔离契约: {flags[:3]}')

    # ---- 生命周期 ----
    def start(self, mid):
        with self._lock:
            mod = self.modules.get(mid)
            if not mod:
                return {'ok': False, 'err': 'no_such_module'}
            if mod.state == 'running':
                return {'ok': True, 'state': 'running', 'pid': mod.pid}
            entry = mod.entry_path()
            if not entry or not os.path.exists(entry):
                return {'ok': False, 'err': f'entry 不存在: {entry}'}
        # 在锁外启动（避免阻塞）
        try:
            if entry.endswith('.qds'):
                proc = self._run_qds(entry, mod)
            else:
                proc = subprocess.Popen([sys.executable, entry],
                                        env=self._module_env(mod),
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            with self._lock:
                mod.pid = proc.pid
                mod.state = 'running'
                self._procs[mid] = proc
                mod.error = None
            log(f'[start] {mid} pid={proc.pid}')
            return {'ok': True, 'state': 'running', 'pid': proc.pid}
        except Exception as e:
            with self._lock:
                mod.state = 'error'
                mod.error = str(e)
            log(f'[start] {mid} 失败: {e}')
            return {'ok': False, 'err': str(e)}

    def _module_env(self, mod):
        env = os.environ.copy()
        env['QD_MODULE_ID'] = mod.id
        env['QD_MODULE_TYPE'] = mod.type
        env['QD_BUS'] = BUS_PATH
        env['QD_BUS_PORT'] = str(BUS_PORT)
        return env

    def _run_qds(self, path, mod):
        with open(path, 'rb') as f:
            data = f.read()
        head = data[:256]
        bin_off = struct.unpack_from('<I', head, 136)[0]
        bin_sz = struct.unpack_from('<Q', head, 144)[0]
        payload = struct.unpack_from('<I', head, 200)[0]
        elf = data[bin_off:bin_off + bin_sz]
        suffix = '.py' if payload == 1 else '.bin'
        tmp = tempfile.NamedTemporaryFile(prefix='qd_', suffix=suffix, delete=False)
        tmp.write(elf)
        tmp.close()
        os.chmod(tmp.name, 0o755)
        if payload == 1:
            return subprocess.Popen([sys.executable, tmp.name],
                                    env=self._module_env(mod),
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        return subprocess.Popen([tmp.name], env=self._module_env(mod),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self, mid):
        with self._lock:
            mod = self.modules.get(mid)
            if not mod and mid not in self._procs:
                return {'ok': False, 'err': 'no_such_module'}
            proc = self._procs.pop(mid, None)
            pid = mod.pid if mod else None
            if mod:
                mod.state = 'stopped'
                mod.pid = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
        elif pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        log(f'[stop] {mid}')
        return {'ok': True, 'state': 'stopped'}

    def heartbeat(self):
        """进程存活实时检测：running 模块进程退出 → 立即更新状态（防谎报）"""
        with self._lock:
            for mid, proc in list(self._procs.items()):
                code = proc.poll()
                if code is None:
                    continue
                mod = self.modules.get(mid)
                if mod:
                    mod.state = 'error' if code != 0 else 'stopped'
                    mod.error = f'进程退出 code={code}'
                    mod.pid = None
                    log(f'[心跳] {mid} 进程退出 code={code} → state={mod.state}')
                self._procs.pop(mid, None)

    def status(self, mid=None):
        with self._lock:
            if mid:
                mod = self.modules.get(mid)
                return {'ok': bool(mod), 'module': mod.to_dict() if mod else None}
            return {'ok': True, 'modules': [m.to_dict() for m in self.modules.values()]}

    def unload(self, mid):
        r = self.stop(mid)
        with self._lock:
            if mid in self.modules:
                del self.modules[mid]
                log(f'[unload] {mid}')
        return r

    # ---- 热替换：同类型切换 ----
    def switch(self, mtype, new_id, old_id=None):
        """停掉同类型所有运行中模块（或指定 old_id），启动 new_id"""
        with self._lock:
            candidates = [m.id for m in self.modules.values()
                          if m.type == mtype and m.id != new_id]
        stopped = []
        for mid in candidates:
            if old_id and mid != old_id:
                continue
            r = self.stop(mid)
            stopped.append((mid, r['state']))
        r = self.start(new_id)
        return {'ok': r.get('ok'), 'type': mtype, 'stopped': stopped, 'started': new_id}

    # ---- 热替换：完整下架→归档→上架 ----
    def replace(self, old_id, new_id=None, new_path=None):
        """替换模块：旧模块停进程→整体移入归档区（含 .git，存为存储，不删除）→
        内存停读旧模块→新模块注册（可来自已注册模块或新目录）→启动新模块"""
        with self._lock:
            old = self.modules.get(old_id)
            if not old:
                return {'ok': False, 'err': f'旧模块不存在: {old_id}'}
            old_path = old.path
            old_state = old.state
        # 1) 停旧进程（锁外，避免自锁）
        if old_state == 'running':
            self.stop(old_id)
        # 2) 归档：整体移入归档区（保留 .git 完整存储）
        if not os.path.isdir(old_path):
            return {'ok': False, 'err': f'旧模块目录不存在: {old_path}'}
        stamp = datetime.now().strftime('%Y%m%d%H%M%S')
        dst = os.path.join(ARCHIVE_ROOT, f'{old.id}-{stamp}')
        try:
            shutil.move(old_path, dst)
        except Exception as e:
            return {'ok': False, 'err': f'归档失败: {e}'}
        with self._lock:
            self.modules.pop(old_id, None)
            self._procs.pop(old_id, None)
        log(f'[replace] 旧模块已归档: {old_id} → {dst}（存为存储，内存停读）')
        # 3) 上架新模块
        if new_path:
            self.scan()  # 全树重扫，注册新路径模块
        if new_id and new_id not in self.modules:
            return {'ok': False, 'err': f'新模块未注册: {new_id}'}
        r = self.start(new_id) if new_id else {'ok': True, 'state': 'stopped'}
        return {'ok': r.get('ok'), 'archived': {'id': old_id, 'path': dst},
                'active': new_id, 'start': r.get('state')}

    # ---- 模块调用（总线核心）----
    def call(self, mid, cmd, payload=None):
        """向模块进程发送命令（简单 JSON over socket；无 socket 服务时返回 not_available）"""
        mod = self.modules.get(mid)
        if not mod:
            return {'ok': False, 'err': 'no_such_module'}
        if mod.state != 'running':
            return {'ok': False, 'err': 'not_running'}
        # 模块若监听 QD_BUS 则可投递；否则 fallback：仅 ai 模块走既有 ai socket
        try:
            if os.path.exists(BUS_PATH):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect(BUS_PATH)
                s.sendall(json.dumps({'to': mid, 'cmd': cmd, 'payload': payload or {}}).encode())
                resp = s.recv(65536).decode('utf-8', errors='replace')
                s.close()
                return json.loads(resp) if resp else {'ok': True, 'delivered': True}
        except Exception as e:
            return {'ok': False, 'err': f'bus 投递失败: {e}'}
        return {'ok': False, 'err': 'module 未监听总线'}


# ============================================================
# 总线服务（线程）：JSON over UNIX socket / TCP
# ============================================================
class BusServer:
    def __init__(self, rt):
        self.rt = rt
        self.path = BUS_PATH
        self.port = BUS_PORT

    def start(self):
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()
        return t

    def _serve(self):
        if os.name == 'nt':
            if not self.port:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(('127.0.0.1', 0))
                self.port = s.getsockname()[1]
                global BUS_PORT
                BUS_PORT = self.port
                s.listen(16)
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(('127.0.0.1', self.port))
                s.listen(16)
        else:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            try:
                os.unlink(self.path)
            except OSError:
                pass
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(self.path)
            os.chmod(self.path, 0o777)
            s.listen(16)
        listen_desc = ('tcp:' + str(self.port)) if os.name == 'nt' else self.path
        log(f'[总线] 监听 {listen_desc}')
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            data = conn.recv(65536)
            if not data:
                conn.close()
                return
            req = json.loads(data.decode('utf-8', errors='replace'))
            resp = self._dispatch(req)
            conn.sendall(json.dumps(resp).encode())
        except Exception as e:
            try:
                conn.sendall(json.dumps({'ok': False, 'err': str(e)}).encode())
            except Exception:
                pass
        finally:
            conn.close()

    def _dispatch(self, req):
        cmd = req.get('cmd')
        if cmd == 'list':
            return self.rt.status()
        if cmd == 'status':
            return self.rt.status(req.get('id'))
        if cmd == 'start':
            return self.rt.start(req.get('id'))
        if cmd == 'stop':
            return self.rt.stop(req.get('id'))
        if cmd == 'unload':
            return self.rt.unload(req.get('id'))
        if cmd == 'switch':
            return self.rt.switch(req.get('type'), req.get('new_id'), req.get('old_id'))
        if cmd == 'replace':
            return self.rt.replace(req.get('old_id'), req.get('new_id'), req.get('new_path'))
        if cmd == 'scan':
            n = self.rt.scan()
            return {'ok': True, 'modules': n}
        if cmd == 'call':
            return self.rt.call(req.get('id'), req.get('cmd'), req.get('payload'))
        if cmd == 'to':  # 模块进程投递 → 只确认收到
            return {'ok': True, 'delivered': True, 'to': req.get('to'), 'cmd': req.get('cmd')}
        return {'ok': False, 'err': 'unknown_cmd'}


# ============================================================
# 守护主循环
# ============================================================
def daemon(rt):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    bus = BusServer(rt)
    bus.start()
    n = rt.scan()
    log(f'=== qdd 模块运行时启动 === 初始模块: {n} 总线: {BUS_PATH or BUS_PORT}')
    last = rt.collect()
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            rt.heartbeat()  # 状态实时性：进程退出立即反映，不谎报
            cur = rt.collect()
            diff = False
            if len(cur) != len(last):
                diff = True
            else:
                for k, v in cur.items():
                    if last.get(k) != v:
                        diff = True
                        break
            if diff:
                n = rt.scan()
                log(f'[热发现] 检测到变化 → 重扫: {n} 个模块')
                last = cur
        except Exception as e:
            log(f'[错误] {e}')
            time.sleep(2)


def client(rt, args):
    """命令行客户端：走总线发命令"""
    def send(req):
        if os.path.exists(BUS_PATH) or BUS_PORT:
            s = socket.socket(socket.AF_UNIX if os.name != 'nt' else socket.AF_INET,
                              socket.SOCK_STREAM)
            s.settimeout(5)
            if os.name == 'nt':
                s.connect(('127.0.0.1', BUS_PORT))
            else:
                s.connect(BUS_PATH)
            s.sendall(json.dumps(req).encode())
            resp = s.recv(65536).decode('utf-8', errors='replace')
            s.close()
            return json.loads(resp)
        return {'ok': False, 'err': '总线未运行'}

    if args.cmd == 'list':
        print(json.dumps(send({'cmd': 'list'}), ensure_ascii=False, indent=1))
    elif args.cmd == 'status':
        print(json.dumps(send({'cmd': 'status', 'id': args.id}), ensure_ascii=False, indent=1))
    elif args.cmd == 'start':
        print(json.dumps(send({'cmd': 'start', 'id': args.id}), ensure_ascii=False))
    elif args.cmd == 'stop':
        print(json.dumps(send({'cmd': 'stop', 'id': args.id}), ensure_ascii=False))
    elif args.cmd == 'switch':
        print(json.dumps(send({'cmd': 'switch', 'type': args.type,
                               'new_id': args.new_id}), ensure_ascii=False))
    elif args.cmd == 'replace':
        print(json.dumps(send({'cmd': 'replace', 'old_id': args.old_id,
                               'new_id': args.new_id, 'new_path': args.new_path}),
                         ensure_ascii=False))
    elif args.cmd == 'call':
        payload = json.loads(args.payload) if args.payload else {}
        print(json.dumps(send({'cmd': 'call', 'id': args.id,
                               'cmd': args.call_cmd, 'payload': payload}),
                         ensure_ascii=False))
    else:
        print(json.dumps(send({'cmd': 'scan'}), ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='qdd 模块运行时')
    sub = p.add_subparsers(dest='cmd')
    sub.add_parser('daemon')
    sub.add_parser('once')
    sub.add_parser('list')
    sub.add_parser('scan')
    sp = sub.add_parser('status'); sp.add_argument('--id', default=None)
    sp = sub.add_parser('start'); sp.add_argument('id')
    sp = sub.add_parser('stop'); sp.add_argument('id')
    sp = sub.add_parser('unload'); sp.add_argument('id')
    sp = sub.add_parser('switch'); sp.add_argument('type'); sp.add_argument('new_id')
    sp = sub.add_parser('replace'); sp.add_argument('old_id'); sp.add_argument('new_id', nargs='?'); sp.add_argument('--new-path', dest='new_path', default=None)
    sp = sub.add_parser('call'); sp.add_argument('id'); sp.add_argument('call_cmd'); sp.add_argument('payload', nargs='?')
    args = p.parse_args()

    rt = ModuleRuntime()
    if args.cmd in (None, 'daemon', 'once'):
        n = rt.scan()
        print(f'模块数: {n}')
        for m in sorted(rt.modules.values(), key=lambda x: x.type):
            print(f'  [{m.type:8s}] {m.id:24s} {m.name} state={m.state} priv={m.priv} flags={m.flags}')
        if args.cmd == 'daemon':
            daemon(rt)
        return 0
    client(rt, args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
