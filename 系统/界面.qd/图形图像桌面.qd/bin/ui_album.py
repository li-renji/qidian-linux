#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_album.py —— 奇点OS 相册（系统级影像聚合 + AI 查询接口）
# 规范要求（用户令 2026-09-27）：
#   · 整个系统的所有照片/视频都能看到，无论散落在哪个文件夹
#   · 相册可快速定位到照片原位置
#   · 系统级 AI 可经相册快速定位照片（→ 本地 JSON 查询接口）
# 架构：常驻后台服务（索引 + socket），窗口按需创建
#   · .xinitrc 常驻：python3 /奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_album.py
#   · 打开界面：    python3 /奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_album.py --show（单例，已运行则请求开窗）
#   · socket /tmp/qd_album.sock：show / search / stats / rescan / locate / open
# 依赖的 UI token 全部来自 qd_ui_common（TreeSolo 原型）
# ============================================================
import os, sys, gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango
import qd_ui_common as C

WIN_W, WIN_H = 900, 640
HEADER_H, FOOTER_H = 44, 28

# 预览缩略图尺寸范围（G.21：设置迁到"设置›个性化›相册"，相册内不再放控件；
# 最小 40——太小时无法看清内容，只能点击看图，符合用户预期）
THUMB_MIN, THUMB_MAX = 40, 320
THUMB_DEFAULT = 160


def _thumb_cfg():
    """读相册预览设置：{'on': bool, 'size': px}"""
    cfg = C.load_conf()
    try:
        size = int(cfg.get("album_thumb", THUMB_DEFAULT))
    except Exception:
        size = THUMB_DEFAULT
    size = max(THUMB_MIN, min(THUMB_MAX, size))
    return {"on": bool(cfg.get("album_preview", True)), "size": size}


def _save_thumb_cfg(on=None, size=None):
    cfg = C.load_conf()
    keys = []
    if on is not None:
        cfg["album_preview"] = on
        keys.append("album_preview")
    if size is not None:
        cfg["album_thumb"] = int(size)
        keys.append("album_thumb")
    C.save_conf(cfg, keys)


IMG_EXT = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".svg",
    ".tiff",
    ".ico",
    ".avif",
}
VID_EXT = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".flv",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".ts",
}
# 扫描排除：虚拟文件系统/易变的临时区（避免走进 /proc 这种无底洞）
SCAN_ROOTS = [
    os.path.expanduser("~/图片"),
    os.path.expanduser("~/Pictures"),
    os.path.expanduser("~/桌面"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/下载"),
    "/奇点OS/用户.qd",
    "/奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_assets",
]

EXCLUDE = {
    os.path.expanduser("~/.cache"),
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/tmp",
    "/var/tmp",
    "/lost+found",
    "/media",
    "/mnt",
    "/boot",
    "/proc/sys",
}

CACHE_FILE = "/home/qduser/.cache/qidos_album.json"
SOCK_PATH = "/tmp/qd_album.sock"
FILTERS = (("all", "全部"), ("image", "图片"), ("video", "视频"))


def _rgba(color, a=1.0):
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)) + (a,)


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXT:
        return "image"
    if ext in VID_EXT:
        return "video"
    return None


