#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# qwm.py v2 —— 奇点OS 自研窗口管理器（X11 · 浮动 WM）
#
# G.15 本轮改造（对标 berry / zwm / lwm / Openbox 的功能集）：
#   【修复】标题栏三键在小窗/全屏后消失 —— 根因：frame 未订阅 ExposureMask，
#           X server 在 map/resize 时用 background_pixel 覆盖标题栏后无人重绘。
#           现补 ExposureMask + 状态变化即重绘 + 错误限频。
#   【新增】标题栏绘制走 cairo(ImageSurface) + X put_image：
#           窗口标题文字（中文可用）+ 活动/非活动配色 + — □ ✕ 三按钮。
#   【新增】四处可抓边（BW=5px）：鼠标移到窗口外框即可八向缩放（不再只有 Alt+右键）。
#   【新增】几何变更节流（~16ms 合并一次），消除拖尾/残影。
#   【新增】berry 式动作：左/右吸附、最大化、全屏(无装饰)、居中、方向聚焦、
#           循环切换、智能放置、4 工作区（Alt+1..4 切换 / Alt+Shift+1..4 送入）。
#   【新增】qwmctl 命令通道（对标 berryc）：/tmp/qd_qwm.sock。
#   【新增】日志限频：同一错误 5 秒内只打一次，杜绝 100MB 级日志爆炸。
#
# 规范：见《奇点OS_Linux版系统规范_v1.1.md》14.5 / G.11 / G.13 / G.15
# ============================================================
import os
import sys
import time
import json
import socket
import threading
import traceback
from select import select

from Xlib import X, Xatom, display as xdisplay, XK
from Xlib import error as xerror
from Xlib.protocol import event as xe

try:
    import cairo

    _HAS_CAIRO = True
except Exception:
    _HAS_CAIRO = False

# ---------- 常量 ----------
TB = 28  # 标题栏高
BTN = 22  # 标题栏按钮热区
BW = 5  # 四周抓边宽（视觉上成为窗口外框）
RADIUS = 10  # 与 picom corner-radius 对齐（仅用于绘制内圆角参考）
WORK_Y0 = 60  # 工作区上界（灵动岛下沿）
WORK_Y1 = 736  # 工作区下界（Dock 上沿）
MIN_W, MIN_H = 160, 100
CASCADE = 32  # G.16：无空位时的级联偏移量（保证窗口永不完全重叠）
FRAME_OPACITY = 204  # 默认 80%（G.11 设计值；G.19 修复 32 位标度后恢复）
N_WORKSPACE = 4

COL_BG_ACT = (0x2A / 255, 0x2A / 255, 0x2C / 255)  # 活动标题栏
COL_BG_INA = (0x1C / 255, 0x1C / 255, 0x1E / 255)  # 非活动标题栏
COL_FRAME = 0x1C1C1E  # 外框底色
COL_BORDER = (0x3A / 255, 0x3A / 255, 0x3C / 255)
COL_FG_ACT = (0xF5 / 255, 0xF5 / 255, 0xF7 / 255)
COL_FG_INA = (0x8E / 255, 0x8E / 255, 0x93 / 255)
COL_ACCENT = (0x2E / 255, 0x8D / 255, 0xFF / 255)

_NET_WM_NAME = "_NET_WM_NAME"
_NET_ACTIVE_WINDOW = "_NET_ACTIVE_WINDOW"
_NET_CLIENT_LIST = "_NET_CLIENT_LIST"
_NET_CLIENT_LIST_STACKING = "_NET_CLIENT_LIST_STACKING"
_NET_WM_STATE = "_NET_WM_STATE"
_NET_WM_STATE_HIDDEN = "_NET_WM_STATE_HIDDEN"
_NET_WM_STATE_FULLSCREEN = "_NET_WM_STATE_FULLSCREEN"
# G.22：最大化状态（规范 14.5.2 要求写 EWMH，供外部/面板读取）
_NET_WM_STATE_MAXIMIZED_VERT = "_NET_WM_STATE_MAXIMIZED_VERT"
_NET_WM_STATE_MAXIMIZED_HORZ = "_NET_WM_STATE_MAXIMIZED_HORZ"
_NET_WM_STATE_ABOVE = "_NET_WM_STATE_ABOVE"
_NET_WM_WINDOW_TYPE = "_NET_WM_WINDOW_TYPE"
_NET_WM_WINDOW_TYPE_DESKTOP = "_NET_WM_WINDOW_TYPE_DESKTOP"
_NET_WM_WINDOW_TYPE_DOCK = "_NET_WM_WINDOW_TYPE_DOCK"
_NET_WM_WINDOW_TYPE_DIALOG = "_NET_WM_WINDOW_TYPE_DIALOG"
_NET_WM_DESKTOP = "_NET_WM_DESKTOP"
_NET_NUMBER_OF_DESKTOPS = "_NET_NUMBER_OF_DESKTOPS"
_NET_CURRENT_DESKTOP = "_NET_CURRENT_DESKTOP"
_NET_SUPPORTING_WM_CHECK = "_NET_SUPPORTING_WM_CHECK"
_NET_SUPPORTED = "_NET_SUPPORTED"
_NET_WM_WINDOW_OPACITY = "_NET_WM_WINDOW_OPACITY"
_NET_WM_MOVERESIZE = "_NET_WM_MOVERESIZE"
_NET_WM_SYNC_REQUEST = "_NET_WM_SYNC_REQUEST"
_NET_WM_SYNC_REQUEST_COUNTER = "_NET_WM_SYNC_REQUEST_COUNTER"
_UTF8_STRING = "UTF8_STRING"

SUPPORTED_ATOMS = [
    _NET_WM_NAME,
    _NET_ACTIVE_WINDOW,
    _NET_CLIENT_LIST,
    _NET_CLIENT_LIST_STACKING,
    _NET_WM_STATE,
    _NET_WM_STATE_HIDDEN,
    _NET_WM_STATE_FULLSCREEN,
    _NET_WM_STATE_MAXIMIZED_VERT,
    _NET_WM_STATE_MAXIMIZED_HORZ,
    _NET_WM_STATE_ABOVE,
    _NET_WM_WINDOW_TYPE,
    _NET_WM_WINDOW_TYPE_DESKTOP,
    _NET_WM_WINDOW_TYPE_DOCK,
    _NET_WM_WINDOW_TYPE_DIALOG,
    _NET_WM_DESKTOP,
    _NET_NUMBER_OF_DESKTOPS,
    _NET_CURRENT_DESKTOP,
    _NET_SUPPORTING_WM_CHECK,
    _NET_SUPPORTED,
    _NET_WM_WINDOW_OPACITY,
    _NET_WM_MOVERESIZE,
    _NET_WM_SYNC_REQUEST,
    _NET_WM_SYNC_REQUEST_COUNTER,
]

# Alt+键 动作表：keysym 名 -> (动作, 是否要 Shift)
RESIZE_LIMIT_MS = 16


# ============================================================
# 日志限频：同一消息 5 秒内只输出一次，第 N 次追加计数
# ============================================================
_LOG_LAST = {}
_LOG_SKIP = {}
_LOG_LOCK = threading.Lock()


def log(msg):
    try:
        key = str(msg)[:160]
        now = time.time()
        with _LOG_LOCK:
            if now - _LOG_LAST.get(key, 0) < 5.0:
                _LOG_SKIP[key] = _LOG_SKIP.get(key, 0) + 1
                return
            skip = _LOG_SKIP.pop(key, 0)
            _LOG_LAST[key] = now
            # 清理老化键，避免字典无界增长
            if len(_LOG_LAST) > 400:
                for k in [k for k, t in _LOG_LAST.items() if now - t > 60]:
                    _LOG_LAST.pop(k, None)
        line = "[qwm] " + key
        if skip:
            line += "  (+%d 次重复)" % skip
        print(line, flush=True)
    except Exception:
        pass


