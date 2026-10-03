#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_contextmenu.py —— 桌面右键系统菜单（Windows 式）
# 触发：qwm 捕获根窗口 Button3 → /tmp/qd_ctxmenu.sock {'cmd':'menu','x','y'}
# 菜单项（用户令 2026-09-27）：
#   终端 / 文本编辑器 / 新建×2 / 相册 / 计算器 / 回收站 / 系统监视器 /
#   桌面图标显隐 / 显示设置 / 关机重启（G.18 扩到 11 项）
# 交互：鼠标悬停高亮 + ↑↓ 键选择 + Enter 执行 + Esc/失焦关闭
# 常驻后台服务：菜单窗口按需创建，用完即销毁
# ============================================================
import os, sys, subprocess, json
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import qd_ui_common as C

SOCK_PATH = "/tmp/qd_ctxmenu.sock"
MENU_W, ITEM_H, ITEM_PAD = 190, 30, 6
DESKTOP_DIR = "/奇点OS/用户.qd/桌面"


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unique(base, ext=""):
    """新建文本.txt → 新建文本 2.txt（重名自动加序号）"""
    name = base + ext
    p = os.path.join(DESKTOP_DIR, name)
    if not os.path.exists(p):
        return name
    i = 2
    while True:
        name = "%s %d%s" % (base, i, ext)
        if not os.path.exists(os.path.join(DESKTOP_DIR, name)):
            return name
        i += 1


def _ensure_desktop():
    try:
        os.makedirs(DESKTOP_DIR, exist_ok=True)
    except Exception:
        pass


def _launch(argv):
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _wm_cmd(cmd, wid):
    """G.22：把窗口操作命令发给 qwm（窗口菜单动作执行通道）。
    最小化 / 最大化还原 / 关闭统一走 /tmp/qd_qwm.sock，带目标窗口 id。"""
    try:
        import socket as _sock

        s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s.settimeout(1)
        s.connect("/tmp/qd_qwm.sock")
        s.sendall(json.dumps({"cmd": cmd, "id": wid}).encode())
        s.close()
    except Exception:
        pass


ACTIONS = {
    "terminalWidget": lambda: _launch(
        [
            "xterm",
            "-T",
            "终端小组件",
            "-bg",
            "#16161e",
            "-fg",
            "#c0caf5",
            "-geometry",
            "92x24+620+90",
        ]
    ),
    "editor": lambda: _launch(["python3", "/奇点OS/运行/ui_editor.py"]),
    "newText": lambda: None,
    "newFolder": lambda: None,
    "album": lambda: _launch(["python3", "/奇点OS/运行/ui_album.py", "--show"]),
    "toggleIcons": lambda: C.send_icons({"cmd": "toggle"}),
    "settings": lambda: _launch(["python3", "/奇点OS/运行/ui_settings.py"]),
    "calc": lambda: _launch(["python3", "/奇点OS/运行/ui_calc.py"]),
    "trash": lambda: _launch(["python3", "/奇点OS/运行/ui_trash.py"]),
    "monitor": lambda: _launch(["python3", "/奇点OS/运行/ui_monitor.py"]),
    "power": lambda: _launch(["python3", "/奇点OS/运行/ui_power.py"]),
}

MENU_ITEMS = [
    ("terminal", "终端", "terminalWidget"),
    ("file-text", "文本编辑器", "editor"),
    ("circle-plus", "新建文本文件", "newText"),
    ("folder", "新建文件夹", "newFolder"),
    ("image", "打开相册", "album"),
    ("calculator", "计算器", "calc"),
    ("trash-2", "回收站", "trash"),
    ("activity", "系统监视器", "monitor"),
    ("layout-grid", "桌面图标显隐", "toggleIcons"),
    ("settings", "显示设置", "settings"),
    ("power", "关机 / 重启", "power"),
]


