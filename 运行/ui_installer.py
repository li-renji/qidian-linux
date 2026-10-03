#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_installer.py —— 奇点OS 应用中心
# 用户令："用户安装软件不能像 Linux 那样麻烦，应该像 Windows 一样简单"
# 三种来源：
#   ① 精选：内置常用软件清单，点一下即装（无需输密码，sudoers 免密）
#   ② 搜索：按名字搜 Arch 仓库并安装
#   ③ 本地：选一个安装包（pkg.tar.zst / AppImage / deb / tar.gz）向导安装
# 装完自动登记到 ~/.config/qidos/apps.json，
# 并通知桌面图标层与快捷启动栏 reload —— 图标立刻出现在桌面和 Dock。
# 真正干活的 root 操作由 /usr/local/bin/qd-install 承担。
# ============================================================
import os, sys, json, subprocess, threading, shlex
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango
import qd_ui_common as C

WIN_W, WIN_H = 880, 620
QD_INSTALL = "/usr/local/bin/qd-install"

# 精选清单（pkg=仓库包名，cmd=装好后的启动命令，icon=本地图标资源名）
STORE_APPS = [
    {
        "id": "firefox",
        "pkg": "firefox",
        "icon": "globe",
        "name": "Firefox",
        "cmd": "firefox",
        "desc": "开源网页浏览器",
    },
    {
        "id": "chromium",
        "pkg": "chromium",
        "icon": "globe",
        "name": "Chromium",
        "cmd": "chromium",
        "desc": "Chrome 开源版",
    },
    {
        "id": "vlc",
        "pkg": "vlc",
        "icon": "circle-play",
        "name": "VLC",
        "cmd": "vlc",
        "desc": "万能媒体播放器",
    },
    {
        "id": "mpv",
        "pkg": "mpv",
        "icon": "circle-play",
        "name": "mpv",
        "cmd": "mpv",
        "desc": "轻量视频播放器",
    },
    {
        "id": "gimp",
        "pkg": "gimp",
        "icon": "pen-line",
        "name": "GIMP",
        "cmd": "gimp",
        "desc": "图像编辑器",
    },
    {
        "id": "mousepad",
        "pkg": "mousepad",
        "icon": "file-text",
        "name": "文本编辑器",
        "cmd": "mousepad",
        "desc": "轻量文本编辑",
    },
    {
        "id": "thunar",
        "pkg": "thunar",
        "icon": "folder-open",
        "name": "文件管理器",
        "cmd": "thunar",
        "desc": "图形化文件管理",
    },
    {
        "id": "gnome-calculator",
        "pkg": "gnome-calculator",
        "icon": "calculator",
        "name": "计算器",
        "cmd": "gnome-calculator",
        "desc": "系统计算器",
    },
    {
        "id": "flameshot",
        "pkg": "flameshot",
        "icon": "image",
        "name": "截图工具",
        "cmd": "flameshot",
        "desc": "截图与标注",
    },
    {
        "id": "geany",
        "pkg": "geany",
        "icon": "file-text",
        "name": "Geany",
        "cmd": "geany",
        "desc": "代码编辑器",
    },
    {
        "id": "audacious",
        "pkg": "audacious",
        "icon": "circle-play",
        "name": "音乐播放器",
        "cmd": "audacious",
        "desc": "音频播放",
    },
    {
        "id": "transmission-gtk",
        "pkg": "transmission-gtk",
        "icon": "arrow-down",
        "name": "下载器",
        "cmd": "transmission-gtk",
        "desc": "BT 下载工具",
    },
]


def _sudo_install(args):
    """经 sudoers 免密调用安装后端"""
    return subprocess.run(
        ["sudo", "-n", QD_INSTALL] + args, capture_output=True, text=True, timeout=900
    )


def _installed_ids():
    return {a.get("id") for a in C.load_installed()}


