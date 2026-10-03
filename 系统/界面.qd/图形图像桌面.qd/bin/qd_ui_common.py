#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# qd_ui_common.py —— 桌面组件共享库
# 设计 token 唯一来源：运行回收/ui_design/pages/index.html（TreeSolo 原型）
# 任何颜色/尺寸必须能在原型里指出出处，禁止凭空值
# ============================================================
import os
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio

# ---------- 颜色（原型 .dark 变量解析值） ----------
BG = "#000000"  # --background-900
CARD = "#1c1c1e"  # --card
POPOVER = "#3a3a3c"  # --popover
BORDER = "#3a3a3c"  # --border
FG = "#f5f5f7"  # --text-50
MUTED_FG = "#8e8e93"  # --text-400
PRIMARY = "#2e8dff"  # --brand-400
SUCCESS = "#30d158"  # --state-success-dark
DESTRUCT = "#ff453a"  # --state-error-dark
CHART4 = "#5e5ce6"  # --chart-4（壁纸第二光晕）

FONT = "WenQuanYi Zen Hei"

# ---------- 尺寸（原型 class 值） ----------
ISLAND_TOP = 12  # top-3
ISLAND_MINW = 280  # min-width 280px
ISLAND_H = 40  # height 40px
DOCK_BOTTOM = 16  # bottom-4
DOCK_H = 64  # h-16
DOCK_ITEM = 44  # w-11 h-11
DOCK_ICON = 20  # w-5 h-5
DRAWER_BOTTOM = 96  # bottom-24
DRAWER_W = 640  # w-[640px]
TOAST_TOP = 56  # top-14

UI_DIR = "/奇点OS/运行/ui_design"  # TreeSolo 原型资产（含 backdrop.png）

ICON_DIRS = [
    "/奇点OS/运行/ui_design/assets/icons/lucide",
    "/奇点OS/运行/ui_design/assets/icons/dl_builtin_apple",
]

import socket, json, threading

SOCK_PATH = "/tmp/qd_drawer.sock"  # 兼容保留（抽屉已移除）
ICONS_SOCK = "/tmp/qd_icons.sock"  # 桌面图标层（toggle 显隐 / toast / reload）
DOCK_SOCK = "/tmp/qd_dock.sock"  # 快捷启动栏（reload 重建应用按钮）

# ---------- 配置层（G.14）：所有桌面设置与已装应用都落在这里 ----------
CONF_DIR = "/home/qduser/.config/qidos"
CONF_FILE = CONF_DIR + "/settings.json"
APPS_FILE = CONF_DIR + "/apps.json"  # 用户安装的应用注册表

DEFAULT_CONF = {
    "wallpaper": UI_DIR + "/wallpaper.png",
    "opacity": 204,  # 标题栏不透明度 0~255（qwm 建 frame 时读取并换算
    # 成 EWMH 32 位标度；G.19 修复：此前直写 0-255 值
    # ≈ 全透明 → 合成器下装帧窗口全黑）
    "icon_size": 40,  # 桌面图标尺寸 px（32 小 / 40 中 / 48 大 / 64 超大）
    "icon_gap": 10,  # 图标间距 px
    "icon_label": True,  # 是否显示图标文字标签
    "label_size": 11,  # 标签字号 px
    "icon_x": 24,  # 图标列起始 x
    "icon_y0": 76,  # 图标列起始 y（顶让灵动岛）
    "per_col": 5,  # 每列几个图标
    # G.25 桌面图标拖拽：icon_pos = {图标名: [x, y]}（点击区/绘制同源的像素坐标）
    "icon_pos": {},
    # G.25 Dock 自定义：dock_apps = [{"id":"...","icon":"...","name":"...","cmd":"..."}, ...]
    # 用户未配置时为 None，回退到默认 DOCK_APPS + 已装最近几个
    "dock_apps": None,
}

# 系统内置应用（不进注册表，始终存在）
# 注意：用户令"应用商店不做桌面图标"——安装器只在 Dock/设置出现，不进桌面
BUILTIN_APPS = [
    {
        "id": "browser",
        "icon": "globe",
        "name": "浏览器",
        "cmd": "python3 /奇点OS/运行/ui_browser.py",
    },
    {
        "id": "files",
        "icon": "folder-open",
        "name": "文件",
        "cmd": "python3 /奇点OS/运行/ui_filebrowser.py",
    },
    {
        "id": "album",
        "icon": "image",
        "name": "相册",
        "cmd": "python3 /奇点OS/运行/ui_album.py --show",
    },
    {
        "id": "trash",
        "icon": "trash-2",
        "name": "回收站",
        "cmd": "python3 /奇点OS/运行/ui_trash.py",
    },
    # G.24 用户令：系统级 AI 不再做桌面图标、也不进快捷窗，
    # 入口统一收进灵动岛（ui_island.py V5 AI 面板）。
]

