#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""奇点OS 桌面外壳 V3（深度定制，透明叠加）
- 壁纸：由 xfdesktop 渲染（原壁纸原样显示）
- 桌面壳：全屏透明窗口，仅绘制奇点OS 图标层（系统主题图标 + 中文名，原布局）
- 空白区域 input shape 穿透（可触发 xfdesktop 右键菜单）；图标区域可点击，
  单击选中/双击打开奇点OS文件浏览器
"""
import subprocess
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango, cairo

QD_ROOT = '/奇点OS'

# 图标配置：(显示名, 打开路径, 图标主题名, 布局row, col) —— 位置照搬原 rc
ICONS = [
    ('奇点OS 状态', QD_ROOT + '/运行/status_panel.py', 'applications-system', 0, 0),
    ('AI.qd',       QD_ROOT + '/AI.qd',                 'folder',              0, 1),
    ('奇点OS文件系统', QD_ROOT,                          'system-file-manager', 2, 1),
    ('用户.qd',     QD_ROOT + '/用户.qd',               'folder',              1, 1),
    ('系统',        QD_ROOT + '/系统',                  'folder',              6, 0),
    ('意图 INTENT', QD_ROOT + '/AI.qd/intent_agent.qd/share/intent_gui.py', 'system-run', 5, 0),
]

GRID_W, GRID_H = 116, 108


class IconWidget(Gtk.EventBox):
    def __init__(self, name, path, icon_name, row, col):
        super().__init__()
        self.name = name
        self.path = path
        self.selected = False
        self.row, self.col = row, col
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_halign(Gtk.Align.CENTER)
        self.image = Gtk.Image()
        theme = Gtk.IconTheme.get_default()
        try:
            pixbuf = theme.load_icon(icon_name, 48, 0)
            self.image.set_from_pixbuf(pixbuf)
        except Exception:
            self.image.set_size_request(48, 48)
        label = Gtk.Label(label=name)
        label.override_color(Gtk.StateFlags.NORMAL, Gdk.RGBA(1, 1, 1, 1))
        label.override_font(Pango.FontDescription('sans 12'))
        # 选中背景
        label.override_background_color(Gtk.StateFlags.NORMAL, Gdk.RGBA(0, 0, 0, 0))
        box.pack_start(self.image, False, False, 0)
        box.pack_start(label, False, False, 0)
        self.add(box)
        self.connect('button-press-event', self.on_press)

    def on_press(self, widget, event):
        if event.button == 1:
            self.get_toplevel().select_only(self)
            if event.type == Gdk.EventType._2BUTTON_PRESS:
                self.get_toplevel().open_icon(self)
            return True
        return False


class QDDesktop(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_title('奇点OS桌面')
        self.fullscreen()
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_app_paintable(True)

        # RGBA 透明
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.connect('draw', self.on_draw)
        self.connect('realize', self.on_realize)

        fixed = Gtk.Fixed()
        self.add(fixed)
        self.fixed = fixed

        self.icons = []
        for name, path, icon_name, row, col in ICONS:
            icon = IconWidget(name, path, icon_name, row, col)
            icon.show_all()
            fixed.put(icon, 30 + col * GRID_W, 24 + row * GRID_H)
            self.icons.append(icon)

        self.current_selected = None
        self.connect('delete-event', Gtk.main_quit)

    def on_draw(self, widget, cr):
        # 全透明背景（壁纸由 xfdesktop 渲染）
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        return False

    def on_realize(self, widget):
        win = widget.get_window()
        if win is None:
            return
        rects = []
        for ic in self.icons:
            alloc = ic.get_allocation()
            rects.append(Gdk.Rectangle(alloc.x, alloc.y, alloc.width, alloc.height))
        if rects:
            region = Gdk.cairo_region_create_from_rectangles(rects)
            win.input_shape_combine_region(region)
            # 输入形状变化后需重新设输入（GTK 默认全部可点，shape 后自动只留区域）
        # 窗口置底不抢焦点
        win.set_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                       Gdk.EventMask.BUTTON_RELEASE_MASK |
                       Gdk.EventMask.POINTER_MOTION_MASK |
                       Gdk.EventMask.STRUCTURE_MASK)

    def select_only(self, keep):
        for ic in self.icons:
            ic.selected = (ic is keep)
        self.current_selected = keep

    def open_icon(self, icon):
        if icon.path.endswith('.py'):
            subprocess.Popen(['python3', icon.path])
        else:
            subprocess.Popen(['python3', '/奇点OS/系统/界面.qd/图形图像桌面.qd/bin/qd_fm.py', icon.path])


def main():
    win = QDDesktop()
    win.show_all()
    Gtk.main()

if __name__ == '__main__':
    main()