def _do_new_file(is_dir):
    _ensure_desktop()
    if is_dir:
        name = _unique("新建文件夹")
        try:
            os.makedirs(os.path.join(DESKTOP_DIR, name), exist_ok=True)
        except Exception:
            C.send_icons({"cmd": "toast", "text": "创建失败"})
            return
        C.send_icons({"cmd": "toast", "text": "已创建文件夹：%s" % name})
        _launch(["python3", "/奇点OS/运行/ui_filebrowser.py", DESKTOP_DIR])
        return
    # 文本文件：创建后直接用内置文本编辑器打开（G.15 用户令）
    name = _unique("新建文本", ".txt")
    p = os.path.join(DESKTOP_DIR, name)
    try:
        open(p, "a").close()
    except Exception:
        C.send_icons({"cmd": "toast", "text": "创建失败"})
        return
    C.send_icons({"cmd": "toast", "text": "已创建文本：%s" % name})
    _launch(["python3", "/奇点OS/运行/ui_editor.py", p])


ACTIONS["newText"] = lambda: _do_new_file(False)
ACTIONS["newFolder"] = lambda: _do_new_file(True)


# ---------- G.19 单实例 ----------
# 事故:连续右键弹出多个菜单叠加;grab 竞争后点击穿透到底层窗口(编辑器等)
# 弹出 GTK 默认菜单(剪切/复制/粘贴)。因果逻辑:此类菜单全局只能存在一个。
_current_menu = None


def _close_current():
    """销毁现存菜单(若在)。菜单窗口按需创建用完即销毁,这里只管唯一性。"""
    global _current_menu
    if _current_menu is not None:
        try:
            _current_menu.destroy()
        except Exception:
            pass
        _current_menu = None


class ContextMenu(Gtk.Window):
    def __init__(self, x=None, y=None):
        # G.19:先销毁上一个菜单——同屏唯一,多开=逻辑错误(用户实测报告)
        _close_current()
        super().__init__(type=Gtk.WindowType.POPUP)
        global _current_menu
        _current_menu = self
        self.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)

        C.setup_css(b"""
            .cm-box { background: #3a3a3c; border: 1px solid #3a3a3c;
                      border-radius: 12px; }
            .cm-item { background: transparent; border: none; padding: 0; }
            .cm-row { background: transparent; border-radius: 8px; }
            .cm-row.sel { background: #2e8dff; }
        """)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(ITEM_PAD)
        box.set_margin_bottom(ITEM_PAD)
        box.set_margin_start(ITEM_PAD)
        box.set_margin_end(ITEM_PAD)
        box.get_style_context().add_class("cm-box")
        self.add(box)

        self.rows = []
        for icon, name, key in MENU_ITEMS:
            row = Gtk.Box(spacing=8)
            row.set_margin_start(6)
            row.set_margin_end(6)
            row.set_size_request(MENU_W - 24, ITEM_H - 6)
            row.get_style_context().add_class("cm-row")
            row.pack_start(C.icon_image(icon, 16, C.FG), False, False, 0)
            lbl = C.label(name, 12, C.FG)
            row.pack_start(lbl, False, False, 0)

            eb = Gtk.EventBox()
            eb.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK
            )
            eb.connect(
                "button-press-event", lambda w, e, k=key: (self.activate(k), True)[1]
            )
            eb.connect(
                "enter-notify-event", lambda w, e, r=row: (self.select(r), True)[1]
            )
            eb.add(row)
            box.pack_start(eb, False, False, 0)
            self.rows.append((row, lbl, key))

        h = len(MENU_ITEMS) * ITEM_H + ITEM_PAD * 2
        self.set_size_request(MENU_W, h)

        # 位置：右下越界自动回收（Windows 行为）
        if x is not None:
            sw = Gdk.Screen.get_default().get_width()
            sh = Gdk.Screen.get_default().get_height()
            x = min(int(x), sw - MENU_W - 4)
            y = min(int(y), sh - h - 4) if y is not None else int(y or 0)
            self.move(max(0, x), max(0, y))

        self.connect("key-press-event", self._on_key)
        self.connect("button-press-event", self._on_press_root)
        self.selected = 0
        self.select(self.rows[0][0])
        self.show_all()
        # 抓取指针/键盘：实现"点击外部关闭 + ↑↓ 选择 + Enter 执行"（Windows 菜单行为）
        GLib.idle_add(self._grab)

    def _grab(self):
        try:
            seat = Gdk.Display.get_default().get_default_seat()
            seat.grab(
                self.get_window(),
                Gdk.SeatCapabilities.POINTER | Gdk.SeatCapabilities.KEYBOARD,
                True,
                None,
                None,
                None,
                None,
            )
        except Exception:
            pass
        return False

    def _on_press_root(self, _w, e):
        """点到菜单外面 → 关闭"""
        alloc = self.get_allocation()
        if not (0 <= e.x < alloc.width and 0 <= e.y < alloc.height):
            self.close()
            return True
        return False

    # ---------- 选择/执行 ----------
    def select(self, row):
        self.selected = next(i for i, r in enumerate(self.rows) if r[0] is row)
        for i, (r, lbl, _k) in enumerate(self.rows):
            if i == self.selected:
                r.get_style_context().add_class("sel")
                lbl.set_markup(
                    '<span font_family="%s" font_size="12000" color="#ffffff">%s</span>'
                    % (C.FONT, _esc(MENU_ITEMS[i][1]))
                )
            else:
                r.get_style_context().remove_class("sel")
                lbl.set_markup(
                    '<span font_family="%s" font_size="12000" color="%s">%s</span>'
                    % (C.FONT, C.FG, _esc(MENU_ITEMS[i][1]))
                )
        return False

    def activate(self, key):
        self.close()
        fn = ACTIONS.get(key)
        if fn:
            GLib.idle_add(lambda: (fn(), False)[1])
        return False

    def _on_key(self, _w, e):
        kv = Gdk.keyval_name(e.keyval)
        if kv in ("Up", "KP_Up"):
            i = (self.selected - 1) % len(self.rows)
            self.select(self.rows[i][0])
            return True
        if kv in ("Down", "KP_Down"):
            i = (self.selected + 1) % len(self.rows)
            self.select(self.rows[i][0])
            return True
        if kv in ("Return", "KP_Enter"):
            self.activate(self.rows[self.selected][2])
            return True
        if kv == "Escape":
            self.close()
            return True
        return False

    def close(self):
        try:
            Gdk.Display.get_default().get_default_seat().ungrab()
        except Exception:
            pass
        global _current_menu
        if _current_menu is self:
            _current_menu = None
        GLib.idle_add(self.destroy)
        return False


