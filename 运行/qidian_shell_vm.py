#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# qidian_shell.py - 起点 OS 桌面 Shell 原型 v2（QiDian Glass）
# ============================================================
# 设计语言 docs/ui-design.md：
#   底部居中玻璃 Dock（默认空，抽屉右键固定）+ 全应用抽屉
#   + 全系统玻璃材质 + 顶部最小化窗口架（双击数字键还原）
#   + Win 键打开 Intent T / Ctrl+空格 语音转文字指令条
# 运行: python shell/qidian_shell.py   （依赖 PySide6）
# ============================================================
import sys
import os
import json
from pathlib import Path
import math
import re
import time
import datetime
import shutil
import socket
import subprocess
import urllib.parse

from PySide6.QtCore import Qt, QRectF, QTimer, QPropertyAnimation, QEasingCurve, QUrl, QPointF
from PySide6.QtGui import (QPainter, QColor, QLinearGradient, QRadialGradient, QBrush,
                           QPainterPath, QImage, QFont, QFontMetrics, QPen, QPixmap,
                           QGuiApplication, QShortcut, QKeySequence, QCursor)
from PySide6.QtWidgets import (QApplication, QWidget, QLineEdit, QGraphicsScene,
                               QGraphicsPixmapItem, QGraphicsBlurEffect,
                               QGraphicsOpacityEffect, QGridLayout, QPushButton,
                               QSlider, QLabel, QScrollArea)
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_OK = True
except ImportError:
    QWebEngineView = None
    WEBENGINE_OK = False
try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
    from PySide6.QtOpenGL import QOpenGLShaderProgram, QOpenGLShader, QOpenGLBuffer
    GL_OK = False   # 半透明顶层 + QOpenGLWidget 在 Windows 原型上不稳定(黑屏/不渲染),
                    # 阶段二随 Wayland 合成器启用着色器路径; 当前用 QPainter 光带
except ImportError:
    GL_OK = False

# 浏览器默认主页 = Intent T 真实控制面板(webui serve.cjs, 端口 8080)
# "浏览器默认打开意图T" = 打开真正在运行的意图T Web 控制面板
INTENT_HOME_URL = "intent://home"
WEBUI_URL = "http://localhost:8080"
INTENT_HOME_HTML = """\n<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font-family:'Microsoft YaHei UI',sans-serif;color:#fff;height:100vh;
     display:flex;flex-direction:column;align-items:center;justify-content:center;
     background:linear-gradient(135deg,#141d3a,#3b2560 55%,#0f2c46)}
.logo{font-size:46px;font-weight:700;letter-spacing:2px}
.sub{opacity:.78;margin-top:12px;font-size:14px}
.box{margin-top:38px;width:72%;max-width:560px;display:flex}
input{flex:1;padding:14px 18px;border-radius:24px;border:none;outline:none;font-size:15px}
button{margin-left:10px;padding:0 26px;border-radius:24px;border:none;
       background:#5a78ff;color:#fff;font-size:15px;cursor:pointer}
.chips{margin-top:30px;display:flex;gap:12px}
.chip{background:rgba(255,255,255,.14);padding:8px 18px;border-radius:16px;font-size:13px}
</style></head><body>
<div class="logo">🧠 Intent T</div>
<div class="sub">起点 OS 内置浏览器 · 默认主页即 Intent T（工作台即首页）</div>
<div class="box"><input placeholder="问 Intent T 任何事…（Intent T 起始页演示）"><button>发送</button></div>
<div class="chips"><div class="chip">🎤 语音指令</div><div class="chip">🧠 本地模型</div>
<div class="chip">🗂️ 记忆</div><div class="chip">🛠️ Agent</div></div>
</body></html>
"""

def load_shell_config():
    cfg = {"pinned": [], "mouse": {"enabled": True, "mode": "glow",
                                   "color": "#5b78ff", "size": 70, "smooth": 10}}
    try:
        cfg.update(json.loads((Path.home() / ".qidian_shell.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_shell_config(cfg):
    try:
        (Path.home() / ".qidian_shell.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


DOCK_RADIUS = 24
WIN_RADIUS = 12
SHADOW_MARGIN = 30


# ------------------------------------------------------------
# 应用清单（原型：emoji 占位图标；正式版由 Intent T 提供真实图标）
# ------------------------------------------------------------
APPS_BY_KEY = {
    "intent":   {"key": "intent",   "emoji": "🧠", "name": "Intent T"},
    "terminal": {"key": "terminal", "emoji": "🖥️", "name": "终端"},
    "files":    {"key": "files",    "emoji": "📁", "name": "文件"},
    "about":    {"key": "about",    "emoji": "💠", "name": "关于起点"},
    "browser":  {"key": "browser",  "emoji": "🌐", "name": "浏览器"},
    "settings": {"key": "settings", "emoji": "⚙️", "name": "设置"},
    "music":    {"key": "music",    "emoji": "🎵", "name": "音乐"},
    "photos":   {"key": "photos",   "emoji": "🖼️", "name": "相册"},
    "mail":     {"key": "mail",     "emoji": "📮", "name": "邮件"},
    "calendar": {"key": "calendar", "emoji": "📅", "name": "日历"},
    "calc":     {"key": "calc",     "emoji": "🧮", "name": "计算器"},
    "notes":    {"key": "notes",    "emoji": "📝", "name": "备忘录"},
    "store":    {"key": "store",    "emoji": "🛍️", "name": "软件商店"},
    "wechat":   {"key": "wechat",   "emoji": "💬", "name": "微信"},
    "wps":      {"key": "wps",      "emoji": "📊", "name": "WPS"},
    "video":    {"key": "video",    "emoji": "🎬", "name": "视频"},
}
DRAWER_ORDER = ["intent", "terminal", "files", "about", "browser", "settings",
                "music", "photos", "mail", "calendar", "calc", "notes",
                "store", "wechat", "wps", "video"]
# 原型里可真实打开的窗口（其余弹提示）
REAL_WINDOWS = {"terminal", "files", "about", "intent", "browser", "settings"}


# ------------------------------------------------------------
# 壁纸 / 模糊 / 玻璃 / 阴影 工具
# ------------------------------------------------------------
def build_wallpaper(size):
    w, h = size.width(), size.height()
    img = QImage(size, QImage.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0.0, QColor("#141d3a"))
    grad.setColorAt(0.5, QColor("#3b2560"))
    grad.setColorAt(1.0, QColor("#0f2c46"))
    p.fillRect(0, 0, w, h, grad)
    r = 0.5 * min(w, h)
    for bx, by, color, alpha in [(0.22, 0.24, "#7b5cff", 150), (0.80, 0.18, "#2bb3ff", 130),
                                 (0.72, 0.82, "#ff5c8a", 120), (0.22, 0.86, "#22d3a5", 110)]:
        g = QRadialGradient(bx * w, by * h, r)
        c = QColor(color); c.setAlpha(alpha)
        g.setColorAt(0.0, c)
        c2 = QColor(color); c2.setAlpha(0)
        g.setColorAt(1.0, c2)
        p.fillRect(0, 0, w, h, g)
    v = QRadialGradient(w / 2, h / 2, max(w, h) * 0.75)
    v.setColorAt(0.55, QColor(0, 0, 0, 0))
    v.setColorAt(1.0, QColor(0, 0, 0, 130))
    p.fillRect(0, 0, w, h, v)
    p.end()
    return img


def blurred_image(img, radius):
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(img))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    out = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    p = QPainter(out)
    scene.render(p)
    p.end()
    return out


_SHADOW_CACHE = {}


def shadow_image(w, h, radius, blur=16):
    """真正跟随圆角形状的柔和阴影（缓存）。"""
    key = (w, h, radius, blur)
    if key in _SHADOW_CACHE:
        return _SHADOW_CACHE[key]
    m = SHADOW_MARGIN
    base = QImage(w + 2 * m, h + 2 * m, QImage.Format_ARGB32_Premultiplied)
    base.fill(Qt.transparent)
    p = QPainter(base)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 110))
    p.drawRoundedRect(QRectF(m, m + 4, w, h), radius, radius)
    p.end()
    img = blurred_image(base, blur)
    _SHADOW_CACHE[key] = img
    return img


def paint_glass(p, widget, blurred, radius,
                top_alpha=180, bottom_alpha=100, border_alpha=30):
    """玻璃 v2：模糊壁纸回贴 + 上亮下暗渐变叠色 + 顶部高光线。
    '通透感'来自: 强模糊底 + 奶白渐变 + 边缘光。"""
    rect = QRectF(widget.rect())
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setClipPath(path)
    p.drawImage(-widget.geometry().topLeft(), blurred)
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    g.setColorAt(0.0, QColor(255, 255, 255, top_alpha))
    g.setColorAt(1.0, QColor(255, 255, 255, bottom_alpha))
    p.fillRect(rect, g)
    p.restore()
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(0, 0, 0, border_alpha), 1))
    p.drawPath(path)
    p.setPen(QPen(QColor(255, 255, 255, 150), 1.2))
    p.drawPath(path)


def draw_shadow(p, widget, radius):
    img = shadow_image(widget.width(), widget.height(), radius)
    p.drawImage(-SHADOW_MARGIN, -SHADOW_MARGIN + 4, img)


def paint_border_glow(p, rect, radius, mouse_pos, color="#8fb0ff", intensity=1.0):
    """BorderGlow: 细亮线(2px) + 两层平滑泛光, 亮度以鼠标为中心径向衰减。
    intensity 由'鼠标到边框的距离'决定——靠近即触发, 不必进入图标。"""
    if intensity <= 0.02:
        return
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    reach = max(70.0, rect.width() * 0.9)

    def _grad(amul):
        g = QRadialGradient(mouse_pos, reach)
        for stop, a in ((0.0, 1.0), (0.55, 0.5), (1.0, 0.0)):
            c = QColor(color)
            c.setAlpha(int(a * 255 * intensity * amul))
            g.setColorAt(stop, c)
        return QBrush(g)

    p.setBrush(Qt.NoBrush)
    for width, amul in ((2.0, 1.0), (5.0, 0.40), (10.0, 0.16)):
        p.setPen(QPen(_grad(amul), width))
        p.drawPath(path)


def set_font(p, size, bold=False, color=Qt.white):
    f = QFont("Microsoft YaHei UI", size)
    f.setBold(bold)
    p.setFont(f)
    p.setPen(QColor(color))


