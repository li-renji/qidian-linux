#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
status_panel.py —— 奇点OS 系统状态面板（GTK 原生，深色中文）
显示：systemd 服务、AI 服务/推理引擎、资源占用、最近体检结果。
替代已删除的 Web 壳状态页（localhost:8080/qd-status.html）。
"""
import os
import subprocess
import json

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Pango

BG = '#10131a'
PANEL = '#171b24'
TEXT = '#e2e8f0'
DIM = '#8fa3bf'
OK = '#68d391'
ERR = '#fc8181'
ACCENT = '#63b3ed'

SERVICES = ['qd-os', 'ai-qd', 'qd-hotload', 'qd-power']
IPC_SOCKET = '/tmp/qd_ai.sock'
INFER = '/奇点OS/AI.qd/推理引擎.qd/推理引擎.qds'
RUN = '/奇点OS/运行/qd-run.py'
HEALTH_LOG = '/奇点OS/运行/logs/health_report.log'


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ''


def hex_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def GdkColor(r, g, b):
    from gi.repository import Gdk
    return Gdk.RGBA(r, g, b, 1.0)


class StatusPanel:
    def __init__(self):
        self.win = Gtk.Window()
        self.win.set_title('奇点OS 系统状态')
        self.win.set_default_size(660, 540)
        self.win.set_position(Gtk.WindowPosition.CENTER)
        self.win.override_background_color(Gtk.StateFlags.NORMAL, GdkColor(*hex_rgb(BG)))
        self.win.connect('destroy', Gtk.main_quit)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.override_background_color(Gtk.StateFlags.NORMAL, GdkColor(*hex_rgb(BG)))
        self.win.add(box)

        # 标题
        title = Gtk.Label(label='奇点OS 系统状态')
        title.override_color(Gtk.StateFlags.NORMAL, GdkColor(*hex_rgb(ACCENT)))
        title.override_font(Pango.FontDescription('Sans 16'))
        box.pack_start(title, False, False, 0)

        sub = Gtk.Label(label='服务 · AI · 资源 · 体检（每 10 秒自动刷新）')
        sub.override_color(Gtk.StateFlags.NORMAL, GdkColor(*hex_rgb(DIM)))
        box.pack_start(sub, False, False, 0)

        # 状态内容（等宽字体，一次性渲染）
        self.body = Gtk.Label()
        self.body.set_xalign(0)
        self.body.set_selectable(True)          # 可长按复制
        self.body.override_color(Gtk.StateFlags.NORMAL, GdkColor(*hex_rgb(TEXT)))
        self.body.override_font(Pango.FontDescription('Monospace 12'))
        self.body.set_line_wrap(True)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.body)
        box.pack_start(sw, True, True, 0)

        # 底部按钮
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh = Gtk.Button(label='立即刷新')
        refresh.override_background_color(Gtk.StateFlags.NORMAL, GdkColor(*hex_rgb(PANEL)))
        refresh.override_color(Gtk.StateFlags.NORMAL, GdkColor(*hex_rgb(ACCENT)))
        refresh.connect('clicked', lambda *a: self.refresh())
        close = Gtk.Button(label='关闭')
        close.connect('clicked', lambda *a: self.win.destroy())
        btn_row.pack_end(close, False, False, 0)
        btn_row.pack_end(refresh, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        self.win.show_all()
        self.refresh()
        GLib.timeout_add_seconds(10, self.refresh)

    # ---------- 数据采集 ----------
    def _services(self):
        lines = []
        for svc in SERVICES:
            st = run(['systemctl', 'is-active', svc])
            dot = '●' if st == 'active' else '○'
            color = OK if st == 'active' else ERR
            lines.append((dot, color, f'{svc:<14} {st}'))
        return lines

    def _ai(self):
        lines = []
        alive = False
        try:
            import socket
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(IPC_SOCKET)
            s.sendall(json.dumps({'cmd': 'ping'}).encode())
            alive = json.loads(s.recv(1024).decode()).get('status') == 'alive'
            s.close()
        except Exception:
            alive = False
        lines.append(('●' if alive else '○',
                      OK if alive else ERR,
                      f'意图/规则引擎（ai_service） {"在线" if alive else "未响应"}'))
        # 推理引擎
        try:
            r = subprocess.run([RUN, INFER, 'config'],
                               capture_output=True, text=True, timeout=15)
            d = json.loads(r.stdout).get('data', {})
            if d.get('configured'):
                lines.append(('●', OK,
                              f"推理引擎 云端已配置（{d.get('provider','?')} · {d.get('model','?')}）"))
            else:
                lines.append(('○', ERR, '推理引擎 云端未配置（意图窗口「⚙ 云端设置」可配）'))
        except Exception:
            lines.append(('○', ERR, '推理引擎 读取失败'))
        return lines

    def _resources(self):
        lines = []
        mem = run(['bash', '-c', "free -m | awk 'NR==2{printf \\\"%d MB / %d MB\\\", $3, $2}'"])
        disk = run(['bash', '-c', "df -h / | awk 'NR==2{printf \\\"%s 已用 %s\\\", $5, $3}'"])
        load = run(['bash', '-c', "uptime | sed 's/.*load average: //'"])
        lines.append((DIM, f'内存    {mem}'))
        lines.append((DIM, f'磁盘 /  {disk}'))
        lines.append((DIM, f'负载    {load}'))
        return lines

    def _health(self):
        lines = []
        try:
            with open(HEALTH_LOG, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read().strip()
            # 累积日志：只取最后一份报告
            marker = '===== 奇点OS 底层健康检查'
            idx = text.rfind(marker)
            last = text[idx:] if idx >= 0 else text
            passes = last.count('[PASS]')
            fails = last.count('[FAIL]')
            ts = ''
            for ln in last.split('\n'):
                if '健康检查' in ln:
                    ts = ln.strip().replace('=====', '').strip()
            lines.append((OK if fails == 0 else ERR,
                          f'{ts} · C1-C9 {passes}/9 通过' + ('' if fails == 0 else f'，{fails} 项失败')))
        except Exception:
            lines.append((ERR, '体检日志未找到'))
        return lines

    # ---------- 渲染 ----------
    def refresh(self, *a):
        parts = []
        svc = self._services()
        parts.append('■ 服务状态')
        for dot, color, text in svc:
            parts.append('  ' + text)
        parts.append('')
        ai = self._ai()
        parts.append('■ AI 服务')
        for dot, color, text in ai:
            parts.append('  ' + text)
        parts.append('')
        res = self._resources()
        parts.append('■ 资源占用')
        for color, text in res:
            parts.append('  ' + text)
        parts.append('')
        hl = self._health()
        parts.append('■ 最近体检')
        for color, text in hl:
            parts.append('  ' + text)
        self.body.set_text('\n'.join(parts))
        return False


def main():
    StatusPanel()
    Gtk.main()


if __name__ == '__main__':
    main()
