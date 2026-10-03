#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_wallpaper.py —— 桌面图标壁纸合成（替代已丢失的宿主 PIL 管线）
# 由来：本机 Xorg fbdev 无合成器、ARGB 渲染成黑、SHAPE 不裁剪（G.9 实证），
#       所以桌面图标必须"烧进壁纸"；早期管线在宿主用 PIL 合成，脚本已丢失。
# 现在：VM 内自给——用 GTK/cairo/Pango 从 APPS 列表实时合成 icons_on.png，
#       改图标列表即自动生效，不再依赖宿主。
# 布局常量与 ui_icons.py 的点击区必须一致（ICON_X/Y0/W/H/PER_COL/GAP）
# ============================================================
import os
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, Pango, PangoCairo
import cairo
import qd_ui_common as C

WALLPAPER = C.UI_DIR + "/wallpaper.png"
BACKDROP = C.UI_DIR + "/backdrop.png"
# 合成图必须写到 qduser 可写目录：/奇点OS 属 root，组件以 qduser 运行会写失败
ICONS_DIR = "/home/qduser/.cache/qidos"
OUT_ON = ICONS_DIR + "/icons_on.png"
OUT_OFF = ICONS_DIR + "/icons_off.png"

ICON_SIZE = 48
LABEL_SIZE = 12
SHADOW_R = 34  # 图标圆角垫底色块（低对比，模拟选中/定位感）


def _pick_cjk_font():
    """挑一个能出中文的字体家族（VM 上装的是 Noto CJK）"""
    try:
        fbctx = Gtk.Widget.get_default_style_context()
        import subprocess

        out = subprocess.run(
            ["fc-list", ":lang=zh", "family"], capture_output=True, text=True
        ).stdout
        fams = set()
        for line in out.splitlines():
            for f in line.split(","):
                f = f.strip()
                if f:
                    fams.add(f)
        for pref in (
            "Noto Sans CJK SC",
            "Noto Sans CJK",
            "Source Han Sans SC",
            "WenQuanYi Zen Hei",
        ):
            if pref in fams:
                return pref
        if fams:
            return sorted(fams)[0]
    except Exception:
        pass
    return C.FONT


def _draw_label(cr, text, cx, y, font, size=LABEL_SIZE):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription("%s %d" % (font, size))
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    lw, _lh = layout.get_pixel_size()
    # 描边 + 填充，保证任何壁纸上都可读
    cr.save()
    cr.translate(cx - lw / 2, y)
    PangoCairo.layout_path(cr, layout)
    cr.set_source_rgba(0, 0, 0, 0.75)
    cr.set_line_width(2.5)
    cr.stroke()
    cr.restore()
    cr.save()
    cr.translate(cx - lw / 2, y)
    cr.set_source_rgba(0.96, 0.96, 0.98, 1)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def render(apps, layout, wallpaper=None, out_on=OUT_ON, out_off=OUT_OFF):
    """apps: [(icon, name)]；layout: dict(ICON_X,ICON_Y0,ICON_W,ICON_H,PER_COL,GAP)"""
    # pygobject 只接受 str 列表（传 [b''] 会 TypeError: Must be string, not bytes）
    try:
        if not Gtk.init_check([])[0]:
            return False
    except Exception:
        try:
            if not Gtk.init_check(["ui_wallpaper"])[0]:
                return False
        except Exception:
            return False
    src = wallpaper or WALLPAPER
    if not os.path.exists(src):
        src = BACKDROP
    pb = GdkPixbuf.Pixbuf.new_from_file(src)
    # PyGObject 的 Gdk.cairo_surface_create_from_pixbuf 需要 3 参且行为不一，
    # 直接用 ImageSurface + cairo_set_source_pixbuf 最稳
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pb.get_width(), pb.get_height())
    cr = cairo.Context(surface)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    font = _pick_cjk_font()

    ix, iy0 = layout["ICON_X"], layout["ICON_Y0"]
    iw, ih = layout["ICON_W"], layout["ICON_H"]
    per, gap = layout["PER_COL"], layout["GAP"]
    # G.14：图标尺寸/标签字号/标签显隐均来自设置（layout 里带进来）
    isize = int(layout.get("ICON_SIZE", ICON_SIZE))
    lsize = int(layout.get("LABEL_SIZE", LABEL_SIZE))
    show_label = bool(layout.get("SHOW_LABEL", True))

    # G.25：拖拽后图标坐标由 layout['POSITIONS']（与 apps 顺序对齐）承载；
    # 无则回落默认网格（兼容旧调用）
    pos_list = layout.get("POSITIONS") or []
    for i, (icon, name) in enumerate(apps):
        if i < len(pos_list) and pos_list[i][0] == name:
            x, y = pos_list[i][1], pos_list[i][2]
        else:
            col, row = divmod(i, per)
            x = ix + col * (iw + gap)
            y = iy0 + row * (ih + gap)
        cx = x + iw / 2
        # 图标（lucide/apple SVG → pixbuf，主色描边）
        cxp = C.pixbuf_icon(icon, isize, C.FG)
        if cxp is not None:
            # 圆角垫底，浅色壁纸上也不会糊
            cr.save()
            cr.set_source_rgba(0.11, 0.11, 0.13, 0.55)
            pad = 8
            px, py = cx - isize / 2 - pad, y + 4
            r = SHADOW_R / 2
            bw = isize + pad * 2
            cr.new_path()
            cr.arc(px + r, py + r, r, 3.1416, 4.7124)
            cr.arc(px + bw - r, py + r, r, 4.7124, 6.2832)
            cr.arc(px + bw - r, py + bw - r, r, 0, 1.5708)
            cr.arc(px + r, py + bw - r, r, 1.5708, 3.1416)
            cr.close_path()
            cr.fill()
            cr.restore()
            Gdk.cairo_set_source_pixbuf(cr, cxp, cx - isize / 2, y + 12)
            cr.paint()
        if show_label:
            _draw_label(cr, name, cx, y + ih - 22, font, lsize)

    surface.flush()
    os.makedirs(os.path.dirname(out_on), exist_ok=True)
    surface.write_to_png(out_on)
    # off 版 = 干净壁纸（原样复制，零重绘）
    try:
        import shutil

        shutil.copyfile(src, out_off)
    except Exception:
        pass
    return True


if __name__ == "__main__":
    # 独立执行时按当前设置渲染（ui_icons 内部调用 render() 亦同源）
    cfg = C.load_conf()
    iw, ih = C.icon_cell(cfg)
    apps = [(a["icon"], a["name"]) for a in C.all_apps()]
    lay = dict(
        ICON_X=int(cfg.get("icon_x", 24)),
        ICON_Y0=int(cfg.get("icon_y0", 76)),
        ICON_W=iw,
        ICON_H=ih,
        PER_COL=int(cfg.get("per_col", 5)),
        GAP=int(cfg.get("icon_gap", 10)),
        ICON_SIZE=int(cfg.get("icon_size", 40)),
        LABEL_SIZE=int(cfg.get("label_size", 11)),
        SHOW_LABEL=bool(cfg.get("icon_label", True)),
    )
    # G.25：独立执行同样走 icon_positions，与 ui_icons 渲染/点击区同源
    from qd_ui_common import icon_positions

    lay["POSITIONS"] = [(n, x, y) for n, x, y in icon_positions(cfg, apps)]
    ok = render(apps, lay, wallpaper=cfg.get("wallpaper"))
    print("render ok" if ok else "render failed")
