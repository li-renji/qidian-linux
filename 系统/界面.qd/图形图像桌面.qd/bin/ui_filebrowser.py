#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_filebrowser.py —— 目录导图文件浏览器（TreeSolo 原型 1:1 复刻）
# 蓝本：ui_design/pages/index.html #file-browser（L435-538 + CSS L774-837）
#   · 头部：folder-open(主色) + "奇点OS 目录导图" + house/refresh-cw/chevrons-down-up
#   · 树：虚线连接线(1.5px dashed #3a3a3c)、缩进 20px/级
#     - 文件夹节点：圆角 8 卡片 bg #1c1c1e border #3a3a3c；展开态 bg #20334b border 主色
#     - 根节点：bg #3a3a3c border 主色
#     - 文件节点：bg #000000 border #3a3a3c 文字 #8e8e93
#     - 箭头 ▾/▸ 主色 10px；folder/folder-open 16px 主色；file-text 16px
#   · 底部状态条："当前根：/奇点OS · 单击目录展开 · 单击文件打开"
#   · 单击目录展开（懒加载真实文件系统）；单击文件 toast
#   · 交互与原型一致：house 回根/refresh 刷新/chevrons 全部收起
# ============================================================
import os, sys
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Gdk, GLib, Pango, PangoCairo
import cairo
import qd_ui_common as C

# ROOT_PATH 支持命令行指定（相册"定位到此目录"：python3 ui_filebrowser.py <目录>）
_arg_root = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None
ROOT_PATH = _arg_root or "/奇点OS"
ROOT_NAME = os.path.basename(_arg_root.rstrip("/")) if _arg_root else "奇点OS根"
WIN_W, WIN_H = 340, 620
HEADER_H = 44
FOOTER_H = 28

FONT_PX = 13  # .tree font-size 0.8125rem
ROW_PAD_X = 8  # node-row padding 0.5rem
ROW_PAD_Y = 5  # node-row padding 0.3rem≈4.8
ROW_MARGIN = 4  # li margin 0.25rem
INDENT = 20  # ul padding-left 1.25rem
LINE_X = 8  # ul::before left 0.5rem
ELBOW_W = 12  # li::before width 0.75rem
ELBOW_Y = 14  # li::before top 0.875rem
RADIUS = 8  # node-row border-radius 0.5rem

# 原型语义色（.dark 解析值）
NODE_BG = "#1c1c1e"  # --qd-node-bg = card
NODE_EDGE = "#3a3a3c"  # --qd-node-edge = border
NODE_OPEN = "#20334b"  # card 80% + primary 20%（color-mix）
NODE_EDGE_HL = C.PRIMARY  # --qd-node-edge-hl
FILE_BG = "#000000"  # --qd-file-bg = background
TEXT_DIM = C.MUTED_FG  # --qd-text-dim
LINE_COLOR = "#3a3a3c"  # --qd-line


IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tiff", ".ico"}
TEXT_EXT = {
    ".txt",
    ".md",
    ".py",
    ".sh",
    ".json",
    ".conf",
    ".ini",
    ".css",
    ".js",
    ".html",
    ".xml",
    ".yaml",
    ".yml",
    ".log",
    ".csv",
    ".bash_profile",
    ".xinitrc",
    ".service",
    ".desktop",
}
QD_EXT = {".qds", ".qdai", ".qdmeta", ".qd"}


def _open_file(path):
    """G.20 文件打开分发（此前 .qds 等文件点击只弹 toast，打不开）：
    图片 → 图片查看器；QD 模块/二进制 → 文件查看器(qd_viewer)；文本 → 文本编辑器"""
    import subprocess

    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    if ext in IMG_EXT:
        argv = ["python3", "/奇点OS/运行/ui_imageview.py", path]
    elif ext in QD_EXT or ext in ("", ".qds"):
        argv = ["python3", "/奇点OS/运行/qd_viewer.py", path]
    elif ext in TEXT_EXT or name in (".xinitrc", ".bash_profile", "PKGINFO"):
        argv = ["python3", "/奇点OS/运行/ui_editor.py", path]
    else:
        argv = ["python3", "/奇点OS/运行/qd_viewer.py", path]
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _rgba(color, a=1.0):
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)) + (a,)


