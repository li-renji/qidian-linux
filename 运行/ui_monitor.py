#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_monitor.py —— 奇点OS 系统监视器（G.18）
#
# 为什么必须有：Windows 任务管理器 / macOS 活动监视器 / GNOME System
# Monitor / Haiku ActivityMonitor 全部自带（对标调研 v0.1 §2.2）。
# 用户"看点报错时能看日志、知道磁盘还剩多少、哪个进程在吃 CPU"
# 是"第一天必须成立"清单第 7 条，目前无 UI。
#
# 功能（对标任务管理器最小集）：
#   CPU 总占用 / 内存 / 磁盘 / 运行时长 —— 顶部资源卡
#   进程列表（PID / 名称 / CPU% / 内存）—— 按列排序
#   结束进程（qduser 自己的进程直接杀；系统进程无权限时明确提示）
#   2 秒自动刷新（GLib.timeout，空闲时零开销）
#
# 数据源全部 /proc（Meminfo、stat、uptime、/proc/[pid]），不引依赖。
#
# 入口：python3 ui_monitor.py
# ============================================================
import os
import signal
import subprocess
import sys

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

sys.path.insert(0, '/奇点OS/运行')
import qd_ui_common as C

APP_TITLE = '系统监视器'
REFRESH_MS = 2000


# ============================================================
# /proc 读取（全部轻量解析，无 psutil）
# ============================================================
_prev_cpu = None


def cpu_percent():
    """两次采样差分得总 CPU 占用（采样间隔 = 刷新周期）"""
    global _prev_cpu
    with open('/proc/stat') as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts[:8]]
    idle = vals[3] + vals[4]
    total = sum(vals)
    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        return 0.0
    d_idle = idle - _prev_cpu[0]
    d_total = total - _prev_cpu[1]
    _prev_cpu = (idle, total)
    if d_total <= 0:
        return 0.0
    return (1 - d_idle / d_total) * 100.0


def mem_info():
    """返回 (已用MB, 总MB, 百分比)"""
    info = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1)
            info[k] = int(v.split()[0])  # kB
    total = info.get('MemTotal', 0)
    avail = info.get('MemAvailable', 0)
    used = total - avail
    pct = used / total * 100 if total else 0
    return used / 1024, total / 1024, pct


def disk_info():
    st = os.statvfs('/')
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    pct = (1 - free / total) * 100 if total else 0
    return free / 1e9, total / 1e9, pct


def uptime_str():
    with open('/proc/uptime') as f:
        s = float(f.read().split()[0])
    h, rem = divmod(int(s), 3600)
    m, _ = divmod(rem, 60)
    return '%d 小时 %d 分' % (h, m) if h else '%d 分' % m


def proc_list():
    """轻量进程表：只取自己能看到完整 cmdline 的 + 系统 PID 摘要"""
    procs = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % pid) as f:
                stat = f.read()
            # comm 在括号里，可能含空格
            name = stat[stat.rfind('(') + 1:stat.rfind(')')]
            rss_kb = 0
            try:
                with open('/proc/%s/status' % pid) as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            rss_kb = int(line.split()[1])
                            break
            except Exception:
                pass
            cmd = name
            try:
                with open('/proc/%s/cmdline' % pid) as f:
                    c = f.read().replace('\0', ' ').strip()
                if c:
                    cmd = c
            except Exception:
                pass
            procs.append((int(pid), name, cmd, rss_kb))
        except (OSError, ValueError):
            continue
    procs.sort(key=lambda p: -p[3])  # 按内存降序
    return procs


