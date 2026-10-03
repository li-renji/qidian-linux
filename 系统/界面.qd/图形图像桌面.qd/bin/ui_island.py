#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_island.py V5 —— 灵动岛 + 系统级 AI 面板（G.24）
#
# G.24 用户令（汇总）：
#   · 系统级 AI 不再做桌面图标、不进快捷窗，入口统一收进灵动岛；
#   · 单击岛本体展开/收起 AI 面板：AI 状态、快捷指令、对话输入；
#   · 文字输入走 ai_service 的 QD-IPC（/tmp/qd_ai.sock chat）；
#   · 语音输入入口保留：本地检测 sherpa-onnx，缺引擎时如实提示，
#     不伪造"正在听"等假状态；
#   · 面板与岛共用 RGBA visual（G.21 黑框修复三件套保留）。
#
# 沿用 V4 蓝本：ui_design/pages/index.html #dynamic-island
#   · top-3 居中 · min-w 280 · h 40 · rounded-full
# ============================================================
import json
import os
import socket
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import qd_ui_common as C

BAR_H = 40  # 岛高（球同高，与岛同平面）
ORB_D = 40  # 圆球直径 = 岛高
ORB_GAP = 8  # 球间距
PAD = 6  # 窗口内边距
GAP = 10  # 岛与面板间距
MIN_FILE = "/tmp/qd_minimized.json"
MAX_PER_SIDE = 5
AI_SOCK = "/tmp/qd_ai.sock"
PANEL_W = 460  # AI 面板宽
PANEL_H = 430  # AI 面板高
AI_TIMEOUT = 45  # chat 最长等待（本地模型可能较慢）
PING_INTERVAL = 3  # AI 心跳秒数

CSS = b"""
    * { background: transparent; }
    .island { background: rgba(28,28,30,230); border: 1px solid rgba(58,58,58,102);
              border-radius: 20px; }
    .orb { background: rgba(58,58,60,235); border: none; border-radius: 20px; }
    .orb:hover { background: rgba(46,141,255,200); }
    .panel { background: rgba(28,28,30,242); border: 1px solid rgba(58,58,58,120);
             border-radius: 20px; }
    .qbtn { background: rgba(58,58,60,200); border-radius: 10px; padding: 4px 10px; }
    .qbtn:hover { background: rgba(46,141,255,170); }
    .qbtn label { color: #f5f5f7; font-family: WenQuanYi Zen Hei; font-size: 12px; }
    .entry { background: rgba(58,58,60,220); border-radius: 12px;
             color: #f5f5f7; font-family: WenQuanYi Zen Hei; font-size: 13px;
             padding: 4px 10px; }
    .sendbtn { background: rgba(46,141,255,210); border-radius: 12px; padding: 2px 10px; }
    .sendbtn:hover { background: rgba(46,141,255,255); }
"""


def load_minimized():
    """读 qwm 写的最小化清单（按时间戳=最小化顺序）"""
    try:
        data = json.load(open(MIN_FILE))
        return [(int(m["id"]), str(m["title"]), m.get("ts", 0)) for m in data]
    except Exception:
        return []