class Node:
    __slots__ = ("path", "name", "is_dir", "children", "loaded")

    def __init__(self, path, name, is_dir):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.children = None  # None=未加载
        self.loaded = False


def load_children(node):
    """读真实文件系统：目录优先、按名排序、跳过隐藏项"""
    if node.loaded:
        return
    out = []
    try:
        for name in sorted(os.listdir(node.path)):
            if name.startswith("."):
                continue
            p = os.path.join(node.path, name)
            out.append(Node(p, name, os.path.isdir(p)))
        out.sort(key=lambda n: (not n.is_dir, n.name.lower()))
    except PermissionError:
        pass
    except OSError:
        pass
    node.children = out
    node.loaded = True


def build_root():
    root = Node(ROOT_PATH, ROOT_NAME, True)
    load_children(root)
    return root


class Tree(Gtk.DrawingArea):
    """目录导图主体：自绘虚线连接树（原型 .tree CSS 1:1）"""

    def __init__(self, fb):
        super().__init__()
        self.fb = fb
        self.root = build_root()
        self.expanded = {ROOT_PATH, ROOT_PATH + "/系统", ROOT_PATH + "/系统/界面.qd"}
        self.rows = []  # (node, x, y, h, depth)
        self.content_h = 1
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.SCROLL_MASK)
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_click)
        self.set_size_request(WIN_W - 4, 100)

    # ---------- 展开态 ----------
    def toggle(self, node):
        if node.path in self.expanded:
            self.expanded.discard(node.path)
        else:
            load_children(node)
            self.expanded.add(node.path)
        self.relayout()

    def collapse_all(self):
        self.expanded = {ROOT_PATH}
        self.relayout()

    def go_home(self):
        self.expanded = {ROOT_PATH, ROOT_PATH + "/系统", ROOT_PATH + "/系统/界面.qd"}
        self.relayout()

    def refresh(self):
        self.root = build_root()
        self.expanded = {ROOT_PATH, ROOT_PATH + "/系统", ROOT_PATH + "/系统/界面.qd"}
        self.relayout()

    # ---------- 布局 ----------
    def relayout(self):
        self.rows = []
        y = ROW_MARGIN
        self._walk(self.root, 0, 0, y, True)
        self.content_h = max(y + 10, 100)
        self.set_size_request(WIN_W - 4, self.content_h)
        self.queue_draw()

    def _walk(self, node, depth, x, y, is_last_path):
        """递归铺行；返回该子树块底 y"""
        h = FONT_PX + ROW_PAD_Y * 2
        self.rows.append((node, x, y, h, depth))
        y += h + ROW_MARGIN
        if node.is_dir and node.path in self.expanded and node.children:
            child_x = x + INDENT
            last = node.children[-1]
            for ch in node.children:
                y = self._walk(ch, depth + 1, child_x, y, ch is last)
        return y

    # ---------- 绘制 ----------
    def _on_draw(self, w, cr):
        cr.set_source_rgba(*_rgba("#1c1c1e"))  # 原型面板 bg-card/95
        cr.paint()
        # 每个展开目录块：竖虚线 + 各子行肘线（原型 ul::before / li::before）
        cr.set_source_rgba(*_rgba(LINE_COLOR))
        cr.set_line_width(1.5)
        cr.set_dash([3, 3])
        for node, x, y, h, depth in self.rows:
            if not (node.is_dir and node.path in self.expanded and node.children):
                continue
            # 子块范围
            idx = self.rows.index((node, x, y, h, depth))
            vx = x + LINE_X
            top = y + h
            bot = y + h
            for cn, cx, cy, ch, cd in self.rows[idx + 1 :]:
                if cd <= depth:
                    break
                bot = cy + ch
            cr.move_to(vx, top)
            cr.line_to(vx, bot - ROW_MARGIN)
            cr.stroke()
            # 每个子行的肘线：竖线 → 行左
            for cn, cx, cy, ch, cd in self.rows[idx + 1 :]:
                if cd <= depth:
                    break
                if cd == depth + 1:
                    cr.move_to(vx, cy + ELBOW_Y)
                    cr.line_to(cx, cy + ELBOW_Y)
                    cr.stroke()
        cr.set_dash([])

        # 行：箭头 + 图标 + 卡片 + 标签
        for node, x, y, h, depth in self.rows:
            self._draw_row(cr, node, x, y, h)
        return False

    def _round_rect(self, cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + r, y + r, r, 3.1416, 4.7124)
        cr.arc(x + w - r, y + r, r, 4.7124, 6.2832)
        cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
        cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
        cr.close_path()

    def _draw_row(self, cr, node, x, y, h):
        w = WIN_W - 4 - ROW_MARGIN * 2 - x
        is_root = node.path == ROOT_PATH
        expanded = node.path in self.expanded
        # 卡片（文件夹/根有卡片，文件也有素卡）
        if is_root:
            bg, edge = "#3a3a3c", NODE_EDGE_HL
        elif node.is_dir and expanded:
            bg, edge = NODE_OPEN, NODE_EDGE_HL
        elif node.is_dir:
            bg, edge = NODE_BG, NODE_EDGE
        else:
            bg, edge = FILE_BG, NODE_EDGE
        cr.set_source_rgba(*_rgba(bg))
        self._round_rect(cr, x, y, w, h, RADIUS)
        cr.fill()
        cr.set_source_rgba(*_rgba(edge))
        cr.set_line_width(1)
        self._round_rect(cr, x, y, w, h, RADIUS)
        cr.stroke()
        # 内容基线
        cy = y + h / 2
        cx = x + ROW_PAD_X
        # 箭头（仅目录）
        if node.is_dir:
            self._text(cr, "▾" if expanded else "▸", cx, cy, 10, C.PRIMARY)
            cx += 12 + 6  # .arrow 0.75rem + gap 0.4rem
        # 图标
        icon = (
            ("folder-open" if (node.is_dir and expanded) else "folder")
            if node.is_dir
            else "file-text"
        )
        color = C.PRIMARY if node.is_dir else TEXT_DIM
        pb = C.pixbuf_icon(icon, 16, color)
        if pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, pb, cx, cy - 8)
            cr.paint()
        cx += 16 + 6
        # 标签
        self._text(
            cr,
            node.name,
            cx,
            cy,
            FONT_PX,
            C.FG if (node.is_dir or is_root) else TEXT_DIM,
        )

    def _text(self, cr, s, x, cy, px, color):
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription("%s %d" % (C.FONT, px))
        layout.set_font_description(desc)
        layout.set_text(s, -1)
        lw, lh = layout.get_pixel_size()
        cr.set_source_rgba(*_rgba(color))
        cr.move_to(x, cy - lh / 2)
        PangoCairo.show_layout(cr, layout)

    # ---------- 点击 ----------
    def _on_click(self, w, e):
        sy = e.y
        for node, x, y, h, depth in self.rows:
            if y <= sy < y + h + ROW_MARGIN:
                if e.button == 1:
                    if node.is_dir:
                        self.toggle(node)
                    else:
                        _open_file(node.path)
                        self.fb.toast("打开 " + node.name)
                elif e.button == 3:
                    # G.23：右键移到回收站（文件夹式，不经 git）
                    self._move_to_trash(node)
                return False
        return False

    def _move_to_trash(self, node):
        """把文件/目录移入回收站，刷新视图"""
        try:
            sys.path.insert(0, "/奇点OS/运行")
            from ui_trash import trash_move

            if trash_move(node.path):
                self.fb.toast("已移入回收站：" + node.name)
                # 从父目录的 children 中移除，无需全盘刷新
                parent_path = os.path.dirname(node.path)
                for n, x, y, h, d in self.rows:
                    if n.path == parent_path and n.children:
                        n.children = [c for c in n.children if c.path != node.path]
                        break
                self.relayout()
            else:
                self.fb.toast("移入回收站失败")
        except Exception as e:
            self.fb.toast("回收站错误：%s" % e)


