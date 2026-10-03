#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_service.py - AI.qd 中枢服务（严格按规范 5.x 节）
职责：
  1. 加载并解释所有 .qdai 规则（自然语言 → 结构化规则）
  2. 策略层：调度决策、权限决策（眼/笔/橡皮工具授权）
  3. 统一记忆系统（记忆库存储/检索）
  4. 服务调度：向执行层下发策略
  5. 提供 QD-IPC 通信入口（UNIX socket + HTTP API）

运行模式：
  ai_service.py daemon        # 守护模式（后台常驻）
  ai_service.py scan          # 扫描并解释所有 qdai
  ai_service.py memory add    # 添加记忆
  ai_service.py memory search # 检索记忆
"""
import os
import sys
import re
import json
import time
import uuid
import signal
import socket
import secrets
import struct
import threading
import argparse
from datetime import datetime

# 路径常量（基于 Linux 魔改版：根目录 /奇点OS）
# QD_ROOT 支持环境变量覆盖，便于测试与跨平台部署
QD_ROOT = os.environ.get('QD_ROOT', '/奇点OS')
SYSTEM_DIR = os.path.join(QD_ROOT, '系统')
AI_DIR = os.path.join(QD_ROOT, 'AI.qd')
USER_DIR = os.path.join(QD_ROOT, '用户.qd')
MEMORY_DIR = os.path.join(AI_DIR, '记忆库')
KNOWLEDGE_DIR = os.environ.get(
    'QD_KNOWLEDGE_DIR', os.path.join(AI_DIR, '知识库'))
IPC_SOCKET = os.environ.get('QD_AI_SOCK', '/tmp/qd_ai.sock')
IPC_PORT = int(os.environ.get('QD_AI_PORT', '19231'))
# 权限令牌文件（运行时生成，0o600，替代明文硬编码）
TOKEN_FILE = os.environ.get('QD_TOKEN_FILE',
                            os.path.join(QD_ROOT, '运行', 'tokens.json'))

# 引入 qd_loader 的 QdaiParser（实现正确的四段解析，修复 _parse 死代码）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qd_loader import QdaiParser  # noqa: E402

# 可选：接入本地模型管理器（ai/model_manager.py）
_HERE = os.path.dirname(os.path.abspath(__file__))
_AI_DIR_SRC = os.path.abspath(os.path.join(_HERE, '..', '..', 'ai'))
if _AI_DIR_SRC not in sys.path:
    sys.path.insert(0, _AI_DIR_SRC)

# 错误码（规范附录A）
ERR_OK = 0
ERR_MODULE_NOT_FOUND = 1001
ERR_PERMISSION_DENIED = 2001
ERR_RULE_PARSE = 3001
ERR_MEMORY = 4001


class QdaiEngine:
    """.qdai 规则引擎：解析 + 求值 + 触发"""

    def __init__(self, ai):
        self.ai = ai
        self.rules = []  # 全部已加载规则

    def scan_all(self):
        """扫描所有 .qd 模块内的 .qdai 文件"""
        self.rules = []
        for root in (SYSTEM_DIR, AI_DIR, USER_DIR):
            self._scan_dir(root)
        return self.rules

    def _scan_dir(self, directory):
        if not os.path.isdir(directory):
            return
        for entry in sorted(os.listdir(directory)):
            full = os.path.join(directory, entry)
            if os.path.isfile(full) and entry.endswith('.qdai'):
                rule = self._parse(full)
                if rule:
                    self.rules.append(rule)
            elif os.path.isdir(full):
                self._scan_dir(full)

    def _parse(self, path):
        """解析 .qdai 文件（YAML头 + 4段式）

        修复说明：旧实现先用 `text = before + after` 剥除所有分段标记，
        导致随后的重切分全部落入 TAIL，策略层恒为空（死代码）。
        现统一委托给 qd_loader.QdaiParser.parse（实现正确，pack/loader 双方对齐）。
        """
        try:
            rule = QdaiParser.parse(path)
            rule['path'] = path
            return rule
        except Exception as e:
            print(f'[qdai] 解析失败 {path}: {e}')
            return None

    def explain(self, path):
        """AI 解读 .qdai（用户长按调用）"""
        rule = self._parse(path)
        if not rule:
            return '无法解析该文件'
        lines = []
        lines.append(f'规则名称: {rule["meta"].get("name", "未知")}')
        lines.append(f'目标: {rule["meta"].get("target", "无")}')
        lines.append(f'优先级: {rule["meta"].get("priority", "无")}')
        if rule['natural']:
            lines.append(f'意图说明: {rule["natural"][:200]}')
        if rule['rules']:
            n = len(rule['rules'].get('rules', []))
            lines.append(f'结构化规则: {n} 条')
        if rule['lua']:
            lines.append('包含 Lua 脚本（IO路由）')
        return '\n'.join(lines)


class MemorySystem:
    """统一记忆系统（AI.qd 专属存储，规范 5.8）"""

    def __init__(self):
        os.makedirs(MEMORY_DIR, exist_ok=True)

    def add(self, content, category='general', source='ai'):
        """写入一条记忆"""
        mem = {
            'id': uuid.uuid4().hex[:16],  # uuid4 避免截断 MD5 的碰撞风险
            'content': content,
            'category': category,
            'source': source,
            'time': datetime.now().isoformat(),
        }
        path = os.path.join(MEMORY_DIR, f'{mem["id"]}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
        return mem['id']

    def search(self, keyword, limit=10):
        """关键词检索记忆"""
        results = []
        if not os.path.isdir(MEMORY_DIR):
            return results
        for fname in sorted(os.listdir(MEMORY_DIR)):
            if not fname.endswith('.json'):
                continue
            try:
                with open(os.path.join(MEMORY_DIR, fname), 'r', encoding='utf-8') as f:
                    mem = json.load(f)
                if keyword.lower() in mem['content'].lower() or \
                   keyword.lower() in mem['category'].lower():
                    results.append(mem)
            except Exception:
                continue
            if len(results) >= limit:
                break
        return results

    def list_all(self):
        result = []
        if not os.path.isdir(MEMORY_DIR):
            return result
        for fname in sorted(os.listdir(MEMORY_DIR)):
            if fname.endswith('.json'):
                try:
                    with open(os.path.join(MEMORY_DIR, fname), 'r', encoding='utf-8') as f:
                        result.append(json.load(f))
                except Exception:
                    continue
        return result


class KnowledgeSystem:
    """知识库（规范 L180 知识库.qd / L411 KNOWLEDGE 段的消费端，v1.1 补件）

    双来源：
      1. .qdai 文件的 KNOWLEDGE 段（QdaiParser 解析产物，此前无人消费）
      2. AI.qd/知识库/ 目录下的 *.txt / *.md 文件
    检索：轻量词频打分（stdlib，无向量库依赖，与瘦身目标兼容）。
    消费：chat 命令自动把 top 命中注入 system prompt（最小可用 RAG 环路）。
    """

    def __init__(self, knowledge_dir=None):
        self.dir = knowledge_dir or KNOWLEDGE_DIR
        os.makedirs(self.dir, exist_ok=True)
        self.entries = []  # [{'source': str, 'text': str}]

    def reload(self, qdai_rules=None):
        """重建知识条目：.qdai 知识段 + 知识库目录文件"""
        self.entries = []
        for r in (qdai_rules or []):
            text = (r.get('knowledge') or '').strip()
            if text:
                self.entries.append({
                    'source': os.path.basename(r.get('path', '?')) + '#KNOWLEDGE',
                    'text': text})
        if os.path.isdir(self.dir):
            for fn in sorted(os.listdir(self.dir)):
                if not fn.lower().endswith(('.txt', '.md')):
                    continue
                try:
                    with open(os.path.join(self.dir, fn), 'r', encoding='utf-8') as f:
                        text = f.read().strip()
                    if text:
                        self.entries.append({'source': fn, 'text': text})
                except Exception:
                    continue
        return len(self.entries)

    def query(self, keyword, limit=3):
        """词频打分检索；keyword 为空时返回空（不盲目注入）"""
        kws = [w for w in re.split(r'[\s,，。;；、]+', (keyword or '').strip()) if w]
        if not kws:
            return []
        scored = []
        for e in self.entries:
            low = e['text'].lower()
            score = sum(low.count(w.lower()) for w in kws)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return [dict(e, score=s) for s, e in scored[:limit]]

    def add(self, title, text):
        """写入知识文件（清洗文件名，防路径穿越）"""
        safe = re.sub(r'[\\/:*?"<>|\s]+', '_', (title or 'untitled').strip())[:60]
        if not safe:
            safe = 'untitled'
        path = os.path.join(self.dir, safe + '.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text or '')
        self.reload()
        return path


def hashlib_md5(s):
    import hashlib
    return hashlib.md5(s.encode()).hexdigest()[:16]


class AICore:
    """AI.qd 中枢"""

    def __init__(self):
        self.qdai = QdaiEngine(self)
        self.memory = MemorySystem()
        self.knowledge = KnowledgeSystem()
        self.running = True
        self.policies = {}  # 已下发策略
        self._model_manager = None   # 惰性初始化的本地模型管理器
        self._model_checked = False
        self.tool_perms = {  # 用户权限工具（眼/笔/橡皮，默认用户所有）
            'eye': True,     # 读
            'pen': True,     # 写
            'eraser': False, # 删（默认需授权）
        }

    def start(self):
        print('=== AI.qd 中枢启动 ===')
        print(f'系统目录: {SYSTEM_DIR}')
        print(f'AI目录: {AI_DIR}')
        print(f'记忆库: {MEMORY_DIR}')

        # 1. 扫描并解释所有 qdai
        rules = self.qdai.scan_all()
        print(f'[1/6] 已加载 {len(rules)} 条 .qdai 规则')
        for r in rules[:10]:
            print(f'      - {r["meta"].get("name", "?"):20s} target={r["meta"].get("target", "?")}')

        # 2. 初始化记忆 + 装载知识库（.qdai KNOWLEDGE 段 + 知识库目录）
        mem_count = len(self.memory.list_all())
        k_count = self.knowledge.reload(rules)
        print(f'[2/6] 记忆库: {mem_count} 条记忆；知识库: {k_count} 条知识')

        # 3. 启动 IPC 服务（线程）
        threading.Thread(target=self._ipc_server, daemon=True).start()
        print(f'[3/6] QD-IPC 监听: {IPC_SOCKET} + :{IPC_PORT}')

        # 4. 加载执行层策略
        self._load_policies()
        print(f'[4/6] 策略层: {len(self.policies)} 条策略')

        # 5. 生成本次运行的权限令牌（替代明文硬编码，文件 0o600）
        tokens = self._ensure_tokens()
        print(f'[5/6] 权限令牌已生成: {TOKEN_FILE} (tools={list(tokens)})')

        # 6. 就绪
        print(f'[6/6] AI.qd 就绪 (PID={os.getpid()})')
        print('      intent_agent: 就绪')

    def _ensure_tokens(self):
        """为本机授权工具（眼/笔/橡皮）生成随机令牌，写入 0o600 文件。

        令牌不再随 agent_config.json 分发；intent_agent 等消费方
        在本机读取该文件获得令牌，远程方无法获知。
        """
        os.makedirs(os.path.dirname(TOKEN_FILE) or '.', exist_ok=True)
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass  # 损坏则重新生成
        tokens = {t: 'QD-' + t.upper() + '-' + secrets.token_hex(8)
                  for t in ('eye', 'pen', 'eraser')}
        fd = os.open(TOKEN_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(tokens, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
        return tokens

    def _get_model_manager(self):
        """惰性接入本地模型管理器；不可用时返回 None（调用方自行降级）。"""
        if self._model_checked:
            return self._model_manager
        self._model_checked = True
        try:
            from model_manager import ModelManager  # noqa: F401
            self._model_manager = ModelManager()
        except Exception as e:
            print(f'[model] model_manager 不可用，已降级: {e}')
            self._model_manager = None
        return self._model_manager

    def _load_policies(self):
        """从 qdai 结构化规则生成可执行策略"""
        for rule in self.qdai.rules:
            rules = rule['rules'].get('rules', []) if isinstance(rule['rules'], dict) else []
            for r in rules:
                self.policies[r.get('id', 'unknown')] = {
                    'condition': r.get('condition', ''),
                    'action': r.get('action', {}),
                    'priority': r.get('priority', 50),
                }

    def _ipc_server(self):
        """UNIX socket IPC（QD-IPC 通信入口）"""
        try:
            os.unlink(IPC_SOCKET)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(IPC_SOCKET)
        srv.listen(8)
        while self.running:
            try:
                conn, _ = srv.accept()
                threading.Thread(target=self._handle_ipc, args=(conn,), daemon=True).start()
            except Exception:
                break

    def _handle_ipc(self, conn):
        try:
            # 对端校验：仅允许同 uid 进程（Linux SO_PEERCRED；其他平台跳过）
            try:
                import struct as _s
                creds = conn.getsockopt(
                    socket.SOL_SOCKET,
                    getattr(socket, 'SO_PEERCRED', 17),
                    _s.calcsize('3i'))
                if creds:
                    _, _, uid = _s.unpack('3i', creds)
                    if uid != -1 and uid != os.getuid():
                        conn.sendall(json.dumps(
                            {'ok': False, 'error': 'peer uid mismatch'},
                            ensure_ascii=False).encode('utf-8'))
                        return
            except (AttributeError, OSError):
                pass  # 非 Linux 平台无 SO_PEERCRED，跳过
            data = conn.recv(65536)
            if not data:
                return
            req = json.loads(data.decode('utf-8'))
            resp = self._dispatch(req)
            conn.sendall(json.dumps(resp, ensure_ascii=False).encode('utf-8'))
        except Exception as e:
            conn.sendall(json.dumps({'error': str(e)}).encode('utf-8'))
        finally:
            conn.close()

    def _dispatch(self, req):
        """QD-IPC 请求分发"""
        cmd = req.get('cmd', '')
        if cmd == 'ping':
            return {'ok': True, 'status': 'alive', 'time': datetime.now().isoformat()}
        if cmd == 'memory_add':
            mid = self.memory.add(req.get('content', ''), req.get('category', 'general'))
            return {'ok': True, 'id': mid}
        if cmd == 'memory_search':
            return {'ok': True, 'results': self.memory.search(req.get('keyword', ''))}
        if cmd == 'memory_list':
            return {'ok': True, 'results': self.memory.list_all()}
        if cmd == 'knowledge_query':
            return {'ok': True,
                    'results': self.knowledge.query(req.get('keyword', ''),
                                                    int(req.get('limit', 3)))}
        if cmd == 'knowledge_add':
            path = self.knowledge.add(req.get('title', ''), req.get('text', ''))
            return {'ok': True, 'path': path,
                    'total': len(self.knowledge.entries)}
        if cmd == 'knowledge_reload':
            return {'ok': True, 'total': self.knowledge.reload(self.qdai.rules)}
        if cmd == 'explain_qdai':
            return {'ok': True, 'text': self.qdai.explain(req.get('path', ''))}
        if cmd == 'scan_modules':
            return {'ok': True, 'rules': len(self.qdai.rules)}
        if cmd == 'tool_perm':
            tool = req.get('tool', '')
            if tool in self.tool_perms:
                return {'ok': True, 'perm': self.tool_perms[tool]}
            return {'ok': False, 'error': 'unknown tool'}
        if cmd == 'policies':
            return {'ok': True, 'policies': self.policies}
        if cmd == 'chat':
            return self._cmd_chat(req)
        if cmd == 'model_status':
            mgr = self._get_model_manager()
            if mgr is None:
                return {'ok': False, 'error': 'model_manager 不可用'}
            return {'ok': True, 'state': mgr.get_state()}
        if cmd == 'model_classify':
            return self._cmd_model_classify(req)
        return {'ok': False, 'error': f'unknown cmd: {cmd}', 'code': ERR_RULE_PARSE}

    # ------------------------------------------------------------
    # 真实 LLM 命令（第二档修复：打通 model_manager -> ai_service 链路）
    # ------------------------------------------------------------
    _INTENTS = ['查询', '控制', '调度', '创作', '故障排查', '界面操纵']

    def _cmd_chat(self, req):
        """chat 命令：优先本地模型推理；模型不可用时优雅降级。

        知识注入（最小 RAG 环路）：按请求文本检索知识库 top-2，
        拼入 system prompt；降级路径同样附带知识命中。
        """
        text = (req.get('text') or '').strip()
        if not text:
            return {'ok': False, 'error': 'text 为空'}
        k_hits = self.knowledge.query(text[:40], limit=2)
        knowledge_used = [h['source'] for h in k_hits]
        system = req.get('system', '') or ''
        if k_hits:
            system = (system + '\n[知识库参考]\n' +
                      '\n'.join('- ' + h['text'][:300] for h in k_hits)).strip()
        mgr = self._get_model_manager()
        if mgr is not None:
            try:
                reply = mgr.request_text(text, system_prompt=system)
            except Exception as e:
                reply = None
                reason = str(e)
            else:
                reason = getattr(mgr, '_last_error', None)
            if reply:
                return {'ok': True, 'degraded': False, 'knowledge_used': knowledge_used,
                        'model': mgr.active_model, 'reply': reply}
        else:
            reason = 'model_manager 未加载'
        # 降级路径：知识库/记忆检索结果，绝不伪装成模型输出
        parts = []
        if k_hits:
            parts.append('[知识库] ' + ' | '.join(
                h['text'][:100].replace('\n', ' ') for h in k_hits))
        hits = self.memory.search(text[:20], limit=3)
        if hits:
            parts.append('[记忆库]\n' + '\n'.join('- ' + h['content'][:120] for h in hits))
        if parts:
            reply = '（本地模型不可用，以下为知识库/记忆库检索结果）\n' + '\n'.join(parts)
        else:
            reply = '（本地模型不可用，降级回复）已收到你的请求：' + text[:100]
        return {'ok': True, 'degraded': True, 'knowledge_used': knowledge_used,
                'reply': reply, 'reason': reason}

    def _cmd_model_classify(self, req):
        """用模型确认意图分类；失败时返回 model=None，由调用方回落关键词结果。"""
        text = (req.get('text') or '').strip()
        if not text:
            return {'ok': False, 'error': 'text 为空'}
        mgr = self._get_model_manager()
        if mgr is None:
            return {'ok': True, 'model': None, 'reason': 'model_manager 未加载'}
        prompt = ('请把下面的用户输入分类为以下意图之一：' +
                  '、'.join(self._INTENTS) +
                  '。只输出意图名称本身，不要任何解释。\n输入：' + text[:200])
        try:
            out = mgr.request_text(prompt, system_prompt='你是意图分类器。')
        except Exception:
            out = None
        if not out:
            return {'ok': True, 'model': None,
                    'reason': getattr(mgr, '_last_error', '模型调用失败')}
        out = out.strip().splitlines()[0].strip().strip('"\'，。 ')
        if out in self._INTENTS:
            return {'ok': True, 'model': mgr.active_model, 'intent': out}
        return {'ok': True, 'model': mgr.active_model, 'intent': None,
                'raw': out[:50]}


# ============================================================
def main():
    parser = argparse.ArgumentParser(description='AI.qd 中枢服务')
    parser.add_argument('cmd', nargs='?', default='daemon',
                        choices=['daemon', 'scan', 'memory', 'explain'])
    parser.add_argument('--path', help='explain 模式的目标 .qdai 路径')
    args = parser.parse_args()

    if args.cmd == 'scan':
        ai = AICore()
        rules = ai.qdai.scan_all()
        print(f'扫描到 {len(rules)} 条 .qdai 规则:')
        for r in rules:
            print(f'  {r["path"]}  [{r["meta"].get("name", "?")}]')
        return 0

    if args.cmd == 'explain':
        ai = AICore()
        if args.path:
            print(ai.qdai.explain(args.path))
        return 0

    if args.cmd == 'memory':
        ai = AICore()
        mems = ai.memory.list_all()
        print(f'记忆库: {len(mems)} 条')
        for m in mems:
            print(f'  [{m["category"]}] {m["content"][:60]}')
        return 0

    # daemon 模式
    ai = AICore()
    ai.start()

    def on_signal(sig, frame):
        ai.running = False
        print('\nAI.qd 停止')
        sys.exit(0)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    while ai.running:
        time.sleep(1)


if __name__ == '__main__':
    main()
