#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_filepicker.py —— 奇点OS 自研文件选择弹窗（G.23）
#
# 为什么不用 GTK FileChooserDialog：
#   GTK 原生弹窗不认识 .qd/.qds/.qdai/.qdmeta 等奇点OS 格式，
#   且视觉风格与自研桌面断层。本组件走 qwm 托管的 Gtk.Window，
#   内嵌思维导图样式自绘文件树，与 ui_filebrowser 同源。
#
# 用法：
#   python3 ui_filepicker.py --mode open  [--root /奇点OS/用户.qd/桌面]
#   python3 ui_filepicker.py --mode save  [--root ...] [--default 新建.txt]
#   python3 ui_filepicker.py --mode dir   [--root ...]
# 返回：stdout 输出选中路径（取消或异常则空行）
# ============================================================
import os, sys, argparse, json
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Gdk, GLib, Pango, PangoCairo
import cairo

import qd_ui_common as C

WIN_W, WIN_H = 540, 420
FONT_PX = 13
ROW_PAD_X = 8
ROW_PAD_Y = 5
ROW_MARGIN = 4
INDENT = 20
LINE_X = 8
RADIUS = 8

NODE_BG = "#1c1c1e"
NODE_EDGE = "#3a3a3c"
NODE_OPEN = "#20334b"
FILE_BG = "#000000"
TEXT_DIM = "#8e8e93"
LINE_COLOR = "#3a3a3c"
HL_BG = "#2e3a5a"

QD_EXTS = {".qd", ".qds", ".qdai", ".qdmeta"}


def _rgba(color, a=1.0):
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)) + (a,)


class Node:
    __slots__ = ("path", "name", "is_dir", "children", "loaded")

    def __init__(self, path, name, is_dir):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.children = None
        self.loaded = False


def load_children(node, filters=None):
    if node.loaded:
        return
    out = []
    try:
        for name in sorted(os.listdir(node.path)):
            if name.startswith("."):
                continue
            p = os.path.join(node.path, name)
            is_dir = os.path.isdir(p)
            if not is_dir and filters:
                ext = os.path.splitext(name)[1].lower()
                # 无扩展名时给空串匹配；目录始终保留
                if ext not in filters and "" not in filters:
                    continue
            out.append(Node(p, name, is_dir))
        out.sort(key=lambda n: (not n.is_dir, n.name.lower()))
    except (PermissionError, OSError):
        pass
    node.children = out
    node.loaded = True