# ------------------------------------------------------------
# GlowCursor 鼠标辉光(react-bits GlowCursor 的 Qt 移植)
# ------------------------------------------------------------
# 原 react-bits GlowCursor 着色器(逐行移植; 去掉 ES-only 的 precision 行)
GLSL_VERTEX = """
attribute vec2 position;
attribute vec2 uv;
varying vec2 vUv;

void main() {
  vUv = uv;
  gl_Position = vec4(position, 0.0, 1.0);
}
"""

GLSL_FRAGMENT = """
#define MAX_POINTS 64

uniform vec2 uResolution;
uniform vec2 uPoints[MAX_POINTS];
uniform float uPointCount;
uniform vec3 uColor;
uniform vec3 uSecondaryColor;
uniform float uTrailWidth;
uniform float uTaper;
uniform float uGlowIntensity;
uniform float uGlowSpread;
uniform float uHotspot;
uniform float uBrightness;
uniform float uOpacity;
uniform float uPulseSpeed;
uniform float uNoiseStrength;
uniform float uNormalBlend;
uniform float uTime;
uniform float uFade;

varying vec2 vUv;

float sRGB(float x) {
  if (x <= 0.00031308) return 12.92 * x;
  return 1.055 * pow(x, 1.0 / 2.4) - 0.055;
}

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float filmGrain(vec2 p, float time) {
  float frame = time * 18.0;
  float frameIndex = mod(floor(frame), 256.0);
  float nextFrameIndex = mod(frameIndex + 1.0, 256.0);
  float blend = fract(frame);
  blend = blend * blend * (3.0 - 2.0 * blend);
  vec2 pixel = floor(p);
  float current = hash(pixel + vec2(frameIndex * 17.0, frameIndex * 31.0));
  float next = hash(pixel + vec2(nextFrameIndex * 17.0, nextFrameIndex * 31.0));
  return mix(current, next, blend) * 2.0 - 1.0;
}

void main() {
  vec2 pixel = vUv * uResolution;
  float denominator = max(uPointCount - 1.0, 1.0);
  float strongest = 0.0;
  float strongestCore = 0.0;
  float colorWeight = 0.0;
  vec3 colorSum = vec3(0.0);

  for (int i = 0; i < MAX_POINTS - 1; i++) {
    float index = float(i);
    float active = 1.0 - step(uPointCount - 1.0, index);
    vec2 start = uPoints[i];
    vec2 end = uPoints[i + 1];
    vec2 toPixel = pixel - start;
    vec2 segment = end - start;
    float along = clamp(dot(toPixel, segment) / max(dot(segment, segment), 0.0001), 0.0, 1.0);
    float progress = clamp((index + along) / denominator, 0.0, 1.0);
    float life = pow(max(1.0 - progress, 0.0), mix(0.55, 1.25, uTaper));
    float width = uTrailWidth * mix(1.0, 0.25, pow(progress, mix(0.55, 1.6, uTaper)));
    float distanceToTrail = length(toPixel - segment * along);
    float falloff = max(width * (0.8 + uGlowSpread * 1.4), 0.5);
    float beam = min(1.0, (falloff * falloff) / (distanceToTrail * distanceToTrail + falloff * falloff));
    float core = exp(-pow(distanceToTrail / max(width, 0.5), 2.0) * 2.5);
    float pulseAmount = min(abs(uPulseSpeed), 1.0);
    float pulse = 1.0 + sin(uTime * uPulseSpeed * 3.0 - progress * 11.0) * 0.16 * pulseAmount;
    float intensity = (core + beam * uGlowIntensity * 0.55) * life * pulse * active;
    vec3 segmentColor = mix(uColor, uSecondaryColor, progress);

    strongest = max(strongest, intensity);
    strongestCore = max(strongestCore, core * life * active);
    colorSum += segmentColor * intensity;
    colorWeight += intensity;
  }

  float grain = filmGrain(pixel, uTime);
  float noiseAmount = (1.0 - exp(-uNoiseStrength * 2.2)) * 0.4;
  float alpha = clamp(strongest * uOpacity * uFade, 0.0, 1.0);
  if (alpha < 0.0005) discard;

  vec3 color = colorSum / max(colorWeight, 0.0001);
  color = mix(color, vec3(1.0), smoothstep(0.25, 0.95, strongestCore) * uHotspot);
  float luminance = sRGB(clamp(strongest * uBrightness, 0.0, 1.0));
  luminance *= 1.0 + grain * noiseAmount;
  vec3 additiveColor = color * luminance;
  float normalAlpha = clamp(strongest * uBrightness * uOpacity * uFade, 0.0, 1.0);
  vec3 normalColor = mix(color, vec3(1.0), smoothstep(0.45, 1.0, strongestCore) * uHotspot * 0.35);
  gl_FragColor = vec4(mix(additiveColor, normalColor, uNormalBlend), mix(alpha, normalAlpha, uNormalBlend));
}
"""


class GlowOverlay(QWidget):
    """react-bits GlowCursor 的 Qt 移植:
    64 点链式跟随 -> 双色锥形光带(圆头线段, 亮度/宽度沿轨迹衰减),
    头部热点增白, 正弦脉冲, 闲置 700ms 淡出(900ms 渐变)。"""
    MAX_POINTS = 40
    TRAIL_T = 0.55

    def __init__(self, geometry):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setWindowFlag(Qt.WindowTransparentForInput, True)
        self.points = [QPointF(0, 0) for _ in range(self.MAX_POINTS)]
        self.target = QPointF(0, 0)
        self.head = QPointF(0, 0)
        self.initialized = False
        self.fade = 0.0
        self.last_input = 0.0
        self.last_pos = None
        self.trail = []
        self._pending_zone = None
        self.setGeometry(geometry)
        self._last_frame = time.time()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def _tick(self):
        cfg = SHELL_REF.mouse_cfg if SHELL_REF else {}
        now = time.time()
        if not cfg.get("enabled", True):
            if self.isVisible():
                self.hide()
            return
        if not self.isVisible():
            self.show()
        pos = QPointF(QCursor.pos())
        if self.last_pos is None or pos != self.last_pos:
            self.last_pos = QPointF(pos)
            self.last_input = now
        if not self.initialized:
            self.target = QPointF(pos)
            self.head = QPointF(pos)
            for pt in self.points:
                pt.setX(pos.x()); pt.setY(pos.y())
            self.initialized = True
            self.fade = 1.0
        # 链式跟随: 头追目标, 其余逐点追前一点 (delta 归一化, 与原版一致)
        self.target = QPointF(pos)
        delta = min(max((now - self._last_frame) / 0.016667, 1.0), 3.0)
        self._last_frame = now
        follow = max(0.01, min(0.99, float(cfg.get("smooth", 10)) / 60.0))
        head_ease = 1.0 - (1.0 - follow) ** delta
        chain_base = min(max(0.28 + follow * 0.35, 0.08), 0.92)
        chain_ease = 1.0 - (1.0 - chain_base) ** delta
        self.head.setX(self.head.x() + (self.target.x() - self.head.x()) * head_ease)
        self.head.setY(self.head.y() + (self.target.y() - self.head.y()) * head_ease)
        self.points[0].setX(self.head.x()); self.points[0].setY(self.head.y())
        for i in range(1, self.MAX_POINTS):
            prev = self.points[i - 1]
            pt = self.points[i]
            pt.setX(pt.x() + (prev.x() - pt.x()) * chain_ease)
            pt.setY(pt.y() + (prev.y() - pt.y()) * chain_ease)
        # 闲置淡出
        idle = now - (self.last_input or now)
        fade_target = 1.0 if idle < 0.7 else 0.0
        self.fade += (fade_target - self.fade) * min(1.0, (0.016 * delta / 0.9) * 7)
        # 火焰拖尾模式的时间采样轨迹
        if cfg.get("mode", "beam") == "flame":
            self.trail.append((QPointF(self.head), now))
            self.trail = [(pt, ts) for (pt, ts) in self.trail if now - ts <= self.TRAIL_T]
        self.update()

    def paintEvent(self, _):
        if not SHELL_REF or not SHELL_REF.mouse_cfg.get("enabled", True):
            return
        cfg = SHELL_REF.mouse_cfg
        mode = cfg.get("mode", "beam")
        if mode == "glow":
            mode = "blob"
        if mode == "beam" and GL_OK:
            return          # 光带由 GL 层渲染
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.fade <= 0.02:
            return
        if mode == "blob":
            self._paint_blob(p, cfg)
        elif mode == "flame":
            self._paint_flame(p, cfg)
        else:
            self._paint_beam(p, cfg)

    def _paint_beam(self, p, cfg):
        """GlowCursor 光带: 双色渐变 + 锥形收细 + 核心亮线 + 泛光 + 热点。"""
        width = max(float(cfg.get("size", 8)), 1.0)
        col_a = QColor(cfg.get("color", "#67E8F9"))
        col_b = QColor(cfg.get("secondary", "#A78BFA"))
        taper = 0.8
        now = time.time()
        p.setCompositionMode(QPainter.CompositionMode_Plus)
        n = self.MAX_POINTS
        for i in range(n - 1):
            prog = i / (n - 1)
            life = max(1.0 - prog, 0.0) ** (0.55 + 0.7 * taper)
            seg_w = width * (1.0 - 0.75 * (prog ** 1.3))
            pulse = 1.0 + math.sin(now * 1.1 * 3.0 - prog * 11.0) * 0.16
            f = QColor(col_a.red() + (col_b.red() - col_a.red()) * prog,
                       col_a.green() + (col_b.green() - col_a.green()) * prog,
                       col_a.blue() + (col_b.blue() - col_a.blue()) * prog)
            fade = self.fade
            # 泛光层
            glow_pen = QPen(QColor(f.red(), f.green(), f.blue(),
                                   int(46 * life * pulse * fade)), seg_w * 2.6)
            glow_pen.setCapStyle(Qt.RoundCap)
            p.setPen(glow_pen)
            p.setBrush(Qt.NoBrush)
            p.drawLine(self.points[i], self.points[i + 1])
            # 核心亮线
            core_pen = QPen(QColor(f.red(), f.green(), f.blue(),
                                   int(165 * life * pulse * fade)), seg_w)
            core_pen.setCapStyle(Qt.RoundCap)
            p.setPen(core_pen)
            p.drawLine(self.points[i], self.points[i + 1])
        # 头部热点增白
        head = self.points[0]
        hr = width * 2.2
        g = QRadialGradient(head, hr)
        c = QColor(255, 255, 255, int(150 * self.fade * 0.65 * 1.4))
        g.setColorAt(0.0, c)
        c = QColor(col_a.red(), col_a.green(), col_a.blue(), 0)
        g.setColorAt(1.0, c)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(head, hr, hr)

    def _paint_blob(self, p, cfg):
        size = float(cfg.get("size", 70)) * 2.0
        color = QColor(cfg.get("color", "#5b78ff"))
        pos = QPointF(self.head)
        r = size
        g = QRadialGradient(pos, r)
        c = QColor(color); c.setAlpha(int(105 * self.fade)); g.setColorAt(0.0, c)
        c = QColor(color); c.setAlpha(int(50 * self.fade)); g.setColorAt(0.35, c)
        c = QColor(color); c.setAlpha(0); g.setColorAt(1.0, c)
        p.setCompositionMode(QPainter.CompositionMode_Plus)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(pos, r, r)
        g2 = QRadialGradient(pos, 30)
        c = QColor(255, 255, 255, int(75 * self.fade)); g2.setColorAt(0.0, c)
        c = QColor(255, 255, 255, 0); g2.setColorAt(1.0, c)
        p.setBrush(QBrush(g2))
        p.drawEllipse(pos, 30, 30)

    def _paint_flame(self, p, cfg):
        size = float(cfg.get("size", 70))
        color = QColor(cfg.get("color", "#5b78ff"))
        now = time.time()
        p.setCompositionMode(QPainter.CompositionMode_Plus)
        for pt, ts in self.trail:
            k = max(0.0, 1.0 - (now - ts) / self.TRAIL_T)
            s = size * (0.30 + 0.70 * k)
            g = QRadialGradient(pt, s)
            c = QColor(color); c.setAlpha(int(95 * k * self.fade)); g.setColorAt(0.0, c)
            c = QColor(color); c.setAlpha(int(30 * k * self.fade)); g.setColorAt(0.5, c)
            c = QColor(color); c.setAlpha(0); g.setColorAt(1.0, c)
            p.setBrush(QBrush(g))
            p.setPen(Qt.NoPen)
            p.drawEllipse(pt, s, s)
        if self.trail:
            head = self.trail[-1][0]
            g = QRadialGradient(head, size * 0.55)
            c = QColor(color); c.setAlpha(int(160 * self.fade)); g.setColorAt(0.0, c)
            c = QColor(color); c.setAlpha(0); g.setColorAt(1.0, c)
            p.setBrush(QBrush(g))
            p.drawEllipse(head, size * 0.55, size * 0.55)
            p.setBrush(QColor(255, 255, 255, int(220 * self.fade)))
            p.drawEllipse(head, 5, 5)


