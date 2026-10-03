#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_drawer.py V2 —— 应用抽屉 + 提示条（TreeSolo 原型逐像素复刻）
# 蓝本：ui_design/pages/index.html（L540-609 结构 + L612-658 交互）
#
# V2 关键修正（fbdev 无合成器）：
#   ARGB 半透明窗口在无合成器环境渲染成不透明黑 → 弃用 CSS rgba，
#   改为三窗口预混合方案：
#     · backdrop_win：全屏，铺预变暗壁纸 PNG（壁纸 + 20% 黑，离线生成）
#     · drawer_win：实色 #1c1c1e 面板（原型 bg card/98 以实色近似）
#     · toast_win：实色 #3a3a3c 胶囊
#   · 打开：双击桌面空白 / launcher（经 socket）；关闭：X / backdrop / 点应用
#   · toast：top-14 居中 · rounded-full · 1.8s 消失
#   · socket /tmp/qd_drawer.sock 接收 dock/qwm 指令（toggle/toast）
# ============================================================
import gi, os, json, socket, subprocess, threading, time
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib
import qd_ui_common as C

APPS = [
    ('globe',       '浏览器',     None),
    ('terminal',    '终端',       'xterm -T 奇点OS终端 -bg #16161e -fg #c0caf5'),
    ('folder-open', '文件浏览器', 'thunar'),
    ('settings',    '设置',       None),
    ('calculator',  '计算器',     None),
    ('file-text',   '文本编辑',   None),
    ('store',       '应用商店',   None),
    ('image',       '相册',       None),
]

BACKDROP_PNG = os.path.join(C.UI_DIR, 'backdrop.png')


def _dbg(msg):
    try:
        with open('/tmp/drawer_dbg.log', 'a') as f:
            f.write(msg + '\n')
    except Exception:
        pass