def ai_call(payload, timeout=AI_TIMEOUT):
    """向 ai_service 发 QD-IPC 请求，返回响应 dict；失败抛异常"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(AI_SOCK)
        s.sendall(json.dumps(payload).encode("utf-8"))
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8") or "{}")
    finally:
        s.close()


def speech_engine_available():
    """语音识别引擎探测：sherpa-onnx 必须可 import 才算就绪"""
    try:
        import sherpa_onnx  # noqa: F401

        return True
    except Exception:
        return False


class Island(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_decorated(False)
        # G.24 修复：POPUP/DOCK 窗口默认不接收键盘输入焦点，
        # 不设置则点击 AI 输入框后打字无效（用户验收指出的交互缺陷）
        self.set_accept_focus(True)
        self.set_focus_on_map(True)
        self.set_app_paintable(True)
        # G.21 关键：RGBA visual（修黑框——没有这行 transparent=黑）
        rgba = Gdk.Screen.get_default().get_rgba_visual()
        if rgba:
            self.set_visual(rgba)

        C.setup_css(CSS)
        self.fixed = Gtk.Fixed()
        self.add(self.fixed)

        self.expanded = False  # AI 面板是否展开
        self.ai_online = False  # ping 心跳结果
        self._ai_busy = False  # chat 进行中

        self._build_bar()
        self._build_panel()

        self.orb_widgets = []
        self._last_sig = None
        self._tick()
        self._relayout()
        self.show_all()
        self._poll()
        GLib.timeout_add_seconds(1, self._tick)
        GLib.timeout_add(500, self._poll)
        GLib.timeout_add_seconds(PING_INTERVAL, self._heartbeat)

    # ---------------- 岛本体（胶囊，可单击） ----------------
    def _build_bar(self):
        self.bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.bar.get_style_context().add_class("island")
        self.bar.set_margin_start(16)
        self.bar.set_margin_end(16)

        g1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ov = Gtk.Overlay()
        ov.add(C.icon_image("message-circle-more", 16, C.PRIMARY))
        dot = Gtk.Fixed()
        red = Gtk.EventBox()
        red.add(C._dot(C.DESTRUCT, 8))
        dot.put(red, 12, -2)
        ov.add_overlay(dot)
        g1.pack_start(ov, False, False, 0)
        g1.pack_start(C._vline(), False, False, 0)
        self.bar_state_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.fg_dot = C._dot(C.SUCCESS, 8)
        self.bar_state_box.pack_start(self.fg_dot, False, False, 0)
        self.bar_state_box.pack_start(
            C.label("AI 在线", 12, C.FG, "500"), False, False, 0
        )
        g1.pack_start(self.bar_state_box, False, False, 0)
        self.bar.pack_start(g1, False, False, 0)

        g2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        g2.pack_start(C.icon_image("atom", 16, C.PRIMARY), False, False, 0)
        g2.pack_start(C.label("奇点 OS", 12, C.FG, "600"), False, False, 0)
        self.bar.pack_start(g2, False, False, 0)

        g3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.date_lbl = C.label("", 12, C.MUTED_FG)
        self.time_lbl = C.label("", 12, C.FG, "500")
        g3.pack_start(self.date_lbl, False, False, 0)
        g3.pack_start(self.time_lbl, False, False, 0)
        self.bar.pack_start(g3, False, False, 0)

        # 单击岛本体 -> 展开/收起 AI 面板（G.24）
        self.bar_ev = Gtk.EventBox()
        self.bar_ev.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.bar_ev.connect("button-press-event", self._bar_press)
        self.bar_ev.add(self.bar)

    def _bar_press(self, _w, e):
        # 4 = GDK_BUTTON_PRESS（单击）；双击（5）不在此处理
        if int(getattr(e, "type", 0)) == 4:
            self._toggle_panel()
        return False

    def _tick(self):
        now = time.localtime()
        wk = "一二三四五六日"[int(time.strftime("%w"))]
        self.date_lbl.set_markup(
            '<span font_family="%s" font_size="12000" color="%s">%s 周%s</span>'
            % (C.FONT, C.MUTED_FG, time.strftime("%m/%d"), wk)
        )
        self.time_lbl.set_markup(
            '<span font_family="%s" font_size="12000" weight="500" color="%s">%s</span>'
            % (C.FONT, C.FG, time.strftime("%H:%M:%S"))
        )
        return True

    # ---------------- AI 面板 ----------------
    def _build_panel(self):
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.panel.get_style_context().add_class("panel")
        self.panel.set_size_request(PANEL_W, PANEL_H)
        self.panel.set_margin_start(14)
        self.panel.set_margin_end(14)
        self.panel.set_margin_top(12)
        self.panel.set_margin_bottom(12)

        # 1) 状态行：AI 在线/离线 + 关闭
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.p_dot = self._mk_dot(C.SUCCESS, 8)
        self.p_state = C.label("AI 在线", 13, C.FG, "600")
        self.p_model = C.label("", 11, C.MUTED_FG)
        row.pack_start(self.p_dot, False, False, 0)
        row.pack_start(self.p_state, False, False, 0)
        row.pack_start(self.p_model, False, False, 0)
        close = Gtk.Button.new_from_icon_name(
            "window-close-symbolic", Gtk.IconSize.MENU
        )
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.get_style_context().add_class("qbtn")
        close.connect("clicked", lambda *_a: self._collapse())
        row.pack_end(close, False, False, 0)
        self.panel.pack_start(row, False, False, 0)

        # 2) 快捷指令区（G.24：系统级功能直通）
        qrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for icon, name, cmd in (
            ("globe", "浏览器", "python3 /奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_browser.py"),
            ("folder-open", "文件", "python3 /奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_filebrowser.py"),
            ("image", "相册", "python3 /奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_album.py --show"),
            ("trash-2", "回收站", "python3 /奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_trash.py"),
            ("settings", "设置", "python3 /奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_settings.py"),
            ("file-text", "编辑器", "python3 /奇点OS/系统/界面.qd/图形图像桌面.qd/bin/ui_editor.py"),
        ):
            btn = self._qbtn(icon, name, cmd)
            qrow.pack_start(btn, False, False, 0)
        self.panel.pack_start(qrow, False, False, 0)

        # 3) 对话历史
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_size_request(-1, 210)
        self.chat = Gtk.TextView()
        self.chat.set_editable(False)
        self.chat.set_cursor_visible(False)
        self.chat.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.chat.set_left_margin(8)
        self.chat.set_right_margin(8)
        self.chat.set_top_margin(6)
        self.buf = self.chat.get_buffer()
        self.buf.create_tag("user", foreground="#2e8dff", font="WenQuanYi Zen Hei 12")
        self.buf.create_tag("ai", foreground="#f5f5f7", font="WenQuanYi Zen Hei 12")
        self.buf.create_tag("meta", foreground="#8e8e93", font="WenQuanYi Zen Hei 10")
        self.buf.create_tag("warn", foreground="#f5c344", font="WenQuanYi Zen Hei 11")
        sw.add(self.chat)
        # G.25 残影修复：滚动时降低更新频率，减少重绘压力
        sw.get_vadjustment().connect("value-changed", self._on_scroll)
        self._scroll_queued = False
        self.panel.pack_start(sw, True, True, 0)
        self._append(
            "奇点AI",
            "你好，我是奇点 OS 系统 AI。\n可以直接输入问题，或点上面的快捷指令。",
            "ai",
        )

        # 4) 输入行：语音 + 文字 + 发送
        inrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mic = Gtk.Button.new_from_icon_name(
            "audio-input-microphone-symbolic", Gtk.IconSize.BUTTON
        )
        mic.set_relief(Gtk.ReliefStyle.NONE)
        mic.get_style_context().add_class("qbtn")
        mic.connect("clicked", self._voice)
        inrow.pack_start(mic, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.get_style_context().add_class("entry")
        self.entry.set_placeholder_text("问问 AI（Enter 发送）")
        self.entry.connect("activate", self._send)
        inrow.pack_start(self.entry, True, True, 0)

        send = Gtk.Button.new_from_icon_name("send-symbolic", Gtk.IconSize.BUTTON)
        send.set_relief(Gtk.ReliefStyle.NONE)
        send.get_style_context().add_class("sendbtn")
        send.connect("clicked", self._send)
        inrow.pack_start(send, False, False, 0)
        self.panel.pack_start(inrow, False, False, 0)

        self.fixed.put(self.panel, 0, 0)  # 位置在 _arrange 里校正
        self.panel.hide()

    def _mk_dot(self, color, d):
        state = {"c": color}
        da = Gtk.DrawingArea()
        da.set_size_request(d, d)

        def on_draw(_, cr):
            c = state["c"]
            cr.set_source_rgba(*(int(c[i : i + 2], 16) / 255 for i in (1, 3, 5)), 1)
            cr.arc(d / 2, d / 2, d / 2, 0, 6.2832)
            cr.fill()
            return False

        da.connect("draw", on_draw)
        da._set_color = lambda c: (state.__setitem__("c", c), da.queue_draw())
        return da

    def _qbtn(self, icon, name, cmd):
        btn = Gtk.Button()
        btn.get_style_context().add_class("qbtn")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        box.pack_start(C.icon_image(icon, 14, C.FG), False, False, 0)
        box.pack_start(C.label(name, 12, C.FG), False, False, 0)
        btn.add(box)
        btn.connect("clicked", lambda *_a: self._launch(cmd))
        return btn

    def _launch(self, cmd):
        if not cmd:
            return
        try:
            subprocess.Popen(
                cmd.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass

    # ---------------- 对话 / 语音 ----------------
    def _append(self, who, text, tag):
        if who:
            self.buf.insert_with_tags_by_name(
                self.buf.get_end_iter(), who + "\n", "meta"
            )
        self.buf.insert_with_tags_by_name(self.buf.get_end_iter(), text + "\n\n", tag)
        # G.25 残影修复：滚动到末尾用 idle_add 批量，避免逐字触发重绘风暴
        GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self):
        """统一滚动到底，避免多次 scroll_to_iter 触发逐帧重绘"""
        self.chat.scroll_to_iter(self.buf.get_end_iter(), 0.0, False, 0, 0)
        return False

    def _on_scroll(self, adj):
        """滚动 value-changed 信号：队列化，避免滚轮连发导致重绘风暴"""
        if not getattr(self, "_scroll_queued", False):
            self._scroll_queued = True
            GLib.timeout_add(50, self._scroll_redraw)
        return False

    def _scroll_redraw(self):
        self._scroll_queued = False
        self.chat.queue_draw()
        return False

    def _send(self, *_a):
        if self._ai_busy:
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self._append("你", text, "user")
        self._set_ai_state(False, "思考中…")
        self._ai_busy = True
        threading.Thread(target=self._ai_work, args=(text,), daemon=True).start()

    def _ai_work(self, text):
        try:
            resp = ai_call({"cmd": "chat", "text": text})
            ok = resp.get("ok", False)
            reply = resp.get("reply", "") or "（无回复）"
            if resp.get("degraded"):
                reply += "\n（本地模型不可用，以上为知识/记忆检索降级结果）"
            err = str(resp.get("error", ""))
        except Exception as e:
            ok, reply, err = False, "", str(e)
        GLib.idle_add(self._on_reply, ok, reply, err)
        return

    def _on_reply(self, ok, reply, err):
        self._ai_busy = False
        if ok:
            self._append("奇点AI", reply, "ai")
            self._set_ai_state(True, "AI 在线")
        else:
            msg = "AI 服务暂时不可用：%s" % err if err else "AI 服务暂时不可用。"
            self._append("奇点AI", msg, "warn")
            self._set_ai_state(False, "AI 离线")
        return False

    def _voice(self, *_a):
        if speech_engine_available():
            self._flash_state("语音识别引擎就绪。当前版本请先用文字输入。")
        else:
            self._flash_state("语音识别未启用：未检测到 sherpa-onnx 引擎。")

    def _flash_state(self, text):
        """在状态行短暂显示提示，2.5s 后恢复真实状态"""
        prev = self.p_state.get_label()
        self.p_state.set_label(text)
        self.p_state.set_use_markup(False)

        def restore():
            try:
                self.p_state.set_label(prev)
                self.p_state.set_use_markup(False)
            except Exception:
                pass
            return False

        GLib.timeout_add(2500, restore)

    # ---------------- AI 心跳 ----------------
    def _heartbeat(self):
        def work():
            try:
                resp = ai_call({"cmd": "ping"}, timeout=3)
                ok = bool(resp.get("ok"))
            except Exception:
                ok = False
            GLib.idle_add(self._apply_heartbeat, ok)
            return

        threading.Thread(target=work, daemon=True).start()
        return True

    def _apply_heartbeat(self, ok):
        self.ai_online = ok
        for ch in list(self.bar_state_box.get_children()):
            self.bar_state_box.remove(ch)
        self.bar_state_box.pack_start(
            self._mk_dot(C.SUCCESS if ok else C.DESTRUCT, 8), False, False, 0
        )
        self.bar_state_box.pack_start(
            C.label(
                "AI 在线" if ok else "AI 离线", 12, C.FG if ok else C.MUTED_FG, "500"
            ),
            False,
            False,
            0,
        )
        self.bar_state_box.show_all()
        self._set_ai_state(ok)
        return False

    def _set_ai_state(self, online, override=None):
        self.p_dot._set_color(C.SUCCESS if online else C.DESTRUCT)
        label = override if override else ("AI 在线" if online else "AI 离线")
        color = C.FG if online else C.MUTED_FG
        self.p_state.set_markup(
            '<span font_family="%s" font_size="13000" weight="600" color="%s">%s</span>'
            % (C.FONT, color, label)
        )
        return False

    # ---------------- 展开 / 收起 ----------------
    def _toggle_panel(self):
        if self.expanded:
            self._collapse()
        else:
            self._expand()

    def _expand(self):
        self.expanded = True
        self._arrange()
        # G.24 修复：展开后把键盘焦点交给输入框，否则用户无法打字
        self.present()
        GLib.timeout_add(120, self._focus_entry)
        return

    def _focus_entry(self):
        try:
            win = self.get_window()
            if win is not None:
                win.focus(Gdk.CURRENT_TIME)
            self.entry.grab_focus()
        except Exception:
            pass
        return False

    def _collapse(self):
        self.expanded = False
        self._arrange()

    # ---------------- 布局（V4 球 + V5 面板共用） ----------------
    def _arrange(self):
        _, nat_w = self.bar_ev.get_preferred_width()
        bar_w = max(C.ISLAND_MINW, nat_w or 0)
        max_side = MAX_PER_SIDE * (ORB_D + ORB_GAP)
        if self.expanded:
            win_w = max(
                PANEL_W + 2 * PAD, max_side + 2 * PAD + bar_w + 2 * PAD + max_side
            )
            win_h = PAD + BAR_H + GAP + PANEL_H + PAD
        else:
            win_w = max_side + 2 * PAD + bar_w + 2 * PAD + max_side
            win_h = BAR_H + 2 * PAD
        bar_x = (win_w - bar_w) // 2
        if self.bar_ev not in self.fixed.get_children():
            self.fixed.put(self.bar_ev, bar_x, PAD)
        else:
            self.fixed.move(self.bar_ev, bar_x, PAD)
        if self.expanded:
            self.fixed.move(self.panel, (win_w - PANEL_W) // 2, PAD + BAR_H + GAP)
            self.panel.show_all()
        else:
            self.panel.hide()
        self.resize(win_w, win_h)
        self._position()

    # ---------------- 圆球（V4 逻辑保留） ----------------
    def _poll(self):
        mins = load_minimized()
        sig = [(m[0], m[1]) for m in mins]
        if sig != self._last_sig:
            self._last_sig = sig
            self._rebuild_orbs(mins)
        return True

    def _rebuild_orbs(self, mins):
        for w in self.orb_widgets:
            self.fixed.remove(w)
        self.orb_widgets = []

        n = len(mins)
        left = mins[: (n + 1) // 2]
        right = mins[(n + 1) // 2 :]

        _, nat_w = self.bar_ev.get_preferred_width()
        bar_w = max(C.ISLAND_MINW, nat_w or 0)
        max_side = MAX_PER_SIDE * (ORB_D + ORB_GAP)
        if self.expanded:
            win_w = max(
                PANEL_W + 2 * PAD, max_side + 2 * PAD + bar_w + 2 * PAD + max_side
            )
        else:
            win_w = max_side + 2 * PAD + bar_w + 2 * PAD + max_side
        bar_x = (win_w - bar_w) // 2
        if self.bar_ev not in self.fixed.get_children():
            self.fixed.put(self.bar_ev, bar_x, PAD)
        else:
            self.fixed.move(self.bar_ev, bar_x, PAD)

        def add_orb(num, wid, title, x):
            ev = Gtk.EventBox()
            ev.get_style_context().add_class("orb")
            ev.set_size_request(ORB_D, ORB_D)
            ev.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            ev.connect("button-press-event", self._orb_press, wid, num, title)
            ev.add(C.label(str(num), 15, C.FG, "bold"))
            self.fixed.put(ev, x, PAD)
            self.orb_widgets.append(ev)

        x = bar_x - ORB_GAP - ORB_D
        for i, (wid, title, _ts) in enumerate(left):
            if i >= MAX_PER_SIDE:
                break
            add_orb(i + 1, wid, title, x)
            x -= ORB_D + ORB_GAP
        x = bar_x + bar_w + ORB_GAP
        for j, (wid, title, _ts) in enumerate(right):
            if j >= MAX_PER_SIDE:
                break
            add_orb(len(left) + j + 1, wid, title, x)
            x += ORB_D + ORB_GAP

        self._arrange()

    def _orb_press(self, _w, e, wid, num, title):
        # 双击还原（用户令：双击数字打开窗口）
        if int(getattr(e, "type", 0)) == 5:
            try:
                subprocess.run(
                    ["/奇点OS/系统/界面.qd/图形图像桌面.qd/bin/qwmctl", "unhide", str(wid)],
                    timeout=3,
                    capture_output=True,
                )
            except Exception:
                pass
        return False

    def _relayout(self):
        self._rebuild_orbs(load_minimized())

    def _position(self):
        sw = Gdk.Screen.get_default().get_width()
        w, _h = self.get_size()
        self.move((sw - w) // 2, C.ISLAND_TOP)


if __name__ == "__main__":
    win = Island()
    Gtk.main()