class GlowOverlayGL(QOpenGLWidget):
    """原版 GlowCursor 的 GL 渲染层: 同一着色器, 同一 uniforms。"""
    MAX_POINTS = 64

    def __init__(self, geometry):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setWindowFlag(Qt.WindowTransparentForInput, True)
        fmt = self.format()
        fmt.setAlphaBufferSize(8)
        self.setFormat(fmt)
        self.points = [QPointF(0, 0) for _ in range(self.MAX_POINTS)]
        self.target = QPointF(0, 0)
        self.head = QPointF(0, 0)
        self.initialized = False
        self.fade = 0.0
        self.last_input = 0.0
        self.last_pos = None
        self.last_frame = time.time()
        self.setGeometry(geometry)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def _tick(self):
        cfg = SHELL_REF.mouse_cfg if SHELL_REF else {}
        now = time.time()
        if not cfg.get("enabled", True):
            if self.isVisible():
                self.hide()
            return
        if not self.isVisible():
            self.show()
        pos = QPointF(QCursor.pos())
        if self.last_pos is None or pos != self.last_pos:
            self.last_pos = QPointF(pos)
            self.last_input = now
        if not self.initialized:
            self.target = QPointF(pos)
            self.head = QPointF(pos)
            for pt in self.points:
                pt.setX(pos.x()); pt.setY(pos.y())
            self.initialized = True
            self.fade = 1.0
        # 链式跟随(与原版 JS 逻辑一致)
        self.target = QPointF(pos)
        delta = min(max((now - self.last_frame) / 0.016667, 1.0), 3.0)
        self.last_frame = now
        follow = max(0.01, min(0.99, float(cfg.get("smooth", 10)) / 60.0))
        head_ease = 1.0 - (1.0 - follow) ** delta
        chain_base = min(max(0.28 + follow * 0.35, 0.08), 0.92)
        chain_ease = 1.0 - (1.0 - chain_base) ** delta
        self.head.setX(self.head.x() + (self.target.x() - self.head.x()) * head_ease)
        self.head.setY(self.head.y() + (self.target.y() - self.head.y()) * head_ease)
        self.points[0].setX(self.head.x()); self.points[0].setY(self.head.y())
        for i in range(1, self.MAX_POINTS):
            prev = self.points[i - 1]
            pt = self.points[i]
            pt.setX(pt.x() + (prev.x() - pt.x()) * chain_ease)
            pt.setY(pt.y() + (prev.y() - pt.y()) * chain_ease)
        # 闲置淡出(700ms / 900ms)
        idle = now - (self.last_input or now)
        fade_target = 1.0 if idle < 0.7 else 0.0
        self.fade += (fade_target - self.fade) * min(1.0, (0.016 * delta) / 0.9 * 7)
        self.update()
    def initializeGL(self):
        self.prog = QOpenGLShaderProgram(self)
        if not self.prog.addShaderFromSourceCode(QOpenGLShader.Vertex, GLSL_VERTEX):
            raise RuntimeError("vertex: " + self.prog.log())
        if not self.prog.addShaderFromSourceCode(QOpenGLShader.Fragment, GLSL_FRAGMENT):
            raise RuntimeError("fragment: " + self.prog.log())
        self.prog.bindAttributeLocation("position", 0)
        self.prog.bindAttributeLocation("uv", 1)
        if not self.prog.link():
            raise RuntimeError("link: " + self.prog.log())
        with open("D:/qemu-kit/gl_debug.log", "a", encoding="utf-8") as f:
            f.write("GL init OK, vlog: " + str(self.prog.log())[:200] + chr(10))
        import struct
        verts = struct.pack("<12f", -1, -1, 0, 0,
                            3, -1, 2, 0,
                            -1, 3, 0, 2)
        self.buf = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self.buf.create()
        self.buf.bind()
        self.buf.allocate(verts, len(verts))
        self.prog.enableAttributeArray(0)
        self.prog.setAttributeBuffer(0, 0x1406, 0, 2, 0)       # GL_FLOAT
        self.prog.enableAttributeArray(1)
        self.prog.setAttributeBuffer(1, 0x1406, 16, 2, 0)
        self.buf.release()

    def paintGL(self):
        from PySide6.QtGui import QVector2D
        fns = self.context().functions()
        fns.glClearColor(0.0, 0.0, 0.0, 0.0)
        fns.glClear(0x00004000)                                 # GL_COLOR_BUFFER_BIT
        if not hasattr(self, "prog"):
            return
        cfg = SHELL_REF.mouse_cfg if SHELL_REF else {}
        self.prog.bind()
        dpr = self.devicePixelRatioF()
        W = max(self.width() * dpr, 1.0)
        H = max(self.height() * dpr, 1.0)
        self.prog.setUniformValue("uResolution", W, H)
        arr = []
        for pt in self.points:
            arr.append(QVector2D(pt.x() * dpr, (self.height() - pt.y()) * dpr))
        self.prog.setUniformValueArray("uPoints", arr)
        self.prog.setUniformValue("uPointCount", float(min(64, self.MAX_POINTS)))
        self.prog.setUniformValue("uColor", QColor(cfg.get("color", "#67E8F9")))
        self.prog.setUniformValue("uSecondaryColor", QColor(cfg.get("secondary", "#A78BFA")))
        self.prog.setUniformValue("uTrailWidth", max(float(cfg.get("size", 8)), 0.1))
        self.prog.setUniformValue("uTaper", 0.8)
        self.prog.setUniformValue("uGlowIntensity", 1.9)
        self.prog.setUniformValue("uGlowSpread", 1.2)
        self.prog.setUniformValue("uHotspot", 0.65)
        self.prog.setUniformValue("uBrightness", 1.25)
        self.prog.setUniformValue("uOpacity", 1.0)
        self.prog.setUniformValue("uPulseSpeed", 1.1)
        self.prog.setUniformValue("uNoiseStrength", 0.035)
        self.prog.setUniformValue("uNormalBlend", 0.0)          # screen(加性)混合
        self.prog.setUniformValue("uTime", time.time() % 3600.0)
        self.prog.setUniformValue("uFade", float(self.fade))
        fns.glDrawArrays(4, 0, 3)                               # GL_TRIANGLES
        self.prog.release()


SHELL_REF = None


SHELL_REF = None