class WindowMenu(Gtk.Window):
    """G.22：窗口菜单 —— 右键标题栏 / Alt+Space 触发（规范 14.5.3）。
    项：最小化 / 最大化（还原）/ 关闭；动作经 qwm socket 通道执行。
    交互与桌面右键菜单一致：悬停高亮 + ↑↓ 键 + Enter 执行 + Esc/失焦关闭。"""

    def __init__(self, wid, x=None, y=None, maxed=False):
        _close_current()
        super().__init__(type=Gtk.WindowType.POPUP)
        global _current_menu
        _current_menu = self
        self.wid = wid
        self.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)

        C.setup_css(b"""
            .wm-box { background: #3a3a3c; border: 1px solid #3a3a3c;
                      border-radius: 12px; }
            .wm-row { background: transparent; border-radius: 8px; }
            .wm-row.sel { background: #2e8dff; }
        """)

        self.items = [
            ("minus", "最小化", "window_minimize"),
            ("maximize", "还原" if maxed else "最大化", "window_monocle"),
            ("x", "关闭", "window_close"),
        ]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(ITEM_PAD)
        box.set_margin_bottom(ITEM_PAD)
        box.set_margin_start(ITEM_PAD)
        box.set_margin_end(ITEM_PAD)
        box.get_style_context().add_class("wm-box")
        self.add(box)

        self.rows = []
        for icon, name, cmd in self.items:
            row = Gtk.Box(spacing=8)
            row.set_margin_start(6)
            row.set_margin_end(6)
            row.set_size_request(MENU_W - 24, ITEM_H - 6)
            row.get_style_context().add_class("wm-row")
            row.pack_start(C.icon_image(icon, 16, C.FG), False, False, 0)
            lbl = C.label(name, 12, C.FG)
            row.pack_start(lbl, False, False, 0)

            eb = Gtk.EventBox()
            eb.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK
            )
            eb.connect(
                "button-press-event", lambda w, e, c=cmd: (self.activate(c), True)[1]
            )
            eb.connect(
                "enter-notify-event", lambda w, e, r=row: (self.select(r), True)[1]
            )
            eb.add(row)
            box.pack_start(eb, False, False, 0)
            self.rows.append((row, lbl, cmd))

        h = len(self.items) * ITEM_H + ITEM_PAD * 2
        self.set_size_request(MENU_W, h)

        if x is not None:
            sw = Gdk.Screen.get_default().get_width()
            sh = Gdk.Screen.get_default().get_height()
            x = min(int(x), sw - MENU_W - 4)
            y = min(int(y), sh - h - 4) if y is not None else int(y or 0)
            self.move(max(0, x), max(0, y))

        self.connect("key-press-event", self._on_key)
        self.connect("button-press-event", self._on_press_root)
        self.selected = 0
        self.select(self.rows[0][0])
        self.show_all()
        GLib.idle_add(self._grab)

    def _grab(self):
        try:
            seat = Gdk.Display.get_default().get_default_seat()
            seat.grab(
                self.get_window(),
                Gdk.SeatCapabilities.POINTER | Gdk.SeatCapabilities.KEYBOARD,
                True,
                None,
                None,
                None,
                None,
            )
        except Exception:
            pass
        return False

    def _on_press_root(self, _w, e):
        alloc = self.get_allocation()
        if not (0 <= e.x < alloc.width and 0 <= e.y < alloc.height):
            self.close()
            return True
        return False

    def select(self, row):
        self.selected = next(i for i, r in enumerate(self.rows) if r[0] is row)
        for i, (r, lbl, _c) in enumerate(self.rows):
            if i == self.selected:
                r.get_style_context().add_class("sel")
                lbl.set_markup(
                    '<span font_family="%s" font_size="12000" color="#ffffff">%s</span>'
                    % (C.FONT, _esc(self.items[i][1]))
                )
            else:
                r.get_style_context().remove_class("sel")
                lbl.set_markup(
                    '<span font_family="%s" font_size="12000" color="%s">%s</span>'
                    % (C.FONT, C.FG, _esc(self.items[i][1]))
                )
        return False

    def activate(self, cmd):
        self.close()
        GLib.idle_add(lambda: (_wm_cmd(cmd, self.wid), False)[1])
        return False

    def _on_key(self, _w, e):
        kv = Gdk.keyval_name(e.keyval)
        if kv in ("Up", "KP_Up"):
            i = (self.selected - 1) % len(self.rows)
            self.select(self.rows[i][0])
            return True
        if kv in ("Down", "KP_Down"):
            i = (self.selected + 1) % len(self.rows)
            self.select(self.rows[i][0])
            return True
        if kv in ("Return", "KP_Enter"):
            self.activate(self.rows[self.selected][2])
            return True
        if kv == "Escape":
            self.close()
            return True
        return False

    def close(self):
        try:
            Gdk.Display.get_default().get_default_seat().ungrab()
        except Exception:
            pass
        global _current_menu
        if _current_menu is self:
            _current_menu = None
        GLib.idle_add(self.destroy)
        return False


# ---------- socket 服务 ----------
def serve():
    import socket, json, threading

    try:
        os.unlink(SOCK_PATH)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(8)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except Exception:
                return
            try:
                data = conn.recv(4096).decode("utf-8", "replace")
                cmd = json.loads(data) if data.strip() else {}
                if cmd.get("cmd") == "menu":
                    GLib.idle_add(
                        lambda: (ContextMenu(cmd.get("x"), cmd.get("y")), False)[1]
                    )
                elif cmd.get("cmd") == "window_menu":
                    # G.22：窗口菜单（右键标题栏 / Alt+Space 触发）
                    GLib.idle_add(
                        lambda: (
                            WindowMenu(
                                cmd.get("wid"),
                                cmd.get("x"),
                                cmd.get("y"),
                                bool(cmd.get("max")),
                            ),
                            False,
                        )[1]
                    )
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    threading.Thread(target=loop, daemon=True).start()


def _ping():
    import socket, json

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK_PATH)
        s.close()
        return True
    except Exception:
        return False


def main():
    if _ping():
        return  # 单实例
    serve()
    if "--show" in sys.argv:
        GLib.idle_add(lambda: (ContextMenu(), False)[1])
    Gtk.main()


if __name__ == "__main__":
    main()
