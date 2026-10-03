#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# ui_calc.py —— 奇点OS 计算器（G.18）
#
# 为什么必须有：Windows/macOS/GNOME/XFCE/SerenityOS/Haiku 全部自带
# 计算器（对标调研 v0.1 §2.2），这是"第一天必须成立"清单之外的
# 第二高频小工具。开源参照：mousepad 的"小而全"哲学——一个窗口
# 做完所有事，不做科学模式、不做历史记录云同步、不做单位换算。
#
# 功能（对标 Windows 计事本级极简）：
#   四则运算 + 百分号 + 括号 + 正负号 + 退格 + 清空
#   键盘全支持（数字/运算符/Enter/=/Esc/Backspace）
#   表达式直算（非"逐步计算"），显示当前表达式与结果
#   除零/语法错误给出行内提示，不崩溃
#
# 实现说明：求值走"中缀→AST→递归求值"自研解析器（约 60 行），
# 不用 eval()——桌面组件不能执行任意输入字符串，这是红线。
#
# 入口：python3 ui_calc.py
# ============================================================
import sys

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, Pango

sys.path.insert(0, '/奇点OS/运行')
import qd_ui_common as C

APP_TITLE = '计算器'


# ============================================================
# 表达式解析器（递归下降）
# 文法: expr := term (('+'|'-') term)*
#       term := factor (('*'|'/'|'%') factor)*
#       factor := ('-')? primary
#       primary := NUM | '(' expr ')' | NUM '%'
# ============================================================
class CalcError(Exception):
    pass