# ------------------------------------------------------------
# 玻璃窗口
# ------------------------------------------------------------
class GlassWindow(QWidget):
    TITLE_H = 40

    def __init__(self, shell, key):
        super().__init__(shell)
        self.shell = shell
        self.app = APPS_BY_KEY[key]
        self.key = key
        self.minimized = False
        self.maximized = False
        self.drag_at = None
        self.blink = True
        self.setAttribute(Qt.WA_TranslucentBackground)
        idx = len(shell.windows) % 5
        self.resize(820, 560 if key == "browser" else 400)
        self.move(120 + idx * 36, 90 + idx * 32)
        if key == "browser":
            self._build_browser()
        if key == "settings":
            self.resize(560, 540)
            self._build_settings()
        if key == "intent":
            self.messages = [("ai", "你好，我是 Intent T。按 Ctrl+空格 随时语音下达指令。")]
            self.input = QLineEdit(self)
            self.input.setPlaceholderText("给 Intent T 下达指令…")
            self.input.setStyleSheet(
                "QLineEdit{background:rgba(255,255,255,200);border:1px solid rgba(0,0,0,40);"
                "border-radius:10px;padding:0 12px;font-size:13px;}"
                "QLineEdit:focus{border:1px solid rgba(90,120,255,160);}")
            self.input.returnPressed.connect(self._send)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flip)
        self._timer.start(600)

    def _build_browser(self):
        self.urlbar = QLineEdit(self)
        self.urlbar.setPlaceholderText("搜索或输入网址（默认主页：intent://home）")
        self.urlbar.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,215);border:1px solid rgba(0,0,0,35);"
            "border-radius:14px;padding:0 12px;font-size:12px;}"
            "QLineEdit:focus{border:1px solid rgba(90,120,255,170);}")
        self.urlbar.returnPressed.connect(self._nav)
        self._btns = []
        for label, fn in (("←", lambda: self.web.back() if WEBENGINE_OK else None),
                          ("→", lambda: self.web.forward() if WEBENGINE_OK else None),
                          ("⟳", lambda: self.web.reload() if WEBENGINE_OK else None),
                          ("🏠", self._home)):
            b = QPushButton(label, self)
            b.setFixedSize(34, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet("QPushButton{background:rgba(255,255,255,190);border:1px solid rgba(0,0,0,30);"
                            "border-radius:10px;font-size:14px;}"
                            "QPushButton:hover{background:rgba(255,255,255,235)}")
            b.clicked.connect(fn)
            self._btns.append(b)
        if WEBENGINE_OK:
            self.web = QWebEngineView(self)
            self.web.urlChanged.connect(self._sync_url)
            self._home()
        else:
            from PySide6.QtWidgets import QTextBrowser
            self.web = QTextBrowser(self)
            self.web.setStyleSheet("QTextBrowser{background:rgba(10,12,24,235);border:none;"
                                   "color:#cdd6f4;font-size:14px;}")
            self.web.setHtml("<div style='padding:36px;font-family:Microsoft YaHei UI,serif;'>"
                             "<h2 style='color:#8fb0ff'>浏览器组件未安装（QtWebEngine）</h2>"
                             "<p>当前虚拟机未安装 QtWebEngine（磁盘空间限制）。</p>"
                             "<p>桌面 Shell 主体（玻璃 Dock / 抽屉 / 灵动岛 / 圆球最小化 / 分屏 / 语音）运行正常。</p>"
                             "<p>后续腾出磁盘安装 PySide6-Addons 即可启用真浏览器。</p></div>")
            self.urlbar.setPlaceholderText("浏览器组件未安装（QtWebEngine）")

    def _ensure_webui(self):
        s = socket.socket()
        s.settimeout(0.4)
        alive = s.connect_ex(("127.0.0.1", 8080)) == 0
        s.close()
        if alive:
            return
        node = shutil.which("node")
        webui = (Path(__file__).resolve().parent.parent.parent / "ten" / "webui")
        serve = webui / "serve.cjs"
        if node and serve.exists():
            subprocess.Popen([node, str(serve)], cwd=str(webui),
                             creationflags=0x08000000)
            time.sleep(1.2)

    def _home(self):
        if not WEBENGINE_OK:
            self.urlbar.setText("浏览器组件未安装（QtWebEngine）")
            return
        self._ensure_webui()
        self.web.load(QUrl(WEBUI_URL))
        self.urlbar.setText(INTENT_HOME_URL)

    def _sync_url(self, url):
        s = url.toString()
        if s.startswith("intent://") or s == "about:blank" or s in ("http://localhost:8080", "http://localhost:8080/"):
            self.urlbar.setText(INTENT_HOME_URL)
        else:
            self.urlbar.setText(s)

    def _nav(self):
        if not WEBENGINE_OK:
            self.urlbar.setText("浏览器组件未安装（QtWebEngine）")
            return
        text = self.urlbar.text().strip()
        if not text or "intent" in text:
            self._home()
            return
        if "://" not in text:
            if re.match(r"^[\w.-]+\.[a-zA-Z]{2,}(/.*)?$", text):
                text = "https://" + text
            else:                                  # 关键词 -> 搜索
                text = "https://www.baidu.com/s?wd=" + urllib.parse.quote(text)
        self.web.load(QUrl(text))

    def _build_settings(self):
        self._set_widgets = []
        cfg = self.shell.mouse_cfg
        self.mode_chips = []
        for m, label in (("beam", "光带 GlowCursor"), ("flame", "火焰拖尾"), ("blob", "柔光")):
            b = QPushButton(label, self)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, mm=m: self._set_mode(mm))
            self.mode_chips.append((b, m))
            self._set_widgets.append(b)
        self._restyle_modes()
        self.tgl = QPushButton("鼠标效果：已开启" if cfg.get("enabled") else "鼠标效果：已关闭", self)
        self.tgl.setCheckable(True)
        self.tgl.setChecked(bool(cfg.get("enabled")))
        self.tgl.setFixedHeight(34)
        self.tgl.setCursor(Qt.PointingHandCursor)
        self.tgl.setStyleSheet("QPushButton{background:rgba(91,120,255,220);color:white;"
                               "border:none;border-radius:10px;font-size:13px;padding:0 14px;}"
                               "QPushButton:checked{background:rgba(125,125,140,180);}")
        self.tgl.toggled.connect(self._toggle_glow)
        self._set_widgets.append(self.tgl)

        self.color_chips = []
        for c in ["#5b78ff", "#7b5cff", "#ff5c8a", "#22d3a5", "#ff9f43"]:
            b = QPushButton(self)
            b.setFixedSize(30, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, cc=c: self._set_color(cc))
            self.color_chips.append((b, c))
            self._set_widgets.append(b)
        self._restyle_chips()

        self.size_slider = QSlider(Qt.Horizontal, self)
        self.size_slider.setRange(4, 20)
        self.size_slider.setValue(int(cfg.get("size", 8)))
        self.size_slider.valueChanged.connect(self._size_changed)
        self._set_widgets.append(self.size_slider)

        self.color_chips2 = []
        for c in ["#A78BFA", "#67E8F9", "#ff5c8a", "#22d3a5", "#ff9f43"]:
            b = QPushButton(self)
            b.setFixedSize(30, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, cc=c: self._set_color2(cc))
            self.color_chips2.append((b, c))
            self._set_widgets.append(b)
        self._restyle_chips2()

        self.smooth_slider = QSlider(Qt.Horizontal, self)
        self.smooth_slider.setRange(4, 20)
        self.smooth_slider.setValue(int(cfg.get("size", 8)))
        self.smooth_slider.setRange(2, 30)
        self.smooth_slider.setValue(int(cfg.get("smooth", 10)))
        self.smooth_slider.valueChanged.connect(self._smooth_changed)
        self._set_widgets.append(self.smooth_slider)

        self.hint = QLabel("移动鼠标即可预览 · 设置保存在 ~/.qidian_shell.json", self)
        self.hint.setStyleSheet("color:rgba(90,90,100,175);font-size:12px;")
        self._set_widgets.append(self.hint)
        self._layout_settings()

    def _layout_settings(self):
        c = self.content_rect().toRect()
        off = c.top() + 14
        for i, (b, _) in enumerate(self.mode_chips):
            b.setFixedHeight(32)
            b.move(22 + i * 118, off + 88)
        self.tgl.adjustSize()
        self.tgl.move(22, off + 134)
        for i, (b, _) in enumerate(self.color_chips):
            b.move(22 + i * 40, off + 190)
        for i, (b, _) in enumerate(self.color_chips2):
            b.move(22 + i * 40, off + 254)
        self.size_slider.setGeometry(22, off + 316, 320, 24)
        self.smooth_slider.setGeometry(22, off + 378, 320, 24)
        self.hint.setGeometry(22, off + 422, 460, 24)

    def _restyle_modes(self):
        cur = self.shell.mouse_cfg.get("mode", "beam")
        if cur == "glow":
            cur = "blob"
        for b, m in self.mode_chips:
            sel = (m == cur)
            b.setStyleSheet("QPushButton{background:" + ("rgba(91,120,255,235)" if sel else "rgba(255,255,255,55)")
                            + ";color:" + ("white" if sel else "rgba(230,230,235,195)")
                            + ";border:1px solid rgba(255,255,255," + ("150" if sel else "55") + ");"
                            "border-radius:16px;font-size:13px;}"
                            "QPushButton:hover{background:rgba(91,120,255,150);}")

    def _set_mode(self, m):
        self.shell.mouse_cfg["mode"] = m
        self._restyle_modes()
        save_shell_config(self.shell.cfg)
        if hasattr(self.shell, "update_overlays"):
            self.shell.update_overlays()

    def _restyle_chips(self):
        cfg = self.shell.mouse_cfg
        for b, c in self.color_chips:
            sel = c.lower() == str(cfg.get("color", "")).lower()
            border = "rgba(255,255,255,235)" if sel else "rgba(255,255,255,70)"
            b.setStyleSheet("QPushButton{background:" + c + ";border:3px solid " + border
                            + ";border-radius:15px;}")

    def _toggle_glow(self, on):
        self.shell.mouse_cfg["enabled"] = bool(on)
        self.tgl.setText("鼠标效果：已开启" if on else "鼠标效果：已关闭")
        save_shell_config(self.shell.cfg)
        self.update()

    def _set_color(self, c):
        self.shell.mouse_cfg["color"] = c
        self._restyle_chips()
        save_shell_config(self.shell.cfg)

    def _restyle_chips2(self):
        cfg = self.shell.mouse_cfg
        for b, c in self.color_chips2:
            sel = c.lower() == str(cfg.get("secondary", "#A78BFA")).lower()
            border = "rgba(255,255,255,235)" if sel else "rgba(255,255,255,70)"
            b.setStyleSheet("QPushButton{background:" + c + ";border:3px solid " + border
                            + ";border-radius:15px;}")

    def _set_color2(self, c):
        self.shell.mouse_cfg["secondary"] = c
        self._restyle_chips2()
        save_shell_config(self.shell.cfg)

    def _size_changed(self, v):
        self.shell.mouse_cfg["size"] = v
        self.update()

    def _smooth_changed(self, v):
        self.shell.mouse_cfg["smooth"] = v
        self.update()

    def _flip(self):
        self.blink = not self.blink
        if self.isVisible():
            self.update()

    def _send(self):
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.messages.append(("user", text))
        self.update()
        QTimer.singleShot(500, lambda: self._reply(text))

    def _reply(self, text):
        self.messages.append(("ai", f"收到：「{text}」。（原型为演示应答；正式版走本地 llama.cpp / 云端 provider）"))
        self.update()

    # 布局
    def content_rect(self):
        r = QRectF(self.rect())
        return r.adjusted(1, self.TITLE_H, -1, -1)

    def resizeEvent(self, _):
        c = self.content_rect().toRect()
        if self.key == "intent":
            self.input.setGeometry(c.left() + 12, c.bottom() - 44, c.width() - 24, 34)
        elif self.key == "browser":
            y = c.top() + 8
            for i, b in enumerate(self._btns):
                b.move(c.left() + 8 + i * 40, y)
            self.urlbar.setGeometry(c.left() + 8 + 4 * 40 + 6, y, c.width() - 24 - 4 * 40, 30)
            self.web.setGeometry(c.left() + 8, y + 40, c.width() - 16, c.height() - 48)
            self._toolbar_rect = QRectF(c.left(), c.top() + 4, c.width(), 40)
        elif self.key == "settings":
            self._layout_settings()

    def workarea(self):
        return self.shell.rect().adjusted(16, 60, -16, -180)

    def toggle_max(self):
        if self.maximized:
            self.setGeometry(self._saved)
            self.maximized = False
        else:
            self._saved = self.geometry()
            self.setGeometry(self.workarea())
            self.maximized = True
        self.update()

    def minimize(self):
        self.minimized = True
        self.hide()
        self.shell.window_minimized(self)

    def close_window(self):
        self.deleteLater()
        self.shell.windows.pop(self.key, None)
        if self in self.shell.minimized:
            self.shell.minimized.remove(self)
        self.shell.refresh_top()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self.key == "browser" and getattr(self, "_toolbar_rect", None)                     and self._toolbar_rect.contains(e.position()):
                self.urlbar.setFocus()
                self.urlbar.selectAll()
            self.raise_()
            pos = e.position().toPoint()
            w = self.width()
            # 显式按钮: [— 最小化] [✕ 退出]
            if QRectF(w - 100, 8, 42, 24).contains(pos):
                self.minimize(); return
            if QRectF(w - 54, 8, 42, 24).contains(pos):
                self.close_window(); return
            if pos.y() <= self.TITLE_H:
                if 14 <= pos.x() <= 70:   # 红黄绿
                    d = pos.x()
                    if d <= 30:
                        self.close_window(); return
                    elif d <= 50:
                        self.minimize(); return
                    else:
                        self.toggle_max(); return
                self.drag_at = pos
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.drag_at is not None and not self.maximized:
            self.move(self.mapToParent(e.position().toPoint() - self.drag_at))
            gp = self.mapToGlobal(e.position().toPoint())
            zone = self.shell.zone_for(gp)
            self._pending_zone = zone
            self.shell.show_snap_preview(zone)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.drag_at is not None:
            zone = getattr(self, "_pending_zone", None)
            self._pending_zone = None
            self.shell.hide_snap_preview()
            if zone:
                self.shell.apply_snap(self, zone)
        self.drag_at = None
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat() and self.key != "intent":
            self.shell.handle_space_press(); return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat() and self.key != "intent":
            self.shell.handle_space_release(); return
        super().keyReleaseEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        draw_shadow(p, self, WIN_RADIUS)
        paint_glass(p, self, self.shell.blur_dock, WIN_RADIUS,
                    top_alpha=150, bottom_alpha=110)
        content = self.content_rect()
        cpath = QPainterPath()
        cpath.addRoundedRect(content, 8, 8)
        p.save()
        p.setClipPath(cpath)
        p.fillRect(content, QColor(248, 250, 253, 240))
        self._paint_content(p, content)
        p.restore()
        # 标题: 左侧红黄绿
        for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            p.setPen(QPen(QColor(0, 0, 0, 40), 0.8))
            p.setBrush(QColor(c))
            p.drawEllipse(QRectF(16 + i * 19, self.TITLE_H / 2 - 6, 12, 12))
        set_font(p, 10, True, "#333")
        p.drawText(rect.adjusted(84, 0, -140, -(rect.height() - self.TITLE_H)),
                   Qt.AlignVCenter | Qt.AlignLeft, f"{self.app['emoji']} {self.app['name']}")
        # 右侧显式按钮(纯图标)
        for r, glyph, bg, fg in ((QRectF(self.width() - 100, 8, 42, 24), "–", QColor(255, 255, 255, 165), "#333"),
                                 (QRectF(self.width() - 54, 8, 42, 24), "✕", QColor(255, 120, 110, 185), "#5a1010")):
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 12, 12)
            set_font(p, 10, True, fg)
            p.drawText(r, Qt.AlignCenter, glyph)

    def _paint_content(self, p, c):
        if self.key == "browser":
            return
        if self.key == "settings":
            set_font(p, 14, True, "#222")
            p.drawText(int(c.left()) + 22, int(c.top()) + 30, "鼠标键盘")
            set_font(p, 11, True, "#555")
            p.drawText(int(c.left()) + 22, int(c.top()) + 66, "鼠标效果")
            set_font(p, 9, color="#777")
            p.drawText(int(c.left()) + 22, int(c.top()) + 180, "辉光颜色")
            p.drawText(int(c.left()) + 22, int(c.top()) + 244, "拖尾颜色（光带模式）")
            p.drawText(int(c.left()) + 22, int(c.top()) + 304,
                       "光带宽度：" + str(self.shell.mouse_cfg.get("size", 8)) + " px")
            p.drawText(int(c.left()) + 22, int(c.top()) + 366,
                       "跟随灵敏度：" + str(self.shell.mouse_cfg.get("smooth", 10)) + "（越大越跟手）")
            return
        if self.key == "intent":
            p.fillRect(c, QColor(244, 246, 252, 250))
            y = c.top() + 14
            for role, text in self.messages[-7:]:
                set_font(p, 10, color="#333")
                fm = p.fontMetrics()
                tw = min(int(fm.horizontalAdvance(text)) + 24, int(c.width()) - 120)
                bh = 36
                if role == "user":
                    br = QRectF(c.right() - 14 - tw, y, tw, bh)
                    p.setPen(Qt.NoPen); p.setBrush(QColor(90, 120, 255, 235))
                else:
                    br = QRectF(c.left() + 14, y, tw, bh)
                    p.setPen(Qt.NoPen); p.setBrush(QColor(255, 255, 255, 250))
                p.drawRoundedRect(br, 10, 10)
                p.setPen(QColor("#222" if role == "user" else "#333"))
                p.setFont(QFont("Microsoft YaHei UI", 10))
                p.drawText(br.adjusted(10, 0, -10, 0), Qt.AlignVCenter | Qt.TextWordWrap, text)
                y += bh + 10
        elif self.key == "terminal":
            p.fillRect(c, QColor(24, 26, 32))
            lines = [("QiDianOS skeleton v0.2 — 终端", "#9ad1ff"),
                     ("qidians# uname -a", "#e6e6e6"),
                     ("Linux qidians 6.8.0-138-generic #x86_64", "#c8c8c8"),
                     ("qidians# echo Intent\\ T", "#e6e6e6"),
                     ("Intent T", "#8ee6b8")]
            y = c.top() + 18
            for text, color in lines:
                set_font(p, 10, color=color)
                p.drawText(int(c.left()) + 16, int(y), text)
                y += 22
            set_font(p, 10, color="#e6e6e6")
            p.drawText(int(c.left()) + 16, int(y), "qidians# " + ("_" if self.blink else " "))
        elif self.key == "files":
            rows = [("🏠", "主目录"), ("📄", "文档"), ("🖼️", "图片"), ("🎵", "音乐"),
                    ("🖥️", "系统"), ("🚮", "回收站")]
            y = c.top() + 16
            for emoji, name in rows:
                set_font(p, 11, color="#333")
                p.drawText(int(c.left()) + 18, int(y), emoji + "  " + name)
                y += 30
        else:
            set_font(p, 16, True, "#222")
            p.drawText(int(c.left()) + 22, int(c.top()) + 44, "起点 OS (QiDianOS)")
            set_font(p, 10, color="#555")
            for i, line in enumerate([
                "v0.2 skeleton · QiDian Glass 玻璃设计语言",
                "工作台: Intent T (bundled, sync pending)",
                "AI 运行时: llama.cpp 原生集成 (阶段二)",
                "权限: 用户级管理员 + 三级门控 + 审计",
                "Win 键 = 呼出 Intent T · Ctrl+空格 = 语音指令",
            ]):
                p.drawText(int(c.left()) + 22, int(c.top()) + 78 + i * 26, line)


