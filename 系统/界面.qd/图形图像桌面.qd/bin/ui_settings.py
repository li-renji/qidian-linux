#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_settings.py —— 奇点OS 设置 v3（G.20 重构）
#
# 用户令（2026-09-28）：
#   ① 布局参考 Windows 设置：左侧大类导航，右侧具体内容分组
#   ② 二级页返回 = 返回上一级（不是最初一级）
#   ③ 所有调整实时生效
#
# 结构：
#   左侧导航：个性化 / 桌面 / 系统 / 组件 / 关于
#   右侧内容：每大类一页（ScrolledWindow，可滚动）
#   二级页：壁纸库（从"个性化"进入，← 返回个性化）
#   导航栈：nav_stack 记录层级，返回按钮 pop 一级
#
# 实时生效：
#   不透明度 → qwmctl redecorate（qwm 重绘所有标题栏，ARGB 只作用"框"）
#   图标类   → send_icons reload（重绘合成壁纸）
#   壁纸     → send_icons reload
#
# 设置持久化：/home/qduser/.config/qidos/settings.json（C.load_conf/save_conf）
# ============================================================
import os, sys, json, subprocess, shutil, signal, time

# 僵尸防护：本进程 Popen 起组件/feh 不 wait，忽略 SIGCHLD 由内核自动回收
try:
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
except Exception:
    pass
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf
import qd_ui_common as C

WIN_W, WIN_H = 720, 560
WALLPAPER_DIR = C.CONF_DIR + "/wallpapers"
NAV_W = 150

ICON_SIZES = [(32, "小"), (40, "中"), (48, "大"), (64, "超大")]


def _run(argv):
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _reload_icons():
    """通知图标层热更新（重绘壁纸 + 重建点击区）"""
    C.send_icons({"cmd": "reload"})


def _redecorate():
    """通知 qwm 重绘所有标题栏（不透明度实时生效）"""
    _run(["/奇点OS/运行/qwmctl", "redecorate"])


def _toast(text):
    C.send_icons({"cmd": "toast", "text": text})


# ============================================================
# 小部件工厂
# ============================================================
def _row(label_text, widget, hint=None):
    """一行：左标签 右控件"""
    r = Gtk.Box(spacing=8)
    r.set_margin_start(10)
    r.set_margin_end(10)
    lbl = C.label(label_text, 12, C.FG)
    lbl.set_xalign(0)
    lbl.set_size_request(150, -1)
    r.pack_start(lbl, False, False, 0)
    r.pack_end(widget, False, False, 0)
    if hint:
        h = C.label(hint, 10, C.MUTED_FG)
        r.pack_end(h, False, False, 8)
    return r


def _section(title, rows):
    """一节：标题 + 行列表"""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_margin_top(8)
    box.set_margin_bottom(8)
    box.set_margin_start(12)
    box.set_margin_end(12)
    box.get_style_context().add_class("st-sec")
    box.pack_start(C.label(title, 12, C.PRIMARY, "bold"), False, False, 0)
    for w in rows:
        box.pack_start(w, False, False, 0)
    return box


def _slider(value, lo, hi, cb, width=150, fmt=None):
    """fmt: None=整数px；'pct'=百分比(0-255 → 0-100%)"""
    box = Gtk.Box(spacing=6)
    adj = Gtk.Adjustment(
        value=value, lower=lo, upper=hi, step_increment=1, page_increment=5
    )
    scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
    scale.set_size_request(width, -1)
    scale.set_digits(0)
    scale.set_draw_value(False)
    lbl = C.label(fmt(value) if fmt else "%d" % value, 11, C.MUTED_FG)
    scale.connect("value-changed", lambda s: cb(s, lbl))
    box.pack_start(scale, False, False, 0)
    box.pack_start(lbl, False, False, 0)
    return box


def _btn(text, fn):
    b = Gtk.Button(label=text)
    b.connect("clicked", lambda _w: fn())
    return b