class Client:
    def __init__(self, win, wm):
        self.win = win
        self.wm = wm
        self.x = 0
        self.y = 0
        self.w = 1
        self.h = 1
        self.mapped = False
        self.hidden = False  # 用户最小化（→ 灵动岛圆球）
        self.minimized_at = 0.0  # G.21：最小化时间戳（圆球编号=最小化顺序）
        self.fullscreen = False  # 无装饰全屏
        self.decorated = True  # 是否绘制标题栏
        self.dock = False
        self.desktop = False
        self.dialog = False
        self.frame = None
        self.gc = None
        self.maximized = False
        self.snapped = None  # 'left'/'right'/None
        self.pre_geom = None
        self.ws = 0  # 工作区
        self.title = ""
        self.overridden = False  # override_redirect（桌面自绘层，不托管）
        self._title_cache = None
        self._dock_reserved = False
        self._read_attrs()

    # ---------- 属性 ----------
    def _read_attrs(self):
        try:
            a = self.win.get_attributes()
            self.w, self.h = a.width, a.height
            # override_redirect：桌面覆盖层/工具窗（图标点击层、Dock、灵动岛、
            # 右键菜单…）。绝不能被当成应用窗口接管——G.15 事故：qwm 重启时
            # _adopt_existing 把 ui_icons 的 80×88 点击窗口 frame 成了 890×593。
            self.overridden = bool(getattr(a, "override_redirect", False))
        except Exception:
            pass
        try:
            t = self.win.get_full_property(
                self.wm.atoms[_NET_WM_WINDOW_TYPE], X.AnyPropertyType
            )
            if t and t.value:
                typ = self.wm.reverse_atoms.get(t.value[0], "")
                if typ == _NET_WM_WINDOW_TYPE_DOCK:
                    self.dock = True
                elif typ == _NET_WM_WINDOW_TYPE_DESKTOP:
                    self.desktop = True
                elif typ == _NET_WM_WINDOW_TYPE_DIALOG:
                    self.dialog = True
        except Exception:
            pass
        self.read_title()

    def read_title(self):
        """标题：_NET_WM_NAME(UTF8) → WM_NAME → WM_CLASS → 无标题"""
        t = ""
        try:
            p = self.win.get_full_property(
                self.wm.atoms[_NET_WM_NAME], self.wm.atoms[_UTF8_STRING]
            )
            if p and p.value:
                t = p.value
                if isinstance(t, bytes):
                    t = t.decode("utf-8", "replace")
        except Exception:
            pass
        if not t:
            try:
                t = self.win.get_wm_name() or ""
            except Exception:
                t = ""
        if not t:
            try:
                cls = self.win.get_wm_class()
                if cls:
                    t = cls[1] if len(cls) > 1 else cls[0]
            except Exception:
                t = ""
        self.title = (t or "").strip().replace("\n", " ")[:120]
        return self.title

    def geom(self):
        """frame 几何（含外框）：x,y,w,h"""
        return (self.x, self.y, self.w + 2 * BW, self.h + TB + BW)

    def is_managed(self):
        """可托管窗口：排除桌面/Dock 与一切 override_redirect 层"""
        return not (self.dock or self.desktop or self.overridden)


