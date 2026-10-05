#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rule_engine.py —— .qdai 规则引擎（Agent 运行时的规则加载与生效层）

契约（与规范 3.3.1 对应）：
  1. 加载时机：Agent 启动 init 阶段全树加载；运行中 reload 命令重载
  2. 加载范围：/奇点OS 下所有 .qd 模块内 .qdai + AI.qd 根部 .qdai（全树）
  3. 五段式生效：
     NATURAL_LANGUAGE → 拼接进 Agent 系统提示（行为约束）
     STRUCTURED_RULES → JSON 规则表，工具执行前 check_action 校验
     CONTRACT         → 契约库（供 AI 生成代码的前置约束，sdk 场景读取）
     LUA              → 沙箱执行（IO 路由/数据转发；本 Linux 版用系统 lua+沙箱）
     KNOWLEDGE        → 知识注入（供知识库条目建立）
  4. 跳转：qdai 规则头部 target=另.qdai；命中 trigger 时目标规则优先生效
  5. 优先级：priority 数值，高者先生效；同优先级按路径字典序
  6. 权限：规则内工具调用仍走 Agent 权限闸门（眼/笔/橡皮），规则引擎不越权
"""
import os
import re
import json
import glob

QD_ROOT = os.environ.get('QD_ROOT', '/奇点OS')

SECTION_RE = re.compile(r'^===QD_SECTION_(\w+)===', re.M)

# 五段式段名（规范 3.3）
SECTIONS = ['NATURAL_LANGUAGE', 'STRUCTURED_RULES', 'CONTRACT', 'LUA', 'KNOWLEDGE']


def parse_yaml_head(text):
    """简化 YAML 头解析（name/target/priority/trigger 等 key: value），不依赖 yaml 库"""
    head = {}
    m = re.search(r'^---\s*\n(.*?)(?:^---\s*$|\n===)', text, re.M | re.S)
    if not m:
        return head
    for line in m.group(1).splitlines():
        line = line.strip()
        if ':' in line and not line.startswith('#'):
            k, v = line.split(':', 1)
            head[k.strip().lower()] = v.strip().strip('"\'')
    return head


def split_sections(text):
    """按 ===QD_SECTION_X=== 分割五段式"""
    parts = {}
    pos = 0
    marks = [(m.start(), m.group(1)) for m in SECTION_RE.finditer(text)]
    for i, (start, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[start:end]
        body = re.sub(r'^===QD_SECTION_\w+===', '', body, count=1).strip()
        parts[name] = body
    return parts


def collect_qdai(root=None):
    """全树收集 .qdai 文件（含 .qd 模块内 + AI.qd 根部）"""
    root = root or QD_ROOT
    files = []
    for base, dirs, fnames in os.walk(root):
        dirs[:] = [d for d in dirs if d != '.git' and d != '模块归档']
        for fn in fnames:
            if fn.endswith('.qdai'):
                files.append(os.path.join(base, fn))
    return sorted(files)


def load_rule(path):
    """解析单个 .qdai → Rule dict"""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    head = parse_yaml_head(text)
    parts = split_sections(text)
    return {
        'path': path,
        'name': head.get('name', os.path.basename(path)),
        'target': head.get('target', 'auto'),      # 跳转目标 qdai（或 auto/self）
        'priority': int(head.get('priority', 50)), # 0-100，高者先生效
        'trigger': head.get('trigger', 'auto'),    # 触发词/条件
        'sections': parts,
    }


def load_all(root=None):
    """全树加载所有规则，按 (priority desc, path) 排序"""
    rules = [load_rule(p) for p in collect_qdai(root)]
    rules.sort(key=lambda r: (-r['priority'], r['path']))
    return rules


def to_system_prompt(rules):
    """NATURAL_LANGUAGE 段 → Agent 系统提示（行为约束，按优先级拼接）"""
    blocks = []
    for r in rules:
        nl = r['sections'].get('NATURAL_LANGUAGE', '')
        if nl:
            blocks.append(f"[规则:{r['name']}](priority={r['priority']})\n{nl}")
    return '\n\n'.join(blocks)


def to_contract_lib(rules):
    """CONTRACT 段 → 契约库（AI 生成代码前置约束）"""
    lib = {}
    for r in rules:
        c = r['sections'].get('CONTRACT', '')
        if c:
            lib[r['name']] = c
    return lib


def to_knowledge(rules):
    """KNOWLEDGE 段 → 知识注入条目"""
    items = []
    for r in rules:
        k = r['sections'].get('KNOWLEDGE', '')
        if k:
            items.append({'rule': r['name'], 'content': k})
    return items


def parse_structured(rule):
    """STRUCTURED_RULES 段 → 动作规则表 [{action, allow|deny, note, ...}]"""
    sr = rule['sections'].get('STRUCTURED_RULES', '')
    if not sr:
        return []
    try:
        data = json.loads(sr)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except Exception:
        return []
    return []


def check_action(action, rules, allowlist=None):
    """工具执行前校验：返回 (allowed, matched_rule, reason)
    - deny 命中 → 拒绝；allow 命中 → 放行；无命中 → 默认放行（allowlist 参数可收紧）
    """
    for r in rules:
        for item in parse_structured(r):
            act = item.get('action', '')
            if act and (act == action or act == '*'):
                if item.get('allow') is True:
                    return (True, r['name'], item.get('note', ''))
                if item.get('deny') is True:
                    return (False, r['name'], item.get('note', 'denied by qdai rule'))
    return (True, None, '')


def resolve_jump(rules, trigger=None):
    """跳转规则：起点 qdai 命中 trigger → 目标 qdai 规则生效
    返回目标规则列表（target 指到的 .qdai 的规则对象）
    """
    hits = []
    for r in rules:
        if r['target'] in ('auto', 'self', ''):
            continue
        t = trigger or r['trigger']
        if r['trigger'] == 'auto' or (t and t in (r['trigger'] or '')):
            target_name = r['target']
            for other in rules:
                if other['name'] == target_name:
                    hits.append({'from': r['name'], 'to': other['name'], 'rule': other})
                    break
    return hits


# ---- CLI 自检：qdai 规则加载/校验 ----
def selfcheck(root=None):
    rules = load_all(root)
    lines = []
    lines.append(f'qdai 规则总数: {len(rules)}')
    bad = 0
    for r in rules:
        has_contract = 'CONTRACT' in r['sections']
        lines.append(f"  [{r['priority']:>3}] {r['name']:24s} target={r['target']:12s} contract={'Y' if has_contract else 'N'} {r['path']}")
        if not has_contract:
            bad += 1
    lines.append(f'缺 CONTRACT 段的规则: {bad}')
    return '\n'.join(lines)


if __name__ == '__main__':
    print(selfcheck())