def tokenize(s):
    toks, i, n = [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch.isspace():
            i += 1
        elif ch in '0123456789.':
            j = i
            while j < n and (s[j] in '0123456789.'):
                j += 1
            toks.append(('num', s[i:j]))
            i = j
        elif ch in '+-*/%()':
            toks.append((ch, ch))
            i += 1
        else:
            raise CalcError('无法识别的字符: %s' % ch)
    return toks


class Parser:
    def __init__(self, toks):
        self.toks = toks
        self.pos = 0

    def peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def eat(self):
        t = self.peek()
        self.pos += 1
        return t

    def expr(self):
        v = self.term()
        while self.peek()[0] in ('+', '-'):
            op = self.eat()[0]
            r = self.term()
            v = v + r if op == '+' else v - r
        return v

    def term(self):
        v = self.factor()
        while self.peek()[0] in ('*', '/', '%'):
            op = self.eat()[0]
            r = self.factor()
            if op == '*':
                v = v * r
            elif op == '/':
                if r == 0:
                    raise CalcError('除数不能为 0')
                v = v / r
            else:
                if r == 0:
                    raise CalcError('除数不能为 0')
                v = v % r
        return v

    def factor(self):
        if self.peek()[0] == '-':
            self.eat()
            return -self.factor()
        return self.primary()

    def primary(self):
        t, v = self.eat()
        if t == 'num':
            try:
                val = float(v)
            except ValueError:
                raise CalcError('数字格式错误: %s' % v)
            # 百分号后缀：50% → 0.5（紧跟数字后的 % 视为 /100）
            if self.peek()[0] == '%':
                # 仅当 % 不再参与二元运算时视为后缀
                self.eat()
                return val / 100.0
            return val
        if t == '(':
            v = self.expr()
            if self.eat()[0] != ')':
                raise CalcError('括号不匹配')
            return v
        raise CalcError('表达式不完整')


def evaluate(expr):
    """求值入口：去掉 = 与空格 → 词法 → 语法 → 求值"""
    s = expr.replace('=', '').replace('×', '*').replace('÷', '/').strip()
    if not s:
        raise CalcError('请输入算式')
    p = Parser(tokenize(s))
    v = p.expr()
    if p.pos != len(p.toks):
        raise CalcError('表达式有多余内容')
    # 整数结果去掉 .0
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return ('%.10g' % v)


# ============================================================
class Calculator(Gtk.Window):
    KEY_ROWS = [
        [('C', 'clear'), ('⌫', 'back'), ('(', 'char'), (')', 'char')],
        [('7', 'char'), ('8', 'char'), ('9', 'char'), ('÷', 'char')],
        [('4', 'char'), ('5', 'char'), ('6', 'char'), ('×', 'char')],
        [('1', 'char'), ('2', 'char'), ('3', 'char'), ('−', 'char')],
        [('±', 'neg'), ('0', 'char'), ('.', 'char'), ('+', 'char')],
        [('=', 'eq', 2),],
    ]

    def __init__(self):
        super().__init__(title=APP_TITLE)
        self.set_default_size(320, 460)
        self.set_size_request(300, 420)

        C.setup_css(b'''
            .ca-root  { background: #16161e; }
            .ca-expr  { background: #1c1c1e; border: 1px solid #3a3a3c;
                        border-radius: 10px; padding: 10px 14px; }
            .ca-btn   { background: #1c1c1e; border: 1px solid #3a3a3c;
                        border-radius: 10px; font-size: 18px; color: #f5f5f7; }
            .ca-btn:hover { background: #3a3a3c; }
            .ca-op    { background: #2a2a2c; color: #2e8dff; }
            .ca-eq    { background: #2e8dff; color: #ffffff; border: none; }
            .ca-eq:hover { background: #4da2ff; }
        ''')

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.get_style_context().add_class('ca-root')
        root.set_border_width(12)
        self.add(root)

        # 显示区：上=表达式 下=结果
        self.expr_lbl = C.label('', 20, C.FG)
        self.expr_lbl.set_halign(Gtk.Align.END)
        self.expr_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.res_lbl = C.label('0', 32, C.FG, weight='bold')
        self.res_lbl.set_halign(Gtk.Align.END)
        self.err_lbl = C.label('', 12, C.DESTRUCT)
        self.err_lbl.set_halign(Gtk.Align.END)

        disp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        disp.get_style_context().add_class('ca-expr')
        disp.pack_start(self.expr_lbl, False, False, 0)
        disp.pack_start(self.res_lbl, False, False, 0)
        disp.pack_start(self.err_lbl, False, False, 0)
        root.pack_start(disp, False, False, 0)

        # 键盘区
        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)
        grid.set_row_homogeneous(True)
        grid.set_column_homogeneous(True)
        root.pack_start(grid, True, True, 0)

        for r, row in enumerate(self.KEY_ROWS):
            c = 0
            for key in row:
                text, kind = key[0], key[1]
                span = key[2] if len(key) > 2 else 1
                btn = Gtk.Button.new_with_label(text)
                btn.get_style_context().add_class('ca-btn')
                if text in '÷×−+±()':
                    btn.get_style_context().add_class('ca-op')
                if kind == 'eq':
                    btn.get_style_context().add_class('ca-eq')
                btn.connect('clicked', self._on_key, text, kind)
                grid.attach(btn, c, r, span, 1)
                c += span

        self.connect('key-press-event', self._on_kb)

    # ---------- 交互 ----------
    def _on_key(self, _b, text, kind):
        if kind == 'char':
            self.expr_lbl.set_text(self.expr_lbl.get_text() + ('-' if text == '−' else text))
        elif kind == 'clear':
            self.expr_lbl.set_text('')
            self.res_lbl.set_text('0')
            self.err_lbl.set_text('')
        elif kind == 'back':
            self.expr_lbl.set_text(self.expr_lbl.get_text()[:-1])
        elif kind == 'neg':
            cur = self.expr_lbl.get_text()
            if cur.startswith('-'):
                self.expr_lbl.set_text(cur[1:])
            else:
                self.expr_lbl.set_text('-' + cur)
        elif kind == 'eq':
            self._calc()

    def _calc(self):
        try:
            self.err_lbl.set_text('')
            self.res_lbl.set_text(evaluate(self.expr_lbl.get_text()))
        except CalcError as e:
            self.err_lbl.set_text(str(e))
        except Exception:
            self.err_lbl.set_text('计算错误')

    def _on_kb(self, _w, ev):
        k = Gdk.keyval_name(ev.keyval)
        if k.startswith('KP_'):
            k = k[3:]
        if k in ('Return', 'Enter', 'equal'):
            self._calc()
        elif k == 'Escape':
            self.expr_lbl.set_text('')
            self.res_lbl.set_text('0')
            self.err_lbl.set_text('')
        elif k == 'BackSpace':
            self._on_key(None, '⌫', 'back')
        else:
            ch = {'plus': '+', 'minus': '−', 'multiply': '×',
                  'divide': '÷', 'period': '.', 'comma': '.',
                  'parenleft': '(', 'parenright': ')',
                  'percent': '%'}.get(k)
            if ch is None and len(k) == 1 and k in '0123456789.':
                ch = k
            if ch:
                self._on_key(None, ch, 'char')
        return True


# ============================================================
if __name__ == '__main__':
    if not C.single_instance('calc'):
        sys.exit(0)
    win = Calculator()
    C.set_raise_handler('calc', win.present)
    win.connect('destroy', Gtk.main_quit)
    win.show_all()
    Gtk.main()
