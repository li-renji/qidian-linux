#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_icons.py V3 —— 桌面图标层（壁纸合成 + input-only 点击区）
# 背景：本机 Xorg fbdev 无合成器且 SHAPE 扩展不生效（实测区域提交成功
#       但渲染不裁剪），ARGB 半透明渲染成黑——窗口方案无法透明。
# 方案：图标+标签一次性合成进桌面壁纸（icons_on.png / icons_off.png），
#       feh 切换显隐；每个图标一个 override-redirect input-only 窗口
#       （不可见）接收双击启动应用。渲染零开销、天然透明。
#   · 双击图标启动应用；未安装 → toast "未安装"
#   · socket /tmp/qd_icons.sock：toggle 显隐 / toast / reload 热更新
# 布局：全部来自 ~/.config/qidos/settings.json（G.14）
#   icon_size / icon_gap / icon_label / label_size / icon_x / icon_y0 / per_col
# 点击区尺寸与绘制尺寸同源（C.icon_cell），保证点了不偏。
# ============================================================
import os, sys, json, time, socket, subprocess, threading, shlex, select, signal
# 僵尸防护：本进程会频繁 Popen 起 feh 且不 wait，子进程退出后无人回收会
# 变成僵尸堆积（实测 28+）。忽略 SIGCHLD 交给内核自动回收。
try:
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
except Exception:
    pass
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import qd_ui_common as C
from Xlib import display, X
import cairo


def _pick_cjk_font():
    try:
        out = subprocess.run(
            ["fc-list", ":lang=zh", "family"], capture_output=True, text=True
        ).stdout
        fams = set()
        for line in out.splitlines():
            for f in line.split(","):
                f = f.strip()
                if f:
                    fams.add(f)
        for wanted in ("WenQuanYi Zen Hei", "Noto Sans CJK SC"):
            if wanted in fams:
                return wanted
        for f in fams:
            if any(k in f for k in ("CJK", "SC", "Hei", "Kai", "Song", "Fang")):
                return f
        for f in fams:
            return f
    except Exception:
        pass
    return "monospace"


