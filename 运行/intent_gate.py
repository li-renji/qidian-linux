#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intent_gate.py —— 意图 Agent 独立门禁（规范 5 章）
独立于 QDSS 打包运行，供 module_main selfcheck 与 test_hook --intent-selfcheck 共用。
关键设计：绝不嵌套调用 QDSS/QdsLoader，杜绝递归与进程堆积。

用法: python3 /奇点OS/运行/intent_gate.py   # 输出 JSON，exit 0/1
"""
import os
import sys
import json
import glob
import subprocess

INTENT_MAIN = "/奇点OS/AI.qd/intent_agent.qd/bin/module_main"
CAL_DIR = "/奇点OS/AI.qd/记忆库/日历"
DETAIL_DIR = "/奇点OS/AI.qd/记忆库/详情"


def run(args, timeout=30):
    """调 module_main 子命令（均不触发 selfcheck，无递归）"""
    try:
        p = subprocess.run([INTENT_MAIN] + args, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as e:
        return 1, str(e)


def main():
    results = []

    # 1. 打断合并三分支
    cases = [
        (["merge", "--first", "把这个界面改一下", "--second", "算了，还是用原来的吧"], "replace", "推翻/更正"),
        (["merge", "--first", "写个报告", "--second", "另外要加上数据来源"], "merge", "补充/约束"),
        (["merge", "--first", "检查系统", "--second", "帮我查一下天气"], "queue", "无关新话题"),
    ]
    for args, expect, label in cases:
        rc, out = run(args)
        rel = ""
        try:
            rel = json.loads(out).get("relation", "")
        except Exception:
            pass
        ok = rc == 0 and rel == expect
        results.append({"name": "intent:merge:" + label, "status": "pass" if ok else "fail",
                        "detail": "期望 {} 实际 {}".format(expect, rel)})

    # 2. 8 核心工具注册表
    rc, out = run(["tools"])
    n = 0
    try:
        n = len(json.loads(out).get("tools", []))
    except Exception:
        pass
    ok = rc == 0 and n >= 8
    results.append({"name": "intent:tools-8", "status": "pass" if ok else "fail",
                    "detail": "{} 工具".format(n)})

    # 3. 系统感知
    rc, out = run(["status"])
    ok = rc == 0 and "modules" in out
    results.append({"name": "intent:status", "status": "pass" if ok else "fail",
                    "detail": "感知快照" if ok else out[:80]})

    # 4. 文件工具授权（无令牌必须拒绝 code=3）
    rc, out = run(["tool", "--tool", "file/read", "--path", "/etc/hostname"])
    code = -1
    try:
        code = json.loads(out).get("code", -1)
    except Exception:
        pass
    ok = code == 3
    results.append({"name": "intent:auth-deny", "status": "pass" if ok else "fail",
                    "detail": "无令牌应拒绝(code=3)，实际 code={}".format(code)})

    # 5. 记忆闭环：写入 → 检索命中 → 清理
    mid = None
    rc, out = run(["memory-add", "--title", "intent-gate-test",
                   "--summary", "门禁测试记忆", "--content", "test"])
    try:
        mid = json.loads(out).get("memory_id")
    except Exception:
        pass
    ok = rc == 0 and mid
    if ok:
        rc2, out2 = run(["memory-search", "--text", "intent-gate-test"])
        try:
            hits = json.loads(out2)
            ok = isinstance(hits, list) and len(hits) > 0
        except Exception:
            ok = False
        # 清理测试记忆
        if mid:
            for f in glob.glob(os.path.join(DETAIL_DIR, mid + ".json")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            for cf in glob.glob(os.path.join(CAL_DIR, "*.json")):
                try:
                    items = json.load(open(cf, encoding="utf-8"))
                    items = [x for x in items if x.get("id") != mid]
                    json.dump(items, open(cf, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=1)
                except Exception:
                    pass
    results.append({"name": "intent:memory", "status": "pass" if ok else "fail",
                    "detail": "写入→检索→清理" if ok else "记忆闭环失败"})

    all_ok = all(r["status"] == "pass" for r in results)
    print(json.dumps({"ok": all_ok, "code": 0 if all_ok else 5, "tests": results},
                     ensure_ascii=False, indent=2))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
