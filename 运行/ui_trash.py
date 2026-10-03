#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_trash.py —— 奇点OS 回收站（G.18）
#
# 为什么必须有：Windows 废纸篓 / macOS 废纸篓 / GNOME / XFCE / XFCE
# Clipman 全有（对标调研 v0.1 §2.2），"rm 直接没"是第一天级缺陷。
# 遵循 freedesktop Trash 规范（GNOME/Thunar 同一套目录结构）：
#   ~/.local/share/Trash/files/      被删文件本体
#   ~/.local/share/Trash/info/*.trashinfo  记录原路径与删除时间
#
# 功能：
#   列表（名称 / 原位置 / 删除时间 / 大小）
#   还原（按 trashinfo 的 Path 放回原处）
#   彻底删除（单个/清空）
#   文件浏览器右键"删除"改为移入回收站（另见 ui_filebrowser 接入）
#
# trashinfo 格式（freedesktop 标准）：
#   [Trash Info]\nPath=/home/qduser/xx\nDeletionDate=2026-09-28T12:00:00
#
# 入口：python3 ui_trash.py
# ============================================================
import os
import shutil
import sys
import time

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

sys.path.insert(0, '/奇点OS/运行')
import qd_ui_common as C

APP_TITLE = '回收站'
TRASH_FILES = os.path.expanduser('~/.local/share/Trash/files')
TRASH_INFO = os.path.expanduser('~/.local/share/Trash/info')


# ============================================================
# freedesktop Trash 操作（不引 gi-trash 库，协议本身就两个目录）
# ============================================================
def ensure_dirs():
    os.makedirs(TRASH_FILES, exist_ok=True)
    os.makedirs(TRASH_INFO, exist_ok=True)


def trash_move(src):
    """把 src 移入回收站，返回 True/False"""
    ensure_dirs()
    src = os.path.abspath(src)
    if not os.path.exists(src):
        return False
    name = os.path.basename(src)
    dst = os.path.join(TRASH_FILES, name)
    i = 1
    while os.path.exists(dst):
        stem, ext = os.path.splitext(name)
        dst = os.path.join(TRASH_FILES, '%s %d%s' % (stem, i, ext))
        i += 1
    try:
        shutil.move(src, dst)
        info = os.path.join(TRASH_INFO, os.path.basename(dst) + '.trashinfo')
        with open(info, 'w') as f:
            f.write('[Trash Info]\nPath=%s\nDeletionDate=%s\n'
                    % (src, time.strftime('%Y-%m-%dT%H:%M:%S')))
        return True
    except Exception:
        return False