def _dbg(msg):
    try:
        with open("/tmp/icons_dbg.log", "a") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _feh(png):
    subprocess.Popen(
        ["feh", "--bg-fill", png], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


class ClickAreas(threading.Thread):
    """input-only 窗口组：不可见，仅接收双击

    G.14 健壮性修复：此前线程只阻塞在 next_event()，X 服务器重启或窗口
    被外部销毁后它毫无察觉 → 点击落到 root 被 qwm 当"双击空白"处理，
    表现为"双击图标没反应、图标反而整体消失"。现在改为：
      · select 超时循环（1s），超时即做一次窗口存活校验，缺失立即重建
      · 主线程可通过 set_cfg 触发重建（改图标大小/增减应用后同步点击区）
      · 连接断开自愈（沿用 G.10）
    """

    def __init__(self, bridge, app):
        super().__init__(daemon=True)
        self.bridge = bridge  # GLib.idle_add 桥（跨线程调 GTK/Glib）
        self.app = app
        self.cfg = app.cfg
        self.regen = False
        self.wins = {}  # window_id -> (name, cmd)
        self.wmap = {}  # window_id -> window 对象（销毁/存活校验用）
        self.lock = threading.Lock()

    def set_cfg(self, cfg):
        """主线程调用：配置变更后要求重建点击区"""
        self.cfg = cfg
        self.regen = True

    def _regions(self):
        """按当前配置与注册表算出点击区 [(name, cmd, x, y, w, h)]（G.25 统一走 icon_positions）"""
        with self.lock:
            cfg = dict(self.cfg)
        iw, ih = C.icon_cell(cfg)
        out = []
        apps = C.all_apps()
        # G.25：icon_positions 是渲染/点击区/拖拽落点的唯一来源
        for name, x, y in C.icon_positions(cfg, apps):
            for a in apps:
                if a.get("name") == name:
                    out.append((name, a.get("cmd"), x, y, iw, ih))
                    break
        return out

    def run(self):
        while True:
            try:
                self._serve()
            except Exception as e:
                _dbg("click areas err: %r" % e)
            time.sleep(1.0)

    def _rebuild(self, root):
        for w in list(self.wmap.values()):
            try:
                w.destroy()
            except Exception:
                pass
        self.wins.clear()
        self.wmap.clear()
        for name, cmd, x, y, iw, ih in self._regions():
            w = root.create_window(
                x,
                y,
                iw,
                ih,
                0,
                X.CopyFromParent,
                X.InputOnly,
                X.CopyFromParent,
                override_redirect=True,
                event_mask=X.ButtonPressMask
                | X.PointerMotionMask
                | X.ButtonReleaseMask,
            )
            w.map()
            self.wins[w.id] = (name, cmd)
            self.wmap[w.id] = w
        self.regen = False
        _dbg("click areas: %d 个点击窗口" % len(self.wins))

    def _alive(self):
        """窗口存活校验：任一窗口取属性失败即判定失效"""
        if not self.wmap:
            return False
        for wid, w in list(self.wmap.items()):
            try:
                w.get_attributes()
            except Exception:
                _dbg("窗口 %s 已失效，触发重建" % wid)
                return False
        return True

    def _serve(self):
        d = display.Display()
        root = d.screen().root
        self._rebuild(root)
        d.sync()
        last = {"id": 0, "t": 0.0}
        self._drag = None
        last_check = time.time()
        while True:
            if self.regen:
                self._rebuild(root)
                d.sync()
                continue
            if time.time() - last_check > 3.0:
                last_check = time.time()
                if not self._alive():
                    self._rebuild(root)
                    d.sync()
                    continue
            try:
                fd = d.socket.fileno()
            except Exception:
                fd = d.fileno()
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                continue
            ev = d.next_event()
            if ev.type == X.ButtonPress and ev.detail == 1:
                info = self.wins.get(ev.window.id)
                if not info:
                    continue
                name, cmd = info
                now = time.time()
                if self._drag and self._drag.get("dragging"):
                    continue
                if self._drag:
                    d.ungrab_pointer(X.CurrentTime)
                    self._drag = None
                self._drag = {
                    "name": name,
                    "cmd": cmd,
                    "t": now,
                    "px": ev.root_x,
                    "py": ev.root_y,
                    "dragging": False,
                }
                # G.25：input-only 窗口注册 PointerMotionMask + ButtonReleaseMask，
                # X11 在按下后会自动建立隐式指针抓取（implicit grab），
                # motion/release 事件路由到当前窗口，无需显式 grab_pointer。
                last = {"id": ev.window.id, "t": now}
            elif ev.type == X.MotionNotify and self._drag:
                if self._drag["dragging"]:
                    self.bridge(self.app.drag_move, ev.root_x, ev.root_y)
                else:
                    dx = abs(ev.root_x - self._drag["px"])
                    dy = abs(ev.root_y - self._drag["py"])
                    if dx > 8 or dy > 8:
                        self._drag["dragging"] = True
                        self.bridge(
                            self.app.drag_start,
                            self._drag["name"],
                            self._drag["px"],
                            self._drag["py"],
                        )
                        self.bridge(self.app.drag_move, ev.root_x, ev.root_y)
            elif ev.type == X.ButtonRelease and ev.detail == 1 and self._drag:
                if self._drag["dragging"]:
                    self.bridge(self.app.drag_drop, ev.root_x, ev.root_y)
                d.ungrab_pointer(X.CurrentTime)
                # 如果是单击（未进入拖拽），检查是否构成双击
                now = time.time()
                if (
                    not self._drag["dragging"]
                    and last["id"] == ev.window.id
                    and now - last["t"] < 0.4
                ):
                    _dbg("dblclick %s" % self._drag["name"])
                    last = {"id": 0, "t": 0.0}
                    self.bridge(self._launch, self._drag["name"], self._drag["cmd"])
                self._drag = None

    def _launch(self, name, cmd):
        # G.16 用户令：启动成功**不再弹 toast**——窗口弹出来本身就是反馈，
        # 弹"启动 X"反而像在骗人（此前窗口常被旧窗口完全盖住，看着就是没开）。
        # 只有真的出了问题（未安装 / 起不来）才提示。
        if not cmd:
            self.bridge(self.app.toast, name + " 未安装")
            return
        try:
            subprocess.Popen(
                shlex.split(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            _dbg("launch %s: %s" % (name, cmd))
        except Exception as ex:
            _dbg("启动失败 %s: %s" % (name, ex))
            self.bridge(self.app.toast, name + " 启动失败")


class Icons:
    def __init__(self):
        self.cfg = C.load_conf()
        self._toast_win = self._mk_toast()
        self._toast_timer = None
        self.visible = True

        self.render()
        _feh(self.on_png)  # 初始确保 on 版上屏

        self.click = ClickAreas(GLib.idle_add, self)
        self.click.start()
        threading.Thread(target=self._serve, daemon=True).start()

    # ---------- 壁纸合成 ----------
    def paths(self):
        try:
            import ui_wallpaper as _W

            return _W.OUT_ON, _W.OUT_OFF
        except Exception:
            return C.UI_DIR + "/icons_on.png", C.UI_DIR + "/icons_off.png"

    def render(self):
        """按当前配置 + 应用注册表重绘桌面合成壁纸（G.25：位置走 icon_positions）"""
        self.on_png, self.off_png = self.paths()
        all_apps = C.all_apps()
        apps = [(a["icon"], a["name"]) for a in all_apps]
        iw, ih = C.icon_cell(self.cfg)
        try:
            import ui_wallpaper

            ui_wallpaper.render(
                apps,
                dict(
                    ICON_X=int(self.cfg.get("icon_x", 24)),
                    ICON_Y0=int(self.cfg.get("icon_y0", 76)),
                    ICON_W=iw,
                    ICON_H=ih,
                    PER_COL=int(self.cfg.get("per_col", 5)),
                    GAP=int(self.cfg.get("icon_gap", 10)),
                    ICON_SIZE=int(self.cfg.get("icon_size", 40)),
                    LABEL_SIZE=int(self.cfg.get("label_size", 11)),
                    SHOW_LABEL=bool(self.cfg.get("icon_label", True)),
                    POSITIONS=[
                        (n, x, y) for n, x, y in C.icon_positions(self.cfg, all_apps)
                    ],
                ),
                wallpaper=self.cfg.get("wallpaper"),
                out_on=self.on_png,
                out_off=self.off_png,
            )
        except Exception as e:
            _dbg("wallpaper render err: %r" % e)

    def reload(self):
        """设置变更后热更新：重绘壁纸 + 重建点击区（无需重启组件）"""
        self.cfg = C.load_conf()
        self.render()
        _feh(self.on_png if self.visible else self.off_png)
        self.click.set_cfg(self.cfg)
        _dbg(
            "reload 完成 size=%s gap=%s label=%s"
            % (
                self.cfg.get("icon_size"),
                self.cfg.get("icon_gap"),
                self.cfg.get("icon_label"),
            )
        )
        return False

    # ---------- G.25 图标拖拽 ----------
    def drag_start(self, name, x, y):
        """创建跟随鼠标的 ghost 窗口（显示被拖图标+标签）"""
        self._drag_name = name
        _dbg("drag_start %s @(%d,%d)" % (name, x, y))
        iw, ih = C.icon_cell(self.cfg)
        isize = int(self.cfg.get("icon_size", 40))
        # ghost 窗口 = 比原图标大一圈，半透明
        gw, gh = iw + 16, ih + 16
        self._drag_win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self._drag_win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self._drag_win.set_decorated(False)
        self._drag_win.set_skip_taskbar_hint(True)
        self._drag_win.set_app_paintable(True)
        self._drag_win.set_size_request(gw, gh)
        self._drag_win.set_opacity(0.65)
        rgba = Gdk.Screen.get_default().get_rgba_visual()
        if rgba:
            self._drag_win.set_visual(rgba)
        da = Gtk.DrawingArea()
        da.set_size_request(gw, gh)

        def draw(_, cr):
            # 圆角底
            cr.set_source_rgba(0.11, 0.11, 0.13, 0.85)
            r = 12
            cr.new_sub_path()
            cr.arc(gw - r, r, r, -1.571, 0)
            cr.arc(gw - r, gh - r, r, 0, 1.571)
            cr.arc(r, gh - r, r, 1.571, 3.1416)
            cr.arc(r, r, r, 3.1416, 4.7124)
            cr.close_path()
            cr.fill()
            # 图标
            cx = gw / 2
            cxp = C.pixbuf_icon(
                [a for a in C.all_apps() if a.get("name") == name][0].get("icon"),
                isize,
                C.FG,
            )
            if cxp is not None:
                Gdk.cairo_set_source_pixbuf(cr, cxp, cx - isize / 2, 6)
                cr.paint()
            # 标签
            cr.set_source_rgba(0.96, 0.96, 0.98, 0.9)
            font = _pick_cjk_font()
            cr.select_font_face(font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(11)
            fb = cr.text_extents(name)
            cr.move_to(cx - fb.width / 2, 8 + isize + 6 + fb.height)
            cr.show_text(name)
            return False

        da.connect("draw", draw)
        self._drag_win.add(da)
        self._drag_win.show_all()
        self.drag_move(x, y)
        return False

    def drag_move(self, x, y):
        if not getattr(self, "_drag_win", None):
            return False
        # 鼠标中心对齐
        w = self._drag_win.get_allocated_width()
        h = self._drag_win.get_allocated_height()
        self._drag_win.move(int(x) - w // 2, int(y) - h // 2)
        return False

    def drag_drop(self, x, y):
        """释放：吸附最近网格中心，写入 icon_pos 持久化，并重绘壁纸+点击区"""
        name = getattr(self, "_drag_name", None)
        if not name:
            return False
        iw, ih = C.icon_cell(self.cfg)
        ix = int(self.cfg.get("icon_x", 24))
        iy0 = int(self.cfg.get("icon_y0", 76))
        gap = int(self.cfg.get("icon_gap", 10))
        per = max(1, int(self.cfg.get("per_col", 5)))
        # 最近网格吸附
        col = round((x - ix - iw / 2) / (iw + gap))
        row = round((y - iy0 - ih / 2) / (ih + gap))
        col = max(0, col)
        row = max(0, row)
        while row >= per:
            row -= per
            col += 1
        nx = ix + col * (iw + gap)
        ny = iy0 + row * (ih + gap)
        _dbg("drag_drop %s -> (%d,%d)" % (name, nx, ny))
        # 持久化
        pos = self.cfg.get("icon_pos") or {}
        pos[name] = [nx, ny]
        self.cfg["icon_pos"] = pos
        C.save_conf(self.cfg)
        # 重绘（幽灵隐藏、点击区重建自动调用 icon_positions）
        try:
            self._drag_win.destroy()
        except Exception:
            pass
        self._drag_win = None
        self._drag_name = None
        self.render()
        _feh(self.on_png if self.visible else self.off_png)
        self.click.set_cfg(self.cfg)
        self.toast(f"已移动 {name} 到 ({nx},{ny})")
        return False

    # ---------- 全局 toast（原型 #toast：top-14 圆角胶囊 popover，1800ms） ----------
    def _mk_toast(self):
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        win.set_decorated(False)
        win.set_app_paintable(True)
        win.set_skip_taskbar_hint(True)
        C.setup_css(b"""
            .toast { background: rgba(58,58,60,255); border: 1px solid rgba(58,58,60,128);
                     border-radius: 9999px; padding: 8px 16px; }
            .toast label { color: #f5f5f7; font-family: WenQuanYi Zen Hei; font-size: 14px; }
        """)
        lbl = Gtk.Label(label="提示信息")
        box = Gtk.Box()
        box.get_style_context().add_class("toast")
        box.add(lbl)
        win.add(box)
        sw = Gdk.Screen.get_default().get_width()
        win.show_all()

        def place():
            w = box.get_allocated_width() + 32
            win.move((sw - w) // 2, C.TOAST_TOP)
            return False

        GLib.timeout_add(50, place)
        win.hide()
        win._lbl = lbl
        return win

    def toast(self, text):
        self._toast_win._lbl.set_text(text)
        self._toast_win.show_all()
        if self._toast_timer is not None:
            GLib.source_remove(self._toast_timer)
        self._toast_timer = GLib.timeout_add(1800, self._toast_hide)

    def _toast_hide(self):
        self._toast_win.hide()
        self._toast_timer = None
        return False

    # ---------- socket 服务：toggle / toast / reload ----------
    def _serve(self):
        SOCK_PATH = "/tmp/qd_icons.sock"
        if os.path.exists(SOCK_PATH):
            try:
                os.remove(SOCK_PATH)
            except OSError:
                pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        srv.listen(4)
        while True:
            try:
                conn, _ = srv.accept()
                data = conn.recv(8192).decode()
                conn.close()
                _dbg("recv: %s" % data)
                msg = json.loads(data)
                cmd = msg.get("cmd")
                if cmd == "toggle":
                    GLib.idle_add(self.toggle)
                elif cmd == "toast":
                    GLib.idle_add(self.toast, msg.get("text", ""))
                elif cmd == "reload":
                    GLib.idle_add(self.reload)
                elif cmd == "show":
                    GLib.idle_add(self.set_visible, True)
                elif cmd == "hide":
                    GLib.idle_add(self.set_visible, False)
            except Exception:
                time.sleep(0.5)

    def set_visible(self, v):
        if self.visible != bool(v):
            self.toggle()
        return False

    def toggle(self):
        self.visible = not self.visible
        _feh(self.on_png if self.visible else self.off_png)
        _dbg("toggle → %s" % ("显示" if self.visible else "隐藏"))
        return False


if __name__ == "__main__":
    Icons()
    Gtk.main()
