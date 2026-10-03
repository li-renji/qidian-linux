#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_imageview.py —— 奇点OS 图片查看器（G.20 新增）
#
# 为什么必须有：相册双击图片需要"直接打开图片"（用户令 G.20-1），
# 对标 Windows 照片 / GNOME Loupe / Haiku ShowImage 的最小集：
#   打开 / 缩放适应窗口 / 100% 原始尺寸 / 左右切换（同目录图片）
#   滚轮缩放 / 键盘 ←→ 切换 / Esc 关闭
# 不做：编辑、幻灯片、EXIF——那些是"重"查看器的事。
#
# 入口：python3 ui_imageview.py <图片路径>
# ============================================================
import os
import sys

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

sys.path.insert(0, '/奇点OS/运行')
import qd_ui_common as C

APP_TITLE = '图片查看器'
IMG_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.tiff', '.ico'}


def siblings(path):
    """同目录的图片列表（排序），返回 (列表, 当前索引)"""
    d = os.path.dirname(path) or '.'
    try:
        lst = sorted(f for f in os.listdir(d)
                     if os.path.splitext(f)[1].lower() in IMG_EXT)
    except Exception:
        return [path], 0
    if path not in lst:
        return [path], 0
    return [os.path.join(d, f) for f in lst], lst.index(path)


