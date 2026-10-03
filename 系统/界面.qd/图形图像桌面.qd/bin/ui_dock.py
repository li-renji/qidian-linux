#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_dock.py V6 —— 快捷栏（TreeSolo 原型逐像素复刻）
# 蓝本：ui_design/pages/index.html #dock（L584-603 + CSS L764-769）
#   · bottom-4 居中 · h-16(64) · px-3(12) · gap-1.5(6) · rounded-2xl(16)
#   · bg card/90 · border border/40
#   · launcher(layout-grid, primary) | 竖线 | 终端/文件/浏览器/设置(20px,fg)
#   · 按钮 44x44 rounded-xl(12) · hover 上浮 6px + bg muted · active 回落
#   · 点击 → toast + 真实启动；右键保留；动态任务栏归灵动岛圆球
# ============================================================
import gi, os, sys, json, socket, shlex, threading, subprocess
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib
import qd_ui_common as C

# 应用源不再是硬编码：内置应用 + 固定项（安装器/终端/设置）+ 用户新装的应用
# 应用中心装完软件后经 socket 通知 reload，Dock 与桌面图标同步出现新图标

LIFT = 6     # hover translateY(-6px)
STEP = 2     # tween 步长 px
MS = 55      # tween 间隔（约 165ms 完成三步，贴合原型 160ms ease）


class Dock(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_decorated(False)
        self.set_app_paintable(True)
        # G.21 修黑框：RGBA visual（24 位窗口上 CSS transparent = 黑色）
        rgba = Gdk.Screen.get_default().get_rgba_visual()
        if rgba:
            self.set_visual(rgba)

        C.setup_css(b'''
            * { background: transparent; }
            .dock { background: rgba(28,28,30,230); border: 1px solid rgba(58,58,58,102);
                    border-radius: 16px; }
            .dock-item { background: transparent; border: none; border-radius: 12px; }
            .dock-item.hover { background: rgba(58,58,58,180); }
        ''')

        self.fixed = Gtk.Fixed()
        self.add(self.fixed)
        self.bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)  # gap-1.5
        self.bar.get_style_context().add_class('dock')
        self.bar.set_margin_start(12)   # px-3
        self.bar.set_margin_end(12)

        # launcher（主色图标，切换桌面图标显隐——抽屉已移除，规范 14 章）
        launcher = self._mk_button('layout-grid', C.PRIMARY, '桌面图标')
        launcher.connect('button-press-event',
                         lambda w, e: (C.send_icons({'cmd': 'toggle'}), False)[1])
        self._pack(launcher)
        self._add_sep()
        # 应用项
        self.btns = {}
        self._build_apps()

        self.fixed.put(self.bar, 4, 4)
        self.show_all()
        GLib.timeout_add(300, self._position)
        GLib.timeout_add_seconds(2, lambda: (self._position(), True)[1])
        threading.Thread(target=self._serve, daemon=True).start()

    def _build_apps(self):
        """按当前应用源重建按钮（安装新软件后 reload 调用）"""
        for btn in list(self.btns):
            try:
                btn.destroy()
            except Exception:
                pass
        self.btns.clear()
        for app in C.dock_items():
            btn = self._mk_button(app['icon'], C.FG, app['name'])
            btn.set_margin_top(LIFT)          # 常态下移 6px，hover 升回 0 = 上浮
            btn.connect('button-press-event',
                        lambda w, e, a=app: self._activate(w, e, a))
            btn.connect('enter-notify-event', self._on_enter)
            btn.connect('leave-notify-event', self._on_leave)
            self._pack(btn)
            self.btns[btn] = app
        self.show_all()

    def _serve(self):
        """socket：接收 reload（应用中心装完软件后刷新 Dock）"""
        if os.path.exists(C.DOCK_SOCK):
            try:
                os.remove(C.DOCK_SOCK)
            except OSError:
                pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(C.DOCK_SOCK)
        srv.listen(4)
        while True:
            try:
                conn, _ = srv.accept()
                data = conn.recv(4096).decode()
                conn.close()
                if json.loads(data).get('cmd') == 'reload':
                    GLib.idle_add(self._reload)
            except Exception:
                import time
                time.sleep(0.5)

    def _reload(self):
        self._build_apps()
        GLib.timeout_add(200, self._position)
        return False

    # ---------- 结构 ----------
    def _pack(self, w):
        self.bar.pack_start(w, False, False, 0)

    def _mk_button(self, icon, color, tip):
        btn = Gtk.Button()
        btn.set_size_request(C.DOCK_ITEM, C.DOCK_ITEM)
        btn.get_style_context().add_class('dock-item')
        btn.set_tooltip_text(tip)
        btn.add(C.icon_image(icon, C.DOCK_ICON, color))
        return btn

    def _add_sep(self):
        sep = Gtk.DrawingArea()
        sep.set_size_request(1, 24)   # w-px h-6
        sep.set_margin_start(4)       # mx-1
        sep.set_margin_end(4)

        def on_draw(_, cr):
            cr.set_source_rgba(58 / 255, 58 / 255, 60 / 255, 0.6)
            cr.rectangle(0, 0, 1, 24)
            cr.fill()
            return False
        sep.connect('draw', on_draw)
        self._pack(sep)

    # ---------- hover 上浮 tween（原型 transition 160ms） ----------
    # 修复：每按钮只保留一条动画链（source id 挂按钮上），
    # 新 tween 先移除旧链——此前多条链互相打架永不停止，CPU 长期 8%+
    def _tween(self, btn, target, step_ms=MS):
        old = getattr(btn, '_tween_src', None)
        if old is not None:
            GLib.source_remove(old)
        cur = btn.get_margin_top()
        if cur == target:
            btn._tween_src = None
            return
        nxt = cur - STEP if target < cur else cur + STEP
        btn.set_margin_top(max(0, min(LIFT, nxt)))
        if btn.get_margin_top() != target:
            btn._tween_src = GLib.timeout_add(step_ms, self._tween, btn, target)
        else:
            btn._tween_src = None

    def _on_enter(self, btn, e):
        btn.get_style_context().add_class('hover')
        self._tween(btn, 0)
        return False

    def _on_leave(self, btn, e):
        btn.get_style_context().remove_class('hover')
        self._tween(btn, LIFT)
        return False

    # ---------- 点击 ----------
    def _activate(self, btn, e, app=None):
        app = app or self.btns.get(btn)
        if not app:
            return False
        name, cmd = app['name'], app.get('cmd')
        print('[dock] activate %s (cmd=%s)' % (app.get('id'), bool(cmd)), flush=True)
        # G.16 用户令：不再弹"启动 X"——窗口自己弹出来就是反馈
        if cmd:
            try:
                subprocess.Popen(shlex.split(cmd), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception:
                pass
        return False

    # ---------- 位置 ----------
    def _position(self):
        sw = Gdk.Screen.get_default().get_width()
        sh = Gdk.Screen.get_default().get_height()
        w = self.bar.get_allocated_width() + 8
        if w < 200:
            return True
        self.resize(w, C.DOCK_H + 8)
        self.move((sw - w) // 2, sh - C.DOCK_BOTTOM - C.DOCK_H - 4)
        return False


if __name__ == '__main__':
    win = Dock()
    Gtk.main()