class FileBrowser(Gtk.Window):
    def __init__(self):
        super().__init__(title="文件浏览器")
        self.set_default_size(WIN_W, WIN_H)
        self.set_size_request(WIN_W, WIN_H)
        self.set_resizable(False)  # PMaxSize hints：qwm 880 兜底后 GTK 恢复设计尺寸
        self.set_position(Gtk.WindowPosition.NONE)
        self.move(360, 120)
        # 原型面板 bg-card/95；head/footer 同底、分隔线 border/40
        C.setup_css(b"""
            .fb-panel { background: #1c1c1e; }
            .fb-head, .fb-foot { background: #1c1c1e; }
            .fb-head button { background: transparent; border: none; padding: 3px; min-width: 0; min-height: 0; }
            .fb-head button:hover { background: rgba(58,58,60,180); border-radius: 4px; }
        """)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.get_style_context().add_class("fb-panel")
        self.add(vbox)

        # ---------- 头部（原型 L438-448） ----------
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.get_style_context().add_class("fb-head")
        head.set_margin_start(12)
        head.set_margin_end(12)
        head.set_margin_top(8)
        head.set_margin_bottom(8)
        head.pack_start(C.icon_image("folder-open", 16, C.PRIMARY), False, False, 0)
        head.pack_start(C.label("奇点OS 目录导图", 14, C.FG, "500"), False, False, 0)
        for icon, tip, fn in (
            ("chevrons-down-up", "全部收起", self._collapse),
            ("refresh-cw", "刷新", self._refresh),
            ("house", "奇点OS根", self._home),
        ):
            btn = Gtk.Button()
            btn.set_image(C.icon_image(icon, 14, C.FG))
            btn.set_tooltip_text(tip)
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.connect("clicked", fn)
            head.pack_end(btn, False, False, 0)
        sep1 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(head, False, False, 0)
        vbox.pack_start(sep1, False, False, 0)

        # ---------- 树主体（滚动） ----------
        self.tree = Tree(self)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.tree)
        vbox.pack_start(scroll, True, True, 0)

        # ---------- 底部状态条（原型 L535-537） ----------
        sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        foot.get_style_context().add_class("fb-foot")
        lbl = C.label("当前根：/奇点OS · 单击目录展开 · 单击文件打开", 11, C.MUTED_FG)
        lbl.set_margin_start(12)
        lbl.set_margin_top(4)
        lbl.set_margin_bottom(6)
        foot.add(lbl)
        vbox.pack_start(sep2, False, False, 0)
        vbox.pack_start(foot, False, False, 0)

        self.show_all()
        GLib.idle_add(self.tree.relayout)

    def toast(self, text):
        C.send_icons({"cmd": "toast", "text": text})

    def _home(self, w=None):
        self.tree.go_home()
        self.toast("已回到 奇点OS根")

    def _refresh(self, w=None):
        self.tree.refresh()
        self.toast("目录已刷新")

    def _collapse(self, w=None):
        self.tree.collapse_all()
        self.toast("已全部收起")


if __name__ == "__main__":
    # G.16：单例。此前点一次桌面图标就多一个进程（实测堆到 6 个），
    # 新窗口又和旧的重合 → 用户看到"提示打开了却没弹窗"。
    if not C.single_instance("filebrowser"):
        sys.exit(0)
    win = FileBrowser()
    C.set_raise_handler("filebrowser", win.present)
    Gtk.main()
