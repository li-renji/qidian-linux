#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""奇点OS 文件查看器：查看 .qds / .qdai / .qdmeta 等文件内容
- QDSS 容器（magic SSDQ）：解析 256 字节头，显示元信息 + payload 文本/ELF 说明 + 内嵌配置
- 文本类（.qdai/.qdmeta/shebang）：直接显示全文
"""
import gi, struct, sys, os
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, Pango

MAGIC_QDSS = 0x51445353  # "SSDQ"

CSS = b"""
window { background-color: #14161f; color: #e8eaf0; }
textview { background-color: #14161f; color: #e8eaf0; font-family: 'Noto Sans Mono CJK SC', monospace; }
.meta { background-color: #1b1e2a; color: #8fd0ff; padding: 6px 10px; font-weight: bold; }
.pathbar { background-color: #1b1e2a; color: #aeb6cf; border: 1px solid #2a2e42; border-radius: 8px; padding: 6px 10px; }
.statusbar { background-color: #1b1e2a; color: #8b93ad; padding: 4px 12px; font-size: 12px; }
"""


def parse_qdss(data):
    """返回 QDSS 元信息 dict"""
    info = {}
    info['格式'] = 'QDSS 容器'
    info['头部版本'] = struct.unpack('<H', data[4:6])[0]
    info['格式版本'] = struct.unpack('<H', data[6:8])[0]
    info['名称'] = data[8:72].split(b'\x00')[0].decode('utf-8', 'replace') or '(无)'
    info['入口'] = data[72:136].split(b'\x00')[0].decode('utf-8', 'replace') or '(无)'
    arch = struct.unpack('<I', data[136:140])[0]
    info['架构'] = {1: 'x86_64', 0: 'aarch64'}.get(arch, '未知(%d)' % arch)
    info['bin_offset'] = struct.unpack('<I', data[140:144])[0]
    info['bin_size'] = struct.unpack('<Q', data[144:152])[0]
    info['config_offset'] = struct.unpack('<Q', data[152:160])[0]
    info['config_size'] = struct.unpack('<Q', data[160:168])[0]
    info['签名偏移'] = struct.unpack('<Q', data[168:176])[0]
    info['签名大小'] = struct.unpack('<Q', data[176:184])[0]
    info['权限'] = struct.unpack('<Q', data[184:192])[0]
    info['最低系统'] = struct.unpack('<I', data[192:196])[0]
    info['API 级别'] = struct.unpack('<I', data[196:200])[0]
    pt = struct.unpack('<I', data[200:204])[0]
    info['载荷类型'] = 'Python 脚本' if pt == 1 else ('ELF 二进制' if pt == 0 else '未知(%d)' % pt)
    return info


def classify(data):
    if len(data) >= 4 and struct.unpack('<I', data[:4])[0] == MAGIC_QDSS:
        return 'qdss'
    if data[:4] == b'\x7fELF':
        return 'elf'
    return 'text'


class QDViewer(Gtk.Window):
    def __init__(self, path):
        Gtk.Window.__init__(self, title='奇点OS 文件查看器')
        self.set_default_size(760, 560)
        self._build(path)

    def _build(self, path):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)

        # 顶部：文件名
        pathbar = Gtk.Entry()
        pathbar.get_style_context().add_class('pathbar')
        pathbar.set_text(path)
        pathbar.set_editable(False)
        box.pack_start(pathbar, False, False, 6)

        scroller = Gtk.ScrolledWindow()
        self.textview = Gtk.TextView()
        self.textview.set_wrap_mode(Gtk.WrapMode.NONE)
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(False)
        self.textview.modify_font(Pango.FontDescription('monospace 12'))
        scroller.add(self.textview)
        box.pack_start(scroller, True, True, 0)

        self.status = Gtk.Label(label='')
        self.status.get_style_context().add_class('statusbar')
        box.pack_start(self.status, False, False, 0)

        self._render(path)

    def _render(self, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception as e:
            self._set_text('无法读取：%s' % e)
            self.status.set_text('错误')
            return

        kind = classify(data)
        buf = self.textview.get_buffer()

        if kind == 'qdss':
            info = parse_qdss(data)
            lines = ['【QDSS 容器元信息】']
            for k, v in info.items():
                lines.append('%-10s  %s' % (k, v))
            lines.append('')
            lines.append('【载荷内容】')
            bo = info['bin_offset']
            bs = info['bin_size']
            payload = data[bo:bo + bs]
            if info['载荷类型'] == 'Python 脚本':
                try:
                    lines.append(payload.decode('utf-8', errors='replace'))
                except Exception:
                    lines.append('(无法解码，二进制 %d 字节)' % len(payload))
            else:
                lines.append('(ELF 二进制 %d 字节——请用 qd-run 执行)' % len(payload))
            co = info['config_offset']
            cs = info['config_size']
            if cs > 0:
                lines.append('')
                lines.append('【内嵌配置 (JSON)】')
                lines.append(data[co:co + cs].decode('utf-8', errors='replace'))
            self._set_text('\n'.join(lines))
            self.status.set_text('QDSS 容器 · %d 字节 · %s' % (len(data), info['名称']))

        elif kind == 'elf':
            self._set_text('【ELF 二进制】\n\n该文件是编译好的原生程序，共 %d 字节。\n'
                           '文本查看器不解析 ELF 内容，请用 qd-run 执行。' % len(data))
            self.status.set_text('ELF 二进制 · %d 字节' % len(data))

        else:
            try:
                text = data.decode('utf-8', errors='replace')
                self._set_text(text)
                self.status.set_text('文本文件 · %d 字节' % len(data))
            except Exception:
                self._set_text('(二进制文件，无法以文本显示，共 %d 字节)' % len(data))
                self.status.set_text('二进制 · %d 字节' % len(data))

    def _set_text(self, text):
        buf = self.textview.get_buffer()
        buf.set_text(text)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: qd_viewer.py <文件路径>')
        sys.exit(1)
    win = QDViewer(sys.argv[1])
    win.connect('destroy', Gtk.main_quit)
    win.show_all()
    Gtk.main()
