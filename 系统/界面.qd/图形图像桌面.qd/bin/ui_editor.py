#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_editor.py —— 奇点OS 文本编辑器（G.15）
#
# 为什么必须有：Windows 的"记事本"就是一个文本编辑器；Linux 下没有它，
# 双击 .txt 会落到 xdg-open 而无人接管。本组件同时承担 text/plain 的
# 默认应用（见 qd-editor.desktop / mimeapps.list）。
#
# 设计原则（对标 Windows 记事本 + 开源 mousepad 的最小集）：
#   只做文本本身的事：新建/打开/保存/另存/查找替换/字号/编码识别/未保存拦截
#   不做语法高亮、不做插件、不做多标签——那些属于"重"编辑器
#
# 入口：python3 ui_editor.py [文件路径]
# 快捷键：Ctrl+N/O/S/Shift+S/W/Q/F/H/Z/Y/X/C/V/A、F3 查找下一个、Esc 关查找条
# ============================================================
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

import qd_ui_common as C

APP_TITLE = "文本编辑器"
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")
UNTITLED = "未命名"


# ---------- 编码读写 ----------
def read_text(path):
    """尽力解码：UTF-8（含 BOM）→ GB18030 → BIG5 → Latin-1"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except Exception:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def write_text(path, text, enc="utf-8"):
    if enc == "utf-8-sig":
        enc = "utf-8"
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(text)


# ============================================================
class Editor(Gtk.Window):
    def __init__(self, path=None):
        super().__init__(title=APP_TITLE)
        self.set_default_size(900, 600)
        self.set_size_request(360, 240)

        self.path = None
        self.enc = "utf-8"
        self.modified = False
        self.font_px = 13
        self._find_from = None

        C.setup_css(b"""
            .ed-bar { background: #1c1c1e; border-bottom: 1px solid #3a3a3c; }
            .ed-tb  { background: #1c1c1e; border-bottom: 1px solid #3a3a3c; padding: 4px 6px; }
            .ed-btn { background: transparent; border: none; border-radius: 6px;
                      padding: 4px 9px; color: #f5f5f7; }
            .ed-btn:hover { background: #3a3a3c; }
            .ed-status { background: #1c1c1e; border-top: 1px solid #3a3a3c; padding: 3px 10px; }
            .ed-find { background: #2a2a2c; border: 1px solid #3a3a3c; border-radius: 6px;
                       padding: 2px 6px; color: #f5f5f7; }
            textview, textview text { background: #16161e; color: #e6e6ea; }
            .ed-root { background: #16161e; }
        """)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("ed-root")
        self.add(root)

        # ---- 工具条 ----
        tb = Gtk.Box(spacing=2)
        tb.get_style_context().add_class("ed-tb")
        root.pack_start(tb, False, False, 0)
        self._tb = tb
        for icon, tip, cb in (
            ("file", "新建 (Ctrl+N)", self.on_new),
            ("folder-open", "打开 (Ctrl+O)", self.on_open),
            ("check", "保存 (Ctrl+S)", self.on_save),
            ("box", "另存为 (Ctrl+Shift+S)", self.on_save_as),
            ("arrow-left", "撤销 (Ctrl+Z)", lambda: self._edit("undo")),
            ("arrow-right", "重做 (Ctrl+Y)", lambda: self._edit("redo")),
            ("funnel", "查找 (Ctrl+F)", lambda: self.toggle_find(True)),
            ("refresh-cw", "替换 (Ctrl+H)", lambda: self.toggle_find(True, True)),
            ("circle-minus", "缩小字号", lambda: self.zoom(-1)),
            ("circle-plus", "放大字号", lambda: self.zoom(+1)),
        ):
            b = Gtk.Button()
            b.get_style_context().add_class("ed-btn")
            b.set_tooltip_text(tip)
            b.add(C.icon_image(icon, 16, C.FG))
            b.connect("clicked", lambda _w, f=cb: (f(), False)[1])
            tb.pack_start(b, False, False, 0)
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        tb.pack_start(sep, False, False, 6)
        self.lbl_font = C.label("%d px" % self.font_px, 11, C.MUTED_FG)
        tb.pack_start(self.lbl_font, False, False, 4)

        # ---- 查找/替换条（默认隐藏）----
        self.findbar = Gtk.Box(spacing=6)
        self.findbar.get_style_context().add_class("ed-bar")
        self.findbar.set_margin_top(4)
        self.findbar.set_margin_bottom(4)
        self.findbar.set_margin_start(8)
        self.findbar.set_margin_end(8)
        self.ent_find = Gtk.Entry()
        self.ent_find.get_style_context().add_class("ed-find")
        self.ent_find.set_placeholder_text("查找…")
        self.ent_find.connect("activate", lambda _w: self.do_find(True))
        self.ent_repl = Gtk.Entry()
        self.ent_repl.get_style_context().add_class("ed-find")
        self.ent_repl.set_placeholder_text("替换为…")
        for w in (
            C.label("查找", 11, C.MUTED_FG),
            self.ent_find,
            C.label("替换", 11, C.MUTED_FG),
            self.ent_repl,
        ):
            self.findbar.pack_start(w, False, False, 0)
        for txt, cb in (
            ("下一个", lambda: self.do_find(True)),
            ("替换", self.do_replace),
            ("全部替换", self.do_replace_all),
            ("关闭", lambda: self.toggle_find(False)),
        ):
            b = Gtk.Button(label=txt)
            b.get_style_context().add_class("ed-btn")
            b.connect("clicked", lambda _w, f=cb: (f(), False)[1])
            self.findbar.pack_start(b, False, False, 0)
        root.pack_start(self.findbar, False, False, 0)
        self.findbar.set_no_show_all(True)
        self.findbar.hide()

        # ---- 文本区 ----
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.buf = Gtk.TextBuffer()
        self.view = Gtk.TextView(buffer=self.buf)
        self.view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.view.set_monospace(True)
        self.view.set_left_margin(10)
        self.view.set_right_margin(10)
        self.view.set_top_margin(8)
        self.view.set_bottom_margin(8)
        self.view.set_accepts_tab(True)
        sw.add(self.view)
        root.pack_start(sw, True, True, 0)

        # ---- 状态栏 ----
        st = Gtk.Box(spacing=10)
        st.get_style_context().add_class("ed-status")
        root.pack_start(st, False, False, 0)
        self.st_left = C.label("", 11, C.FG)
        self.st_right = C.label("", 11, C.MUTED_FG)
        st.pack_start(self.st_left, False, False, 0)
        st.pack_end(self.st_right, False, False, 0)

        # ---- 事件 ----
        self.buf.connect("changed", self.on_changed)
        self.buf.connect("mark-set", self.on_mark_set)
        self.connect("delete-event", self.on_delete)
        self.connect("key-press-event", self.on_key)
        # 拖放打开
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self.on_drop)

        self.apply_font()
        if path and os.path.isfile(path):
            self.load(path)
        else:
            self.set_title(APP_TITLE)
            self.update_status()
        self.show_all()
        self.findbar.hide()
        self.view.grab_focus()

    # ---------- 工具 ----------
    def apply_font(self):
        try:
            fd = Pango.FontDescription("monospace %d" % self.font_px)
            self.view.override_font(fd)
        except Exception:
            pass
        self.lbl_font.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%d px</span>'
            % (C.FONT, C.MUTED_FG, self.font_px)
        )

    def zoom(self, d):
        self.font_px = max(8, min(40, self.font_px + d))
        self.apply_font()

    def _edit(self, what):
        try:
            if what == "undo":
                self.buf.undo()
            elif what == "redo":
                self.buf.redo()
        except Exception:
            pass

    def select_all(self):
        try:
            self.view.select_all()
        except Exception:
            try:
                self.buf.select_range(
                    self.buf.get_start_iter(), self.buf.get_end_iter()
                )
            except Exception:
                pass

    def set_title(self, t):
        self._win_title = t
        Gtk.Window.set_title(self, t)

    def refresh_title(self):
        name = os.path.basename(self.path) if self.path else UNTITLED
        star = " •" if self.modified else ""
        self.set_title("%s%s — %s" % (name, star, APP_TITLE))

    def update_status(self):
        name = self.path or UNTITLED
        self.st_left.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%s</span>'
            % (C.FONT, C.FG, self._esc(name))
        )
        chars = self.buf.get_char_count()
        self.st_right.set_markup(
            '<span font_family="%s" font_size="11000" color="%s">%s · %d 字符 · %s</span>'
            % (C.FONT, C.MUTED_FG, self._pos_text(), chars, self.enc.upper())
        )

    @staticmethod
    def _esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _pos_text(self):
        it = self.buf.get_iter_at_mark(self.buf.get_insert())
        return "行 %d 列 %d" % (it.get_line() + 1, it.get_line_offset() + 1)

    # ---------- 事件 ----------
    def on_changed(self, _b=None):
        self.modified = True
        self.refresh_title()
        GLib.idle_add(self.update_status)

    def on_mark_set(self, _buf, _it, mark):
        if mark.get_name() == "insert":
            GLib.idle_add(self.update_status)

    def on_key(self, _w, e):
        ctrl = bool(e.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(e.state & Gdk.ModifierType.SHIFT_MASK)
        kv = Gdk.keyval_name(e.keyval) or ""
        if kv == "F3":
            self.do_find(True)
            return True
        if kv == "Escape" and self.findbar.get_visible():
            self.toggle_find(False)
            return True
        if not ctrl:
            return False
        if kv in ("n", "N") and not shift:
            self.on_new()
        elif kv in ("o", "O") and not shift:
            self.on_open()
        elif kv in ("s", "S") and not shift:
            self.on_save()
        elif kv in ("s", "S") and shift:
            self.on_save_as()
        elif kv in ("f", "F"):
            self.toggle_find(True)
        elif kv in ("h", "H"):
            self.toggle_find(True, True)
        elif kv in ("w", "W"):
            self.on_delete()
        elif kv in ("a", "A"):
            self.select_all()
        elif kv in ("z", "Z"):
            self.buf.undo()
        elif kv in ("y", "Y"):
            self.buf.redo()
        else:
            return False
        return True

    def on_delete(self, *_a):
        if not self.modified:
            Gtk.main_quit()
            return False
        d = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="是否保存更改？",
        )
        d.format_secondary_text(
            os.path.basename(self.path) if self.path else "未命名的文档"
        )
        d.add_button("取消", Gtk.ResponseType.CANCEL)
        d.add_button("不保存", Gtk.ResponseType.NO)
        d.add_button("保存", Gtk.ResponseType.YES)
        r = d.run()
        d.destroy()
        if r == Gtk.ResponseType.CANCEL:
            return True
        if r == Gtk.ResponseType.YES and not self.on_save():
            return True
        Gtk.main_quit()
        return False

    # ---------- 文件操作 ----------
    def on_new(self):
        if self.modified and not self._confirm_discard():
            return
        self.buf.set_text("")
        self.path = None
        self.enc = "utf-8"
        self.modified = False
        self.refresh_title()
        self.update_status()

    def _confirm_discard(self):
        d = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="当前文档未保存，继续将丢失更改。",
        )
        r = d.run()
        d.destroy()
        return r == Gtk.ResponseType.OK

    def load(self, path):
        try:
            text, enc = read_text(path)
        except Exception as e:
            self.toast("打开失败：%s" % e)
            return
        self.buf.set_text(text)
        self.path = path
        self.enc = enc
        self.modified = False
        self.refresh_title()
        self.update_status()
        self.view.grab_focus()

    def on_open(self):
        if self.modified and not self._confirm_discard():
            return
        p = C.run_picker(
            mode="open",
            root="/奇点OS/用户.qd/桌面",
            filters="txt,md,log,json,py,sh,conf,ini,csv,xml,html,css",
        )
        if p:
            self.load(p)

    def on_save(self):
        if not self.path:
            return self.on_save_as()
        return self._write(self.path)

    def on_save_as(self):
        default = os.path.basename(self.path) if self.path else "新建文本.txt"
        p = C.run_picker(
            mode="save",
            root="/奇点OS/用户.qd/桌面",
            default=default,
            filters="txt,md,log,json,py,sh,conf,ini,csv,xml,html,css",
        )
        if p:
            self.path = p
            return self._write(p)
        return False

    def _write(self, path):
        t = self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False)
        try:
            write_text(path, t, self.enc)
        except Exception as e:
            self.toast("保存失败：%s" % e)
            return False
        self.modified = False
        self.refresh_title()
        self.update_status()
        C.send_icons({"cmd": "toast", "text": "已保存：%s" % os.path.basename(path)})
        return True

    def toast(self, text):
        try:
            C.send_icons({"cmd": "toast", "text": text})
        except Exception:
            pass
        print("[editor]", text, flush=True)

    # ---------- 查找替换 ----------
    def toggle_find(self, show, with_replace=False):
        if show:
            self.findbar.set_no_show_all(False)
            self.findbar.show_all()
            self.ent_repl.set_visible(with_replace)
            self.ent_find.grab_focus()
            sel = self.buf.get_selection_bounds()
            if sel:
                self.ent_find.set_text(self.buf.get_text(sel[0], sel[1], False))
        else:
            self.findbar.hide()
            self.view.grab_focus()

    def _iter_range(self):
        return (self.buf.get_start_iter(), self.buf.get_end_iter())

    def do_find(self, forward=True):
        needle = self.ent_find.get_text()
        if not needle:
            return
        s, e = self._iter_range()
        if forward:
            start = self.buf.get_iter_at_mark(self.buf.get_insert())
            if self._find_from is not None and start.equal(self._find_from):
                start.forward_char()
            m = start.forward_search(needle, Gtk.TextSearchFlags.CASE_INSENSITIVE, e)
            if not m:
                m = s.forward_search(needle, Gtk.TextSearchFlags.CASE_INSENSITIVE, e)
        else:
            m = None
        if m:
            self._find_from = m[0].copy()
            self.buf.select_range(m[0], m[1])
            self.view.scroll_to_iter(m[0], 0.1, False, 0, 0)
            self.view.grab_focus()
        else:
            self.toast("未找到：%s" % needle)

    def do_replace(self):
        needle = self.ent_find.get_text()
        repl = self.ent_repl.get_text()
        if not needle:
            return
        sel = self.buf.get_selection_bounds()
        if sel and self.buf.get_text(sel[0], sel[1], False).lower() == needle.lower():
            self.buf.delete(sel[0], sel[1])
            self.buf.insert(sel[0], repl)
        self.do_find(True)

    def do_replace_all(self):
        needle = self.ent_find.get_text()
        repl = self.ent_repl.get_text()
        if not needle:
            return
        s, e = self._iter_range()
        text = self.buf.get_text(s, e, False)
        low, nl = text.lower(), needle.lower()
        n, i, out = 0, 0, []
        while True:
            j = low.find(nl, i)
            if j < 0:
                out.append(text[i:])
                break
            out.append(text[i:j])
            out.append(repl)
            i = j + len(nl)
            n += 1
        if n:
            self.buf.set_text("".join(out))
            self.toast("已替换 %d 处" % n)
        else:
            self.toast("未找到：%s" % needle)

    # ---------- 拖放 ----------
    def on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        try:
            for uri in data.get_uris():
                p = GLib.filename_from_uri(uri)[0]
                if os.path.isfile(p):
                    self.load(p)
                    break
        except Exception as e:
            self.toast("拖放打开失败：%s" % e)


def main():
    path = None
    for a in sys.argv[1:]:
        if not a.startswith("-") and os.path.exists(a):
            path = os.path.abspath(a)
    # G.16：同一个文件只开一个窗口，再次打开就把它置前（记事本行为同理）。
    # 不同文件仍各开各的——编辑器是多文档应用，不能全局单例。
    tag = "editor"
    if path:
        import hashlib

        tag = "editor_" + hashlib.md5(path.encode("utf-8")).hexdigest()[:10]
    if not C.single_instance(tag):
        sys.exit(0)
    win = Editor(path)
    C.set_raise_handler(tag, win.present)
    win.connect("destroy", lambda *_: Gtk.main_quit())
    Gtk.main()


if __name__ == "__main__":
    main()
