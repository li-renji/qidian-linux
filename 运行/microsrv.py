#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
microsrv.py - 微服务.qd 微型服务器（低功耗常驻，内存数MB）
基于规范 27 章：微服务模块承载远程桌面/多屏协同/文件传输
本文件实现基础框架：TCP 监听 + 请求路由 + 状态上报

用法:
  python3 microsrv.py daemon    # 守护模式（默认端口 19230）
  python3 microsrv.py status    # 查看状态
"""
import os
import sys
import json
import time
import socket
import signal
import threading
import argparse
from datetime import datetime

DEFAULT_PORT = 19230
STATUS_FILE = '/tmp/qd_microsrv_status.json'


class MicroSrv:
    def __init__(self, port=DEFAULT_PORT):
        self.port = port
        self.running = True
        self.services = {}  # 已注册子服务

    def register(self, name, info):
        self.services[name] = info

    def _handle(self, conn, addr):
        try:
            conn.settimeout(10)
            data = conn.recv(4096)
            if not data:
                return
            try:
                req = json.loads(data.decode('utf-8'))
            except json.JSONDecodeError:
                req = {'cmd': 'raw', 'data': data.decode('utf-8', errors='replace')}
            resp = self._route(req)
            conn.sendall(json.dumps(resp, ensure_ascii=False).encode('utf-8'))
        except Exception as e:
            try:
                conn.sendall(json.dumps({'error': str(e)}).encode('utf-8'))
            except Exception:
                pass
        finally:
            conn.close()

    def _route(self, req):
        cmd = req.get('cmd', '')
        if cmd == 'ping':
            return {'ok': True, 'service': 'microsrv', 'version': '0.2.0',
                    'time': datetime.now().isoformat()}
        if cmd == 'services':
            return {'ok': True, 'services': self.services}
        if cmd == 'register':
            name = req.get('name', '')
            info = req.get('info', {})
            if not name:
                return {'ok': False, 'error': 'register 需要 name'}
            self.register(name, info)
            return {'ok': True, 'registered': name, 'services': self.services}
        if cmd == 'status':
            return {'ok': True, 'running': self.running, 'uptime': self.uptime}
        if cmd == 'remote_desktop':
            return self._remote_desktop(req)
        if cmd == 'multi_screen':
            return self._multi_screen(req)
        if cmd == 'file_transfer':
            return self._file_transfer(req)
        return {'ok': False, 'error': f'unknown cmd: {cmd}'}

    def start(self):
        self.uptime = time.time()
        # 注册真实子服务（替代空占位）
        self.register('file_transfer', {
            'version': '0.1.0', 'enabled': True,
            'protocol': 'qd-microsrv-ft', 'note': '通过 microsrv TCP 通道传输文件',
        })
        self.register('remote_desktop', {
            'version': '0.1.0', 'enabled': True,
            'protocol': 'VNC/X11', 'note': '远程桌面探测与接管（VNC 端口 5900）',
        })
        self.register('multi_screen', {
            'version': '0.1.0', 'enabled': True,
            'protocol': 'qd-cast', 'note': '多屏协同（X11 多显示器枚举）',
        })
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('0.0.0.0', self.port))
        srv.listen(16)
        print('=== microsrv 微服务启动 ===')
        print(f'版本: 0.2.0')
        print(f'监听: 0.0.0.0:{self.port}')
        print(f'PID: {os.getpid()}')
        print('等待连接...')
        self._write_status()
        while self.running:
            try:
                conn, addr = srv.accept()
                threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()
            except Exception:
                break

    def _write_status(self):
        st = {
            'service': 'microsrv',
            'version': '0.2.0',
            'port': self.port,
            'pid': os.getpid(),
            'state': 'running',
            'time': datetime.now().isoformat(),
        }
        with open(STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=2)


    def _remote_desktop(self, req):
        """远程桌面：探测 VNC 服务并返回会话信息"""
        import subprocess as sp
        act = req.get('action', 'status')
        info = {'enabled': True, 'protocol': 'VNC/X11'}
        # 探测 VNC 进程与端口
        try:
            r = sp.run(['pgrep', '-f', 'x11vnc|vncserver'], capture_output=True, text=True, timeout=5)
            info['vnc_pids'] = r.stdout.split() if r.returncode == 0 else []
        except Exception:
            info['vnc_pids'] = []
        try:
            r = sp.run(['ss', '-tlnp'], capture_output=True, text=True, timeout=5)
            info['vnc_ports'] = [l.split()[3] for l in r.stdout.splitlines()
                                 if ':590' in l or ':5900' in l]
        except Exception:
            info['vnc_ports'] = []
        try:
            r = sp.run(['ss', '-tlnp'], capture_output=True, text=True, timeout=5)
            info['display'] = ':0'
        except Exception:
            pass
        if act == 'start':
            # 启动 x11vnc（若已安装且未运行）
            if not info['vnc_pids']:
                chk = sp.run(['which', 'x11vnc'], capture_output=True, text=True, timeout=5)
                if chk.returncode == 0:
                    sp.Popen(['x11vnc', '-display', ':0', '-forever', '-shared',
                              '-nopw', '-rfbport', '5900'],
                             stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                    info['started'] = True
                    info['note'] = 'x11vnc 已在 :5900 启动'
                else:
                    info['started'] = False
                    info['note'] = '未安装 x11vnc（apt install x11vnc）'
            else:
                info['started'] = True
                info['note'] = 'VNC 已在运行'
        return {'ok': True, 'data': info}

    def _multi_screen(self, req):
        """多屏协同：枚举 X11 显示器并给出投屏目标"""
        import subprocess as sp
        act = req.get('action', 'status')
        info = {'enabled': True, 'protocol': 'qd-cast'}
        try:
            r = sp.run(['xrandr'], capture_output=True, text=True, timeout=5,
                       env=dict(os.environ, DISPLAY=req.get('display', ':0')))
            screens = []
            for l in r.stdout.splitlines():
                if ' connected' in l:
                    parts = l.split()
                    screens.append({'name': parts[0],
                                    'mode': parts[2] if len(parts) > 2 else '?'})
            info['screens'] = screens
            info['count'] = len(screens)
        except Exception as e:
            info['screens'] = []
            info['error'] = str(e)
        return {'ok': True, 'data': info}

    def _file_transfer(self, req):
        """文件传输：list 列出可访问目录 / recv 读取文件内容（base64）

        支持：action=list [path] | action=recv path=<绝对路径>"""
        import base64
        act = req.get('action', 'list')
        if act == 'list':
            path = req.get('path', '/奇点OS')
            if not os.path.isdir(path):
                return {'ok': False, 'error': f'目录不存在: {path}'}
            try:
                entries = sorted(os.listdir(path))
                items = []
                for e in entries[:100]:
                    full = os.path.join(path, e)
                    try:
                        st = os.stat(full)
                        items.append({'name': e,
                                      'type': 'dir' if os.path.isdir(full) else 'file',
                                      'size': st.st_size})
                    except OSError:
                        pass
                return {'ok': True, 'data': {'path': path, 'items': items,
                                             'count': len(items)}}
            except Exception as e:
                return {'ok': False, 'error': str(e)}
        if act == 'recv':
            path = req.get('path', '')
            if not path or not os.path.isfile(path):
                return {'ok': False, 'error': f'文件不存在: {path}'}
            try:
                with open(path, 'rb') as f:
                    raw = f.read()
                if len(raw) > 2 * 1024 * 1024:
                    return {'ok': False, 'error': f'文件过大（>{2*1024*1024}B），仅支持 2MB 内直传'}
                return {'ok': True, 'data': {
                    'path': path,
                    'size': len(raw),
                    'b64': base64.b64encode(raw).decode('ascii'),
                }}
            except Exception as e:
                return {'ok': False, 'error': str(e)}
        if act == 'send':
            path = req.get('path', '')
            b64 = req.get('b64', '')
            if not path or not b64:
                return {'ok': False, 'error': 'send 需要 path + b64'}
            try:
                raw = base64.b64decode(b64)
                os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(raw)
                return {'ok': True, 'data': {'path': path, 'size': len(raw)}}
            except Exception as e:
                return {'ok': False, 'error': str(e)}
        return {'ok': False, 'error': f'未知 file_transfer action: {act}'}

def main():
    parser = argparse.ArgumentParser(description='microsrv 微服务')
    parser.add_argument('cmd', nargs='?', default='daemon',
                        choices=['daemon', 'status'])
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if args.cmd == 'status':
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                print(json.dumps(json.load(f), ensure_ascii=False, indent=2))
        else:
            print('microsrv: 未运行')
        return 0

    srv = MicroSrv(args.port)

    def on_signal(sig, frame):
        srv.running = False
        print('\nmicrosrv 停止')
        sys.exit(0)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    srv.start()


if __name__ == '__main__':
    main()

