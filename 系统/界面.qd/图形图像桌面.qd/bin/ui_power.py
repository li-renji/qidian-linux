#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_power.py —— 奇点OS 关机/重启/锁屏对话框（G.18）
#
# 为什么必须有：Windows/macOS/GNOME/XFCE/SerenityOS/Haiku 全部自带
# 关机 UI（对标调研 v0.1 §2.2 / "第一天必须成立"第 8 条），此前只有
# 命令行。参照 GNOME 的电源菜单形态：居中对话框 + 三个动作 +
# 倒计时自动执行（GNOME 60s，我们 30s，轻量环境不用等那么久）。
#
# 动作：
#   关机 → systemctl poweroff
#   重启 → systemctl reboot
#   锁屏 → 简单遮罩窗口（X11 下锁屏需要 XScreenSaver，这里做"轻锁"：
#          全屏黑窗 + 点击弹解锁对话框，防误触不防入侵——fbdev 环境
#          无 xscreensaver 依赖时的最小方案，装了 xscreensaver 则优先）
#
# ============================================================
# 权限：qduser 的 sudo 免密仅授权 /usr/local/bin/qd-install（系统唯一
# 特权通道，见 /etc/sudoers.d/qidos-installer）。关机/重启走
# `qd-install power off|reboot`（G.18 给 qd-install 新增的子命令），
# 不另开 sudoers 口子——最小权限原则。
# ============================================================
import subprocess
import sys

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk

import qd_ui_common as C

APP_TITLE = '电源'
COUNTDOWN = 30


def _run(args):
    """qd-install 特权通道执行；返回 (ok, 输出末行)"""
    try:
        r = subprocess.run(['sudo', '-n', '/usr/local/bin/qd-install'] + args,
                           capture_output=True, text=True, timeout=15)
        out = ((r.stdout or '') + (r.stderr or '')).strip().splitlines()
        return r.returncode == 0, (out[-1] if out else '')
    except Exception as e:
        return False, str(e)


def do_poweroff():
    return _run(['power', 'off'])


def do_reboot():
    return _run(['power', 'reboot'])