class Drawer:
    """三窗口组合：backdrop / drawer / toast（各自独立映射/撤销映射）"""

    def __init__(self):
        self.opened = False
        sw = Gdk.Screen.get_default().get_width()
        sh = Gdk.Screen.get_default().get_height()
        self.sw, self.sh = sw, sh

        C.setup_css(b'''
            * { background: transparent; }
            .backdrop-fb { background: #000000; }
            .drawer { background: #1c1c1e; border: 1px solid #3a3a3c;
                      border-radius: 24px; }
            .app { background: transparent; border: none; border-radius: 16px; }
            .app:hover { background: #3a3a3c; }
            .toast { background: #3a3a3c; border: 1px solid #58585a;
                     border-radius: 9999px; }
        ''')

        # ---------- backdrop_win：全屏预变暗壁纸 ----------
        self.backdrop_win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.backdrop_win.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.backdrop_win.set_decorated(False)
        self.backdrop_win.resize(sw, sh)
        self.backdrop_win.move(0, 0)
        bx = Gtk.EventBox()
        if os.path.exists(BACKDROP_PNG):
            bx.add(Gtk.Image.new_from_file(BACKDROP_PNG))
            _dbg('backdrop: 使用 %s' % BACKDROP_PNG)
        else:
            bx.get_style_context().add_class('backdrop-fb')
            _dbg('backdrop: 缺 %s，回退实色黑' % BACKDROP_PNG)
        bx.connect('button-press-event', lambda w, e: self.close())
        self.backdrop_win.add(bx)
        self.backdrop_win.show_all()   # 子 widget 必须先 show，否则空 EventBox 渲染成黑

        # ---------- drawer_win：实色面板 ----------
        self.drawer_win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.drawer_win.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.drawer_win.set_decorated(False)
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.panel.get_style_context().add_class('drawer')
        self.panel.set_margin_top(20)
        self.panel.set_margin_bottom(20)
        self.panel.set_margin_start(20)
        self.panel.set_margin_end(20)
        self.panel.set_size_request(C.DRAWER_W, -1)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        head.pack_start(C.label('应用抽屉', 16, C.FG, '600'), False, False, 0)
        close_btn = Gtk.Button()
        close_btn.set_size_request(28, 28)
        close_btn.get_style_context().add_class('app')
        close_btn.add(C.icon_image('x', 16, C.FG))
        close_btn.connect('clicked', lambda w: self.close())
        head.pack_end(close_btn, False, False, 0)
        self.panel.pack_start(head, False, False, 0)

        grid = Gtk.Grid(row_spacing=12, column_spacing=12)   # gap-3
        grid.set_halign(Gtk.Align.CENTER)
        for i, (icon, name, cmd) in enumerate(APPS):
            grid.attach(self._app_cell(icon, name, cmd), i % 4, i // 4, 1, 1)
        self.panel.pack_start(grid, False, False, 0)
        self.panel.show_all()
        self.drawer_win.add(self.panel)
        self.drawer_win.show()          # 先显示才能拿到真实高度
        GLib.timeout_add(300, self._place_drawer)  # 布局完成后重定位

        # ---------- toast_win：实色胶囊 ----------
        self.toast_win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.toast_win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.toast_win.set_decorated(False)
        self.toast = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.toast.get_style_context().add_class('toast')
        self.toast_lbl = C.label('', 14, C.FG)
        self.toast.set_margin_start(16)
        self.toast.set_margin_end(16)
        self.toast.set_margin_top(8)
        self.toast.set_margin_bottom(8)
        self.toast.pack_start(self.toast_lbl, False, False, 0)
        self.toast_win.add(self.toast)

        # 初始状态：全部隐藏
        self.backdrop_win.hide()
        self.drawer_win.hide()
        self.toast_win.move((sw - 120) // 2, C.TOAST_TOP)
        self.toast_win.show_all()
        self.toast_win.hide()
        GLib.timeout_add(300, lambda: self.toast_win.move(
            (self.sw - max(self.toast.get_allocated_width(), 120)) // 2, C.TOAST_TOP) and False)

        # socket 服务（接收 dock/qwm toggle/toast 指令）
        threading.Thread(target=self._serve, daemon=True).start()
        # 桌面双击由 qwm 检测（root ButtonPressMask 独占，qwm 经 socket 通知）

    # ---------- 抽屉网格单元（原型 L549-580） ----------
    def _app_cell(self, icon, name, cmd):
        btn = Gtk.Button()
        btn.get_style_context().add_class('app')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)  # gap-2
        box.set_margin_top(12)                                          # p-3
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        img = C.icon_image(icon, 32, C.PRIMARY)                         # w-8 h-8 text-primary
        img.set_halign(Gtk.Align.CENTER)
        box.pack_start(img, False, False, 0)
        lbl = C.label(name, 12, C.FG)
        lbl.set_halign(Gtk.Align.CENTER)
        box.pack_start(lbl, False, False, 0)
        btn.add(box)
        btn.connect('clicked', lambda w: self._launch(name, cmd))
        return btn

    def _launch(self, name, cmd):
        _dbg('launch: %s cmd=%s' % (name, cmd))
        self._toast('启动 ' + name)
        self.close()
        if cmd:
            try:
                subprocess.Popen(cmd.split(), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception:
                pass

    # ---------- 开关（原型 180ms 淡入简化为直接显隐） ----------
    def toggle(self):
        self.close() if self.opened else self.open_drawer()

    def open_drawer(self):
        _dbg('open_drawer 调用')
        self.opened = True
        self.backdrop_win.show()
        self.drawer_win.show()
        self._raise_panels()

    def close(self, *a):
        self.opened = False
        self.drawer_win.hide()
        self.backdrop_win.hide()

    def _place_drawer(self):
        h = self.panel.get_allocated_height() or 300
        x = (self.sw - C.DRAWER_W) // 2
        y = self.sh - C.DRAWER_BOTTOM - h - 20
        self.drawer_win.move(x, y)
        _dbg('drawer 位置: %d,%d h=%d' % (x, y, h))

    # ---------- toast（原型 1800ms） ----------
    def _toast(self, text):
        self.toast_lbl.set_markup('<span font_family="%s" font_size="14000" color="%s">%s</span>'
                                  % (C.FONT, C.FG, text))
        self.toast.show_all()
        self.toast_win.show()
        w = max(self.toast.get_allocated_width(), 120)
        self.toast_win.move((self.sw - w) // 2, C.TOAST_TOP)
        GLib.timeout_add(1800, self._hide_toast)

    def _hide_toast(self):
        self.toast_win.hide()
        return False

    # ---------- 保持 dock/island 在 backdrop 之上 ----------
    def _raise_panels(self):
        try:
            from Xlib import display, X
            d = display.Display()
            root = d.screen().root
            for w in root.query_tree().children:
                try:
                    p = w.get_full_property(d.intern_atom('_NET_WM_PID'),
                                            X.AnyPropertyType)
                    if not p:
                        continue
                    cmd = open('/proc/%d/cmdline' % p.value[0]).read()
                except Exception:
                    continue
                if 'ui_dock.py' in cmd or 'ui_island.py' in cmd:
                    w.raise_window()   # configure(stack_mode=Above) 实测不生效
                    _dbg('raise: %s' % cmd.strip())
            d.sync()
        except Exception as e:
            _dbg('raise 失败: %s' % e)

    # ---------- socket 服务 ----------
    def _serve(self):
        _dbg('serve 线程启动')
        if os.path.exists(C.SOCK_PATH):
            try:
                os.remove(C.SOCK_PATH)
            except OSError:
                pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(C.SOCK_PATH)
        srv.listen(4)
        while True:
            try:
                conn, _ = srv.accept()
                data = conn.recv(4096).decode()
                conn.close()
                _dbg('recv: %s' % data)
                GLib.idle_add(self._handle, json.loads(data))
            except Exception:
                time.sleep(0.5)

    def _handle(self, msg):
        cmd = msg.get('cmd')
        if cmd == 'toggle':
            self.toggle()
        elif cmd == 'toast':
            self._toast(msg.get('text', ''))
        return False


if __name__ == '__main__':
    d = Drawer()
    Gtk.main()
