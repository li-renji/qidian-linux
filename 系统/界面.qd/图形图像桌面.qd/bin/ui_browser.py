#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_browser.py —— 奇点浏览器（系统内置，WebKit2 自研壳）
# 选型：用户令"系统要内置一个浏览器"，采用 webkit2gtk-4.1 自绘外壳，
#       界面风格与系统一致（深色 + 设计 token），体积小、可控。
# 功能：多标签、地址栏、后退/前进/刷新/主页、加载进度、新标签起始页
# 依赖：webkit2gtk-4.1（GI: WebKit2 4.1）
# ============================================================
import os, sys
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, Gdk, GLib, WebKit2
import qd_ui_common as C

# G.23：窗口尺寸支持配置化（解决"太大"问题），默认缩小到 860×600
_cfg = C.load_conf()
WIN_W = _cfg.get("browser_w", 860)
WIN_H = _cfg.get("browser_h", 600)
HOME_URL = "https://www.bing.com"

HOME_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>奇点起始页</title><style>
body{margin:0;font-family:sans-serif;background:#0b0b10;color:#f5f5f7;
 display:flex;flex-direction:column;align-items:center;padding-top:90px}
h1{font-size:34px;margin:0 0 6px;letter-spacing:2px}
p.sub{color:#8e8e93;margin:0 0 26px;font-size:13px}
form{display:flex;gap:8px}
input{width:460px;padding:11px 16px;border-radius:22px;border:1px solid #3a3a3c;
 background:#1c1c1e;color:#f5f5f7;font-size:15px;outline:none}
button{padding:11px 20px;border-radius:22px;border:none;background:#2e8dff;
 color:#fff;font-size:14px;cursor:pointer}
.grid{margin-top:38px;display:grid;grid-template-columns:repeat(4,120px);gap:14px}
a{display:flex;flex-direction:column;align-items:center;justify-content:center;
 height:74px;border-radius:14px;background:#1c1c1e;border:1px solid #3a3a3c;
 color:#f5f5f7;text-decoration:none;font-size:13px}
a:hover{background:#2e8dff}
</style></head><body>
<h1>奇点浏览器</h1><p class="sub">输入关键词开始搜索，或从下面快速进入</p>
<form action="https://www.bing.com/search" method="get">
<input name="q" placeholder="搜索…" autofocus><button type="submit">搜索</button></form>
<div class="grid">
<a href="https://www.bing.com">必应</a>
<a href="https://www.baidu.com">百度</a>
<a href="https://github.com">GitHub</a>
<a href="https://www.bilibili.com">哔哩哔哩</a>
<a href="https://zh.wikipedia.org">维基百科</a>
<a href="https://archlinux.org">Arch Linux</a>
<a href="https://docs.python.org">Python 文档</a>
<a href="https://www.zhihu.com">知乎</a>
</div></body></html>"""


def normalize(text):
    """把用户输入变成 URL：带协议直接用，否则按域名补 https，含空格则搜索"""
    t = text.strip()
    if not t:
        return HOME_URL
    if t.startswith(("http://", "https://", "file://", "about:")):
        return t
    if " " in t or "." not in t:
        return "https://www.bing.com/search?q=" + GLib.uri_escape_string(t, None, False)
    return "https://" + t


class BrowserTab(Gtk.Box):
    """一个标签页：WebView + 顶部细进度条"""

    def __init__(self, on_update):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_update = on_update
        self.bar = Gtk.ProgressBar()
        self.bar.set_size_request(-1, 2)
        self.bar.set_fraction(0)
        self.pack_start(self.bar, False, False, 0)

        self.web = WebKit2.WebView()
        s = WebKit2.Settings()
        s.set_property("enable-plugins", False)
        s.set_property("enable-java", False)
        s.set_property("enable-smooth-scrolling", True)
        s.set_property("enable-html5-database", True)
        s.set_property("enable-html5-local-storage", True)
        self.web.set_settings(s)
        self.web.connect("notify::title", self._on_title)
        self.web.connect("notify::estimated-load-progress", self._on_progress)
        self.web.connect("load-changed", self._on_load)
        self.web.connect("create", self._on_create)
        self.web.connect("decide-policy", self._on_policy)
        self.pack_start(self.web, True, True, 0)
        self.show_all()
        self.bar.hide()

    def _on_title(self, web, _p):
        self.on_update(self)

    def _on_progress(self, web, _p):
        f = web.get_estimated_load_progress()
        if f >= 1.0:
            GLib.timeout_add(400, lambda: (self.bar.hide(), False)[1])
        else:
            self.bar.show()
            self.bar.set_fraction(f)

    def _on_load(self, web, _ev):
        self.on_update(self)

    def _on_create(self, web, action):
        """G.23：拦截 target="_blank" / JS window.open，直接在当前页打开，不弹终端"""
        uri = action.get_request().get_uri()
        if uri:
            web.load_uri(uri)
        return None

    def _on_policy(self, web, decision, decision_type):
        """G.23：外部导航策略——新窗口或下载请求在当前页或提示处理，不弹终端"""
        if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            uri = decision.get_request().get_uri()
            if uri:
                web.load_uri(uri)
            decision.ignore()
            return True
        if decision_type == WebKit2.PolicyDecisionType.RESPONSE:
            mime = decision.get_response().get_mime_type() or ""
            if mime.startswith("application/"):
                # 下载：由用户自行处理，不自动弹终端
                decision.ignore()
                C.send_icons(
                    {"cmd": "toast", "text": "检测到下载（%s），请手动处理" % mime[:40]}
                )
                return True
        return False

    @property
    def title(self):
        t = self.web.get_title()
        return t or "新标签页"


class Browser(Gtk.Window):
    def __init__(self, url=None):
        super().__init__(title="奇点浏览器")
        self.set_default_size(WIN_W, WIN_H)
        self.set_size_request(760, 480)

        C.setup_css(b"""
            .br-win { background: #1c1c1e; }
            .br-tool { background: #1c1c1e; }
            .br-entry { background: #2c2c2e; border: 1px solid #3a3a3c;
                        border-radius: 10px; color: #f5f5f7; padding: 6px 12px; }
            button { background: rgba(58,58,58,180); border: none;
                     border-radius: 8px; padding: 5px; }
            button:hover { background: rgba(91,120,255,180); }
            notebook header { background: #1c1c1e; }
            tab { background: #2c2c2e; color: #8e8e93; }
            tab:checked { background: #1c1c1e; color: #f5f5f7; }
            progressbar trough { background: #2c2c2e; min-height: 2px; }
            progressbar progress { background: #2e8dff; min-height: 2px; }
        """)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("br-win")
        self.add(root)

        # ---------- 工具栏 ----------
        bar = Gtk.Box(spacing=6)
        bar.set_size_request(-1, 46)
        bar.set_margin_start(8)
        bar.set_margin_end(8)
        bar.set_margin_top(6)
        bar.get_style_context().add_class("br-tool")

        self.btn_back = self._icon_btn("arrow-left", self.go_back)
        self.btn_fwd = self._icon_btn("arrow-right", self.go_forward)
        self.btn_reload = self._icon_btn("refresh-cw", self.reload)
        self.btn_home = self._icon_btn("house", self.go_home)
        for b in (self.btn_back, self.btn_fwd, self.btn_reload, self.btn_home):
            bar.pack_start(b, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("输入网址或搜索内容")
        self.entry.get_style_context().add_class("br-entry")
        self.entry.connect("activate", self._on_go)
        bar.pack_start(self.entry, True, True, 0)

        bar.pack_start(
            self._icon_btn("circle-plus", lambda _b: self.new_tab()), False, False, 0
        )
        root.pack_start(bar, False, False, 0)

        # ---------- 标签 ----------
        self.nb = Gtk.Notebook()
        self.nb.set_scrollable(True)
        self.nb.connect("switch-page", lambda _n, _p, _i: self._sync())
        root.pack_start(self.nb, True, True, 0)

        self.new_tab(url or None)
        self._sync()

    # ---------- 工具 ----------
    def _icon_btn(self, icon, fn):
        b = Gtk.Button()
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.add(C.icon_image(icon, 18, C.FG))
        b.connect("clicked", fn)
        return b

    def new_tab(self, url=None):
        tab = BrowserTab(self._on_tab_update)
        idx = self.nb.append_page(tab, self._tab_label(tab))
        self.nb.set_current_page(idx)
        self.show_all()
        if url:
            tab.web.load_uri(normalize(url))
        else:
            tab.web.load_html(HOME_HTML, "file:///")
        return tab

    def _tab_label(self, tab):
        box = Gtk.Box(spacing=6)
        lbl = Gtk.Label(label="新标签页")
        lbl.set_max_width_chars(16)
        lbl.set_ellipsize(3)
        box.pack_start(lbl, False, False, 0)
        close = Gtk.Button()
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.add(C.icon_image("x", 12, C.MUTED_FG))
        close.connect("clicked", lambda _b: self._close_tab(tab))
        box.pack_start(close, False, False, 0)
        box.show_all()
        tab._lbl = lbl
        return box

    def _close_tab(self, tab):
        idx = self.nb.page_num(tab)
        if idx >= 0:
            self.nb.remove_page(idx)
        if self.nb.get_n_pages() == 0:
            self.new_tab()

    @property
    def cur(self):
        i = self.nb.get_current_page()
        return self.nb.get_nth_page(i) if i >= 0 else None

    def _on_tab_update(self, tab):
        """网页标题/加载状态变化 → 更新标签文字、地址栏、按钮可用性"""
        if hasattr(tab, "_lbl"):
            tab._lbl.set_text(tab.title)
        if tab is self.cur:
            self._sync()

    def _sync(self):
        tab = self.cur
        if not tab:
            return
        uri = tab.web.get_uri() or ""
        if uri.startswith("file:///") or not uri:
            self.entry.set_text("")
        else:
            self.entry.set_text(uri)
        self.btn_back.set_sensitive(tab.web.can_go_back())
        self.btn_fwd.set_sensitive(tab.web.can_go_forward())

    # ---------- 动作 ----------
    def _on_go(self, _e):
        tab = self.cur
        if tab:
            tab.web.load_uri(normalize(self.entry.get_text()))

    def go_back(self, _b=None):
        if self.cur:
            self.cur.web.go_back()

    def go_forward(self, _b=None):
        if self.cur:
            self.cur.web.go_forward()

    def reload(self, _b=None):
        if self.cur:
            self.cur.web.reload()

    def go_home(self, _b=None):
        if self.cur:
            self.cur.web.load_html(HOME_HTML, "file:///")


def _single_instance_or_exit():
    try:
        import fcntl

        global _lock_fh
        _lock_fh = open("/tmp/qd_browser.lock", "w")
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except Exception:
        return False


def _apply_window_size(win):
    """G.23：qwm 接管窗口早于 GTK default_size 生效（实测 set_default_size
    会被 qwm 的 adopt fallback 覆盖），启动后显式向 qwm 发
    window_resize_absolute 强制按 settings.json 的 browser_w/h 校正尺寸。"""
    try:
        import socket
        import json as _json

        xid = win.get_window().get_xid() if win.get_window() else 0
        if not xid:
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect("/tmp/qd_qwm.sock")
        s.sendall(
            _json.dumps(
                {
                    "cmd": "window_resize_absolute",
                    "id": int(xid),
                    "w": WIN_W,
                    "h": WIN_H,
                }
            ).encode()
        )
        s.close()
    except Exception:
        pass
    return False  # 只跑一次


if __name__ == "__main__":
    if not _single_instance_or_exit():
        sys.exit(0)
    url = sys.argv[1] if len(sys.argv) > 1 else None
    win = Browser(url)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    # 等 qwm 完成窗口接管后校正尺寸；最多重试 5 次以防接管时序抖动
    for _i in range(5):
        GLib.timeout_add(300 * (_i + 1), _apply_window_size, win)
    Gtk.main()