# ------------------------------------------------------------
# 抽屉（右键固定到 Dock）
# ------------------------------------------------------------
class AppTile(QWidget):
    def __init__(self, app, shell):
        super().__init__()
        self.app = app
        self.shell = shell
        self.hover = False
        self.mouse_pos = None
        self.zoom = 1.0
        self.zoom_target = 1.0
        self.setFixedSize(104, 116)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def enterEvent(self, _):
        self.hover = True
        self.zoom_target = 1.14
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.zoom_target = 1.0
        self.mouse_pos = None
        self.update()

    def mouseMoveEvent(self, e):
        self.mouse_pos = e.position().toPoint()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.shell.toggle_pin(self.app["key"])
        elif e.button() == Qt.LeftButton:
            self.shell.launch(self.app["key"])
            self.shell.toggle_drawer(False)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        running = self.app["key"] in self.shell.windows
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(rect, 18, 18)
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, 18, 18)
        pinned = self.app["key"] in self.shell.pinned
        if pinned:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(90, 120, 255, 230))
            p.drawRoundedRect(rect.right() - 18, rect.top() + 6, 12, 12, 6, 6)
        # 未启动的应用: 整体压暗
        if not running:
            p.setOpacity(0.55)
        p.setPen(QColor(35, 35, 40))
        p.setFont(QFont("Segoe UI Emoji", int(30 * self.zoom)))
        p.drawText(rect.adjusted(0, 8, 0, -34), Qt.AlignHCenter | Qt.AlignVCenter, self.app["emoji"])
        set_font(p, 10, color="#f2f2f2" if running else "#c9c9c9")
        p.drawText(rect.adjusted(0, 0, 0, -10), Qt.AlignHCenter | Qt.AlignBottom, self.app["name"])
        p.setOpacity(1.0)
        # BorderGlow: 靠近未启动应用的边框即触发(不必进入内部), 亮度随距离衰减
        if not running:
            gp = self.mapFromGlobal(QCursor.pos())
            dx = max(rect.left() - gp.x(), 0.0, gp.x() - rect.right())
            dy = max(rect.top() - gp.y(), 0.0, gp.y() - rect.bottom())
            dist = math.hypot(dx, dy)
            reach = 64.0
            if dist <= reach:
                paint_border_glow(p, rect, 18, QPointF(gp), intensity=1.0 - dist / reach)


