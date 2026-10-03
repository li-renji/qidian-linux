#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""奇点OS 文件浏览器 V5：思维导图式浏览（PIL 渲染，稳定无 Cairo 依赖）
根目录在顶部，点击节点展开/收起分支，父子节点以贝塞尔曲线连接；
目录节点单击展开/收起，文件节点单击打开（xdg-open → 查看器）。
支持 --daemon / --open（保留）。
"""
import gi, os, subprocess, math, tempfile
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib
from PIL import Image, ImageDraw, ImageFont

QD_ROOT = '/奇点OS'
FONT = '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'
FONT_LATIN = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

MARGIN = 16      # 画布边距
INDENT = 30      # 每层缩进
ROW_H = 36       # 行高（垂直间距）
NODE_H = 26      # 节点高

BG = (20, 22, 31)          # #14161f
NODE_BG = (27, 30, 42)     # #1b1e2a
NODE_EDGE = (58, 64, 92)   # #3a405c
NODE_BG_OPEN = (47, 53, 80)  # #2f3550
NODE_EDGE_HL = (74, 144, 194)  # #4a90c2
FILE_BG = (22, 24, 32)
FILE_EDGE = (42, 46, 66)
TEXT = (232, 234, 240)
TEXT_DIM = (139, 147, 173)
LINE = (118, 132, 172)
MARK = (143, 208, 255)

CSS = b"""
window { background-color: #14161f; color: #e8eaf0; }
* { outline: none; }
.toolbar { background-color: #14161f; border-bottom: 1px solid #2a2e42; padding: 5px; }
.toolbar button { color: #e8eaf0; background: #1b1e2a; border: 1px solid #2a2e42; border-radius: 6px; padding: 5px 10px; }
.toolbar button:hover { background: #262b3d; }
.statusbar { background-color: #1b1e2a; color: #8b93ad; padding: 3px 10px; font-size: 12px; }
"""


class Node:
    def __init__(self, path, name, is_dir, parent=None):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.parent = parent
        self.children = []
        self.expanded = False
        self.loaded = False
        self.x = 0.0
        self.y = 0.0
        self.w = 120
        self.h = 26

    def load(self):
        if self.loaded or not self.is_dir:
            return
        self.loaded = True
        try:
            items = sorted(os.listdir(self.path))
        except OSError:
            return
        for name in items:
            if name.startswith('.'):
                continue
            full = os.path.join(self.path, name)
            self.children.append(Node(full, name, os.path.isdir(full), self))

def dashed_line(d, x1, y1, x2, y2, fill, dash=5, gap=3, width=2):
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 0.5:
        return
    ux, uy = dx / length, dy / length
    pos = 0.0
    while pos < length:
        end = min(pos + dash, length)
        d.line([(x1 + ux * pos, y1 + uy * pos), (x1 + ux * end, y1 + uy * end)], fill=fill, width=width)
        pos = end + gap


class QDMindMap(Gtk.Window):
    def __init__(self, start=None):
        Gtk.Window.__init__(self, title='奇点OS 文件浏览器 · 目录导图')
        self.set_default_size(1000, 720)
        self.root = Node(start or QD_ROOT, self._label(start or QD_ROOT), True)
        self.root.expanded = True
        self.root.load()
        self._build()

    def _label(self, path):
        if path == QD_ROOT:
            return '奇点OS根'
        if path == '/':
            return 'Linux 底座'
        return os.path.basename(path.rstrip('/')) or '/'

    def _build(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)

        toolbar = Gtk.Box(spacing=4)
        toolbar.get_style_context().add_class('toolbar')
        b1 = Gtk.Button(label='🏠 奇点OS根')
        b1.connect('clicked', lambda w: self._set_root(QD_ROOT))
        toolbar.pack_start(b1, False, False, 0)
        b2 = Gtk.Button(label='⟳ 刷新')
        b2.connect('clicked', lambda w: self._set_root(self.root.path))
        toolbar.pack_start(b2, False, False, 0)
        b3 = Gtk.Button(label='− 全部收起')
        b3.connect('clicked', lambda w: self._collapse_all(self.root))
        toolbar.pack_start(b3, False, False, 0)
        vbox.pack_start(toolbar, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        self.eb = Gtk.EventBox()
        self.eb.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.eb.connect('button-press-event', self._on_button)
        self.image = Gtk.Image()
        self.image.set_halign(Gtk.Align.START)
        self.image.set_valign(Gtk.Align.START)
        self.eb.add(self.image)
        scroller.add(self.eb)
        vbox.pack_start(scroller, True, True, 0)

        self.status = Gtk.Label(label='')
        self.status.get_style_context().add_class('statusbar')
        vbox.pack_start(self.status, False, False, 0)

        self._render()

    # ---------- 布局（垂直树：根在上，逐层缩进） ----------
    def _layout(self, node, depth, y):
        node.depth = depth
        node.x = MARGIN + depth * INDENT
        node.y = y
        node.w = max(self._est_w(node.name, 15) + 44, 90)
        node.h = NODE_H
        y += ROW_H
        if node.expanded and node.children:
            for c in node.children:
                y = self._layout(c, depth + 1, y)
        return y

    def _all_nodes(self):
        stack = [self.root]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(n.children)

    def _collapse_all(self, node):
        node.expanded = False
        for c in node.children:
            self._collapse_all(c)
        self._render()

    def _set_root(self, path):
        self.root = Node(path, self._label(path), True)
        self.root.expanded = True
        self.root.load()
        self._render()

    # ---------- 渲染 ----------
    def _render(self):
        self._layout(self.root, 0, MARGIN)
        nodes = list(self._all_nodes())
        w = int(max(max(n.x + n.w for n in nodes) + MARGIN, 980))
        h = int(max(n.y + n.h for n in nodes) + MARGIN)

        img = Image.new('RGB', (w, h), BG)
        d = ImageDraw.Draw(img)
        font = ImageFont.truetype(FONT, 14)
        font_b = ImageFont.truetype(FONT, 15)
        font_s = ImageFont.truetype(FONT, 13)
        font_l = ImageFont.truetype(FONT_LATIN, 14)
        font_lb = ImageFont.truetype(FONT_LATIN, 15)
        font_ls = ImageFont.truetype(FONT_LATIN, 13)

        # 先画虚线，再画节点
        self._draw_lines(d)
        for n in nodes:
            self._draw_node(d, n, font, font_b, font_s, font_l, font_lb, font_ls)

        png = os.path.join(tempfile.gettempdir(), 'qd_fm_render.png')
        img.save(png)
        self.image.set_from_file(png)
        self.image.set_size_request(w, h)
        self.eb.set_size_request(w, h)
        self.canvas_w, self.canvas_h = w, h
        self.status.set_text('目录导图 · 点击目录节点展开/收起 · 单击文件节点打开 · 当前根：%s' % self.root.path)

    def _draw_lines(self, d):
        # 每个展开的父节点：水平虚线连到竖线，竖虚线向下，分支短虚线连子节点
        for node in self._all_nodes():
            if node.children and node.expanded:
                px = node.x + node.w
                py = node.y + node.h // 2
                cx = node.x + INDENT - 8
                children = node.children
                last_y = children[-1].y + children[-1].h // 2
                dashed_line(d, px, py, cx, py, LINE)
                dashed_line(d, cx, py, cx, last_y, LINE)
                for c in children:
                    cy = c.y + c.h // 2
                    dashed_line(d, cx, cy, c.x - 6, cy, LINE)

    def _cf(self, ch, fc, fl):
        return fl if ord(ch) < 0x2500 else fc

    def _est_w(self, text, size):
        # GTK 共存下 PIL 度量 API 损坏，用估算宽度定位
        return sum((size if ord(c) >= 0x2500 else size * 0.6) for c in text)

    def _text_len(self, text, fc, fl):
        return self._est_w(text, fc.size)

    def _draw_mixed(self, d, xy, text, fc, fl, fill):
        x, y = xy
        size = fc.size
        for ch in text:
            f = self._cf(ch, fc, fl)
            d.text((x, y), ch, font=f, fill=fill)
            x += self._est_w(ch, size)

    def _draw_node(self, d, node, font, font_b, font_s, font_l, font_lb, font_ls):
        x, y, w, h = node.x, node.y, node.w, node.h
        is_open = node.expanded and node.children
        is_root = node is self.root
        if not node.is_dir:
            d.rounded_rectangle((x, y, x + w, y + h), radius=6,
                                fill=FILE_BG, outline=FILE_EDGE, width=1)
        elif is_root:
            d.rounded_rectangle((x, y, x + w, y + h), radius=6,
                                fill=(36, 42, 66), outline=NODE_EDGE_HL, width=2)
        elif is_open:
            d.rounded_rectangle((x, y, x + w, y + h), radius=6,
                                fill=NODE_BG_OPEN, outline=NODE_EDGE_HL, width=1)
        else:
            d.rounded_rectangle((x, y, x + w, y + h), radius=6,
                                fill=NODE_BG, outline=NODE_EDGE, width=1)

        color = TEXT if node.is_dir or is_root else TEXT_DIM
        fc = font_b if is_root else font
        fl = font_lb if is_root else font_l
        name = node.name
        max_len = node.w - 44
        if self._text_len(name, fc, fl) > max_len:
            while self._text_len(name + '…', fc, fl) > max_len and len(name) > 1:
                name = name[:-1]
            name += '…'
        self._draw_mixed(d, (x + 8, y + (h - 17) / 2 - 1), name, fc, fl, color)

        # 目录箭头 ▸/▾
        if node.is_dir:
            mark = '▾' if is_open else '▸'
            self._draw_mixed(d, (x + w - 20, y + (h - 17) / 2 - 1), mark, font_s, font_ls, MARK)

    # ---------- 交互 ----------
    def _hit(self, x, y):
        for n in self._all_nodes():
            if n.x <= x <= n.x + n.w and n.y <= y <= n.y + n.h:
                return n
        return None

    def _on_button(self, widget, event):
        n = self._hit(event.x, event.y)
        if n is None:
            return True
        if n.is_dir:
            n.expanded = not n.expanded
            if n.expanded:
                n.load()
            self._render()
        else:
            try:
                subprocess.Popen(['xdg-open', n.path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        return True


if __name__ == '__main__':
    import sys, threading, socket

    SOCK_PATH = os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/tmp'), 'qd_fm.sock')

    def _send_cmd(cmd):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(SOCK_PATH)
            s.sendall(cmd.encode('utf-8'))
            s.close()
            return True
        except Exception:
            return False

    def _norm_start(p):
        if p in ('/', '/奇点OS'):
            return QD_ROOT
        return p

    if '--daemon' in sys.argv:
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass

        def _open_win(p):
            w = QDMindMap(p)
            w.show_all()
            return False

        def _listen():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(SOCK_PATH)
            s.listen(8)
            try:
                os.chmod(SOCK_PATH, 0o600)
            except OSError:
                pass
            while True:
                try:
                    conn, _ = s.accept()
                    data = conn.recv(4096).decode('utf-8', errors='replace').strip()
                    conn.close()
                    if data.startswith('open '):
                        p = _norm_start(data[5:].strip() or QD_ROOT)
                        GLib.idle_add(_open_win, p)
                except Exception:
                    break

        threading.Thread(target=_listen, daemon=True).start()
        Gtk.main()

    elif '--open' in sys.argv:
        i = sys.argv.index('--open')
        p = _norm_start(sys.argv[i + 1] if len(sys.argv) > i + 1 else QD_ROOT)
        if _send_cmd('open ' + p):
            sys.exit(0)
        win = QDMindMap(p)
        win.connect('destroy', Gtk.main_quit)
        win.show_all()
        Gtk.main()

    else:
        start = sys.argv[1] if len(sys.argv) > 1 else None
        win = QDMindMap(_norm_start(start))
        win.connect('destroy', Gtk.main_quit)
        win.show_all()
        Gtk.main()