class Installer(Gtk.Window):
    def __init__(self):
        super().__init__(title="应用中心")
        self.set_default_size(WIN_W, WIN_H)
        self.set_size_request(760, 520)
        self.set_resizable(True)

        C.setup_css(b"""
            .in-win { background: #1c1c1e; }
            .in-side { background: #141416; }
            .in-side listbox, .in-side listbox row { background: transparent; }
            .in-side listbox row:selected { background: rgba(46,141,255,110);
                                            border-radius: 8px; }
            .in-side label { color: #f5f5f7; }
            .in-card { background: #000000; border: 1px solid #3a3a3c;
                       border-radius: 12px; }
            .in-row { background: #2c2c2e; border-radius: 10px; }
            button { background: rgba(58,58,58,180); border: none;
                     border-radius: 8px; color: #f5f5f7; padding: 4px 10px; }
            button:hover { background: rgba(91,120,255,180); }
            button.in-install { background: #2e8dff; }
            button.in-install:hover { background: #5ba8ff; }
            entry { background: #2c2c2e; border: 1px solid #3a3a3c;
                    border-radius: 8px; color: #f5f5f7; padding: 6px 10px; }
            listbox row { background: transparent; }
            listbox row:selected { background: rgba(46,141,255,90); }
        """)

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.get_style_context().add_class("in-win")
        self.add(root)

        # ---------- 侧边栏 ----------
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_size_request(130, -1)
        side.set_margin_top(10)
        side.set_margin_start(8)
        side.set_margin_end(8)
        side.get_style_context().add_class("in-side")
        self.lb = Gtk.ListBox()
        self.lb.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for i, (name, icon) in enumerate(
            [
                ("精选", "layout-grid"),
                ("搜索", "funnel"),
                ("本地安装", "box"),
                ("我的应用", "check"),
            ]
        ):
            row = Gtk.Box(spacing=8)
            row.set_margin_top(8)
            row.set_margin_bottom(8)
            row.set_margin_start(10)
            row.pack_start(C.icon_image(icon, 16, C.FG), False, False, 0)
            row.pack_start(C.label(name, 12, C.FG), False, False, 0)
            self.lb.add(row)
        self.lb.connect("row-selected", self._on_side)
        side.pack_start(self.lb, False, False, 0)
        root.pack_start(side, False, False, 0)

        # ---------- 内容栈 ----------
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scr.add(self.stack)
        scr.set_margin_top(10)
        scr.set_margin_bottom(10)
        scr.set_margin_end(10)

        self.page_store = self._build_store()
        self.page_search = self._build_search()
        self.page_local = self._build_local()
        self.page_mine = self._build_mine()
        for key, page in (
            ("store", self.page_store),
            ("search", self.page_search),
            ("local", self.page_local),
            ("mine", self.page_mine),
        ):
            self.stack.add_named(page, key)

        # 内容区 + 底部状态条（安装过程可见）
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_end(10)
        box.pack_start(scr, True, True, 0)
        self.log = Gtk.Label()
        self.log.set_xalign(0)
        self.log.set_line_wrap(True)
        self.log.set_max_width_chars(90)
        box.pack_start(self.log, False, False, 0)
        root.pack_start(box, True, True, 0)

        self.lb.select_row(self.lb.get_row_at_index(0))

    # ---------- 页：精选 ----------
    def _build_store(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_start(10)
        page.set_margin_end(10)
        page.pack_start(C.label("精选应用", 15, C.FG, "bold"), False, False, 0)
        page.pack_start(
            C.label("点一下即安装，装完图标自动出现在桌面与快捷栏", 11, C.MUTED_FG),
            False,
            False,
            0,
        )
        self.store_flow = Gtk.FlowBox()
        self.store_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.store_flow.set_min_children_per_line(3)
        self.store_flow.set_max_children_per_line(3)
        self.store_flow.set_row_spacing(10)
        self.store_flow.set_column_spacing(10)
        page.pack_start(self.store_flow, False, False, 0)
        self._refresh_store()
        return page

    def _refresh_store(self):
        for c in self.store_flow.get_children():
            self.store_flow.remove(c)
        ids = _installed_ids()
        for app in STORE_APPS:
            self.store_flow.add(self._store_card(app, app["id"] in ids))

    def _store_card(self, app, installed):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.set_size_request(200, 118)
        card.set_margin_top(10)
        card.set_margin_bottom(6)
        card.set_margin_start(10)
        card.set_margin_end(10)
        card.get_style_context().add_class("in-card")

        top = Gtk.Box(spacing=10)
        top.set_margin_top(10)
        top.set_margin_start(10)
        top.pack_start(C.icon_image(app["icon"], 34, C.PRIMARY), False, False, 0)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vb.pack_start(C.label(app["name"], 13, C.FG, "bold"), False, False, 0)
        d = C.label(app["desc"], 10, C.MUTED_FG)
        d.set_ellipsize(Pango.EllipsizeMode.END)
        d.set_max_width_chars(14)
        vb.pack_start(d, False, False, 0)
        top.pack_start(vb, False, False, 0)
        card.pack_start(top, False, False, 0)

        if installed:
            b = Gtk.Button(label="打开")
            b.connect("clicked", lambda _w: self._launch(app["cmd"], app["name"]))
        else:
            b = Gtk.Button(label="安装")
            b.get_style_context().add_class("in-install")
            b.connect("clicked", lambda _w, a=app: self._install_repo(a))
        b.set_margin_start(10)
        b.set_margin_end(10)
        b.set_margin_bottom(10)
        card.pack_end(b, False, False, 0)
        return card

    # ---------- 页：搜索 ----------
    def _build_search(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_start(10)
        page.set_margin_end(10)
        page.pack_start(C.label("搜索安装", 15, C.FG, "bold"), False, False, 0)

        row = Gtk.Box(spacing=8)
        self.se = Gtk.Entry()
        self.se.set_placeholder_text("输入软件名，例如 gimp")
        self.se.connect("activate", lambda _e: self._do_search())
        row.pack_start(self.se, True, True, 0)
        b = Gtk.Button(label="搜索")
        b.connect("clicked", lambda _w: self._do_search())
        row.pack_start(b, False, False, 0)
        page.pack_start(row, False, False, 0)

        self.search_box = Gtk.ListBox()
        self.search_box.set_selection_mode(Gtk.SelectionMode.NONE)
        page.pack_start(self.search_box, False, False, 0)
        return page

    def _do_search(self):
        kw = self.se.get_text().strip()
        if not kw:
            return
        self._log("正在搜索「%s」…" % kw)
        for r in self.search_box.get_children():
            self.search_box.remove(r)

        def work():
            try:
                out = _sudo_install(["search", kw]).stdout
            except Exception as e:
                out = ""
                self._log("搜索失败：%s" % e)
            GLib.idle_add(self._fill_search, out, kw)

        threading.Thread(target=work, daemon=True).start()

    def _fill_search(self, out, kw):
        for r in self.search_box.get_children():
            self.search_box.remove(r)
        n = 0
        cur = None
        for line in out.splitlines():
            if not line.strip():
                continue
            if not line.startswith(" "):  # 仓库/包名 行
                parts = line.split("/")
                cur = parts[1].split(" ", 1)[0] if len(parts) > 1 else None
            else:  # 描述行
                if cur and n < 24:
                    self.search_box.add(self._search_row(cur, line.strip()))
                    n += 1
                cur = None
        self.search_box.show_all()
        self._log("找到 %d 个结果" % n)
        return False

    def _search_row(self, pkg, desc):
        row = Gtk.Box(spacing=10)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.get_style_context().add_class("in-row")
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vb.pack_start(C.label(pkg, 12, C.FG, "bold"), False, False, 0)
        d = C.label(desc[:70], 10, C.MUTED_FG)
        d.set_ellipsize(Pango.EllipsizeMode.END)
        d.set_max_width_chars(50)
        vb.pack_start(d, False, False, 0)
        row.pack_start(vb, True, True, 0)
        b = Gtk.Button(label="安装")
        b.get_style_context().add_class("in-install")
        b.connect(
            "clicked",
            lambda _w: self._install_repo(
                {
                    "id": pkg,
                    "pkg": pkg,
                    "icon": "box",
                    "name": pkg,
                    "cmd": pkg,
                    "desc": desc,
                }
            ),
        )
        row.pack_start(b, False, False, 0)
        return row

    # ---------- 页：本地安装 ----------
    def _build_local(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_start(10)
        page.set_margin_end(10)
        page.pack_start(C.label("本地安装", 15, C.FG, "bold"), False, False, 0)
        page.pack_start(
            C.label(
                "支持 pkg.tar.zst / AppImage / deb / tar.gz，选完填个名字即可安装",
                11,
                C.MUTED_FG,
            ),
            False,
            False,
            0,
        )

        self.local_path = None
        row = Gtk.Box(spacing=8)
        self.lp_lbl = C.label("未选择文件", 12, C.MUTED_FG)
        self.lp_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lp_lbl.set_max_width_chars(40)
        row.pack_start(self.lp_lbl, True, True, 0)
        b = Gtk.Button(label="选择安装包")
        b.connect("clicked", self._pick_local)
        row.pack_start(b, False, False, 0)
        page.pack_start(row, False, False, 0)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        form.set_margin_top(10)
        self.le_name = self._field(form, "应用名称")
        self.le_cmd = self._field(form, "启动命令")
        page.pack_start(form, False, False, 0)

        ib = Gtk.Button(label="开始安装")
        ib.get_style_context().add_class("in-install")
        ib.connect("clicked", self._install_local)
        page.pack_start(ib, False, False, 0)
        return page

    def _field(self, parent, title):
        box = Gtk.Box(spacing=8)
        lbl = C.label(title, 12, C.FG)
        lbl.set_size_request(90, -1)
        lbl.set_xalign(0)
        box.pack_start(lbl, False, False, 0)
        e = Gtk.Entry()
        box.pack_start(e, True, True, 0)
        parent.pack_start(box, False, False, 0)
        return e

    def _pick_local(self, _b):
        p = C.run_picker(
            mode="open",
            root="/home/qduser/下载",
            filters="pkg.tar.zst,pkg.tar.xz,AppImage,deb,tar.gz,tar.xz",
        )
        if not p:
            return
        self.local_path = p
        self.lp_lbl.set_markup(
            '<span font_family="%s" font_size="12000" color="%s">%s</span>'
            % (C.FONT, C.FG, os.path.basename(p))
        )
        base = os.path.basename(p)
        for suf in (
            ".pkg.tar.zst",
            ".pkg.tar.xz",
            ".AppImage",
            ".deb",
            ".tar.gz",
            ".tar.xz",
        ):
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        self.le_name.set_text(base)
        self.le_cmd.set_text("")

    def _install_local(self, _b):
        if not self.local_path:
            self._log("请先选择安装包")
            return
        name = self.le_name.get_text().strip() or "应用"
        cmd = self.le_cmd.get_text().strip()
        path = self.local_path
        self._log("正在安装本地包 %s …（可能需要几分钟）" % os.path.basename(path))

        def work():
            try:
                r = _sudo_install(["local", path])
                out = (r.stdout or "") + (r.stderr or "")
            except Exception as e:
                out = "ERR:%s" % e
            ok = "ERR:" not in out or out.startswith("OK:")
            final_cmd = cmd
            if not final_cmd:
                # 从安装输出里找可执行路径，找不到就退回包名
                for line in out.splitlines():
                    if line.startswith("OK:"):
                        final_cmd = line[3:].strip()
                        break
                if not final_cmd:
                    final_cmd = name
            GLib.idle_add(
                self._after_install,
                {
                    "id": "local-" + name,
                    "icon": "box",
                    "name": name,
                    "cmd": final_cmd,
                    "src": "local",
                },
                out,
                ok,
            )

        threading.Thread(target=work, daemon=True).start()

    # ---------- 页：我的应用 ----------
    def _build_mine(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_start(10)
        page.set_margin_end(10)
        page.pack_start(C.label("我的应用", 15, C.FG, "bold"), False, False, 0)
        page.pack_start(
            C.label("已安装的软件，可打开或移除图标", 11, C.MUTED_FG), False, False, 0
        )
        self.mine_box = Gtk.ListBox()
        self.mine_box.set_selection_mode(Gtk.SelectionMode.NONE)
        page.pack_start(self.mine_box, False, False, 0)
        self._refresh_mine()
        return page

    def _refresh_mine(self):
        for r in self.mine_box.get_children():
            self.mine_box.remove(r)
        apps = C.load_installed()
        if not apps:
            self.mine_box.add(C.label("还没有安装任何软件", 12, C.MUTED_FG))
        for a in apps:
            self.mine_box.add(self._mine_row(a))
        self.mine_box.show_all()

    def _mine_row(self, a):
        row = Gtk.Box(spacing=10)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.get_style_context().add_class("in-row")
        row.pack_start(
            C.icon_image(a.get("icon", "box"), 22, C.PRIMARY), False, False, 0
        )
        row.pack_start(C.label(a.get("name", "?"), 12, C.FG, "bold"), False, False, 0)
        cmd = C.label(a.get("cmd", ""), 10, C.MUTED_FG)
        cmd.set_ellipsize(Pango.EllipsizeMode.END)
        cmd.set_max_width_chars(30)
        row.pack_start(cmd, True, True, 0)
        bo = Gtk.Button(label="打开")
        bo.connect("clicked", lambda _w: self._launch(a.get("cmd"), a.get("name")))
        row.pack_start(bo, False, False, 0)
        br = Gtk.Button(label="移除")
        br.connect("clicked", lambda _w, x=a: self._remove(x))
        row.pack_start(br, False, False, 0)
        return row

    # ---------- 安装/卸载 ----------
    def _install_repo(self, app):
        self._log("正在安装 %s …（首次需下载，请稍候）" % app["name"])

        def work():
            try:
                r = _sudo_install(["repo", app["pkg"]])
                out = (r.stdout or "") + (r.stderr or "")
                ok = r.returncode == 0
            except Exception as e:
                out = str(e)
                ok = False
            GLib.idle_add(
                self._after_install,
                {
                    "id": app["id"],
                    "icon": app["icon"],
                    "name": app["name"],
                    "cmd": app["cmd"],
                    "src": "repo",
                    "pkg": app.get("pkg"),
                },
                out,
                ok,
            )

        threading.Thread(target=work, daemon=True).start()

    def _after_install(self, rec, out, ok):
        tail = [l for l in out.strip().splitlines() if l.strip()][-1:] or [""]
        if ok:
            C.add_installed(rec)
            C.send_icons({"cmd": "reload"})
            C.send_dock({"cmd": "reload"})
            self._log("✅ %s 安装完成，图标已出现在桌面与快捷栏" % rec["name"])
            C.send_icons({"cmd": "toast", "text": rec["name"] + " 安装完成"})
            self._refresh_store()
            self._refresh_mine()
        else:
            self._log("❌ %s 安装失败：%s" % (rec["name"], tail[0][:120]))
            C.send_icons({"cmd": "toast", "text": rec["name"] + " 安装失败"})
        return False

    def _remove(self, a):
        # G.18 真卸载：repo 来源走 qd-install remove（pacman -Rs）；
        # 本地 tar/AppImage 本体不在 pacman 库，退回只清注册表（旧行为）。
        if a.get("src") == "repo" and a.get("pkg"):
            self._log("正在卸载 %s …" % a.get("name"))

            def work():
                try:
                    r = _sudo_install(["remove", a["pkg"]])
                    out = (r.stdout or "") + (r.stderr or "")
                    ok = r.returncode == 0
                except Exception as e:
                    out, ok = str(e), False
                GLib.idle_add(self._after_remove, a, out, ok)

            threading.Thread(target=work, daemon=True).start()
            return
        self._remove_registry_only(a)

    def _after_remove(self, a, out, ok):
        tail = [l for l in out.strip().splitlines() if l.strip()][-1:] or [""]
        if ok:
            self._remove_registry_only(a)
            self._log("✅ %s 已卸载完成" % a.get("name"))
        else:
            self._log("❌ 卸载失败：%s" % tail[0][:120])

    def _remove_registry_only(self, a):
        apps = [x for x in C.load_installed() if x.get("id") != a.get("id")]
        C.save_installed(apps)
        C.send_icons({"cmd": "reload"})
        C.send_dock({"cmd": "reload"})
        self._refresh_store()
        self._refresh_mine()
        self._log("已移除 %s 的图标（软件本体保留）" % a.get("name"))

    @staticmethod
    def _launch(cmd, name):
        if not cmd:
            return
        try:
            subprocess.Popen(
                shlex.split(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            C.send_icons({"cmd": "toast", "text": (name or "应用") + " 启动失败"})

    # ---------- 杂项 ----------
    def _log(self, text):
        # 安装输出可能含 & < 等字符，必须先转义再进 markup
        safe = GLib.markup_escape_text(str(text))
        self.log.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%s</span>'
            % (C.FONT, C.MUTED_FG, safe)
        )

    def _on_side(self, _lb, row):
        if row is None:
            return
        keys = ("store", "search", "local", "mine")
        i = row.get_index()
        if 0 <= i < len(keys):
            self.stack.set_visible_child_name(keys[i])


def _single_instance_or_exit():
    try:
        import fcntl

        global _lock_fh
        _lock_fh = open("/tmp/qd_installer.lock", "w")
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if not _single_instance_or_exit():
        sys.exit(0)
    win = Installer()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