class Drawer(QWidget):
    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("🔍  搜索应用   （左键启动 · 右键固定/取消固定到 Dock）")
        self.search.setFixedSize(420, 40)
        self.search.setAlignment(Qt.AlignCenter)
        self.search.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,60);border:1px solid rgba(255,255,255,110);"
            "border-radius:20px;color:white;font-size:13px;padding:0 16px;}"
            "QLineEdit:focus{background:rgba(255,255,255,90);}")
        self.search.textChanged.connect(self._filter)
        self.grid_holder = QWidget(self)
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(18)
        self.tiles = []
        for i, key in enumerate(DRAWER_ORDER):
            t = AppTile(APPS_BY_KEY[key], shell)
            self.tiles.append(t)
            self.grid.addWidget(t, i // 8, i % 8)
        self.grid_holder.adjustSize()
        self.hide()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._proximity_tick)
        self._timer.start(40)

    def _proximity_tick(self):
        if not self.isVisible():
            return
        for t in self.tiles:
            if abs(t.zoom - t.zoom_target) > 0.002:
                t.zoom += (t.zoom_target - t.zoom) * 0.28
                t.update()
            else:
                t.update()

    def _filter(self, text):
        text = text.strip().lower()
        for t in self.tiles:
            t.setVisible(text in t.app["name"].lower())

    def toggle(self):
        if self.isVisible():
            self.hide()
            self.shell.setFocus()
        else:
            self.search.clear()
            W, H = self.shell.width(), self.shell.height()
            self.setGeometry(0, 0, W, H)
            self.search.move((W - self.search.width()) // 2, 64)
            self.grid_holder.move((W - self.grid_holder.width()) // 2,
                                  (H - self.grid_holder.height()) // 2 + 30)
            self.raise_()
            self.show()
            self.search.setFocus()
            effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(160)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start(QPropertyAnimation.DeleteWhenStopped)

    def mousePressEvent(self, e):
        pos = e.position().toPoint()
        inside = self.grid_holder.geometry().adjusted(-40, -60, 40, 40).contains(pos) or \
            self.search.geometry().adjusted(-20, -20, 20, 20).contains(pos)
        if not inside:
            self.shell.toggle_drawer(False)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap(0, 0, QPixmap.fromImage(self.shell.blur_drawer))
        p.fillRect(self.rect(), QColor(10, 12, 24, 90))


# ------------------------------------------------------------
# 目录导图式文件浏览器（根目录置顶、虚线向下连接、点击展开）
# ------------------------------------------------------------
class MindMapNode:
    def __init__(self, path, name, is_dir, parent=None):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.parent = parent
        self.children = []
        self.expanded = False
        self.loaded = False
        self.rect = QRectF()

    def load(self):
        if self.loaded or not self.is_dir:
            return
        self.loaded = True
        try:
            items = sorted(os.listdir(self.path))
        except OSError:
            return
        for name in items:
            if name.startswith('.'):
                continue
            full = os.path.join(self.path, name)
            self.children.append(MindMapNode(full, name, os.path.isdir(full), self))


class MindMapFileBrowser(QWidget):
    """左侧目录导图：根目录置顶，虚线向下连接，点击目录展开/收起，单击文件打开。"""
    MARGIN = 14
    INDENT = 26
    ROW_H = 30
    NODE_H = 22
    LINE = QColor(118, 132, 172, 200)
    TEXT = QColor(232, 234, 240)
    TEXT_DIM = QColor(139, 147, 173)
    NODE_BG = QColor(27, 30, 42, 220)
    NODE_BG_OPEN = QColor(47, 53, 80, 220)
    NODE_EDGE = QColor(58, 64, 92, 220)
    NODE_EDGE_HL = QColor(74, 144, 194, 220)
    FILE_BG = QColor(22, 24, 32, 200)
    FILE_EDGE = QColor(42, 46, 66, 200)
    MARK = QColor(143, 208, 255)

    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(300)
        self.root_path = self._discover_root()
        self.root = MindMapNode(self.root_path, self._label(self.root_path), True)
        self.root.expanded = True
        self.root.load()
        self.setMouseTracking(True)
        self.hover_node = None
        self._layout()

    def _discover_root(self):
        candidates = ["/奇点OS", "/"]
        for p in candidates:
            if os.path.isdir(p):
                return p
        return os.path.expanduser("~")

    def _label(self, path):
        if path == "/奇点OS":
            return "奇点OS根"
        if path == "/":
            return "Linux 底座"
        return os.path.basename(path.rstrip("/")) or "/"

    def _all_nodes(self):
        stack = [self.root]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(n.children)

    def _layout(self):
        self._layout_node(self.root, 0, self.MARGIN)
        h = int(max((self.root.rect.bottom() if self.root.rect.height() else self.MARGIN) + self.MARGIN,
                    self.shell.height() - 160))
        self.setFixedHeight(h)
        self.update()

    def _layout_node(self, node, depth, y):
        fm = QFontMetrics(QFont("Microsoft YaHei UI", 11))
        w = min(fm.horizontalAdvance(node.name) + 46, 220 - depth * self.INDENT)
        node.rect = QRectF(self.MARGIN + depth * self.INDENT, y, max(w, 84), self.NODE_H)
        y += self.ROW_H
        if node.expanded and node.children:
            for c in node.children:
                y = self._layout_node(c, depth + 1, y)
        return y

    def _hit(self, pos):
        for n in self._all_nodes():
            if n.rect.contains(pos):
                return n
        return None

    def mouseMoveEvent(self, e):
        node = self._hit(e.position())
        if node != self.hover_node:
            self.hover_node = node
            self.setCursor(Qt.PointingHandCursor if node and node.is_dir else Qt.ArrowCursor)
            self.update()

    def mousePressEvent(self, e):
        node = self._hit(e.position())
        if not node:
            return
        if node.is_dir:
            node.expanded = not node.expanded
            if node.expanded:
                node.load()
            self._layout()
        else:
            self._open_file(node.path)

    def _open_file(self, path):
        if sys.platform == "win32":
            os.startfile(path)
        else:
            try:
                subprocess.Popen(["xdg-open", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def _draw_dashed_line(self, p, x1, y1, x2, y2):
        pen = QPen(self.LINE, 1.5)
        pen.setDashPattern([5, 3])
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 背景玻璃条
        paint_glass(p, self, self.shell.blur_dock, 0,
                    top_alpha=80, bottom_alpha=60, border_alpha=20)
        # 先画虚线
        for node in self._all_nodes():
            if node.children and node.expanded:
                px = node.rect.right()
                py = node.rect.center().y()
                cx = node.rect.left() + self.INDENT - 6
                children = [c for c in node.children]
                if not children:
                    continue
                last_y = children[-1].rect.center().y()
                self._draw_dashed_line(p, px, py, cx, py)
                self._draw_dashed_line(p, cx, py, cx, last_y)
                for c in children:
                    cy = c.rect.center().y()
                    self._draw_dashed_line(p, cx, cy, c.rect.left() - 4, cy)
        # 再画节点
        for node in self._all_nodes():
            self._draw_node(p, node)

    def _draw_node(self, p, node):
        r = node.rect
        is_open = node.expanded and node.children
        is_root = node is self.root
        is_hover = (node == self.hover_node)
        if not node.is_dir:
            p.setPen(QPen(self.FILE_EDGE, 1))
            p.setBrush(self.FILE_BG)
        elif is_root:
            p.setPen(QPen(self.NODE_EDGE_HL, 2))
            p.setBrush(QColor(36, 42, 66, 230))
        elif is_open:
            p.setPen(QPen(self.NODE_EDGE_HL, 1))
            p.setBrush(self.NODE_BG_OPEN)
        else:
            p.setPen(QPen(self.NODE_EDGE, 1))
            p.setBrush(self.NODE_BG)
        if is_hover:
            p.setPen(QPen(self.NODE_EDGE_HL, 1.5))
        p.drawRoundedRect(r, 6, 6)
        color = self.TEXT if node.is_dir or is_root else self.TEXT_DIM
        set_font(p, 10, bold=is_root, color=color.name())
        p.drawText(r.adjusted(8, 0, -20, 0), Qt.AlignVCenter | Qt.AlignLeft, node.name)
        if node.is_dir:
            mark = "▾" if is_open else "▸"
            set_font(p, 9, color=self.MARK.name())
            p.drawText(r.adjusted(-18, 0, -4, 0), Qt.AlignVCenter | Qt.AlignRight, mark)

    def reposition(self):
        self._layout()
        self.move(10, 70)


# ------------------------------------------------------------
# Dock（默认空 · 抽屉右键固定 · 始终居中）
# ------------------------------------------------------------
class Dock(QWidget):
    """快捷启动栏: 左侧九宫格抽屉入口 + 固定应用图标，始终居中。"""
    SLOT = 58
    BAR_H = 60
    MARGIN_V = 14
    RADIUS = 30
    GRID_W = 52

    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.hover_x = None
        self.resize(20, self.BAR_H + self.MARGIN_V + 10)

    def reposition(self):
        s = self.shell
        self.move((s.width() - self.width()) // 2, s.height() - self.height() - 4)

    def _total_width(self):
        return self.GRID_W + 12 + len(self.shell.pinned) * self.SLOT + 20

    def refresh(self):
        self.show()
        self.resize(self._total_width(), self.BAR_H + self.MARGIN_V + 10)
        self.reposition()
        self.update()

    def mouseMoveEvent(self, e):
        self.hover_x = e.position().x()
        self.update()

    def leaveEvent(self, _):
        self.hover_x = None
        self.update()

    def mousePressEvent(self, e):
        x = e.position().x()
        # 左侧九宫格区域打开抽屉
        if 8 <= x <= 8 + self.GRID_W:
            self.shell.toggle_drawer()
            return
        idx = int((x - 10 - self.GRID_W - 8) // self.SLOT)
        if 0 <= idx < len(self.shell.pinned):
            self.shell.launch(self.shell.pinned[idx])

    def _scale_at(self, cx):
        if self.hover_x is None:
            return 1.0
        d = abs(cx - self.hover_x)
        if d >= 88:
            return 1.0
        return 1.0 + 0.30 * (1.0 - d / 88) ** 1.5

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bar = QRectF(0, self.MARGIN_V, self.width(), self.BAR_H)
        draw_shadow(p, self, self.RADIUS)
        paint_glass(p, self, self.shell.blur_dock, self.RADIUS,
                    top_alpha=210, bottom_alpha=160)
        gm = self.mapFromGlobal(QCursor.pos())
        # 左侧九宫格入口
        grid_rect = QRectF(8, self.MARGIN_V + 8, self.GRID_W, self.BAR_H - 16)
        hover_grid = grid_rect.contains(gm)
        p.setPen(QPen(QColor(255, 255, 255, 110 if hover_grid else 70), 1))
        p.setBrush(QColor(255, 255, 255, 30 if hover_grid else 12))
        p.drawRoundedRect(grid_rect, 12, 12)
        p.setPen(QPen(QColor(255, 255, 255, 200), 2))
        gap = 8
        cell = 8
        gx = grid_rect.center().x() - cell * 1.5 - gap
        gy = grid_rect.center().y() - cell * 1.5 - gap
        for row in range(3):
            for col in range(3):
                p.drawRoundedRect(gx + col * (cell + gap), gy + row * (cell + gap), cell, cell, 1.5, 1.5)
        # 分隔线
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        sep_x = 8 + self.GRID_W + 4
        p.drawLine(int(sep_x), int(self.MARGIN_V + 12), int(sep_x), int(self.MARGIN_V + self.BAR_H - 12))
        # 固定图标
        x0 = sep_x + 12
        for i, key in enumerate(self.shell.pinned):
            app = APPS_BY_KEY[key]
            cx = x0 + i * self.SLOT + self.SLOT / 2
            center = QRectF(cx - self.SLOT / 2, self.MARGIN_V, self.SLOT, self.BAR_H).center()
            scale = self._scale_at(cx)
            size = 44 * scale
            tile = QRectF(cx - size / 2, center.y() - size / 2, size, size)
            running = key in self.shell.windows
            if not running:
                p.setOpacity(0.55)
            p.setPen(QPen(QColor(0, 0, 0, 25), 1))
            p.setBrush(QColor(255, 255, 255, 215))
            p.drawRoundedRect(tile, 11 * scale, 11 * scale)
            p.setPen(QColor(35, 35, 40))
            p.setFont(QFont("Segoe UI Emoji", int(19 * scale)))
            p.drawText(tile, Qt.AlignCenter, app["emoji"])
            p.setOpacity(1.0)
            if running:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(40, 40, 50, 190))
                p.drawEllipse(QRectF(cx - 2.5, bar.bottom() - 7, 5, 5))
            else:
                dx = max(tile.left() - gm.x(), 0.0, gm.x() - tile.right())
                dy = max(tile.top() - gm.y(), 0.0, gm.y() - tile.bottom())
                dist = math.hypot(dx, dy)
                if dist <= 48.0:
                    mx = min(max(gm.x(), tile.left() + 3), tile.right() - 3)
                    my = min(max(gm.y(), tile.top() + 3), tile.bottom() - 3)
                    paint_border_glow(p, tile, 11 * scale, QPointF(mx, my),
                                      intensity=1.0 - dist / 48.0)


class DynamicIsland(QWidget):
    """顶部灵动岛：胶囊形态，默认显示状态，收到消息时展开提示，点击展开消息列表。"""
    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.mode = "status"          # status / message / expanded
        self.messages = []            # (title, body, time)
        self.current_msg = None
        self.msg_until = 0.0
        self.hover = False
        self.setFixedSize(200, 36)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(200)

    def _tick(self):
        if self.mode == "message" and time.time() > self.msg_until and not self.hover:
            self.mode = "status"
            self._resize_for_mode()
        self.reposition()
        self.update()

    def _resize_for_mode(self):
        if self.mode == "expanded":
            self.setFixedSize(320, 180)
        elif self.mode == "message":
            self.setFixedSize(280, 44)
        else:
            self.setFixedSize(200, 36)
        self.reposition()
        self.update()

    def reposition(self):
        self.move((self.shell.width() - self.width()) // 2, 12)

    def show_message(self, title, body=""):
        self.messages.insert(0, (title, body, time.time()))
        self.messages = self.messages[:8]
        self.current_msg = (title, body)
        self.mode = "message"
        self.msg_until = time.time() + 3.5
        self._resize_for_mode()
        self.raise_()

    def enterEvent(self, _):
        self.hover = True
        if self.mode == "message":
            self.mode = "expanded"
            self._resize_for_mode()

    def leaveEvent(self, _):
        self.hover = False
        if self.mode == "expanded":
            self.mode = "status"
            self._resize_for_mode()

    def mousePressEvent(self, e):
        if self.mode == "status":
            self.mode = "expanded"
        else:
            self.mode = "status"
        self._resize_for_mode()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        paint_glass(p, self, self.shell.blur_dock, radius, top_alpha=210, bottom_alpha=160)
        if self.mode == "message" and self.current_msg:
            title, _ = self.current_msg
            set_font(p, 10, True, "#222")
            p.drawText(rect.adjusted(16, 0, -16, 0), Qt.AlignVCenter | Qt.AlignLeft, f"🔔 {title}")
        elif self.mode == "expanded":
            set_font(p, 10, True, "#222")
            p.drawText(rect.adjusted(16, 12, -16, 0), Qt.AlignLeft, "灵动岛 · 最近消息")
            set_font(p, 9, color="#444")
            y = 38
            if not self.messages:
                p.drawText(rect.adjusted(16, y, -16, 0), Qt.AlignLeft, "暂无新消息")
            for title, body, _ in self.messages[:5]:
                p.drawText(rect.adjusted(16, y, -16, 0), Qt.AlignLeft, f"• {title}")
                y += 22
        else:
            st = self.shell.voice_state
            if st:
                dur = time.time() - st["start"]
                a = 150 + int(100 * abs(math.sin(time.time() * 5)))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 70, 60, a))
                p.drawEllipse(QRectF(rect.left() + 14, rect.center().y() - 5, 10, 10))
                set_font(p, 9, True, "#7a1010")
                tag = "持续录音" if st["mode"] == "cont" else "松开发送"
                p.drawText(rect.adjusted(28, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft, f"{tag} {dur:.1f}s")
            else:
                now = datetime.datetime.now()
                set_font(p, 10, True, "#333")
                p.drawText(rect, Qt.AlignCenter, f"{now:%H:%M}")


class Toast(QWidget):
    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.text = ""
        self.setFixedHeight(44)
        self.hide()

    def show_msg(self, msg):
        self.text = msg
        self.setFixedWidth(60 + len(msg) * 11)
        self.reposition()
        self.show()
        self.raise_()
        QTimer.singleShot(1700, self.hide)

    def reposition(self):
        s = self.shell
        self.move((s.width() - self.width()) // 2, s.height() - 210)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_shadow(p, self, 19)
        paint_glass(p, self, self.shell.blur_dock, 19, top_alpha=90, bottom_alpha=70)
        set_font(p, 10, color="#f0f0f0")
        p.drawText(QRectF(self.rect()), Qt.AlignCenter, "ℹ️  " + self.text)


class VoiceBar(QWidget):
    """Ctrl+空格 唤起的语音转文字指令条（原型 UI；端侧 ASR 用 sherpa-onnx）。"""
    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.pulse = 0
        self.setFixedSize(560, 120)
        self.box = QLineEdit(self)
        self.box.setPlaceholderText("对 Intent T 说出指令…（Enter 发送 · 原型可键入模拟转写）")
        self.box.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,210);border:none;border-radius:10px;"
            "padding:0 12px;font-size:13px;}")
        self.box.returnPressed.connect(self._send)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def _tick(self):
        self.pulse = (self.pulse + 1) % 30
        self.update()

    def _send(self):
        text = self.box.text().strip()
        if not text:
            return
        self.hide()
        self.box.clear()
        self.shell.launch("intent")
        w = self.shell.windows.get("intent")
        if w:
            QTimer.singleShot(120, lambda: (w.messages.append(("user", "🎤 " + text)),
                                            w.update(),
                                            QTimer.singleShot(500, lambda: (w.messages.append(
                                                ("ai", f"已执行语音指令：「{text}」。（正式版由端侧 sherpa-onnx 转写 → llama.cpp 执行）")),
                                                w.update()))))

    def popup(self):
        W = self.shell.width()
        self.move((W - self.width()) // 2, 96)
        self.box.setGeometry(74, 60, self.width() - 92, 38)
        self.raise_()
        self.show()
        self.box.setFocus()
        self._timer.start(50)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_shadow(p, self, 20)
        paint_glass(p, self, self.shell.blur_dock, 20, top_alpha=225, bottom_alpha=185)
        # 麦克风脉冲
        cx, cy = 40, 60
        a = 90 + int(80 * abs(math.sin(self.pulse / 30 * math.pi)))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(90, 120, 255, a))
        p.drawEllipse(QRectF(cx - 16 - self.pulse * 0.2, cy - 16 - self.pulse * 0.2,
                             32 + self.pulse * 0.4, 32 + self.pulse * 0.4))
        p.setBrush(QColor(90, 120, 255, 235))
        p.drawEllipse(QRectF(cx - 16, cy - 16, 32, 32))
        set_font(p, 16)
        p.drawText(QRectF(cx - 16, cy - 16, 32, 32), Qt.AlignCenter, "🎤")
        set_font(p, 11, True, "#333")
        p.drawText(QRectF(70, 22, self.width() - 90, 24), Qt.AlignLeft | Qt.AlignVCenter,
                   "语音指令 · 语音转文字（端侧 ASR）")


# ------------------------------------------------------------
# Shell 主窗口（含顶部最小化窗口架）
# ------------------------------------------------------------
class SnapPreview(QWidget):
    """拖拽分屏时的目标区域玻璃预览。"""
    def __init__(self, shell):
        super().__init__(shell)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.rect_target = None
        self.hide()

    def set_rect(self, r):
        self.rect_target = QRectF(r)
        self.update()

    def paintEvent(self, _):
        if not self.rect_target:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect_target, 14, 14)
        p.save()
        p.setClipPath(path)
        p.drawImage(-self.geometry().topLeft(), self.shell.blur_dock)
        p.fillRect(self.rect_target, QColor(255, 255, 255, 70))
        p.restore()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 160), 1.5))
        p.drawPath(path)


class ShellWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.setGeometry(screen)

        self.wallpaper = build_wallpaper(screen.size())
        self.blur_dock = blurred_image(self.wallpaper, 28)
        self.blur_drawer = self._darkened(blurred_image(self.wallpaper, 40))

        self.cfg = load_shell_config()
        self.mouse_cfg = self.cfg.setdefault(
            "mouse", {"enabled": True, "mode": "beam",
                      "color": "#67E8F9", "secondary": "#A78BFA",
                      "size": 8, "smooth": 10})
        try:
            if float(self.mouse_cfg.get("size", 8)) > 20:
                self.mouse_cfg["size"] = 8
        except (TypeError, ValueError):
            self.mouse_cfg["size"] = 8
        self.windows = {}
        self.minimized = []          # 最小化窗口，按顺序编号
        # pinned 来自配置文件（默认空）
        self._last_digit = None
        self._last_digit_at = 0.0
        # 语音手势状态: None / {"mode":"hold"|"cont","start":t}
        self.voice_state = None
        self._vs_last_press = 0.0
        self._vs_hold_timer = QTimer(self)
        self._vs_hold_timer.setSingleShot(True)
        self._vs_hold_timer.timeout.connect(self._voice_hold_released)

        self.drawer = Drawer(self)
        self.dock = Dock(self)
        self.status = DynamicIsland(self)
        self.file_browser = MindMapFileBrowser(self)
        self.toast = Toast(self)
        self.voice = VoiceBar(self)
        self.pinned = list(self.cfg.get("pinned", []))
        self.dock.refresh()
        self.status.reposition()
        self.file_browser.reposition()
        self.file_browser.show()
        self.file_browser.raise_()
        self.snap_preview = SnapPreview(self)
        self.glow = GlowOverlay(self.screen().availableGeometry())
        self.glow_gl = GlowOverlayGL(self.screen().availableGeometry()) if GL_OK else None
        if self.mouse_cfg.get("mode", "beam") == "beam" and self.glow_gl:
            self.glow_gl.show()
        else:
            self.glow.show()
        QTimer.singleShot(800, lambda: self.status.show_message("奇点 OS 已就绪", "灵动岛、Dock、目录导图浏览器已集成"))

    def update_overlays(self):
        mode = self.mouse_cfg.get("mode", "beam")
        if mode == "beam" and self.glow_gl:
            self.glow.hide()
            self.glow_gl.show()
            self.glow_gl.raise_()
        else:
            if self.glow_gl:
                self.glow_gl.hide()
            self.glow.show()
            self.glow.raise_()

        QShortcut(QKeySequence(Qt.Key_Escape), self,
                  activated=lambda: (self.toggle_drawer(False), self.voice.hide()))
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self.voice.popup)
        QShortcut(QKeySequence(Qt.Key_Meta), self, activated=lambda: self.launch("intent"))

    def _darkened(self, img):
        out = QImage(img)
        p = QPainter(out)
        p.fillRect(out.rect(), QColor(8, 10, 22, 95))
        p.end()
        return out

    # ---- 窗口管理 ----
    def launch(self, key):
        app = APPS_BY_KEY.get(key)
        if app is None:
            return
        if key in self.windows:
            w = self.windows[key]
            if w.minimized or not w.isVisible():
                self.restore_window(w)
            else:
                w.raise_()
                w.update()
            return
        if key in REAL_WINDOWS:
            w = GlassWindow(self, key)
            self.windows[key] = w
            w.show()
            w.raise_()
            if key == "browser":
                QTimer.singleShot(120, lambda: (w.urlbar.setFocus(),
                                                w.urlbar.selectAll()))
        else:
            self.toast.show_msg(f"「{app['name']}」原型未包含 — 阶段二随 Intent T 一起进镜像")

    def window_minimized(self, w):
        if w not in self.minimized:
            self.minimized.append(w)
        self.setFocus()
        self.update()

    def restore_window(self, w):
        if w in self.minimized:
            self.minimized.remove(w)
        w.minimized = False
        w.show()
        w.raise_()
        w.update()
        self.update()

    def refresh_top(self):
        self.update()

    # ---- 分屏吸附 ----
    def zone_for(self, gp):
        w, h = self.width(), self.height()
        m = 14
        top = gp.y() <= m
        bottom = gp.y() >= h - m
        left = gp.x() <= m
        right = gp.x() >= w - m
        if top and left:
            return "tl"
        if top and right:
            return "tr"
        if bottom and left:
            return "bl"
        if bottom and right:
            return "br"
        if top:
            return "max"
        if left:
            return "left"
        if right:
            return "right"
        return None

    def zone_rect(self, zone):
        w, h = self.width(), self.height()
        m = 6
        hw = (w - 3 * m) // 2
        hh = (h - 2 * m) // 2
        if zone == "max":
            return QRectF(m, m, w - 2 * m, h - 2 * m)
        if zone == "left":
            return QRectF(m, m, hw, h - 2 * m)
        if zone == "right":
            return QRectF(w - m - hw, m, hw, h - 2 * m)
        if zone == "tl":
            return QRectF(m, m, hw, hh)
        if zone == "tr":
            return QRectF(w - m - hw, m, hw, hh)
        if zone == "bl":
            return QRectF(m, h - m - hh, hw, hh)
        if zone == "br":
            return QRectF(w - m - hw, h - m - hh, hw, hh)
        return QRectF()

    def show_snap_preview(self, zone):
        r = self.zone_rect(zone) if zone else None
        if r.width() < 10:
            self.snap_preview.hide()
            return
        self.snap_preview.setGeometry(self.rect())
        self.snap_preview.set_rect(r)
        self.snap_preview.show()
        self.snap_preview.raise_()

    def hide_snap_preview(self):
        self.snap_preview.hide()

    def apply_snap(self, win, zone):
        r = self.zone_rect(zone)
        if r.width() < 10:
            return
        if not win.maximized:
            win._saved = win.geometry()
        win.setGeometry(r.toRect())
        win.maximized = (zone == "max")
        win.show()
        win.raise_()
        win.update()

    def toggle_pin(self, key):
        if key in self.pinned:
            self.pinned.remove(key)
            self.toast.show_msg(f"已从 Dock 取消固定「{APPS_BY_KEY[key]['name']}」")
        else:
            self.pinned.append(key)
            self.toast.show_msg(f"已固定「{APPS_BY_KEY[key]['name']}」到 Dock")
        self.cfg["pinned"] = list(self.pinned)
        save_shell_config(self.cfg)
        self.dock.refresh()

    def toggle_drawer(self, force=None):
        visible = self.drawer.isVisible()
        if force is None:
            self.drawer.toggle()
        elif force and not visible:
            self.drawer.toggle()
        elif not force and visible:
            self.drawer.hide()
            self.setFocus()

    # ---- 最小化架(顶部) ----
    def orb_rects(self):
        out = []
        x = self.file_browser.width() + 24 if hasattr(self, "file_browser") else 14
        for i, w in enumerate(self.minimized):
            orb = QRectF(x, 10, 38, 38)
            num = QRectF(x, 50, 38, 18)
            hit = QRectF(x - 4, 8, 46, 62)
            out.append((w, orb, num, hit))
            x += 56
        return out

    def handle_space_press(self):
        now = time.time()
        if self.voice_state and self.voice_state["mode"] == "cont":
            if now - self._vs_last_press < 0.45:
                self._voice_send(self.voice_state["start"])
                self.voice_state = None
            else:
                self._vs_last_press = now
            self.update()
            return
        if self._vs_hold_timer.isActive():      # 双击的第二击 -> 持续录音
            self._vs_hold_timer.stop()
            if not self.windows:                 # 空桌面: 双击空格 = 开关抽屉
                self.voice_state = None
                self.toggle_drawer()
                return
            self.voice_state = {"mode": "cont", "start": time.time()}
        else:
            self.voice_state = {"mode": "hold", "start": time.time()}
        self.update()

    def handle_space_release(self):
        if self.voice_state and self.voice_state["mode"] == "hold":
            self._vs_hold_timer.start(350)       # 延迟判定: 期间再按=转持续录音

    def _voice_hold_released(self):
        if self.voice_state and self.voice_state["mode"] == "hold":
            self._voice_send(self.voice_state["start"])
            self.voice_state = None
            self.update()

    def _voice_send(self, start):
        dur = time.time() - start
        self.launch("intent")
        w = self.windows.get("intent")

        def go():
            if not w:
                return
            w.messages.append(("user", f"🎤 语音消息 ({dur:.1f}s)"))
            w.update()

            def reply():
                w.messages.append(("ai", "已收到语音指令。（正式版：端侧 sherpa-onnx 转写 → llama.cpp 执行）"))
                w.update()
            QTimer.singleShot(500, reply)
        QTimer.singleShot(150, go)
        self.toast.show_msg(f"语音已发送（{dur:.1f}s）")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self.handle_space_press(); return
        text = e.text()
        if text and text in "123456789":
            now = time.time()
            if self._last_digit == text and now - self._last_digit_at < 0.45:
                idx = int(text) - 1
                if 0 <= idx < len(self.minimized):
                    self.restore_window(self.minimized[idx])
                self._last_digit = None
            else:
                self._last_digit = text
                self._last_digit_at = now
                n = len(self.minimized)
                if n:
                    idx = int(text) - 1
                    if idx < n:
                        self.toast.show_msg(f"再按一次 {text} 还原：「{self.minimized[idx].app['name']}」")
                    else:
                        self.toast.show_msg(f"顶部共 {n} 个最小化窗口，数字 1~{n} 可用")
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self.handle_space_release(); return
        super().keyReleaseEvent(e)

    def mousePressEvent(self, e):
        pass

    def mouseDoubleClickEvent(self, e):
        pos = e.position().toPoint()
        for w, orb, num, hit in self.orb_rects():
            if hit.contains(pos):
                self.restore_window(w)
                return
        self.toggle_drawer()          # 双击空桌面: 快捷开关抽屉

    def resizeEvent(self, e):
        self.dock.reposition()
        self.status.reposition()
        self.file_browser.reposition()
        self.toast.reposition()
        super().resizeEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawImage(0, 0, self.wallpaper)
        # 最小化浮窗: 圆形图标球(起点岛同高) + 下方序号, 双击还原
        for i, (w, orb, num, hit) in enumerate(self.orb_rects()):
            path = QPainterPath()
            path.addEllipse(orb)
            p.save()
            p.setClipPath(path)
            p.drawImage(-orb.topLeft().toPoint(), self.blur_dock)
            p.fillRect(orb, QColor(255, 255, 255, 150))
            p.restore()
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 40), 1))
            p.drawEllipse(orb)
            p.setPen(QPen(QColor(255, 255, 255, 130), 1))
            p.drawEllipse(orb)
            p.setPen(QColor(35, 35, 40))
            p.setFont(QFont("Segoe UI Emoji", 15))
            p.drawText(orb, Qt.AlignCenter, w.app["emoji"])
            set_font(p, 9, True, "#fff")
            p.setPen(QColor(0, 0, 0, 150))
            p.drawText(num.adjusted(1, 1, -1, 1), Qt.AlignCenter, str(i + 1))
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(num, Qt.AlignCenter, str(i + 1))



def main():
    global SHELL_REF
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")  # WebEngine 与 GL 层共存稳定性
    app = QApplication(sys.argv)
    app.setApplicationName("QiDian Shell")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    shell = ShellWindow()
    SHELL_REF = shell
    shell.showFullScreen()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