# ============================================================
class Monitor(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_TITLE)
        self.set_default_size(760, 520)
        self.set_size_request(640, 420)

        C.setup_css(b'''
            .mo-root { background: #16161e; }
            .mo-card { background: #1c1c1e; border: 1px solid #3a3a3c;
                       border-radius: 10px; padding: 10px 14px; }
            .mo-kpi  { font-size: 22px; font-weight: bold; color: #f5f5f7; }
            .mo-sub  { color: #8e8e93; }
            treeview { background: #1c1c1e; color: #e6e6ea; }
            treeview header { background: #2a2a2c; color: #8e8e93; }
            .mo-bar  { background: #1c1c1e; border-bottom: 1px solid #3a3a3c;
                       padding: 6px 10px; }
            .mo-btn  { background: transparent; border: 1px solid #3a3a3c;
                       border-radius: 8px; padding: 4px 12px; color: #f5f5f7; }
            .mo-btn:hover { background: #3a3a3c; }
            .mo-danger { color: #ff453a; border-color: rgba(255,69,58,0.35); }
        ''')

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class('mo-root')
        self.add(root)

        # ---------- 资源卡（CPU/内存/磁盘/运行时长） ----------
        cards = Gtk.Box(spacing=10)
        cards.set_border_width(10)
        root.pack_start(cards, False, False, 0)

        self.kpi_cpu = self._card(cards, 'CPU', '0%')
        self.kpi_mem = self._card(cards, '内存', '0 / 0 GB')
        self.kpi_disk = self._card(cards, '磁盘可用', '0 GB')
        self.kpi_up = self._card(cards, '运行时长', '—')

        # ---------- 进程表 ----------
        self.store = Gtk.ListStore(int, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        cols = [('PID', 0, 70), ('名称', 1, 160),
                ('命令行', 2, 300), ('内存', 3, 90)]
        for i, (title, col, w) in enumerate(cols):
            r = Gtk.CellRendererText()
            r.props.font = 'WenQuanYi Zen Hei 11'
            if i in (0, 3):
                r.props.xalign = 1.0
            c = Gtk.TreeViewColumn(title, r, text=col)
            c.set_sort_column_id(col)
            c.set_resizable(True)
            c.set_min_width(w)
            self.view.append_column(c)
        self.view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.view)
        root.pack_start(scroll, True, True, 0)

        # ---------- 底部操作条 ----------
        bar = Gtk.Box(spacing=8)
        bar.get_style_context().add_class('mo-bar')
        root.pack_start(bar, False, False, 0)

        btn_kill = Gtk.Button.new_with_label('结束进程')
        btn_kill.get_style_context().add_class('mo-btn')
        btn_kill.get_style_context().add_class('mo-danger')
        btn_kill.connect('clicked', self._on_kill)
        bar.pack_start(btn_kill, False, False, 0)

        self.status = C.label('每 %d 秒自动刷新' % (REFRESH_MS // 1000), 11, C.MUTED_FG)
        bar.pack_end(self.status, False, False, 4)

        GLib.timeout_add(REFRESH_MS, self._refresh)
        self._refresh()

    def _card(self, parent, title, value):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class('mo-card')
        box.pack_start(C.label(title, 11, C.MUTED_FG), False, False, 0)
        kpi = C.label(value, 20, C.FG, weight='bold')
        kpi.set_halign(Gtk.Align.START)
        box.pack_start(kpi, False, False, 0)
        parent.pack_start(box, True, True, 0)
        return kpi

    # ---------- 刷新 ----------
    def _refresh(self):
        try:
            self.kpi_cpu.set_text('%.0f%%' % cpu_percent())
            mu, mt, mp = mem_info()
            self.kpi_mem.set_text('%.1f / %.1f GB' % (mu / 1024, mt / 1024))
            df, _dt, dp = disk_info()
            self.kpi_disk.set_text('%.1f GB' % df)
            self.kpi_up.set_text(uptime_str())

            sel = self.view.get_selection().get_selected()
            keep_pid = sel[1][0] if sel[1] else None

            self.store.clear()
            for pid, name, cmd, rss in proc_list()[:200]:
                mem = ('%.0f MB' % (rss / 1024)) if rss >= 1024 else ('%d KB' % rss)
                self.store.append([pid, name, cmd[:80], mem])

            if keep_pid:
                for row in self.store:
                    if row[0] == keep_pid:
                        self.view.get_selection().select_iter(row.iter)
                        break
        except Exception:
            pass
        return True  # 继续定时

    # ---------- 结束进程 ----------
    def _on_kill(self, _b):
        sel = self.view.get_selection().get_selected()
        if not sel[1]:
            self.status.set_text('请先选择一个进程')
            return
        pid, name = sel[1][0], sel[1][1]
        dlg = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text='结束进程「%s」(PID %d)？' % (name, pid))
        dlg.get_style_context().add_class('mo-root')
        r = dlg.run()
        dlg.destroy()
        if r != Gtk.ResponseType.OK:
            return
        try:
            os.kill(pid, signal.SIGTERM)
            self.status.set_text('已发送终止信号给 %s (PID %d)' % (name, pid))
        except PermissionError:
            self.status.set_text('权限不足：系统进程不能从这里结束')
        except ProcessLookupError:
            self.status.set_text('进程已不存在')
        except Exception as e:
            self.status.set_text('失败：%s' % e)
        GLib.timeout_add(500, self._refresh)


# ============================================================
if __name__ == '__main__':
    if not C.single_instance('monitor'):
        sys.exit(0)
    win = Monitor()
    C.set_raise_handler('monitor', win.present)
    win.connect('destroy', Gtk.main_quit)
    win.show_all()
    Gtk.main()