# ============================================================
class QWM:
    def __init__(self, display_name=":0"):
        self.d = xdisplay.Display(display_name)
        self.screen = self.d.screen()
        self.root = self.screen.root
        self.sw = self.screen.width_in_pixels
        self.sh = self.screen.height_in_pixels
        # G.20：ARGB frame（32 位 visual）——不透明度只作用于"框"（标题栏+边框），
        # client 内容保持不透明。替代 G.19 的 _NET_WM_WINDOW_OPACITY 方案
        # （那个会把整窗含内容一起变透明，用户实测否决）。
        self._argb_visual = None
        self._argb_cmap = None
        self._setup_argb()
        # G.20：根窗口左箭头光标（此前无 xcursor 主题时全桌面显示 X 叉叉光标）
        self._root_cursor = None
        self._setup_root_cursor()
        self.atoms = {}
        self.reverse_atoms = {}
        for name in SUPPORTED_ATOMS:
            try:
                a = self.d.intern_atom(name)
                self.atoms[name] = a
                self.reverse_atoms[a] = name
            except Exception:
                pass
        self.clients = {}  # win id -> Client
        self.frames = {}  # frame id -> Client
        self.client_list = []
        self.focus = None
        self.alt_mask = self._alt_mask()
        self.shift_mask = X.ShiftMask
        self.dragging = None  # (client, mode, sx, sy, ox, oy, edge)
        self._last_title_click = 0
        self._last_root_click = 0
        self.ws = 0  # 当前工作区
        self._pending = {}  # wid -> 待应用的几何（节流）
        self._pending_ts = 0.0
        self._cmds = []  # socket 命令队列
        self._srv = None
        self._srv_path = "/tmp/qd_qwm.sock"
        self._last_sweep = 0.0  # G.16 上次兜底扫描时间
        self._panel_ids = []  # G.21 岛/Dock XID（置顶用）
        self._setup_wm()
        self._setup_socket()
        self._grab_keys()

    # ---------- 初始化 ----------
    def _setup_argb(self):
        """找 depth-32 TrueColor visual（alpha 在最高字节，Composite 扩展约定）。
        原型验证（G.20）：32 位 frame + border_pixel=0 + map 后绘制，
        picom 下标题栏半透明/client 不透明/透明区露壁纸全部正常。"""
        try:
            for dep in self.d.display.info.roots[0]["allowed_depths"]:
                if dep["depth"] == 32:
                    for v in dep["visuals"]:
                        if v["visual_class"] == X.TrueColor:
                            self._argb_visual = v["visual_id"]
                            break
                if self._argb_visual:
                    break
            if self._argb_visual:
                self._argb_cmap = self.root.create_colormap(
                    self._argb_visual, X.AllocNone
                )
                log("argb visual=%s" % hex(self._argb_visual))
            else:
                log("argb: 无 32 位 visual，回退 24 位 frame")
        except Exception as e:
            log("argb setup err %r" % (e,))

    def _setup_root_cursor(self):
        """根窗口定义左箭头光标。无 xcursor 主题时 X 默认给 X 形叉叉光标，
        表现为整个桌面鼠标显示为 X（用户报障 G.20-6）。"""
        try:
            char = 68  # XC_left_ptr
            try:
                from Xlib.X.cursorfont import XC_left_ptr

                char = XC_left_ptr
            except Exception:
                pass
            f = self.d.open_font("cursor")
            cur = f.create_glyph_cursor(
                f, char, char + 1, (0, 0, 0), (65535, 65535, 65535)
            )
            self.root.change_attributes(cursor=cur)
            self.d.flush()
            self._root_cursor = cur  # 持引用防 GC 回收
        except Exception as e:
            log("root cursor err %r" % (e,))

    def _alt_mask(self):
        try:
            modmap = self.d.get_modifier_mapping()
            for i in range(8):
                for keycode in modmap[i]:
                    if keycode:
                        name = self.d.keycode_to_keysym(keycode, 0)
                        if name in (X.K_Alt_L, X.K_Alt_R):
                            return 1 << i
        except Exception:
            return X.Mod1Mask
        return X.Mod1Mask

    def _setup_wm(self):
        check = self.root.create_window(0, 0, 1, 1, 0, self.screen.root_depth)
        check.set_wm_name("qwm")
        self.root.change_property(
            self.atoms[_NET_SUPPORTING_WM_CHECK], Xatom.WINDOW, 32, [check.id]
        )
        self.root.change_property(
            self.atoms[_NET_SUPPORTED],
            Xatom.ATOM,
            32,
            [self.atoms[n] for n in SUPPORTED_ATOMS if n in self.atoms],
        )
        self.root.change_property(self.atoms[_NET_CLIENT_LIST], Xatom.WINDOW, 32, [])
        self.root.change_property(
            self.atoms[_NET_NUMBER_OF_DESKTOPS], Xatom.CARDINAL, 32, [N_WORKSPACE]
        )
        self.root.change_property(
            self.atoms[_NET_CURRENT_DESKTOP], Xatom.CARDINAL, 32, [0]
        )
        # G.16：SubstructureRedirect 全 X server 只允许一个 client 持有。
        # 拿不到（BadAccess）说明还有别的 WM 活着——此时**绝不能继续跑**：
        # 那会变成"把老窗口 frame 一遍、却永远接管不了新窗口"的坏状态，
        # 表象正是用户报的"点了图标却没弹窗"。直接退出让启动链重来。
        try:
            self.root.change_attributes(
                event_mask=(
                    X.SubstructureRedirectMask
                    | X.SubstructureNotifyMask
                    | X.PropertyChangeMask
                    | X.ButtonPressMask
                    | X.KeyPressMask
                    | X.ButtonMotionMask
                    | X.ButtonReleaseMask
                )
            )
            self.d.flush()
            self.d.sync()
        except Exception as e:
            raise SystemExit("已有窗口管理器占用 SubstructureRedirect：%r" % (e,))

    # Alt 组合键表：(keysym, need_shift, 动作名)
    KEYMAP = [
        # G.22：对齐规范 14.5.3 —— 新增 Alt+F10 最大化切换（保留 F11 兼容）
        # 与 Alt+Space 窗口菜单（Windows/Openbox 惯例）
        ("Tab", 0, "cycle"),
        ("F4", 0, "close"),
        ("F5", 0, "minimize"),
        ("F10", 0, "maximize"),
        ("F11", 0, "maximize"),
        ("Return", 0, "fullscreen"),
        ("space", 0, "menu"),
        ("Left", 0, "snap_left"),
        ("Right", 0, "snap_right"),
        ("Up", 0, "maximize"),
        ("Down", 0, "center"),
        ("c", 0, "center"),
        ("x", 0, "maximize"),
        ("Left", 1, "focus_left"),
        ("Right", 1, "focus_right"),
        ("Up", 1, "focus_up"),
        ("Down", 1, "focus_down"),
        ("1", 0, "ws_0"),
        ("2", 0, "ws_1"),
        ("3", 0, "ws_2"),
        ("4", 0, "ws_3"),
        ("1", 1, "send_0"),
        ("2", 1, "send_1"),
        ("3", 1, "send_2"),
        ("4", 1, "send_3"),
    ]

    def _grab_keys(self):
        mod2 = X.Mod2Mask  # 通常是 NumLock：同时抓取，避免开着大写/数字锁定就失效
        for name, sh, act in self.KEYMAP:
            try:
                ks = XK.string_to_keysym(name)
                kc = self.d.keysym_to_keycode(ks)
                if not kc:
                    continue
                base = self.alt_mask | (self.shift_mask if sh else 0)
                for extra in (0, mod2):
                    self.root.grab_key(
                        kc, base | extra, 1, X.GrabModeAsync, X.GrabModeAsync
                    )
            except Exception as e:
                log("grab %s err %r" % (name, e))

    # ---------- qwmctl 命令通道（对标 berryc） ----------
    def _setup_socket(self):
        try:
            if os.path.exists(self._srv_path):
                os.unlink(self._srv_path)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(self._srv_path)
            srv.listen(8)
            srv.setblocking(False)
            self._srv = srv
        except Exception as e:
            log("socket err %r" % (e,))

    def _accept_cmds(self):
        if not self._srv:
            return
        try:
            conn, _ = self._srv.accept()
        except Exception:
            return
        try:
            conn.settimeout(0.3)
            data = conn.recv(4096).decode("utf-8", "replace")
            req = json.loads(data) if data.strip() else {}
            self._cmds.append(req)
            try:
                conn.sendall(b'{"ok":true}')
            except Exception:
                pass
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _drain_cmds(self):
        while self._cmds:
            req = self._cmds.pop(0)
            try:
                self._do_cmd(req)
            except Exception as e:
                log("cmd err %r %r" % (req, e))

    def _do_cmd(self, req):
        c = (req.get("cmd") or "").strip()
        cl = self._focused()
        cur = cl
        if req.get("id"):
            cur = self.clients.get(int(req["id"]))
        if c == "window_move" and cur:
            cur.x += int(req.get("x", 0))
            cur.y += int(req.get("y", 0))
            self._apply(cur)
        elif c == "window_move_absolute" and cur:
            cur.x, cur.y = int(req.get("x", cur.x)), int(req.get("y", cur.y))
            self._apply(cur)
        elif c == "window_resize" and cur:
            cur.w = max(MIN_W, cur.w + int(req.get("w", 0)))
            cur.h = max(MIN_H, cur.h + int(req.get("h", 0)))
            self._apply(cur)
        elif c == "window_resize_absolute" and cur:
            cur.w = max(MIN_W, int(req.get("w", cur.w)))
            cur.h = max(MIN_H, int(req.get("h", cur.h)))
            self._apply(cur)
        elif c == "window_raise" and cur:
            self._raise(cur)
        elif c == "window_close" and cur:
            self._close(cur.win.id)
        elif c == "window_minimize" and cur:
            # G.22：窗口菜单"最小化"入口（等价 Alt+F5，藏进灵动岛圆球）
            self._toggle_hidden(cur.win.id)
        elif c == "window_center" and cur:
            self._center(cur)
        elif c == "window_monocle" and cur:
            self._maximize(cur)
        elif c == "fullscreen" and cur:
            self._fullscreen(cur)
        elif c == "snap_left" and cur:
            self._snap(cur, "left")
        elif c == "snap_right" and cur:
            self._snap(cur, "right")
        elif c == "toggle_decorations" and cur:
            cur.decorated = not cur.decorated
            self._apply(cur)
            self._decorate(cur)
        elif c == "cycle_focus":
            self._cycle()
        elif c == "switch_workspace":
            self._switch_ws(int(req.get("i", 0)))
        elif c == "send_to_workspace" and cur:
            cur.ws = int(req.get("i", 0)) % N_WORKSPACE
            self._apply_view()
        elif c == "redecorate":
            # G.20：设置页实时生效——不透明度等外观改动后重绘所有 frame
            QWM._cached_opacity = None  # 强制下次读取最新配置
            for cl in list(self.clients.values()):
                if cl.frame:
                    self._decorate(cl)
        elif c == "window_unhide":
            # G.21：灵动岛圆球双击还原（隐藏中的窗口才处理）
            if cur and cur.hidden:
                self._toggle_hidden(cur.win.id)
                if not cur.hidden:
                    self._raise(cur)
        elif c == "list":
            # 写成文件供 qwmctl 读取（socket 连接已关闭，无法直接回包）
            try:
                data = [
                    {
                        "id": c2.win.id,
                        "title": c2.title,
                        "ws": c2.ws,
                        "x": c2.x,
                        "y": c2.y,
                        "w": c2.w,
                        "h": c2.h,
                        "focused": c2.win.id == self.focus,
                        "hidden": c2.hidden,
                        "maximized": c2.maximized,
                        "visible": self._visible(c2),
                    }
                    for c2 in self.clients.values()
                    if c2.is_managed()
                ]
                with open("/tmp/qd_qwm_list.json", "w") as f:
                    json.dump({"ws": self.ws, "clients": data}, f)
            except Exception as e:
                log("list err %r" % (e,))
        elif c == "ping":
            pass

    def _focused(self):
        cl = self.clients.get(self.focus)
        if cl and cl.is_managed():
            return cl
        for wid in reversed(self.client_list):
            cl = self.clients.get(wid)
            if cl and cl.is_managed() and self._visible(cl):
                return cl
        return None

    # ---------- 接管窗口 ----------
    def _adopt_existing(self):
        try:
            for child in self.root.query_tree().children:
                if child.id in self.clients:
                    continue
                try:
                    attrs = child.get_attributes()
                    if attrs.map_state != X.IsViewable:
                        continue
                    if getattr(attrs, "override_redirect", False):
                        continue  # 桌面自绘层，绝不托管
                    try:
                        if (child.get_wm_name() or "") == "qwm-frame":
                            continue  # 上一代 qwm 的残留 frame
                    except Exception:
                        pass
                    self._adopt_one(child)
                except Exception as e:
                    log("adopt one %s %r" % (hex(child.id), e))
            self._update_client_list()
            if self.client_list:
                self._raise(self.clients[self.client_list[0]])
        except Exception as e:
            log("adopt err %r" % (e,))

    def _adopt_one(self, child):
        """把一个已经 map 的 root 子窗口收编进 frame（含标题栏）"""
        cl = Client(child, self)
        if not cl.is_managed():
            return False
        self.clients[child.id] = cl
        self.client_list.append(child.id)
        try:
            geo = child.get_geometry()
            cl.x, cl.y, cl.w, cl.h = geo.x, geo.y, geo.width, geo.height
        except Exception:
            cl.x, cl.y, cl.w, cl.h = 60, WORK_Y0, 880, 560
        # 裸奔窗口（没被 WM 管过）通常停在 (0,0)：重新走一次摆放
        if cl.w < 100 or cl.h < 80:
            cl.w, cl.h = 880, 560
        if cl.x <= 0 and cl.y <= 0:
            self._smart_place(cl)
        try:
            hints = child.get_wm_normal_hints()
            if hints:
                mw = getattr(hints, "min_width", 0) or 0
                mh = getattr(hints, "min_height", 0) or 0
                if cl.w < mw:
                    cl.w = mw
                if cl.h < mh:
                    cl.h = mh
        except Exception:
            pass
        cl.ws = self.ws
        self._frame_create(cl)
        if cl.frame:
            self.frames[cl.frame.id] = cl
            cl.frame.map()
        cl.mapped = True
        log("adopt %s %r" % (hex(child.id), cl.title))
        return True

    def _sweep_orphans(self):
        """G.16 自愈：周期性扫描 root 子窗口，补收编漏掉的窗口。

        为什么需要：MapRequest 只要漏接一次，那个窗口就会以 (0,0)、
        无标题栏的状态裸奔——用户看到的就是"点了图标却没弹窗"。
        有了这个兜底，即使事件链路出过问题，最多 3 秒内也会被收编。"""
        try:
            got = 0
            for child in self.root.query_tree().children:
                if child.id in self.clients:
                    continue
                try:
                    attrs = child.get_attributes()
                    if attrs.map_state != X.IsViewable:
                        continue
                    if getattr(attrs, "override_redirect", False):
                        continue
                    if (child.get_wm_name() or "") == "qwm-frame":
                        continue
                    if self._adopt_one(child):
                        got += 1
                except Exception:
                    continue
            if got:
                self._update_client_list()
                self._apply_view()
        except Exception as e:
            log("sweep err %r" % (e,))

    def _map(self, ev):
        win = ev.window
        if win.id in self.clients:
            return
        cl = Client(win, self)
        if not cl.is_managed():
            return
        try:
            win.change_attributes(
                override_redirect=False,
                event_mask=(
                    X.StructureNotifyMask
                    | X.PropertyChangeMask
                    | X.ButtonPressMask
                    | X.KeyPressMask
                ),
            )
        except Exception:
            pass
        self.clients[win.id] = cl
        self.client_list.append(win.id)
        self._update_client_list()
        try:
            hints = win.get_wm_normal_hints()
            if hints:
                mw = getattr(hints, "min_width", 0) or 0
                mh = getattr(hints, "min_height", 0) or 0
                if cl.w < mw:
                    cl.w = mw
                if cl.h < mh:
                    cl.h = mh
        except Exception:
            pass
        if cl.w < 100 or cl.h < 80:
            cl.w, cl.h = 880, 560
        if cl.w > self.sw - 40:
            cl.w = self.sw - 40
        if cl.h > self.sh - 80:
            cl.h = self.sh - 80
        self._smart_place(cl)  # berry 式：优先放到空位
        cl.ws = self.ws
        self._frame_create(cl)
        if cl.frame:
            self.frames[cl.frame.id] = cl
        self._place(cl)
        if cl.frame:
            cl.frame.map()
        win.map()
        cl.mapped = True
        self._raise(cl)
        self._apply_view()
        log("map %s %r" % (hex(win.id), cl.title))

    def _smart_place(self, cl):
        """在不与现有窗口重叠的位置放置；找不到则居中（对标 berry smart_place）"""
        try:
            others = [
                c
                for c in self.clients.values()
                if c is not cl and c.is_managed() and self._visible(c)
            ]
            fw, fh = cl.w + 2 * BW, cl.h + TB + BW
            step = 40
            best = None
            best_d = None
            cx, cy = (self.sw - fw) // 2, WORK_Y0 + (WORK_Y1 - WORK_Y0 - fh) // 2
            for y in range(WORK_Y0, max(WORK_Y0 + 1, WORK_Y1 - fh) + 1, step):
                for x in range(0, max(1, self.sw - fw) + 1, step):
                    if any(
                        not (
                            x + fw < c.x
                            or x > c.x + c.w + 2 * BW
                            or y + fh < c.y
                            or y > c.y + c.h + TB + BW
                        )
                        for c in others
                    ):
                        continue
                    dist = (x - cx) ** 2 + (y - cy) ** 2
                    if best_d is None or dist < best_d:
                        best_d, best = dist, (x, y)
            if best:
                cl.x, cl.y = best
            else:
                # G.16：找不到空位时**绝不能全部居中**——同尺寸应用会被
                # 一个压一个完全重叠，用户点图标看起来就是"没弹窗"。
                # 改为级联错开：每个新窗口比前一个右下方偏移 CASCADE px，
                # 超出工作区就绕回起点，保证任意两个窗口都不会完全重合。
                n = len(
                    [c for c in self.clients.values() if c is not cl and c.is_managed()]
                )
                off = (n % 8) * CASCADE
                cl.x = min(cx + off, max(0, self.sw - fw))
                cl.y = min(cy + off, max(WORK_Y0, WORK_Y1 - fh))
        except Exception:
            cl.x = (self.sw - cl.w) // 2
            cl.y = WORK_Y0 + 40

    # ---------- 几何 ----------
    def _place(self, cl):
        """同步 frame 与 client 几何（frame 比 client 四周大 BW，顶部多 TB）"""
        if cl.fullscreen:
            fw = cl.w + 2 * BW if cl.decorated else cl.w
            fh = cl.h + (TB + BW if cl.decorated else 0)
            cx, cy = (BW, TB) if cl.decorated else (0, 0)
        else:
            fw, fh, cx, cy = cl.w + 2 * BW, cl.h + TB + BW, BW, TB
        try:
            if cl.frame:
                cl.frame.configure(x=cl.x, y=cl.y, width=fw, height=fh, border_width=0)
                cl.win.configure(x=cx, y=cy, width=cl.w, height=cl.h, border_width=0)
            else:
                cl.win.configure(
                    x=cl.x, y=cl.y, width=cl.w, height=cl.h, border_width=0
                )
        except Exception as e:
            log("place err %r" % (e,))

    def _apply(self, cl):
        """节流后的几何应用：几何变化立刻改记录，真正 configure 按帧合并"""
        self._pending[cl.win.id] = cl
        self.d.flush()

    def _flush_pending(self, force=False):
        """按帧节流应用几何变更（消除 ConfigureNotify 风暴导致的拖尾）"""
        if not self._pending:
            return
        now = time.time()
        if not force and (now - self._pending_ts) * 1000 < RESIZE_LIMIT_MS:
            return
        self._pending_ts = now
        items = list(self._pending.values())
        self._pending.clear()
        for cl in items:
            try:
                self._place(cl)
                # 宽度变化才需要重画标题栏（按钮/文字位置依赖宽度）
                fw = cl.w + 2 * BW
                if getattr(cl, "_last_fw", None) != fw:
                    cl._last_fw = fw
                    self._decorate(cl)
            except Exception as e:
                log("flush err %r" % (e,))
        try:
            self.d.flush()
        except Exception:
            pass

    # ---------- 标题栏 ----------
    _cached_opacity = None
    _cached_opacity_ts = 0.0

    @classmethod
    def _frame_opacity(cls):
        now = time.time()
        if cls._cached_opacity is not None and now - cls._cached_opacity_ts < 2.0:
            return cls._cached_opacity
        try:
            with open(
                "/home/qduser/.config/qidos/settings.json", encoding="utf-8"
            ) as f:
                v = json.load(f).get("opacity")
            if isinstance(v, int) and 0 < v <= 255:
                cls._cached_opacity = v
                cls._cached_opacity_ts = now
                return v
        except Exception:
            pass
        return FRAME_OPACITY

    def _frame_create(self, cl):
        try:
            if self._argb_visual:
                # G.20 ARGB frame：32 位窗口，透明底 + 按像素 alpha 绘制。
                # border_pixel 必须显式给 0，否则 CreateWindow BadMatch（实测踩坑）。
                fw = self.root.create_window(
                    cl.x,
                    cl.y,
                    cl.w + 2 * BW,
                    cl.h + TB + BW,
                    0,
                    32,
                    window_class=X.InputOutput,
                    visual=self._argb_visual,
                    colormap=self._argb_cmap,
                    background_pixel=0x00000000,
                    border_pixel=0,
                    event_mask=(
                        X.ButtonPressMask
                        | X.ButtonReleaseMask
                        | X.ButtonMotionMask
                        | X.ExposureMask
                        | X.SubstructureRedirectMask
                        | X.SubstructureNotifyMask
                    ),
                )
            else:
                # 回退：24 位 frame + 整窗 opacity 属性（G.19 方案）
                fw = self.root.create_window(
                    cl.x,
                    cl.y,
                    cl.w + 2 * BW,
                    cl.h + TB + BW,
                    0,
                    self.screen.root_depth,
                    window_class=X.InputOutput,
                    background_pixel=COL_FRAME,
                    event_mask=(
                        X.ButtonPressMask
                        | X.ButtonReleaseMask
                        | X.ButtonMotionMask
                        | X.ExposureMask
                        | X.SubstructureRedirectMask
                        | X.SubstructureNotifyMask
                    ),
                )
            fw.set_wm_name("qwm-frame")
            if not self._argb_visual:
                try:
                    op = self.d.intern_atom(_NET_WM_WINDOW_OPACITY)
                    v8 = self._frame_opacity() & 0xFF
                    v32 = (v8 << 24) | (v8 << 16) | (v8 << 8) | v8
                    fw.change_property(op, Xatom.CARDINAL, 32, [v32])
                except Exception as e:
                    log("opacity err %r" % (e,))
            self.d.flush()
            cl.win.reparent(fw, BW, TB)
            cl.win.configure(x=BW, y=TB, width=cl.w, height=cl.h)
            self.d.flush()
            cl.frame = fw
            try:
                cl.gc = fw.create_gc(foreground=COL_FRAME, background=COL_FRAME)
            except Exception:
                cl.gc = None
            self._decorate(cl)
        except Exception as e:
            log("frame err %r" % (e,))

    def _is_active(self, cl):
        return cl.win.id == self.focus

    def _draw_button(self, cr, kind, cx, cy, r, act):
        """用 cairo 画 — □ ✕ 三按钮"""
        cr.set_line_width(1.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        if act:
            cr.set_source_rgba(*COL_FG_ACT)
        else:
            cr.set_source_rgba(*COL_FG_INA)
        if kind == "close":
            cr.move_to(cx - r, cy - r)
            cr.line_to(cx + r, cy + r)
            cr.move_to(cx + r, cy - r)
            cr.line_to(cx - r, cy + r)
            cr.stroke()
        elif kind == "max":
            cr.rectangle(cx - r, cy - r, 2 * r, 2 * r)
            cr.stroke()
        elif kind == "min":
            cr.move_to(cx - r, cy)
            cr.line_to(cx + r, cy)
            cr.stroke()

    def _btn_positions(self, fw):
        cx_x = fw - BW - BTN // 2 - 8
        cx_m = cx_x - BTN - 2
        cx_n = cx_m - BTN - 2
        return {"close": cx_x, "max": cx_m, "min": cx_n}

    def _decorate(self, cl):
        """绘制标题栏。优先 cairo(put_image) 路径（支持中文标题），失败回退 core 绘制。"""
        if not cl.frame:
            return
        try:
            if not cl.decorated:
                # 无装饰：整块填外框色（视觉上与内容齐平）
                if cl.gc:
                    cl.gc.change(foreground=self._frame_pixel(COL_FRAME))
                    cl.frame.fill_rectangle(cl.gc, 0, 0, cl.w + 2 * BW, cl.h + TB + BW)
                    self.d.flush()
                return
            fw = cl.w + 2 * BW
            act = self._is_active(cl)
            if _HAS_CAIRO:
                self._decorate_cairo(cl, fw, act)
            else:
                self._decorate_core(cl, fw, act)
        except Exception as e:
            log("decorate err %r" % (e,))
            try:
                self._decorate_core(cl, cl.w + 2 * BW, self._is_active(cl))
            except Exception:
                pass

    def _frame_pixel(self, rgb24, alpha=None):
        """24 位色 → frame 像素值。ARGB frame 时 alpha 放最高字节。"""
        if self._argb_visual:
            a = self._frame_opacity() if alpha is None else alpha
            return ((int(a) & 0xFF) << 24) | (rgb24 & 0xFFFFFF)
        return rgb24

    def _decorate_cairo(self, cl, fw, act):
        # G.20：ARGB frame —— 背景带 alpha（不透明度设置只作用"框"），
        # 文字/按钮全不透明；顶部 accent 反光条已按用户令删除（G.20-7）。
        alpha = self._frame_opacity() / 255.0 if self._argb_visual else 1.0
        depth = 32 if self._argb_visual else self.screen.root_depth
        fh = cl.h + TB + BW  # frame 总高

        def _put(surf, x, y):
            surf.flush()
            raw = surf.get_data()
            data = raw if isinstance(raw, bytes) else bytes(raw)
            cl.frame.put_image(
                cl.gc,
                x,
                y,
                surf.get_width(),
                surf.get_height(),
                X.ZPixmap,
                depth,
                0,
                data,
            )

        # ---- 标题栏（含文字/三按钮/分隔线）----
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(fw), int(TB))
        cr = cairo.Context(surf)
        bg = COL_BG_ACT if act else COL_BG_INA
        cr.set_source_rgba(bg[0], bg[1], bg[2], alpha)
        cr.rectangle(0, 0, fw, TB)
        cr.fill()

        # 标题文字（不透明）
        title = cl.title or ""
        if title:
            try:
                cr.select_font_face(
                    "Noto Sans CJK SC",
                    cairo.FONT_SLANT_NORMAL,
                    cairo.FONT_WEIGHT_NORMAL,
                )
                cr.set_font_size(12.5)
                cr.set_source_rgb(*(COL_FG_ACT if act else COL_FG_INA))
                btns = self._btn_positions(fw)
                right_limit = btns["min"] - BTN // 2 - 8
                max_w = max(20, right_limit - 12)
                ext = cr.text_extents(title)
                show = title
                while show and ext.width > max_w:
                    show = show[:-1]
                    ext = cr.text_extents(show + "…")
                te = cr.text_extents(show)
                ty = TB / 2 + (te.height / 2) - te.height + 3
                cr.move_to(12, ty)
                cr.show_text(show)
            except Exception as e:
                log("title err %r" % (e,))

        # 三按钮（不透明）
        btns = self._btn_positions(fw)
        for kind in ("close", "max", "min"):
            self._draw_button(cr, kind, btns[kind], TB / 2, 6, act)
        if cl.maximized:
            # 最大化时 □ 显示为"还原"样式（双框）
            try:
                cx = btns["max"]
                cr.set_line_width(1.4)
                cr.set_source_rgba(*(COL_FG_ACT if act else COL_FG_INA))
                cr.rectangle(cx - 6, TB / 2 - 6, 9, 9)
                cr.stroke()
                cr.rectangle(cx - 3, TB / 2 - 3, 9, 9)
                cr.stroke()
            except Exception:
                pass

        cr.set_source_rgba(
            COL_BORDER[0], COL_BORDER[1], COL_BORDER[2], min(alpha + 0.2, 1.0)
        )
        cr.set_line_width(1.0)
        cr.move_to(0, TB - 0.5)
        cr.line_to(fw, TB - 0.5)
        cr.stroke()
        _put(surf, 0, 0)

        # ---- ARGB frame：边框条（左/右/底）半透明填充 ----
        if self._argb_visual:
            fh_i = int(fh)
            for bx, by, bw_, bh_ in (
                (0, TB, BW, fh_i - TB),  # 左
                (int(fw) - BW, TB, BW, fh_i - TB),  # 右
                (0, fh_i - BW, int(fw), BW),
            ):  # 底
                if bw_ <= 0 or bh_ <= 0:
                    continue
                s2 = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(bw_), int(bh_))
                c2 = cairo.Context(s2)
                c2.set_source_rgba(bg[0], bg[1], bg[2], alpha)
                c2.rectangle(0, 0, bw_, bh_)
                c2.fill()
                _put(s2, bx, by)
        self.d.flush()

    def _decorate_core(self, cl, fw, act):
        """回退路径：纯 X 线段（无文字）。G.20：像素值经 _frame_pixel
        （ARGB frame 时带 alpha；此前直接传颜色元组是存量 bug）。"""
        gc = cl.gc
        if not gc:
            return
        bg24 = 0x2A2A2C if act else 0x1C1C1E
        fg24 = 0xF5F5F7 if act else 0x8E8E93
        gc.change(foreground=self._frame_pixel(bg24))
        cl.frame.fill_rectangle(gc, 0, 0, fw, TB)
        gc.change(line_width=1)
        gc.change(foreground=self._frame_pixel(0x3A3A3C))
        cl.frame.line(gc, 0, TB - 1, fw, TB - 1)
        gc.change(line_width=2)
        gc.change(foreground=self._frame_pixel(fg24))
        b = self._btn_positions(fw)
        cy = TB // 2
        r = 6
        cl.frame.line(gc, b["close"] - r, cy - r, b["close"] + r, cy + r)
        cl.frame.line(gc, b["close"] + r, cy - r, b["close"] - r, cy + r)
        cl.frame.rectangle(gc, b["max"] - r, cy - r, 2 * r, 2 * r)
        cl.frame.line(gc, b["min"] - r, cy, b["min"] + r, cy)
        self.d.flush()

    def _btn_at(self, cl, ex, ey):
        """frame 坐标 → 'close'/'max'/'min'/None"""
        if ey >= TB:
            return None
        fw = cl.w + 2 * BW
        b = self._btn_positions(fw)
        for kind, cx in b.items():
            if abs(ex - cx) <= BTN // 2:
                return kind
        return None

    def _edge_at(self, cl, ex, ey):
        """frame 坐标 → 缩放方向（'e'/'w'/'s'/'se'/'sw' 等），None 表示不缩放。
        上边缘不参与缩放——上边缘即标题栏，用于拖动移动。"""
        fw, fh = cl.w + 2 * BW, cl.h + TB + BW
        if ey < TB:  # 标题栏区域
            return None
        d = ""
        if ex <= BW:
            d += "w"
        elif ex >= fw - BW:
            d += "e"
        if ey >= fh - BW:
            d += "s"
        return d or None

    # ---------- 交互 ----------
    def _dispatch(self, ev):
        t = ev.type
        if t == X.MapRequest:
            self._map(ev)
        elif t == X.UnmapNotify:
            self._unmap(ev)
        elif t == X.DestroyNotify:
            self._destroy(ev)
        elif t == X.ConfigureRequest:
            self._configure(ev)
        elif t == X.ButtonPress:
            self._button(ev)
        elif t == X.MotionNotify:
            self._motion(ev)
        elif t == X.ButtonRelease:
            self._release(ev)
        elif t == X.KeyPress:
            self._key(ev)
        elif t == X.Expose:
            cl = self.frames.get(ev.window.id)
            if cl and ev.count == 0:
                self._decorate(cl)
        elif t == X.PropertyNotify:
            cl = self.clients.get(ev.window.id)
            if cl and ev.atom in (self.atoms.get(_NET_WM_NAME), Xatom.WM_NAME):
                old = cl.title
                if cl.read_title() != old:
                    self._decorate(cl)

    def _raise(self, cl):
        """聚焦并提升窗口，同时刷新新旧窗口的标题栏配色"""
        try:
            old = self.clients.get(self.focus)
            tgt = cl.frame or cl.win
            tgt.configure(stack_mode=X.Above)
            self.focus = cl.win.id
            cl.win.set_input_focus(X.RevertToParent, X.CurrentTime)
            self._sync_active()
            self._decorate(cl)
            if old is not None and old is not cl and old.decorated:
                self._decorate(old)
            # G.21：任何窗口提升后，灵动岛/Dock 保持在最上层
            self._panels_to_top()
        except Exception as e:
            log("raise err %r" % (e,))

    def _panels_to_top(self):
        """G.21 用户令："无论什么情况，都必须能看到灵动岛"。
        每次窗口提升后把岛/Dock 重新压到栈顶（OR 窗口会被后来者覆盖）。"""
        try:
            if not self._panel_ids:
                self._find_panels()
            for pid in self._panel_ids:
                w = self.d.create_resource_object("window", pid)
                w.configure(stack_mode=X.Above)
            self.d.flush()
        except Exception:
            pass

    def _find_panels(self):
        """按窗口名收集岛/Dock 的 XID（缓存；找不到时重扫）"""
        self._panel_ids = []
        try:
            for c in self.root.query_tree().children:
                try:
                    name = c.get_wm_name() or ""
                except Exception:
                    continue
                if name in ("ui_island.py", "ui_dock.py"):
                    self._panel_ids.append(c.id)
        except Exception:
            pass

    def _button(self, ev):
        wid = ev.window.id
        cl = self.clients.get(wid) or self.frames.get(wid)
        if not cl or not cl.is_managed():
            self._root_button(ev)
            return
        self._raise(cl)
        if ev.window.id in self.frames:
            # frame 点击：标题栏按钮 / 拖动 / 边缘缩放
            ex, ey = ev.event_x, ev.event_y
            if ev.detail == 1 and ey < TB:
                act = self._btn_at(cl, ex, ey)
                if act:
                    self._btn_action(cl, act)
                    return
                now = time.time()
                if now - self._last_title_click < 0.35:
                    self._last_title_click = 0
                    self._maximize(cl)
                    return
                self._last_title_click = now
                self.dragging = (cl, "move", ev.root_x, ev.root_y, cl.x, cl.y, None)
                return
            # G.22：窗口菜单 —— 右键标题栏（无 Alt 修饰；Alt+右键仍保留缩放语义）
            if ev.detail == 3 and ey < TB and not (ev.state & self.alt_mask):
                self._window_menu(cl, int(ev.root_x), int(ev.root_y))
                return
            if ev.detail == 1:
                edge = self._edge_at(cl, ex, ey)
                if edge:
                    self.dragging = (
                        cl,
                        "resize",
                        ev.root_x,
                        ev.root_y,
                        cl.x,
                        cl.y,
                        edge,
                    )
            elif ev.detail == 3 and ev.state & self.alt_mask:
                self.dragging = (cl, "resize", ev.root_x, ev.root_y, cl.x, cl.y, "se")
            return
        if ev.detail == 1 and (ev.state & self.alt_mask):
            self.dragging = (cl, "move", ev.root_x, ev.root_y, cl.x, cl.y, None)
        elif ev.detail == 3 and (ev.state & self.alt_mask):
            self.dragging = (cl, "resize", ev.root_x, ev.root_y, cl.x, cl.y, "se")

    def _root_button(self, ev):
        """点击落在根窗口（桌面空白处）：
        左键双击 = 桌面图标显隐；右键 = 桌面系统菜单。
        注意：窗口外框的 5px 抓边属于 frame 自身（frame 比 client 四周大 BW），
        落在那里的事件由 _button 的 frame 分支处理，不会走到这里。"""
        if ev.window != self.root:
            return
        if ev.detail == 1:
            now = time.time()
            if now - self._last_root_click < 0.35:
                self._last_root_click = 0
                self._send_icons({"cmd": "toggle"})
            else:
                self._last_root_click = now
        elif ev.detail == 3:
            self._send_sock(
                "/tmp/qd_ctxmenu.sock", {"cmd": "menu", "x": ev.root_x, "y": ev.root_y}
            )

    def _send_icons(self, payload):
        self._send_sock("/tmp/qd_icons.sock", payload)

    def _window_menu(self, cl, x, y):
        """G.22：窗口菜单（最小化 / 最大化还原 / 关闭）。
        右键标题栏与 Alt+Space 共用，UI 由常驻的 ui_contextmenu 服务弹出，
        动作经 qwm socket 通道回发执行。带 max 标志供菜单显示"还原"。"""
        self._send_sock(
            "/tmp/qd_ctxmenu.sock",
            {
                "cmd": "window_menu",
                "wid": cl.win.id,
                "x": int(x),
                "y": int(y),
                "max": 1 if cl.maximized else 0,
            },
        )

    @staticmethod
    def _send_sock(path, payload):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(path)
            s.sendall(json.dumps(payload).encode())
            s.close()
        except Exception:
            pass

    def _motion(self, ev):
        if not self.dragging:
            # 悬停光标（仅 frame 上）
            return
        cl, mode, sx, sy, ox, oy, edge = self.dragging
        dx, dy = ev.root_x - sx, ev.root_y - sy
        if mode == "move":
            cl.x, cl.y = ox + dx, oy + dy
            self._clamp(cl)
        else:
            edge = edge or "se"
            x, y, w, h = ox, oy, cl.w, cl.h
            if "e" in edge:
                w = max(MIN_W, cl.w + dx)
            if "s" in edge:
                h = max(MIN_H, cl.h + dy)
            cl.w, cl.h = w, h
            if "w" in edge:
                nw = max(MIN_W, w - dx)
                cl.x = x + (w - nw)
                cl.w = nw
            if "n" in edge:
                nh = max(MIN_H, h - dy)
                cl.y = y + (h - nh)
                cl.h = nh
            cl.maximized = False
            cl.snapped = None
        self._apply(cl)

    def _clamp(self, cl):
        fw, fh = cl.w + 2 * BW, cl.h + TB + BW
        cl.x = max(-BW, min(cl.x, self.sw - fw + BW))
        cl.y = max(0, min(cl.y, self.sh - fh))

    def _release(self, ev):
        if self.dragging:
            cl = self.dragging[0]
            self._flush_pending(force=True)
            # 拖动到屏幕边缘 → 吸附（Windows/berry 行为）
            if self.dragging[1] == "move" and not cl.maximized:
                # G.25 回归修复：放宽边缘吸附阈值（2-3px → 10px），防正常拖动误触发
                if ev.root_x <= 10:
                    self._snap(cl, "left")
                elif ev.root_x >= self.sw - 10:
                    self._snap(cl, "right")
                elif ev.root_y <= 10:
                    self._maximize(cl)
        self.dragging = None

    def _key(self, ev):
        act = None
        for name, sh, a in self.KEYMAP:
            try:
                kc = self.d.keysym_to_keycode(XK.string_to_keysym(name))
            except Exception:
                continue
            if kc == ev.detail:
                shift_now = bool(ev.state & self.shift_mask)
                if shift_now == bool(sh):
                    act = a
                    break
        if not act:
            return
        cl = self._focused()
        if act == "cycle":
            self._cycle()
        elif act == "close" and cl:
            self._close(cl.win.id)
        elif act == "minimize" and cl:
            self._toggle_hidden(cl.win.id)
        elif act == "maximize" and cl:
            self._maximize(cl)
        elif act == "menu" and cl:
            # G.22：Alt+Space 窗口菜单，弹在窗口左上角（Windows 惯例）
            self._window_menu(cl, cl.x + 12, cl.y + TB + 2)
        elif act == "fullscreen" and cl:
            self._fullscreen(cl)
        elif act == "snap_left" and cl:
            self._snap(cl, "left")
        elif act == "snap_right" and cl:
            self._snap(cl, "right")
        elif act == "center" and cl:
            self._center(cl)
        elif act.startswith("focus_") and cl:
            self._cardinal_focus(act.split("_")[1], cl)
        elif act.startswith("ws_"):
            self._switch_ws(int(act[-1]))
        elif act.startswith("send_") and cl:
            cl.ws = int(act[-1]) % N_WORKSPACE
            self._apply_view()
            self._update_client_list()

    # ---------- 动作 ----------
    def _maximize(self, cl):
        if not cl.maximized:
            cl.pre_geom = (cl.x, cl.y, cl.w, cl.h)
            cl.x, cl.y = 0, WORK_Y0
            cl.w = self.sw
            cl.h = WORK_Y1 - WORK_Y0 - TB - BW
            cl.maximized = True
            cl.snapped = None
        else:
            if cl.pre_geom:
                cl.x, cl.y, cl.w, cl.h = cl.pre_geom
            cl.maximized = False
        self._flush_pending(force=True)
        self._place(cl)
        self._decorate(cl)
        # G.22：最大化后同步 EWMH 状态（规范 14.5.2）
        self._sync_state(cl)
        self.d.flush()

    def _snap(self, cl, side):
        if not cl.maximized:
            cl.pre_geom = cl.pre_geom or (cl.x, cl.y, cl.w, cl.h)
        half = self.sw // 2
        cl.x = 0 if side == "left" else self.sw - half
        cl.y = WORK_Y0
        cl.w = half
        cl.h = WORK_Y1 - WORK_Y0 - TB - BW
        cl.snapped = side
        cl.maximized = False
        self._flush_pending(force=True)
        self._place(cl)
        self._decorate(cl)
        # G.22：吸附会清除最大化，同步 EWMH 状态保持一致
        self._sync_state(cl)
        self.d.flush()

    def _fullscreen(self, cl):
        if not cl.fullscreen:
            cl.pre_geom = (cl.x, cl.y, cl.w, cl.h)
            cl.fullscreen = True
            # G.15 刻意差异（与 berry/Openbox 不同）：全屏**保留标题栏**，
            # 否则用户进了全屏就没有鼠标可点的退出/还原/最小化按钮
            # （用户实测反馈："小窗全屏还有退出的 3 个按键又不见了"）。
            # G.21 再补：全屏从 WORK_Y0 起——灵动岛永远可见（用户令）。
            cl.decorated = True
            cl.x, cl.y = 0, WORK_Y0
            cl.w = max(MIN_W, self.sw - 2 * BW)
            cl.h = max(MIN_H, self.sh - WORK_Y0 - TB - BW)
            try:
                self.atoms.get(_NET_WM_STATE_FULLSCREEN) and cl.win.change_property(
                    self.atoms[_NET_WM_STATE],
                    Xatom.ATOM,
                    32,
                    [self.atoms[_NET_WM_STATE_FULLSCREEN]],
                )
            except Exception:
                pass
        else:
            if cl.pre_geom:
                cl.x, cl.y, cl.w, cl.h = cl.pre_geom
            cl.fullscreen = False
            cl.decorated = True
            try:
                cl.win.change_property(self.atoms[_NET_WM_STATE], Xatom.ATOM, 32, [])
            except Exception:
                pass
        self._flush_pending(force=True)
        self._place(cl)
        self._decorate(cl)
        self.d.flush()

    def _center(self, cl):
        cl.x = (self.sw - cl.w - 2 * BW) // 2
        cl.y = WORK_Y0 + max(0, (WORK_Y1 - WORK_Y0 - cl.h - TB - BW) // 2)
        cl.maximized = False
        cl.snapped = None
        self._flush_pending(force=True)
        self._place(cl)
        self._decorate(cl)
        self.d.flush()

    def _cardinal_focus(self, direction, cur):
        cx, cy = cur.x + cur.w / 2, cur.y + cur.h / 2
        best, best_d = None, None
        for other in self.clients.values():
            if other is cur or not other.is_managed() or not self._visible(other):
                continue
            ox, oy = other.x + other.w / 2, other.y + other.h / 2
            dx, dy = ox - cx, oy - cy
            ok = (
                (direction == "left" and dx < -10)
                or (direction == "right" and dx > 10)
                or (direction == "up" and dy < -10)
                or (direction == "down" and dy > 10)
            )
            if not ok:
                continue
            d = abs(dx) + abs(dy)
            if best_d is None or d < best_d:
                best_d, best = d, other
        if best:
            self._raise(best)

    def _visible(self, cl):
        """当前工作区且未被最小化 → 应可见"""
        return (not cl.hidden) and cl.ws == self.ws

    def _apply_view(self):
        """按工作区 / 隐藏状态重算窗口可见性"""
        for cl in list(self.clients.values()):
            if not cl.is_managed() or not cl.frame:
                continue
            try:
                want = self._visible(cl)
                cur = cl.frame.get_attributes().map_state == X.IsViewable
                if want and not cur:
                    cl.frame.map()
                    cl.win.map()
                    cl.mapped = True
                elif not want and cur:
                    cl.frame.unmap()
                    cl.mapped = False
            except Exception as e:
                log("view err %r" % (e,))
        try:
            self.d.flush()
        except Exception:
            pass

    def _switch_ws(self, i):
        i = int(i) % N_WORKSPACE
        if i == self.ws:
            return
        self.ws = i
        try:
            self.root.change_property(
                self.atoms[_NET_CURRENT_DESKTOP], Xatom.CARDINAL, 32, [i]
            )
        except Exception:
            pass
        self._apply_view()
        # 焦点交给该工作区最上面窗口
        nxt = None
        for wid in reversed(self.client_list):
            cl = self.clients.get(wid)
            if cl and cl.is_managed() and self._visible(cl):
                nxt = cl
                break
        if nxt:
            self._raise(nxt)
        elif self.focus:
            self.focus = None
            self._sync_active()
        self._update_client_list()
        self._send_icons({"cmd": "toast", "text": "工作区 %d" % (i + 1)})

    def _btn_action(self, cl, act):
        log("btn %s" % act)
        if act == "close":
            self._close(cl.win.id)
        elif act == "max":
            self._maximize(cl)
        elif act == "min":
            self._toggle_hidden(cl.win.id)

    def _unmap(self, ev):
        wid = ev.window.id
        cl = self.clients.get(wid)
        if cl and cl.is_managed():
            # 父 frame 已不可见（工作区切换/最小化）→ 不算客户端主动 unmap
            try:
                if cl.frame and cl.frame.get_attributes().map_state != X.IsViewable:
                    return
            except Exception:
                pass
            cl.mapped = False
            if self.focus == wid:
                self.focus = None
                self._sync_active()

    def _destroy(self, ev):
        wid = ev.window.id
        if wid in self.clients:
            cl = self.clients.pop(wid)
            if cl.hidden:
                self._write_minimized()  # G.21：球清单即时更新
            if cl.frame:
                self.frames.pop(cl.frame.id, None)
                try:
                    cl.frame.destroy()
                except Exception:
                    pass
                cl.frame = None
            if wid in self.client_list:
                self.client_list.remove(wid)
            self._pending.pop(wid, None)
            self._update_client_list()
            if self.focus == wid:
                self.focus = None
                # 焦点交给剩余最上面窗口
                for w2 in reversed(self.client_list):
                    c2 = self.clients.get(w2)
                    if c2 and c2.is_managed() and self._visible(c2):
                        self._raise(c2)
                        break
                else:
                    self._sync_active()
            log("destroy %s" % hex(wid))

    def _configure(self, ev):
        wid = ev.window.id
        cl = self.clients.get(wid)
        if cl and cl.is_managed() and cl.frame:
            try:
                w = ev.width if ev.value_mask & X.CWWidth else cl.w
                h = ev.height if ev.value_mask & X.CWHeight else cl.h
                if ev.value_mask & X.CWX:
                    pass
                if (w, h) == (cl.w, cl.h):
                    return
                cl.w = max(MIN_W, w)
                cl.h = max(MIN_H, h)
                self._apply(cl)
            except Exception:
                pass
            return
        try:
            ev.window.configure(
                x=ev.x,
                y=ev.y,
                width=ev.width,
                height=ev.height,
                border_width=ev.border_width,
            )
        except Exception:
            pass

    def _cycle(self):
        vis = [
            w
            for w in self.client_list
            if (c := self.clients.get(w)) and c.is_managed() and self._visible(c)
        ]
        if not vis:
            return
        idx = vis.index(self.focus) if self.focus in vis else -1
        nxt = self.clients[vis[(idx + 1) % len(vis)]]
        self._raise(nxt)

    def _toggle_hidden(self, wid):
        cl = self.clients.get(wid)
        if not cl:
            return
        cl.hidden = not cl.hidden
        # G.21：记录最小化时刻（灵动岛圆球按此排序编号）
        cl.minimized_at = time.time() if cl.hidden else 0.0
        self._write_minimized()
        self._apply_view()
        if not cl.hidden:
            self._raise(cl)
        elif self.focus == wid:
            self.focus = None
            self._sync_active()
        self._sync_state(cl)

    MIN_FILE = "/tmp/qd_minimized.json"

    def _write_minimized(self):
        """G.21：把隐藏窗口清单写给灵动岛（id/标题/时间戳，按时间排序）。
        窗口关闭时也会经 _sync_state → 本函数刷新（见 _close 兜底调用）。"""
        try:
            items = sorted(
                (cl for cl in self.clients.values() if cl.hidden and cl.minimized_at),
                key=lambda c: c.minimized_at,
            )
            data = json.dumps(
                [
                    {"id": cl.win.id, "title": cl.title, "ts": cl.minimized_at}
                    for cl in items
                ],
                ensure_ascii=False,
            )
            tmp = self.MIN_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.write(data)
            os.replace(tmp, self.MIN_FILE)
        except Exception as e:
            log("min-file err %r" % (e,))

    def _close(self, wid):
        cl = self.clients.get(wid)
        if not cl:
            return
        try:
            wm_protos = self.d.intern_atom("WM_PROTOCOLS")
            del_win = self.d.intern_atom("WM_DELETE_WINDOW")
            proto = cl.win.get_full_property(wm_protos, X.AnyPropertyType)
            if proto and del_win in proto.value:
                ev = xe.ClientMessageEvent(
                    window=cl.win,
                    client_type=wm_protos,
                    data=(32, [del_win, X.CurrentTime, 0, 0, 0]),
                )
                self.root.send_event(
                    ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask
                )
                self.d.flush()
                return
        except Exception:
            pass
        try:
            cl.win.kill_client()
        except Exception:
            try:
                cl.win.destroy()
            except Exception:
                pass

    # ---------- EWMH ----------
    def _update_client_list(self):
        try:
            self.root.change_property(
                self.atoms[_NET_CLIENT_LIST], Xatom.WINDOW, 32, self.client_list
            )
            order = [
                w
                for w in self.client_list
                if (c := self.clients.get(w)) and self._visible(c)
            ]
            self.root.change_property(
                self.atoms[_NET_CLIENT_LIST_STACKING], Xatom.WINDOW, 32, order
            )
        except Exception:
            pass

    def _sync_active(self):
        try:
            val = [self.focus] if self.focus else [0]
            self.root.change_property(
                self.atoms[_NET_ACTIVE_WINDOW], Xatom.WINDOW, 32, val
            )
        except Exception:
            pass

    def _sync_state(self, cl):
        try:
            atoms = [self.atoms[_NET_WM_STATE_HIDDEN]] if cl.hidden else []
            if cl.fullscreen:
                atoms.append(self.atoms[_NET_WM_STATE_FULLSCREEN])
            elif cl.maximized:
                # G.22：规范 14.5.2 —— 最大化时写 VERT+HORZ（Openbox 语义）
                atoms.append(self.atoms[_NET_WM_STATE_MAXIMIZED_VERT])
                atoms.append(self.atoms[_NET_WM_STATE_MAXIMIZED_HORZ])
            cl.win.change_property(self.atoms[_NET_WM_STATE], Xatom.ATOM, 32, atoms)
        except Exception:
            pass

    # ---------- 退出清理 ----------
    def cleanup(self):
        """把 client 还给 root 再销毁 frame（避免连带销毁应用窗口）"""
        log("cleanup: 归还 %d 个窗口给 root" % len(self.clients))
        for cl in list(self.clients.values()):
            try:
                if cl.frame:
                    try:
                        cl.win.reparent(self.root, cl.x + BW, cl.y + TB)
                    except Exception:
                        pass
                    try:
                        cl.frame.destroy()
                    except Exception:
                        pass
                    cl.frame = None
            except Exception:
                pass
        try:
            self.d.flush()
            self.d.sync()
        except Exception:
            pass
        try:
            if self._srv:
                self._srv.close()
            if os.path.exists(self._srv_path):
                os.unlink(self._srv_path)
        except Exception:
            pass

    # ---------- 主循环 ----------
    def run(self):
        log("奇点OS 自研窗口管理器启动 (v2/G.15)")
        fd = self.d.fileno()
        srv_fd = self._srv.fileno() if self._srv else -1
        watch = [fd] + ([srv_fd] if srv_fd >= 0 else [])
        while True:
            try:
                try:
                    r, _, _ = select(watch, [], [], 0.05)
                except Exception:
                    r = []
                if self.d.pending_events():
                    for _ in range(64):
                        if not self.d.pending_events():
                            break
                        self._dispatch(self.d.next_event())
                elif fd in r:
                    self._dispatch(self.d.next_event())
                if srv_fd in r:
                    self._accept_cmds()
                self._drain_cmds()
                self._flush_pending()
                # G.16 自愈：每 3 秒兜底扫一次，把漏接的窗口收编进 frame
                now = time.time()
                if now - self._last_sweep > 3.0:
                    self._last_sweep = now
                    self._sweep_orphans()
                self.d.flush()
                # G.17 防忙等兜底：select 对常读 fd 会立即返回导致空转，
                # 每轮固定让出 5ms，98% CPU 空转问题即可消除
                time.sleep(0.005)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log("loop err %r" % (e,))
                try:
                    traceback.print_exc()
                except Exception:
                    pass


def main():
    wm = QWM(sys.argv[1] if len(sys.argv) > 1 else ":0")
    wm._adopt_existing()

    # G.15：收到 SIGTERM/SIGINT 时先把 client 还给 root 再退出。
    # 事故：直接 kill 掉 qwm，X 会销毁它建的 frame，而销毁父窗口会连带
    # 销毁其中的 client 窗口 → 所有应用一起消失，且 picom 内部引用失效
    # 后不再渲染任何东西（表现为"整个桌面上的窗口都看不见了"）。
    def _bye(_sig, _frm):
        try:
            wm.cleanup()
        except Exception:
            pass
        os._exit(0)

    try:
        import signal

        signal.signal(signal.SIGTERM, _bye)
        signal.signal(signal.SIGINT, _bye)
    except Exception:
        pass
    wm.run()


if __name__ == "__main__":
    main()