# Dock 固定项（含安装器入口；用户新装的应用追加在后面）
DOCK_APPS = [
    {
        "id": "store",
        "icon": "store",
        "name": "应用中心",
        "cmd": "python3 /奇点OS/运行/ui_installer.py",
    },
    {
        "id": "terminal",
        "icon": "terminal",
        "name": "终端",
        "cmd": 'xterm -T 奇点OS终端 -bg "#16161e" -fg "#c0caf5"',
    },
    {
        "id": "settings",
        "icon": "settings",
        "name": "设置",
        "cmd": "python3 /奇点OS/运行/ui_settings.py",
    },
]


def load_conf():
    """读设置；缺项用默认值补齐，文件损坏则整体回落"""
    cfg = dict(DEFAULT_CONF)
    try:
        if os.path.exists(CONF_FILE):
            cfg.update(json.load(open(CONF_FILE, encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_conf(cfg, keys=None):
    """保存配置。

    keys 为 None：整份写盘（兼容旧调用）；
    keys 非空：只更新指定键——先读磁盘最新配置再合并覆盖，
    其余键保留磁盘现值。这是 G.26 配置竞争修复的关键：
    各组件（设置/图标/相册）持有启动时加载的旧快照，
    若整份写回会把其他组件新写入的键（如拖拽产生的 icon_pos）
    冲掉。增量写 + 原子替换（tmp + os.replace），
    多进程并发保存也不会写坏 JSON。
    """
    os.makedirs(CONF_DIR, exist_ok=True)
    if keys is None:
        data = cfg
    else:
        data = load_conf()
        for k in set(k for k in keys if k in cfg):
            data[k] = cfg[k]
    tmp = CONF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONF_FILE)


def load_installed():
    """读用户已安装应用注册表"""
    try:
        if os.path.exists(APPS_FILE):
            return json.load(open(APPS_FILE))
    except Exception:
        pass
    return []


def save_installed(apps):
    os.makedirs(CONF_DIR, exist_ok=True)
    json.dump(apps, open(APPS_FILE, "w"), ensure_ascii=False, indent=1)


def add_installed(rec):
    """登记一个已装应用（同 id 覆盖）"""
    apps = [a for a in load_installed() if a.get("id") != rec.get("id")]
    apps.append(rec)
    save_installed(apps)


def all_apps():
    """桌面图标与 Dock 的统一应用源：内置 + 用户安装"""
    return list(BUILTIN_APPS) + list(load_installed())


def icon_cell(cfg):
    """图标格子尺寸（点击区与绘制必须同源，否则点了会偏）"""
    s = int(cfg.get("icon_size", 40))
    w = s + 40
    h = s + 48 if cfg.get("icon_label", True) else s + 24
    return w, h


def icon_positions(cfg, icons):
    """图标网格坐标计算（G.25 后桌面图标布局的唯一来源）

    icons: [(name, cmd), ...] 或 [{'name':..., 'cmd':...}]
    返回 [(name, x, y), ...]，与 icons 顺序一一对应。
    规则：
      1. 已配置 cfg['icon_pos'][name] = [x, y] 的用自定义位置；
      2. 未配置的按默认网格排队，跳过已被自定义占用的格子；
      3. 点击区、壁纸绘制、拖拽落点全部走这里，保证半点不偏。
    """
    iw, ih = icon_cell(cfg)
    ix = int(cfg.get("icon_x", 24))
    iy0 = int(cfg.get("icon_y0", 76))
    gap = int(cfg.get("icon_gap", 10))
    per = max(1, int(cfg.get("per_col", 5)))
    pos = cfg.get("icon_pos") or {}

    names = [a.get("name") if isinstance(a, dict) else a[0] for a in icons]
    grid, taken = {}, set()
    # 1) 自定义位置（合法数字对才采用）
    for name in names:
        p = pos.get(name)
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                x, y = int(p[0]), int(p[1])
            except (TypeError, ValueError):
                continue
            grid[name] = (x, y)
            taken.add((x, y))
    # 2) 未配置的按默认网格排队，跳过已被占用的格子
    col = row = 0
    for name in names:
        if name in grid:
            continue
        while True:
            x = ix + col * (iw + gap)
            y = iy0 + row * (ih + gap)
            if (x, y) not in taken:
                grid[name] = (x, y)
                taken.add((x, y))
                break
            row += 1
            if row >= per:
                row = 0
                col += 1
        row += 1
        if row >= per:
            row = 0
            col += 1
    return [(name, grid[name][0], grid[name][1]) for name in names]


def pixbuf_icon(name, size, color):
    """按名字/尺寸/颜色加载 lucide 或本地 SVG（currentColor → color）"""
    data = None
    for d in ICON_DIRS:
        p = os.path.join(d, name + ".svg")
        if os.path.exists(p):
            data = open(p, "rb").read()
            break
    if data is None:
        return None
    data = data.replace(b"currentColor", color.encode())
    stream = Gio.MemoryInputStream.new_from_data(data)
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.set_size(size, size)
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()
    except Exception:
        return None


def icon_image(name, size, color):
    pb = pixbuf_icon(name, size, color)
    if pb is None:
        img = Gtk.Image()
        img.set_size_request(size, size)
        return img
    return Gtk.Image.new_from_pixbuf(pb)


def label(text, px, color=FG, weight="normal"):
    lbl = Gtk.Label()
    lbl.set_markup(
        '<span font_family="%s" font_size="%d" weight="%s" color="%s">%s</span>'
        % (FONT, px * 1000, weight, color, text)
    )
    return lbl


def setup_css(css_bytes):
    provider = Gtk.CssProvider()
    provider.load_from_data(css_bytes)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def _dot(color, d):
    """实心圆点（原型 w-2 h-2 rounded-full）"""
    da = Gtk.DrawingArea()
    da.set_size_request(d, d)

    def on_draw(_, cr):
        cr.set_source_rgba(*(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)), 1)
        cr.arc(d / 2, d / 2, d / 2, 0, 6.2832)
        cr.fill()
        return False

    da.connect("draw", on_draw)
    return da


def _vline(h=14):
    """竖分隔线（原型 h-3.5 w-px bg-border/60）"""
    da = Gtk.DrawingArea()
    da.set_size_request(1, h)

    def on_draw(_, cr):
        cr.set_source_rgba(58 / 255, 58 / 255, 60 / 255, 0.6)
        cr.rectangle(0, 0, 1, h)
        cr.fill()
        return False

    da.connect("draw", on_draw)
    return da


def send_drawer(cmd):
    """向抽屉进程发 JSON（unix socket），抽屉未运行则忽略"""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK_PATH)
        s.sendall(json.dumps(cmd).encode())
        s.close()
    except Exception:
        pass