class Album(object):
    """索引 + 窗口管理（主实例）；socket 线程只发 idle 回调"""

    def __init__(self):
        self.items = {}  # path -> dict
        self.win = None
        self.scanning = False
        self.filter_kind = "all"
        self.query = ""
        self._load_cache()

    # ---------- 索引 ----------
    def _load_cache(self):
        try:
            import json

            if os.path.exists(CACHE_FILE):
                for rec in json.load(open(CACHE_FILE)):
                    if os.path.exists(rec["path"]):
                        self.items[rec["path"]] = rec
        except Exception:
            pass

    def _save_cache(self):
        try:
            import json

            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            json.dump(
                list(self.items.values()), open(CACHE_FILE, "w"), ensure_ascii=False
            )
        except Exception:
            pass

    def scan(self, progress_cb=None):
        "限定目录扫描（线程内执行）"
        if self.scanning:
            return
        self.scanning = True
        found = {}
        n = 0
        for base in SCAN_ROOTS:
            for root, dirs, files in os.walk(
                base, topdown=True, followlinks=False, onerror=None
            ):
                if root in EXCLUDE:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if os.path.join(root, d) not in EXCLUDE]
                for name in files:
                    pth = os.path.join(root, name)
                    k = kind_of(pth)
                    if k is None:
                        continue
                    try:
                        st = os.stat(pth)
                        found[pth] = {
                            "path": pth,
                            "name": name,
                            "type": k,
                            "size": st.st_size,
                            "mtime": int(st.st_mtime),
                        }
                    except OSError:
                        continue
                    n += 1
                    if progress_cb and n % 200 == 0:
                        progress_cb(len(found))
        self.items = found
        self.scanning = False
        self._save_cache()
        if progress_cb:
            progress_cb(-1)

    def search(self, q="", kind="all", limit=50):
        q = (q or "").lower()
        out = []
        for it in self.items.values():
            if kind != "all" and it["type"] != kind:
                continue
            if q and q not in it["path"].lower():
                continue
            out.append(it)
        out.sort(key=lambda i: (-i["mtime"], i["path"]))
        return out[:limit]

    def stats(self):
        n_img = sum(1 for i in self.items.values() if i["type"] == "image")
        return {
            "total": len(self.items),
            "image": n_img,
            "video": len(self.items) - n_img,
            "scanning": self.scanning,
        }

    # ---------- 定位（给 AI / UI 共用） ----------
    @staticmethod
    def locate(path):
        """定位到照片原位置：打开文件浏览器并以该目录为根"""
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            return False
        try:
            import subprocess

            subprocess.Popen(
                ["python3", "/奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_filebrowser.py", folder],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    # ---------- 窗口 ----------

    def _on_scan_progress(self, n):
        if n == -1 and self.win is not None:
            self.win.refresh()
        return False

    def ensure_window(self):
        if self.win is None:
            self.win = AlbumWindow(self)
        self.win.show_all()
        self.win.present()
        return self.win


class ItemCard(Gtk.Frame):
    """一张缩略图卡片。G.20：尺寸/预览开关由构造参数传入（相册头部调节）"""

    def __init__(self, rec, on_open, on_loc, thumb_w=160, thumb_h=110, preview=True):
        super().__init__()
        self.rec = rec
        self.tw, self.th, self.preview = thumb_w, thumb_h, preview
        self.set_size_request(thumb_w + 12, thumb_h + 62)
        self.set_shadow_type(Gtk.ShadowType.NONE)
        self.get_style_context().add_class("ab-card")

        self.ev = Gtk.EventBox()
        self.ev.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        self.ev.connect("button-press-event", self._press, on_open, on_loc)

        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        v.set_margin_top(6)
        v.set_margin_bottom(6)
        v.set_margin_start(6)
        v.set_margin_end(6)
        self.ev.add(v)

        self.img = Gtk.Image()
        self.img.set_size_request(thumb_w, thumb_h)
        v.pack_start(self.img, False, False, 0)

        self.lbl_name = Gtk.Label()
        self.lbl_name.set_max_width_chars(max(10, thumb_w // 9))
        self.lbl_name.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_name.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%s</span>'
            % (C.FONT, C.FG, _esc(rec["name"]))
        )
        v.pack_start(self.lbl_name, False, False, 0)

        self.lbl_path = Gtk.Label()
        self.lbl_path.set_max_width_chars(max(12, thumb_w // 8))
        self.lbl_path.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_path.set_markup(
            '<span font_family="%s" font_size="9000" color="%s">%s</span>'
            % (C.FONT, C.MUTED_FG, _esc(os.path.dirname(rec["path"]) or "/"))
        )
        v.pack_start(self.lbl_path, False, False, 0)

        self.add(self.ev)
        self.load_thumb_async()

    def _press(self, _w, e, on_open, on_loc):
        # G.20 用户令：点击图片直接打开图片（此前双击是定位文件夹，反了）
        # 左键（单击或双击）→ 看图；右键 → 定位原目录
        if e.button == 1:
            on_open(self.rec)
        elif e.button == 3:
            on_loc(self.rec)
        return False

    def load_thumb_async(self):
        GLib.idle_add(self._do_thumb)

    def _do_thumb(self):
        rec = self.rec
        pb = None
        if self.preview and rec["type"] == "image":
            try:
                loader = GdkPixbuf.PixbufLoader.new()
                loader.set_size(self.tw, self.th)
                with open(rec["path"], "rb") as fh:
                    loader.write(fh.read())
                loader.close()
                pb = loader.get_pixbuf()
            except Exception:
                pb = None
        if pb is None:
            icon = "image" if rec["type"] == "image" else "circle-play"
            pb = C.pixbuf_icon(icon, min(48, self.tw // 2), C.PRIMARY)
        if pb is not None:
            self.img.set_from_pixbuf(pb)
        return False


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class AlbumWindow(Gtk.Window):
    def __init__(self, album):
        super().__init__(title="相册")
        self.album = album
        tc = _thumb_cfg()
        self.thumb_on, self.thumb_size = tc["on"], tc["size"]
        self.set_default_size(WIN_W, WIN_H)
        self.set_size_request(560, 380)
        self.set_resizable(True)  # G.20：可缩放，FlowBox 自适应换行
        self.move(180, 80)

        C.setup_css(b"""
            .ab-panel { background: #1c1c1e; }
            .ab-card { background: #000000; border: 1px solid #3a3a3c;
                       border-radius: 10px; }
            .ab-card.sel { border: 1px solid #2e8dff; }
            .ab-head button, .ab-foot button { background: transparent;
                       border: none; min-width: 0; min-height: 0; padding: 3px; }
            .ab-head button:hover { background: rgba(58,58,60,180);
                       border-radius: 4px; }
            entry { background: #000000; color: #f5f5f7; border: 1px solid #3a3a3c;
                    border-radius: 8px; font-size: 12px; }
        """)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.get_style_context().add_class("ab-panel")
        self.add(box)

        # ---------- 头部 ----------
        head = Gtk.Box(spacing=8)
        head.set_size_request(-1, HEADER_H)
        head.set_margin_start(10)
        head.set_margin_end(10)
        head.get_style_context().add_class("ab-head")
        head.pack_start(C.icon_image("image", 18, C.PRIMARY), False, False, 0)
        head.pack_start(C.label("奇点OS 相册", 13, C.FG, "bold"), False, False, 0)

        self.search = Gtk.Entry()
        self.search.set_placeholder_text("搜索文件名或路径…")
        self.search.set_size_request(200, 26)
        self.search.connect("changed", lambda e: self.refresh())
        head.pack_end(self.search, False, False, 0)

        self.combo = Gtk.ComboBoxText()
        for _, name in FILTERS:
            self.combo.append_text(name)
        self.combo.set_active(0)
        self.combo.connect("changed", lambda e: self.refresh())
        head.pack_end(self.combo, False, False, 0)

        btn = Gtk.Button()
        btn.add(C.icon_image("refresh-cw", 16, C.FG))
        btn.set_tooltip_text("重新扫描整个系统")
        btn.connect("clicked", lambda b: self.rescan())
        head.pack_end(btn, False, False, 0)
        # G.21 用户令：预览开关/尺寸迁至 设置›个性化›相册（相册内控件"很丑"）

        box.pack_start(head, False, False, 0)

        # ---------- 主体 ----------
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.get_style_context().add_class("ab-panel")
        self.flow = Gtk.FlowBox()
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_homogeneous(False)
        self.flow.set_min_children_per_line(4)
        self.flow.set_max_children_per_line(4)
        self.flow.set_row_spacing(8)
        self.flow.set_column_spacing(8)
        self.flow.set_margin_top(8)
        self.flow.set_margin_bottom(8)
        self.flow.set_margin_start(10)
        self.flow.set_margin_end(10)
        self.scrolled.add(self.flow)
        box.pack_start(self.scrolled, True, True, 0)

        # ---------- 底部 ----------
        foot = Gtk.Box(spacing=6)
        foot.set_size_request(-1, FOOTER_H)
        foot.set_margin_start(10)
        foot.get_style_context().add_class("ab-foot")
        self.status = C.label("", 11, C.MUTED_FG)
        foot.pack_start(self.status, False, False, 0)
        box.pack_end(foot, False, False, 0)

        self.cards = []
        self.refresh()

    # ---------- 渲染 ----------
    def refresh(self):
        for c in self.cards:
            self.flow.remove(c)
        self.cards = []
        q = self.search.get_text().strip().lower()
        kind = FILTERS[self.combo.get_active()][0]
        items = self.album.search(q, kind, limit=400)
        tw = self.thumb_size
        th = int(tw * 0.68)
        for rec in items:
            card = ItemCard(
                rec,
                self.on_open,
                self.on_locate,
                thumb_w=tw,
                thumb_h=th,
                preview=self.thumb_on,
            )
            self.flow.add(card)
            self.cards.append(card)
        self.flow.show_all()
        st = self.album.stats()
        self.status.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%d 项'
            "（共索引 %d：%d 图 / %d 视频）%s · 点击看图 · 右键定位原目录"
            " · AI 接口 /tmp/qd_album.sock</span>"
            % (
                C.FONT,
                C.MUTED_FG,
                len(items),
                st["total"],
                st["image"],
                st["video"],
                " · 扫描中…" if st["scanning"] else "",
            )
        )
        return False

    def rescan(self):
        def worker():
            self.album.scan(progress_cb=lambda n: GLib.idle_add(self._tick, n))

        import threading

        threading.Thread(target=worker, daemon=True).start()
        C.send_icons({"cmd": "toast", "text": "正在扫描整个系统…"})
        return False

    def _tick(self, n):
        if n == -1:
            self.refresh()
        else:
            self.status.set_markup(
                '<span font_family="%s" font_size="11000" color="%s">已发现 %d 项…</span>'
                % (C.FONT, C.MUTED_FG, n)
            )
        return False

    # ---------- 交互 ----------
    def on_open(self, rec):
        """G.20 用户令：点击图片 → 直接打开图片查看器（不再是选中）"""
        if rec["type"] == "image":
            import subprocess

            subprocess.Popen(
                ["python3", "/奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_imageview.py", rec["path"]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            self.status.set_markup(
                '<span font_family="%s" font_size="11000" color="%s">'
                "视频播放器尚未内置：%s</span>"
                % (C.FONT, C.MUTED_FG, _esc(rec["path"]))
            )
        return False

    def on_locate(self, rec):
        self.album.locate(rec["path"])
        C.send_icons({"cmd": "toast", "text": "已定位到 " + rec["name"]})
        return False


# ---------- socket 服务（AI 整合入口） ----------
def serve(album):
    import socket, json, threading

    path = SOCK_PATH
    try:
        os.unlink(path)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)

    def handle(conn):
        try:
            data = conn.recv(65536).decode("utf-8", "replace")
            cmd = json.loads(data) if data.strip() else {}
            act = cmd.get("cmd", "")
            if act == "show":
                GLib.idle_add(album.ensure_window)
                r = {"ok": True}
            elif act == "hide":
                GLib.idle_add(lambda: album.win and album.win.hide())
                r = {"ok": True}
            elif act == "search":
                r = {
                    "ok": True,
                    "items": album.search(
                        cmd.get("q", ""),
                        cmd.get("type", "all"),
                        int(cmd.get("limit", 50)),
                    ),
                }
            elif act == "stats":
                r = {"ok": True, "stats": album.stats()}
            elif act == "locate":
                r = {"ok": album.locate(cmd.get("path", ""))}
            elif act == "open":
                p = cmd.get("path", "")
                if os.path.exists(p):
                    import subprocess

                    subprocess.Popen(
                        ["xdg-open", p],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    r = {"ok": True}
                else:
                    r = {"ok": False, "err": "not found"}
            elif act == "rescan":

                def worker():
                    album.scan(
                        progress_cb=lambda n: GLib.idle_add(album._on_scan_progress, n)
                    )

                threading.Thread(target=worker, daemon=True).start()
                r = {"ok": True}
            else:
                r = {"ok": False, "err": "unknown cmd"}
            conn.sendall(json.dumps(r, ensure_ascii=False).encode())
        except Exception as e:
            try:
                conn.sendall(json.dumps({"ok": False, "err": repr(e)[:120]}).encode())
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
            except Exception:
                break

    threading.Thread(target=loop, daemon=True).start()


def _noop(*a):
    return False


def _request(obj):
    """向已运行实例发指令（单例/AI 调用共用）"""
    import socket, json

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(SOCK_PATH)
        s.sendall(json.dumps(obj).encode())
        data = s.recv(1 << 20).decode("utf-8", "replace")
        s.close()
        return json.loads(data)
    except Exception as e:
        return {"ok": False, "err": repr(e)[:120]}


def main():
    args = sys.argv[1:]
    # 已运行 → 转发指令后退出（单例）
    probe = _request({"cmd": "stats"})
    if probe.get("ok"):
        if "--show" in args:
            _request({"cmd": "show"})
        elif "--search" in args:
            q = args[args.index("--search") + 1]
            print(json_dumps(_request({"cmd": "search", "q": q})))
        return
    album = Album()
    serve(album)
    if "--show" in args:
        GLib.idle_add(album.ensure_window)
    # 启动后后台补一轮扫描
    import threading

    threading.Thread(
        target=album.scan,
        daemon=True,
        kwargs={"progress_cb": lambda n: GLib.idle_add(_noop)},
    ).start()
    Gtk.main()


def json_dumps(o):
    import json

    return json.dumps(o, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