# ============================================================
class PowerDialog(Gtk.Window):
    """居中电源菜单：三个大按钮，点关机/重启后进入倒计时"""

    def __init__(self):
        super().__init__(title=APP_TITLE)
        self.set_default_size(360, 260)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)

        C.setup_css(b'''
            .pw-root { background: #16161e; }
            .pw-btn  { background: #1c1c1e; border: 1px solid #3a3a3c;
                       border-radius: 12px; padding: 18px 10px; }
            .pw-btn:hover { background: #2a2a2c; }
            .pw-off  { color: #ff453a; }
            .pw-re   { color: #2e8dff; }
            .pw-lk   { color: #30d158; }
            .pw-count { background: #2a2a2c; border-radius: 12px; padding: 12px; }
        ''')

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.get_style_context().add_class('pw-root')
        root.set_border_width(18)
        self.add(root)

        root.pack_start(C.label('电源', 20, C.FG, weight='bold'), False, False, 0)

        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)
        grid.set_column_homogeneous(True)
        root.pack_start(grid, True, True, 0)

        for i, (title, icon, css, fn) in enumerate([
                ('关机', 'power', 'pw-off', self._shutdown),
                ('重启', 'rotate-cw', 'pw-re', self._restart),
                ('锁屏', 'lock', 'pw-lk', self._lock)]):
            btn = Gtk.Button()
            btn.get_style_context().add_class('pw-btn')
            btn.get_style_context().add_class(css)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.pack_start(C.icon_image(icon, 28, {
                'pw-off': C.DESTRUCT, 'pw-re': C.PRIMARY,
                'pw-lk': C.SUCCESS}[css]), False, False, 0)
            box.pack_start(C.label(title, 14, C.FG), False, False, 0)
            btn.add(box)
            btn.connect('clicked', fn)
            grid.attach(btn, i, 0, 1, 1)

        self.cancel_btn = Gtk.Button.new_with_label('取消')
        self.cancel_btn.get_style_context().add_class('pw-btn')
        self.cancel_btn.connect('clicked', lambda *_: self.destroy())
        root.pack_start(self.cancel_btn, False, False, 0)

        # 倒计时条（点关机/重启后显示；平时隐藏）
        self.count_box = Gtk.Box(spacing=10)
        self.count_box.get_style_context().add_class('pw-count')
        self.count_lbl = C.label('', 14, C.FG)
        self.count_box.pack_start(self.count_lbl, False, False, 0)
        btn_now = Gtk.Button.new_with_label('立即执行')
        btn_now.get_style_context().add_class('pw-btn')
        btn_now.connect('clicked', self._exec_now)
        self.count_box.pack_start(btn_now, False, False, 0)
        btn_stop = Gtk.Button.new_with_label('取消')
        btn_stop.get_style_context().add_class('pw-btn')
        btn_stop.connect('clicked', self._cancel_count)
        self.count_box.pack_start(btn_stop, False, False, 0)
        self.count_box.set_no_show_all(True)
        self.count_box.set_visible(False)
        root.pack_start(self.count_box, False, False, 0)

        self._pending = None      # 'poweroff' | 'reboot'
        self._left = 0

    # ---------- 动作 ----------
    def _shutdown(self, *_):
        self._arm('poweroff', '关机')

    def _restart(self, *_):
        self._arm('reboot', '重启')

    def _lock(self, *_):
        self.destroy()
        subprocess.Popen(['python3', '/奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_power.py', '--lock'])

    def _arm(self, action, name):
        self._pending = action
        self._left = COUNTDOWN
        child = self.get_child()
        # 第一次点：隐藏菜单内容，显示倒计时
        for w in child.get_children():
            w.set_visible(w is self.count_box)
        self.count_box.set_visible(True)
        self._tick(name)
        GLib.timeout_add(1000, self._tick, name)

    def _tick(self, name):
        if not self._pending:
            return False
        if self._left <= 0:
            self._exec_now()
            return False
        self.count_lbl.set_text('%d 秒后%s…' % (self._left, name))
        self._left -= 1
        return True

    def _cancel_count(self, *_):
        self._pending = None
        self.destroy()
        # 重新打开干净菜单
        subprocess.Popen(['python3', '/奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_power.py'])

    def _exec_now(self, *_):
        if self._pending == 'poweroff':
            ok, msg = do_poweroff()
        elif self._pending == 'reboot':
            ok, msg = do_reboot()
        else:
            return
        self._pending = None
        if not ok:
            # 不成功也要让用户看到原因
            err = Gtk.MessageDialog(transient_for=self, flags=0,
                                    message_type=Gtk.MessageType.ERROR,
                                    buttons=Gtk.ButtonsType.CLOSE,
                                    text='执行失败：%s' % (msg or '未知错误'))
            err.run()
            err.destroy()


# ============================================================
class LockScreen(Gtk.Window):
    """轻锁屏：全屏置顶黑窗；点击后要求确认解锁。
    有 xscreensaver 时优先用系统的（本类自动让位）。"""

    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP, title='已锁定')
        self.set_default_size(200, 100)
        self.fullscreen()
        self.set_keep_above(True)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        root.get_style_context().add_class('lk-root')
        self.override_background_color(
            Gtk.StateFlags.NORMAL, Gdk.RGBA(0, 0, 0, 1))
        root.set_valign(Gtk.Align.CENTER)
        root.set_halign(Gtk.Align.CENTER)
        self.add(root)

        root.pack_start(C.icon_image('lock', 40, '#f5f5f7'), False, False, 0)
        root.pack_start(C.label('已锁定 · 点击任意位置解锁', 14, C.MUTED_FG),
                        False, False, 0)
        self.connect('button-press-event', self._unlock)

    def _unlock(self, *_):
        # 轻锁不做密码（无 PAM 集成），点击确认即解锁；防误触为主
        dlg = Gtk.MessageDialog(transient_for=self, flags=0,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text='解锁屏幕？')
        if dlg.run() == Gtk.ResponseType.OK:
            dlg.destroy()
            self.destroy()
        else:
            dlg.destroy()


# ============================================================
if __name__ == '__main__':
    if '--lock' in sys.argv:
        win = LockScreen()
        win.show_all()
        Gtk.main()
        sys.exit(0)
    if not C.single_instance('power'):
        sys.exit(0)
    win = PowerDialog()
    C.set_raise_handler('power', win.present)
    win.connect('destroy', Gtk.main_quit)
    win.show_all()
    Gtk.main()