class PickerTree(Gtk.DrawingArea):
    """思维导图样式文件树：虚线连接 + 圆角卡片 + 单击选中/双击确认"""

    def __init__(self, root_path, root_name, filters=None):
        super().__init__()
        self.root = Node(root_path, root_name, True)
        self.filters = filters
        load_children(self.root, self.filters)
        self.expanded = {root_path}
        self.rows = []
        self.selected = None  # 当前选中 node.path
        self.content_h = 1
        self._on_pick = None
        self._on_confirm = None
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.SCROLL_MASK)
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_click)
        self.set_size_request(WIN_W - 24, 100)
        self.relayout()

    def relayout(self):
        self.rows = []
        y = ROW_MARGIN
        self._walk(self.root, 0, 0, y, True)
        self.content_h = max(y + 10, 100)
        self.set_size_request(WIN_W - 24, self.content_h)
        self.queue_draw()

    def _walk(self, node, depth, x, y, is_last_path):
        h = FONT_PX + ROW_PAD_Y * 2
        self.rows.append((node, x, y, h, depth))
        y += h + ROW_MARGIN
        if node.is_dir and node.path in self.expanded and node.children:
            child_x = x + INDENT
            for ch in node.children:
                y = self._walk(ch, depth + 1, child_x, y, ch is node.children[-1])
        return y

    def _on_draw(self, w, cr):
        cr.set_source_rgba(*_rgba("#1c1c1e"))
        cr.paint()
        # 虚线连接
        cr.set_source_rgba(*_rgba(LINE_COLOR))
        cr.set_line_width(1.5)
        cr.set_dash([3, 3])
        for node, x, y, h, depth in self.rows:
            if not (node.is_dir and node.path in self.expanded and node.children):
                continue
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
            for cn, cx, cy, ch, cd in self.rows[idx + 1 :]:
                if cd <= depth:
                    break
                if cd == depth + 1:
                    ex = cx - 6
                    ey = cy + ch / 2
                    cr.move_to(vx, ey)
                    cr.line_to(ex, ey)
                    cr.stroke()
        cr.set_dash([])
        # 节点绘制
        for node, x, y, h, depth in self.rows:
            is_expanded = node.is_dir and node.path in self.expanded
            is_selected = self.selected == node.path
            r = RADIUS
            # 背景
            if is_selected:
                cr.set_source_rgba(*_rgba(HL_BG))
            elif node.is_dir and is_expanded:
                cr.set_source_rgba(*_rgba(NODE_OPEN))
            elif not node.is_dir:
                cr.set_source_rgba(*_rgba(FILE_BG))
            else:
                cr.set_source_rgba(*_rgba(NODE_BG))
            cr.rectangle(
                x,
                y,
                WIN_W - 24 - x - 6,
                h,
            )
            cr.fill()
            # 边框
            if is_selected:
                cr.set_source_rgba(*_rgba(C.PRIMARY))
            else:
                cr.set_source_rgba(*_rgba(NODE_EDGE))
            cr.set_line_width(1)
            cr.rectangle(x, y, WIN_W - 24 - x - 6, h)
            cr.stroke()
            # 箭头 / 图标指示
            ix = x + 6
            iy = y + h / 2
            if node.is_dir:
                cr.set_source_rgba(*_rgba(C.PRIMARY))
                cr.set_font_size(10)
                cr.select_font_face(
                    "monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL
                )
                arrow = "▾" if is_expanded else "▸"
                cr.move_to(ix, iy + 4)
                cr.show_text(arrow)
            # 文字
            tx = ix + (14 if node.is_dir else 0)
            layout = PangoCairo.create_layout(cr)
            desc = Pango.FontDescription("%s %d" % (C.FONT, FONT_PX))
            layout.set_font_description(desc)
            layout.set_text(node.name, -1)
            # QD 格式高亮
            ext = os.path.splitext(node.name)[1].lower()
            if ext in QD_EXTS:
                cr.set_source_rgba(*_rgba(C.PRIMARY))
            elif node.is_dir:
                cr.set_source_rgba(*_rgba(C.FG))
            else:
                cr.set_source_rgba(*_rgba(TEXT_DIM))
            cr.move_to(tx, y + ROW_PAD_Y)
            PangoCairo.show_layout(cr, layout)

    def _on_click(self, w, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return False
        for node, x, y, h, depth in self.rows:
            if x <= ev.x < WIN_W - 24 and y <= ev.y < y + h:
                if node.is_dir:
                    if node.path in self.expanded:
                        self.expanded.discard(node.path)
                    else:
                        load_children(node, self.filters)
                        self.expanded.add(node.path)
                    self.selected = node.path
                else:
                    self.selected = node.path
                self.relayout()
                if self._on_pick:
                    self._on_pick(node.path, node.is_dir)
                # 双击确认：检测短时间内再次点击同一项
                if (
                    ev.type == Gdk.EventType._2BUTTON_PRESS
                    or getattr(self, "_last_dbl", None) == node.path
                ):
                    if self._on_confirm:
                        self._on_confirm(node.path, node.is_dir)
                self._last_dbl = node.path
                GLib.timeout_add(400, lambda: setattr(self, "_last_dbl", None) or False)
                return True
        return False

    def current(self):
        """返回当前选中 (path, is_dir)"""
        for node, x, y, h, depth in self.rows:
            if node.path == self.selected:
                return node.path, node.is_dir
        return None, None


class FilePicker(Gtk.Window):
    """qwm 托管的标准 Gtk.Window（非 Dialog），内嵌 PickerTree"""

    def __init__(self, mode, root, default_name="", filters=None):
        super().__init__(title="奇点OS 文件选择")
        self.mode = mode
        self.result = ""
        self.set_default_size(WIN_W, WIN_H)
        self.set_size_request(420, 300)
        C.setup_css(b"""
            .fp-win { background: #1c1c1e; }
            .fp-btn { background: rgba(58,58,58,180); border: none; border-radius: 8px; color: #f5f5f7; padding: 6px 14px; }
            .fp-btn:hover { background: rgba(91,120,255,180); }
            .fp-entry { background: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 8px; color: #f5f5f7; padding: 4px 8px; }
        """)
        self.get_style_context().add_class("fp-win")
        self.set_position(Gtk.WindowPosition.CENTER)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        self.add(box)

        # 顶部路径条
        self.path_lbl = C.label(root, 10, C.MUTED_FG)
        self.path_lbl.set_xalign(0)
        self.path_lbl.set_selectable(True)
        box.pack_start(self.path_lbl, False, False, 0)

        # 树区（可滚动）
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.tree = PickerTree(
            root, os.path.basename(root.rstrip("/")) or "根", filters
        )
        scroll.add(self.tree)
        box.pack_start(scroll, True, True, 0)

        # 底部操作区
        bot = Gtk.Box(spacing=8)
        bot.set_margin_top(4)

        # save 模式：文件名输入
        if mode == "save":
            self.name_entry = Gtk.Entry()
            self.name_entry.get_style_context().add_class("fp-entry")
            self.name_entry.set_text(default_name)
            self.name_entry.set_placeholder_text("输入文件名…")
            self.name_entry.set_width_chars(24)
            self.name_entry.connect("activate", lambda _e: self._confirm())
            bot.pack_start(C.label("文件名", 10, C.MUTED_FG), False, False, 0)
            bot.pack_start(self.name_entry, True, True, 0)

        bot.pack_end(self._btn("取消", self._cancel), False, False, 0)
        act = {"open": "打开", "save": "保存", "dir": "选择"}[mode]
        self.ok_btn = self._btn(act, self._confirm)
        bot.pack_end(self.ok_btn, False, False, 0)
        box.pack_start(bot, False, False, 0)

        # 回调绑定
        self.tree._on_pick = self._on_pick
        self.tree._on_confirm = self._on_confirm

        self.connect("key-press-event", self._key)
        self.connect("destroy", lambda _w: Gtk.main_quit())
        self.show_all()

    def _on_confirm(self, path, is_dir):
        """树中双击/回车确认：open/dir 返回所选路径，save 回填文件名后保存"""
        if self.mode == "save":
            if not is_dir:
                self.name_entry.set_text(os.path.basename(path))
                self._confirm()
            else:
                self.path_lbl.set_text(path)
        else:
            self.path_lbl.set_text(path)
            self._confirm()

    def _btn(self, text, fn):
        b = Gtk.Button(label=text)
        b.get_style_context().add_class("fp-btn")
        b.connect("clicked", fn)
        return b

    def _on_pick(self, path, is_dir):
        self.path_lbl.set_text(path)
        if self.mode == "open" and is_dir:
            self.ok_btn.set_sensitive(False)
        else:
            self.ok_btn.set_sensitive(True)

    def _confirm(self, _b=None):
        if self.mode == "save":
            name = self.name_entry.get_text().strip()
            if not name:
                C.toast("请输入文件名")
                return
            p, is_dir = self.tree.current()
            if not p or not is_dir:
                # 若当前选中不是目录，取其父目录
                p = os.path.dirname(p) if p else self.tree.root.path
            dst = os.path.join(p, name)
            self.result = dst
        elif self.mode == "dir":
            p, is_dir = self.tree.current()
            if not p:
                p = self.tree.root.path
            self.result = p
        else:
            p, is_dir = self.tree.current()
            if not p or is_dir:
                return
            self.result = p
        self.destroy()

    def _cancel(self, _b=None):
        self.result = ""
        self.destroy()

    def _key(self, w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self._cancel()
            return True
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._confirm()
            return True
        return False


def parse_filters(s):
    if not s:
        return None
    # 支持 .ext 或 ext 两种写法
    exts = set()
    for part in s.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        exts.add(part)
    return exts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["open", "save", "dir"], default="open")
    parser.add_argument("--root", default="/奇点OS")
    parser.add_argument("--default", dest="default_name", default="")
    parser.add_argument("--filter", dest="filters", default="")
    args = parser.parse_args()

    filters = parse_filters(args.filters)
    win = FilePicker(args.mode, args.root, args.default_name, filters)
    Gtk.main()
    print(win.result, flush=True)


if __name__ == "__main__":
    main()