def trash_list():
    """返回 [(files内名, 原路径, 删除时间, 大小str, 绝对路径)]"""
    items = []
    if not os.path.isdir(TRASH_FILES):
        return items
    for name in os.listdir(TRASH_FILES):
        fp = os.path.join(TRASH_FILES, name)
        origin, dt = '(未知来源)', ''
        info = os.path.join(TRASH_INFO, name + '.trashinfo')
        if os.path.exists(info):
            try:
                with open(info) as f:
                    for line in f:
                        if line.startswith('Path='):
                            origin = line[5:].strip()
                        elif line.startswith('DeletionDate='):
                            dt = line[13:].strip().replace('T', ' ')
            except Exception:
                pass
        try:
            if os.path.isdir(fp):
                size = '%d 项' % len(os.listdir(fp))
            else:
                n = os.path.getsize(fp)
                size = ('%.1f MB' % (n / 1e6)) if n > 1e6 else ('%d KB' % max(n // 1024, 1))
        except Exception:
            size = '?'
        items.append((name, origin, dt, size, fp))
    items.sort(key=lambda x: x[2], reverse=True)
    return items


def trash_restore(name):
    """按 trashinfo 还原；原父目录没了则落到用户主目录"""
    fp = os.path.join(TRASH_FILES, name)
    info = os.path.join(TRASH_INFO, name + '.trashinfo')
    origin = None
    if os.path.exists(info):
        with open(info) as f:
            for line in f:
                if line.startswith('Path='):
                    origin = line[5:].strip()
    if not origin:
        origin = os.path.expanduser('~/') + name
    dst = origin
    if not os.path.isdir(os.path.dirname(dst)):
        dst = os.path.expanduser('~/') + name
    i = 1
    while os.path.exists(dst):
        stem, ext = os.path.splitext(origin)
        dst = '%s %d%s' % (stem, i, ext)
        i += 1
    shutil.move(fp, dst)
    try:
        os.remove(info)
    except Exception:
        pass
    return dst


def trash_delete(name):
    """彻底删除单个"""
    fp = os.path.join(TRASH_FILES, name)
    if os.path.isdir(fp) and not os.path.islink(fp):
        shutil.rmtree(fp)
    else:
        try:
            os.remove(fp)
        except Exception:
            pass
    try:
        os.remove(os.path.join(TRASH_INFO, name + '.trashinfo'))
    except Exception:
        pass


def trash_empty():
    for name in os.listdir(TRASH_FILES):
        trash_delete(name)


def trash_count():
    try:
        return len(os.listdir(TRASH_FILES))
    except Exception:
        return 0


# ============================================================
class Trash(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_TITLE)
        self.set_default_size(700, 480)
        self.set_size_request(560, 360)

        C.setup_css(b'''
            .tr-root { background: #16161e; }
            .tr-bar  { background: #1c1c1e; border-bottom: 1px solid #3a3a3c;
                       padding: 6px 10px; }
            .tr-btn  { background: transparent; border: 1px solid #3a3a3c;
                       border-radius: 8px; padding: 4px 12px; color: #f5f5f7; }
            .tr-btn:hover { background: #3a3a3c; }
            .tr-danger { color: #ff453a; border-color: rgba(255,69,58,0.35); }
            treeview { background: #1c1c1e; color: #e6e6ea; }
            treeview header { background: #2a2a2c; color: #8e8e93; }
        ''')

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class('tr-root')
        self.add(root)

        # 顶部操作条
        bar = Gtk.Box(spacing=8)
        bar.get_style_context().add_class('tr-bar')
        root.pack_start(bar, False, False, 0)

        btn_restore = Gtk.Button.new_with_label('还原')
        btn_restore.get_style_context().add_class('tr-btn')
        btn_restore.connect('clicked', self._on_restore)
        bar.pack_start(btn_restore, False, False, 0)

        btn_delete = Gtk.Button.new_with_label('彻底删除')
        btn_delete.get_style_context().add_class('tr-btn')
        btn_delete.get_style_context().add_class('tr-danger')
        btn_delete.connect('clicked', self._on_delete)
        bar.pack_start(btn_delete, False, False, 0)

        btn_empty = Gtk.Button.new_with_label('清空回收站')
        btn_empty.get_style_context().add_class('tr-btn')
        btn_empty.get_style_context().add_class('tr-danger')
        btn_empty.connect('clicked', self._on_empty)
        bar.pack_start(btn_empty, False, False, 0)

        self.status = C.label('', 11, C.MUTED_FG)
        bar.pack_end(self.status, False, False, 4)

        # 列表
        self.store = Gtk.ListStore(str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        for i, (title, w) in enumerate([('名称', 180), ('原位置', 220),
                                        ('删除时间', 150), ('大小', 90)]):
            r = Gtk.CellRendererText()
            r.props.font = 'WenQuanYi Zen Hei 11'
            c = Gtk.TreeViewColumn(title, r, text=i)
            c.set_resizable(True)
            c.set_min_width(w)
            self.view.append_column(c)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.view)
        root.pack_start(scroll, True, True, 0)

        self._refresh()

    def _refresh(self):
        self.store.clear()
        items = trash_list()
        for name, origin, dt, size, _fp in items:
            self.store.append([name, origin, dt, size])
        if not items:
            self.status.set_text('回收站是空的')
        else:
            self.status.set_text('共 %d 项' % len(items))

    def _selected(self):
        sel = self.view.get_selection().get_selected()
        return sel[1][0] if sel[1] else None

    def _on_restore(self, *_):
        name = self._selected()
        if not name:
            self.status.set_text('请先选择一项')
            return
        try:
            dst = trash_restore(name)
            self.status.set_text('已还原到 %s' % dst)
        except Exception as e:
            self.status.set_text('还原失败：%s' % e)
        self._refresh()

    def _on_delete(self, *_):
        name = self._selected()
        if not name:
            self.status.set_text('请先选择一项')
            return
        if not self._confirm('彻底删除「%s」？此操作不可恢复。' % name):
            return
        trash_delete(name)
        self._refresh()

    def _on_empty(self, *_):
        if trash_count() == 0:
            self.status.set_text('回收站已经是空的')
            return
        if not self._confirm('清空全部 %d 项？此操作不可恢复。' % trash_count()):
            return
        trash_empty()
        self._refresh()

    def _confirm(self, text):
        dlg = Gtk.MessageDialog(transient_for=self, flags=0,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
        r = dlg.run()
        dlg.destroy()
        return r == Gtk.ResponseType.OK


# ============================================================
if __name__ == '__main__':
    if '--count' in sys.argv:      # 供其他组件查询条数
        print(trash_count())
        sys.exit(0)
    if not C.single_instance('trash'):
        sys.exit(0)
    ensure_dirs()
    win = Trash()
    C.set_raise_handler('trash', win.present)
    win.connect('destroy', Gtk.main_quit)
    win.show_all()
    Gtk.main()