def send_icons(cmd):
    """向桌面图标层发 JSON（unix socket）：toggle 显隐 / toast / reload"""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(ICONS_SOCK)
        s.sendall(json.dumps(cmd).encode())
        s.close()
    except Exception:
        pass


def send_dock(cmd):
    """向快捷启动栏发 JSON（unix socket）：reload 重建应用按钮"""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(DOCK_SOCK)
        s.sendall(json.dumps(cmd).encode())
        s.close()
    except Exception:
        pass


def dock_items(max_installed=4):
    """快捷启动栏的应用源：内置桌面应用 + 固定项 + 最近安装的若干应用"""
    items = list(BUILTIN_APPS) + list(DOCK_APPS)
    inst = load_installed()
    items += inst[-max_installed:]
    return items


# ============================================================
# G.16 单例 + 重复点击置前
# 事故：ui_filebrowser/ui_album/ui_editor 没有单例，点一次桌面图标
#      就 Popen 一个新进程（实测堆了 6 个），而新窗口又没有标题栏、
#      位置和旧的完全重合 → 用户看到的正是"提示说打开了，但没弹窗"。
# 做法：拿 flock；抢到的是主实例（把 PID 写进锁文件并挂 SIGUSR1）；
#      抢不到的是第二个实例 → 给主实例发 SIGUSR1 让它 present() 后置退出。
# ============================================================
_RAISE_HANDLERS = {}


def single_instance(tag):
    """返回 True 表示本进程是主实例；False 表示已有实例（已请求其置前）。

    典型用法（先判锁再建窗口，避免第二个实例闪一下窗口）：
        if not C.single_instance('filebrowser'):
            sys.exit(0)
        win = FileBrowser()
        C.set_raise_handler('filebrowser', win.present)
        Gtk.main()
    """
    import fcntl

    lock_path = "/tmp/qd_%s.lock" % tag
    try:
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 主实例：登记 PID，供后续实例唤醒
        try:
            fh.write("%d\n" % os.getpid())
            fh.flush()
        except Exception:
            pass
        # 持有 fh 引用，防止被 GC 释放导致锁失效
        _LOCKS[tag] = fh
        try:
            import signal as _sig
            from gi.repository import GLib as _GLib

            # 信号处理器里只排队，真正的置前交给主循环（GTK 线程安全）
            _sig.signal(
                _sig.SIGUSR1,
                lambda *_a: _GLib.idle_add(_safe_raise(_RAISE_HANDLERS.get(tag))),
            )
        except Exception:
            pass
        return True
    except Exception:
        pass
    # 已有实例：读 PID 并请求置前
    try:
        with open(lock_path) as f:
            pid = int(f.read().strip() or 0)
        if pid > 0:
            import signal as _sig

            os.kill(pid, _sig.SIGUSR1)
    except Exception:
        pass
    return False


def set_raise_handler(tag, fn):
    """主实例建好窗口后登记「被再次点击时如何置前」"""
    _RAISE_HANDLERS[tag] = fn


_LOCKS = {}


def _safe_raise(fn):
    def _f():
        try:
            fn()
        except Exception:
            pass
        return False

    return _f


def run_picker(mode="open", root="/奇点OS", default="", filters=""):
    """调用自研文件选择弹窗 ui_filepicker.py，阻塞等待返回路径（取消则空串）。
    mode: open/save/dir；root: 起始目录；default: save 模式默认文件名；
    filters: 扩展名逗号分隔（例 'txt,md,py'），空串不过滤。"""
    import subprocess

    cmd = ["python3", "/奇点OS/运行/ui_filepicker.py", "--mode", mode, "--root", root]
    if default:
        cmd += ["--default", default]
    if filters:
        cmd += ["--filter", filters]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return out.stdout.strip()
    except Exception:
        return ""