class ImageViewer(Gtk.Window):
    MAX_SCALE = 8.0
    MIN_SCALE = 0.05

    def __init__(self, path):
        super().__init__(title=APP_TITLE)
        self.set_default_size(860, 620)
        self.set_size_request(400, 300)

        C.setup_css(b'''
            .iv-root { background: #000000; }
            .iv-bar  { background: #1c1c1e; border-bottom: 1px solid #3a3a3c;
                       padding: 4px 10px; }
            .iv-btn  { background: transparent; border: 1px solid #3a3a3c;
                       border-radius: 8px; padding: 3px 8px; color: #f5f5f7; }
            .iv-btn:hover { background: #3a3a3c; }
            .iv-status { background: #1c1c1e; border-top: 1px solid #3a3a3c;
                         padding: 3px 10px; }
        ''')

        self.paths, self.idx = siblings(os.path.abspath(path))
        self.pixbuf = None
        self.scale = 1.0
        self.mode = 'fill'   # G.21 默认填充（用户令：预览图要充满屏幕，不要黑边）

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class('iv-root')
        self.add(root)

        # 顶栏（G.21：图标按钮替代文字按钮）
        bar = Gtk.Box(spacing=6)
        bar.get_style_context().add_class('iv-bar')
        root.pack_start(bar, False, False, 0)
        self.name_lbl = C.label('', 12, C.FG)
        bar.pack_start(self.name_lbl, False, False, 0)

        def iconbtn(icon, tip, fn):
            b = Gtk.Button()
            b.get_style_context().add_class('iv-btn')
            b.add(C.icon_image(icon, 15, C.FG))
            b.set_tooltip_text(tip)
            b.connect('clicked', fn)
            return b

        bar.pack_end(iconbtn('zoom-in', '放大 (+)', lambda *_: self._set_zoom(1.2)), False, False, 0)
        bar.pack_end(iconbtn('zoom-out', '缩小 (-)', lambda *_: self._set_zoom(1 / 1.2)), False, False, 0)
        bar.pack_end(iconbtn('expand', '原始大小 100%', lambda *_: self._set_zoom(1.0, False)), False, False, 0)
        bar.pack_end(iconbtn('maximize', '充满窗口（默认）', lambda *_: self._set_fill()), False, False, 0)
        bar.pack_end(iconbtn('chevron-right', '下一张 (→)', lambda *_: self._step(1)), False, False, 0)
        bar.pack_end(iconbtn('chevron-left', '上一张 (←)', lambda *_: self._step(-1)), False, False, 0)

        # 画布
        self.da = Gtk.DrawingArea()
        self.da.add_events(Gdk.EventMask.SCROLL_MASK)
        self.da.connect('draw', self._on_draw)
        self.da.connect('scroll-event', self._on_scroll)
        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scr.add(self.da)
        root.pack_start(scr, True, True, 0)

        # 状态栏
        self.status = C.label('', 11, C.MUTED_FG)
        status_bar = Gtk.Box()
        status_bar.get_style_context().add_class('iv-status')
        status_bar.pack_start(self.status, False, False, 0)
        root.pack_start(status_bar, False, False, 0)

        self.connect('key-press-event', self._on_key)
        self._load()

    # ---------- 加载 ----------
    def _load(self):
        path = self.paths[self.idx]
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
        except Exception:
            try:
                # SVG 走缩放加载
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(path, 1024, 1024)
            except Exception as e:
                self.status.set_text('无法加载：%s' % e)
                return
        self.pixbuf = pb
        self.name_lbl.set_text(os.path.basename(path))
        self._set_fit()
        self._update_status()

    def _update_status(self):
        if not self.pixbuf:
            return
        self.status.set_text(
            '%d / %d · %d×%d px · %.0f%% · %s'
            % (self.idx + 1, len(self.paths), self.pixbuf.get_width(),
               self.pixbuf.get_height(), self.scale * 100,
               self.paths[self.idx]))

    # ---------- 缩放 ----------
    def _set_fill(self):
        """G.21 默认模式：等比缩放铺满窗口（cover），无黑边（用户令）"""
        self.mode = 'fill'
        if not self.pixbuf:
            return
        alloc = self.da.get_allocation()
        aw = max(alloc.width, 100)
        ah = max(alloc.height, 100)
        pw, ph = self.pixbuf.get_width(), self.pixbuf.get_height()
        self.scale = max(aw / pw, ah / ph)
        self._apply()

    # 兼容旧名
    _set_fit = _set_fill

    def _set_zoom(self, s, relative=True):
        if not self.pixbuf:
            return
        self.mode = 'zoom'
        self.scale = (self.scale * s if relative else s)
        self.scale = max(self.MIN_SCALE, min(self.MAX_SCALE, self.scale))
        self._apply()

    def _apply(self):
        if not self.pixbuf:
            return
        w = max(1, int(self.pixbuf.get_width() * self.scale))
        h = max(1, int(self.pixbuf.get_height() * self.scale))
        self.scaled = self.pixbuf.scale_simple(
            w, h, GdkPixbuf.InterpType.BILINEAR)
        self.da.set_size_request(w, h)
        self.da.queue_draw()
        self._update_status()

    # ---------- 绘制 ----------
    def _on_draw(self, _w, cr):
        if getattr(self, 'scaled', None) is None:
            return False
        alloc = self.da.get_allocation()
        # 居中绘制
        x = max(0, (alloc.width - self.scaled.get_width()) // 2)
        y = max(0, (alloc.height - self.scaled.get_height()) // 2)
        Gdk.cairo_set_source_pixbuf(cr, self.scaled, x, y)
        cr.paint()
        return False

    # ---------- 事件 ----------
    def _on_scroll(self, _w, e):
        if e.direction == Gdk.ScrollDirection.UP:
            self._set_zoom(1.15)
        elif e.direction == Gdk.ScrollDirection.DOWN:
            self._set_zoom(1 / 1.15)
        return True

    def _on_key(self, _w, e):
        k = Gdk.keyval_name(e.keyval)
        if k in ('Left', 'Right'):
            self._step(1 if k == 'Right' else -1)
        elif k == 'Escape':
            self.destroy()
        elif k in ('plus', 'equal'):
            self._set_zoom(1.15)
        elif k == 'minus':
            self._set_zoom(1 / 1.15)
        return True

    def _step(self, d):
        n = self.idx + d
        if 0 <= n < len(self.paths):
            self.idx = n
            self._load()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: ui_imageview.py <图片路径>')
        sys.exit(1)
    if not C.single_instance('imageview'):
        sys.exit(0)
    win = ImageViewer(sys.argv[1])
    C.set_raise_handler('imageview', win.present)
    win.connect('destroy', Gtk.main_quit)
    win.show_all()
    # 适应窗口要在布局完成后算
    GLib.idle_add(lambda: (win._set_fit(), False)[1])
    Gtk.main()