def _page(rows):
    """页面容器：children= 是 PyGTK 语法，GTK3 必须逐个 pack"""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    for w in rows:
        box.pack_start(w, False, False, 0)
    return box


class Settings(Gtk.Window):
    def __init__(self):
        super().__init__(title="设置")
        self.cfg = C.load_conf()
        self.set_default_size(WIN_W, WIN_H)
        self.set_size_request(560, 420)
        self.move(300, 90)

        C.setup_css(b"""
            .st-panel { background: #1c1c1e; }
            .st-nav   { background: #16161e; }
            .st-sec   { background: #000000; border: 1px solid #3a3a3c;
                        border-radius: 12px; }
            .st-navrow { padding: 8px 10px; border-radius: 8px; }
            .st-navrow.sel { background: #2e8dff; }
            button { background: rgba(58,58,58,180); border: none;
                     border-radius: 8px; color: #f5f5f7; padding: 4px 10px; }
            button:hover { background: rgba(91,120,255,180); }
            switch { background: rgba(58,58,58,180); }
            .st-crumb { color: #8e8e93; }
            .wp-cell  { background: #000000; border: 1px solid #3a3a3c;
                        border-radius: 8px; padding: 4px; }
            .wp-cell.sel { border: 2px solid #2e8dff; }
        """)

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.get_style_context().add_class("st-panel")
        self.add(root)

        # ---------- 左侧导航 ----------
        nav = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        nav.set_size_request(NAV_W, -1)
        nav.get_style_context().add_class("st-nav")
        nav.set_margin_top(10)
        root.pack_start(nav, False, False, 0)

        self.nav_rows = {}
        self.stack = Gtk.Stack()
        # G.21：转场动画=切换闪烁的元凶（转场中间态被用户看到），改无动画
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        # 导航栈：记录访问层级，返回时 pop 上一级
        self.nav_stack = []

        self._nav_add(nav, "个性化", "palette", self._page_appearance)
        self._nav_add(nav, "桌面", "layout-grid", self._page_desktop)
        self._nav_add(nav, "系统", "monitor", self._page_system)
        self._nav_add(nav, "组件", "box", self._page_components)
        self._nav_add(nav, "关于", "info", self._page_about)

        # ---------- 右侧内容 ----------
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.pack_start(content, True, True, 0)

        # 顶栏：返回按钮（二级页才显示）+ 面包屑 + 关闭按钮
        self.topbar = Gtk.Box(spacing=8)
        self.topbar.set_margin_start(10)
        self.topbar.set_margin_top(6)
        self.topbar.set_margin_bottom(4)
        self.btn_back = Gtk.Button()
        self.btn_back.add(C.icon_image("chevron-left", 16, C.FG))
        self.btn_back.set_tooltip_text("返回上一级")
        self.btn_back.set_no_show_all(True)
        self.btn_back.connect("clicked", lambda *_: self._nav_back())
        self.topbar.pack_start(self.btn_back, False, False, 0)
        self.crumb = C.label("设置 › 个性化", 11, C.MUTED_FG)
        self.topbar.pack_start(self.crumb, False, False, 0)
        # G.21 用户令：应用内显式关闭按钮（不依赖窗口管理器标题栏）
        btn_close = Gtk.Button()
        btn_close.add(C.icon_image("x", 14, C.FG))
        btn_close.set_tooltip_text("关闭设置")
        btn_close.connect("clicked", lambda *_: self.destroy())
        self.topbar.pack_end(btn_close, False, False, 0)
        content.pack_start(self.topbar, False, False, 0)

        content.pack_start(self.stack, True, True, 0)

        # 默认选中第一类
        self._nav_select("个性化")

    # ---------- 导航 ----------
    def _nav_add(self, nav_box, name, icon, page_fn):
        eb = Gtk.EventBox()
        row = Gtk.Box(spacing=8)
        row.get_style_context().add_class("st-navrow")
        row.pack_start(C.icon_image(icon, 16, C.FG), False, False, 0)
        row.pack_start(C.label(name, 12, C.FG), False, False, 0)
        eb.add(row)
        eb.connect("button-press-event", lambda *_: (self._nav_select(name), False)[1])
        nav_box.pack_start(eb, False, False, 0)
        self.nav_rows[name] = row

        # 内容页（ScrolledWindow 包内容）
        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scr.set_margin_start(6)
        scr.set_margin_end(10)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.set_margin_bottom(12)
        body.pack_start(page_fn(), False, False, 0)
        scr.add(body)
        self.stack.add_named(scr, name)

    def _nav_select(self, name):
        """一级导航：清空返回栈，切页"""
        self.nav_stack = [name]
        self._show(name)

    def _nav_push(self, name, title):
        """进入二级页"""
        self.nav_stack.append(name)
        if self.stack.get_child_by_name(name) is None:
            scr = Gtk.ScrolledWindow()
            scr.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            body.pack_start(getattr(self, "_build_" + name)(), False, False, 0)
            scr.add(body)
            self.stack.add_named(scr, name)
        self._show(name, title)

    def _nav_back(self):
        """返回上一级（不是最初一级）"""
        if len(self.nav_stack) > 1:
            self.nav_stack.pop()
            self._show(self.nav_stack[-1])

    def _show(self, name, override_title=None):
        child = self.stack.get_child_by_name(name)
        if child is not None:
            child.show_all()  # 动态加的页必须显式 show,否则切换后不显示
            self.stack.set_visible_child(child)
        # 高亮导航行（二级页时高亮其父类）
        top = self.nav_stack[0] if self.nav_stack else name
        for n, row in self.nav_rows.items():
            ctx = row.get_style_context()
            if n == top:
                ctx.add_class("sel")
            else:
                ctx.remove_class("sel")
        # 返回按钮可见性
        if len(self.nav_stack) > 1:
            self.btn_back.show_all()
        else:
            self.btn_back.hide()
        # 面包屑
        if override_title:
            path = "设置 › " + " › ".join([self.nav_stack[0], override_title])
        else:
            path = "设置 › " + name
        self.crumb.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%s</span>'
            % (C.FONT, C.MUTED_FG, path)
        )

    # ============================================================
    # 页面：个性化
    # ============================================================
    def _page_appearance(self):
        return _page(
            [
                _section(
                    "背景",
                    [
                        _row(
                            "背景图片",
                            _btn("浏览壁纸 →", self._goto_wallpapers),
                            "点击进入壁纸库",
                        ),
                    ],
                ),
                _section(
                    "窗口",
                    [
                        _row(
                            "标题栏不透明度",
                            _slider(
                                int(self.cfg.get("opacity", 204)),
                                40,
                                255,
                                self._on_opacity,
                                fmt=lambda v: "%d%%" % round(v / 255 * 100),
                            ),
                            "只作用于窗口边框，内容保持不透明",
                        ),
                    ],
                ),
                _section(
                    "相册",
                    [
                        _row(
                            "预览缩略图",
                            self._album_preview_switch(),
                            "关闭后只显示图标，点击仍可看图",
                        ),
                        _row(
                            "预览尺寸",
                            _slider(
                                int(self.cfg.get("album_thumb", 160)),
                                40,
                                320,
                                self._on_album_thumb,
                                fmt=lambda v: "%d px" % v,
                            ),
                            "缩略图大小 40–320 px",
                        ),
                    ],
                ),
            ]
        )

    def _album_preview_switch(self):
        sw = Gtk.Switch()
        sw.set_active(bool(self.cfg.get("album_preview", True)))
        sw.connect("notify::active", self._on_album_preview)
        return sw

    def _on_album_preview(self, sw, _p):
        self.cfg["album_preview"] = bool(sw.get_active())
        C.save_conf(self.cfg, ["album_preview"])
        _toast("相册重新打开后生效")
        return False

    def _on_album_thumb(self, scale, lbl):
        v = int(scale.get_value())
        self.cfg["album_thumb"] = v
        C.save_conf(self.cfg, ["album_thumb"])
        lbl.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%d px</span>'
            % (C.FONT, C.MUTED_FG, v)
        )
        _toast("相册重新打开后生效")
        return False

    def _goto_wallpapers(self):
        self._nav_push("wallpapers", "壁纸库")

    def _build_wallpapers(self):
        """二级页：壁纸库（内置 + 用户自定义，点击即设）"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(6)
        cur = self.cfg.get("wallpaper", C.DEFAULT_CONF["wallpaper"])
        grid = Gtk.FlowBox()
        grid.set_min_children_per_line(3)
        grid.set_max_children_per_line(3)
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)
        grid.set_margin_start(12)

        items = [
            ("默认星空", C.UI_DIR + "/wallpaper.png"),
            ("原型背景", C.UI_DIR + "/backdrop.png"),
        ]
        try:
            if os.path.isdir(WALLPAPER_DIR):
                for f in sorted(os.listdir(WALLPAPER_DIR)):
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                        items.append((f, os.path.join(WALLPAPER_DIR, f)))
        except Exception:
            pass

        for name, path in items:
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cell.get_style_context().add_class("wp-cell")
            if path == cur:
                cell.get_style_context().add_class("sel")
            try:
                _pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 160, 100, True)
                pb = Gtk.Image.new_from_pixbuf(_pb)
            except Exception:
                pb = C.icon_image("image", 32, C.MUTED_FG)
            cell.pack_start(pb, False, False, 0)
            cell.pack_start(C.label(name, 10, C.MUTED_FG), False, False, 0)
            grid.add(cell)
        box.pack_start(grid, False, False, 0)

        bar = Gtk.Box(spacing=8)
        bar.set_margin_start(12)
        bar.pack_start(_btn("选择本地图片…", self._on_pick_image), False, False, 0)
        box.pack_start(bar, False, False, 0)
        return box

    def _on_pick_image(self, _b=None):
        src = C.run_picker(
            mode="open", root="/home/qduser", filters="png,jpg,jpeg,webp,bmp"
        )
        if not src:
            return
        try:
            os.makedirs(WALLPAPER_DIR, exist_ok=True)
            dst = os.path.join(WALLPAPER_DIR, os.path.basename(src))
            shutil.copyfile(src, dst)
        except Exception:
            dst = src
        self._set_wallpaper(dst)

    def _set_wallpaper(self, path):
        self.cfg["wallpaper"] = path
        C.save_conf(self.cfg, ["wallpaper"])
        _reload_icons()
        _toast("背景已更换")
        # 选择后返回上一级（个性化），并销毁壁纸库页以便下次重建高亮
        old = self.stack.get_child_by_name("wallpapers")
        if old is not None:
            self.stack.remove(old)
        if "wallpapers" in self.nav_stack:
            self.nav_stack.remove("wallpapers")
        self._nav_back()

    def _on_opacity(self, scale, lbl):
        v = int(scale.get_value())
        self.cfg["opacity"] = v
        C.save_conf(self.cfg, ["opacity"])
        lbl.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%d%%</span>'
            % (C.FONT, C.MUTED_FG, round(v / 255 * 100))
        )
        # 实时生效：qwm 重绘所有标题栏（ARGB frame 只作用框）
        _redecorate()
        return False

    # ============================================================
    # 页面：桌面
    # ============================================================
    def _page_desktop(self):
        return _page(
            [
                _section(
                    "桌面图标",
                    [
                        _row("图标大小", self._icon_size_combo()),
                        _row(
                            "图标间距",
                            _slider(
                                int(self.cfg.get("icon_gap", 10)),
                                2,
                                28,
                                self._on_gap,
                                fmt=lambda v: "%d px" % v,
                            ),
                        ),
                        _row("文字标签", self._label_switch()),
                        _row(
                            "标签字号",
                            _slider(
                                int(self.cfg.get("label_size", 11)),
                                9,
                                18,
                                self._on_label_size,
                                fmt=lambda v: "%d px" % v,
                            ),
                        ),
                    ],
                ),
                _section(
                    "交互",
                    [
                        _row(
                            "桌面图标显示",
                            _btn("显隐切换", lambda: C.send_icons({"cmd": "toggle"})),
                        ),
                        _row("系统菜单（右键）", _btn("测试弹出", self._test_menu)),
                    ],
                ),
            ]
        )

    def _icon_size_combo(self):
        combo = Gtk.ComboBoxText()
        for _px, name in ICON_SIZES:
            combo.append_text(name)
        cur = int(self.cfg.get("icon_size", 40))
        idx = next((i for i, (px, _n) in enumerate(ICON_SIZES) if px == cur), 1)
        combo.set_active(idx)
        combo.connect("changed", self._on_icon_size)
        return combo

    def _on_icon_size(self, combo):
        i = combo.get_active()
        if 0 <= i < len(ICON_SIZES):
            self.cfg["icon_size"] = ICON_SIZES[i][0]
            C.save_conf(self.cfg, ["icon_size"])
            _reload_icons()
        return False

    def _on_gap(self, scale, lbl):
        v = int(scale.get_value())
        self.cfg["icon_gap"] = v
        C.save_conf(self.cfg, ["icon_gap"])
        lbl.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%d px</span>'
            % (C.FONT, C.MUTED_FG, v)
        )
        _reload_icons()
        return False

    def _label_switch(self):
        sw = Gtk.Switch()
        sw.set_active(bool(self.cfg.get("icon_label", True)))
        sw.connect("notify::active", self._on_label)
        return sw

    def _on_label(self, sw, _p):
        self.cfg["icon_label"] = bool(sw.get_active())
        C.save_conf(self.cfg, ["icon_label"])
        _reload_icons()
        return False

    def _on_label_size(self, scale, lbl):
        v = int(scale.get_value())
        self.cfg["label_size"] = v
        C.save_conf(self.cfg, ["label_size"])
        lbl.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%d px</span>'
            % (C.FONT, C.MUTED_FG, v)
        )
        _reload_icons()
        return False

    @staticmethod
    def _test_menu():
        import socket

        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect("/tmp/qd_ctxmenu.sock")
            s.sendall(b'{"cmd":"menu","x":600,"y":300}')
            s.close()
        except Exception:
            _toast("菜单服务未运行")

    # ============================================================
    # 页面：系统
    # ============================================================
    def _page_system(self):
        import platform

        return _page(
            [
                _section(
                    "系统信息",
                    [
                        _row(
                            "屏幕分辨率",
                            C.label(
                                "%d × %d"
                                % (
                                    Gdk.Screen.get_default().get_width(),
                                    Gdk.Screen.get_default().get_height(),
                                ),
                                12,
                                C.FG,
                            ),
                        ),
                        _row("运行环境", C.label(platform.platform(), 12, C.FG)),
                        _row("Python", C.label(platform.python_version(), 12, C.FG)),
                        _row("磁盘", C.label(self._disk_str(), 12, C.FG)),
                    ],
                ),
                _section(
                    "应用",
                    [
                        _row("应用中心", _btn("打开", self._open_store)),
                        _row("终端", _btn("打开", self._open_term)),
                        _row("文件浏览器", _btn("打开", self._open_files)),
                        _row(
                            "系统监视器",
                            _btn(
                                "打开",
                                lambda: _run(["python3", "/奇点OS/运行/ui_monitor.py"]),
                            ),
                        ),
                    ],
                ),
            ]
        )

    @staticmethod
    def _disk_str():
        try:
            st = os.statvfs("/")
            free = st.f_bavail * st.f_frsize / 1e9
            total = st.f_blocks * st.f_frsize / 1e9
            return "可用 %.1f GB / 共 %.1f GB" % (free, total)
        except Exception:
            return "未知"

    # ============================================================
    # 页面：组件
    # ============================================================
    def _page_components(self):
        return _page(
            [
                _section(
                    "桌面组件（重启）",
                    [
                        _row("灵动岛", _btn("重启", lambda: self._restart("island"))),
                        _row("快捷启动栏", _btn("重启", lambda: self._restart("dock"))),
                        _row(
                            "桌面图标层", _btn("重启", lambda: self._restart("icons"))
                        ),
                        _row("合成器 picom", _btn("重启", self._restart_picom)),
                    ],
                ),
                _section(
                    "服务",
                    [
                        _row("相册服务", _btn("打开相册", self._open_album)),
                        _row(
                            "回收站",
                            _btn(
                                "打开",
                                lambda: _run(["python3", "/奇点OS/运行/ui_trash.py"]),
                            ),
                        ),
                    ],
                ),
            ]
        )

    @staticmethod
    def _open_album():
        _run(["python3", "/奇点OS/运行/ui_album.py", "--show"])

    @staticmethod
    def _open_store():
        _run(["python3", "/奇点OS/运行/ui_installer.py"])

    @staticmethod
    def _open_term():
        _run(["xterm", "-T", "奇点OS终端", "-bg", "#16161e", "-fg", "#c0caf5"])

    @staticmethod
    def _open_files():
        _run(["python3", "/奇点OS/运行/ui_filebrowser.py"])

    @staticmethod
    def _restart_picom():
        # G.25 隐性 bug 修复：VM 上 pkill 不存在 → 改用 ps + awk + kill
        _run(
            [
                "bash",
                "-c",
                "ps -eo pid,comm | awk '$2==\"picom\"{print $1}' | xargs -r kill 2>/dev/null; "
                "sleep 1; "
                "DISPLAY=:0 setsid picom --config "
                "/home/qduser/.config/picom/picom.conf >/tmp/picom.log 2>&1 &",
            ]
        )
        _toast("picom 重启中")

    @staticmethod
    def _restart(name):
        """重启桌面组件：杀旧 + 拉新（G.25 兜底：pgrep 不存在时用 ps 全扫描）"""
        script = {
            "island": "ui_island.py",
            "dock": "ui_dock.py",
            "icons": "ui_icons.py",
        }[name]
        # VM 上 pkill/pgrep 可能不存在 → 两条独立命令、每条内建 fallback
        _run(
            [
                "bash",
                "-c",
                "# 1) 先试 pgrep（G.19 防自杀模式）"
                "if command -v pgrep >/dev/null 2>&1; then "
                "  pgrep -f '[u]i_%s\\.py' | xargs -r kill 2>/dev/null; "
                "else "
                "  # fallback：ps 全扫描 + awk + kill"
                "  ps -eo pid,cmd | awk '/ui_%s\\.py/ && !/awk/{print $1}' | xargs -r kill 2>/dev/null; "
                "fi; true" % (name, name),
            ]
        )
        time.sleep(1)
        _run(
            [
                "bash",
                "-c",
                "export DISPLAY=:0; setsid python3 /奇点OS/运行/%s "
                ">/tmp/%s.log 2>&1 </dev/null &" % (script, name),
            ]
        )
        _toast("%s 已重启" % name)

    # ============================================================
    # 页面：关于
    # ============================================================
    def _page_about(self):
        return _page(
            [
                _section(
                    "奇点OS",
                    [
                        _row(
                            "系统", C.label("奇点OS Linux 开发版（旧版轨道）", 12, C.FG)
                        ),
                        _row(
                            "桌面", C.label("qwm 自研窗口管理器 + GTK3 组件", 12, C.FG)
                        ),
                        _row(
                            "窗口管理器",
                            C.label("qwm v3 / python-xlib / ARGB frame", 12, C.FG),
                        ),
                    ],
                ),
            ]
        )


if __name__ == "__main__":
    if not C.single_instance("settings"):
        sys.exit(0)
    win = Settings()
    C.set_raise_handler("settings", win.present)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
