#!/bin/bash
# qd-power-start.sh —— 算力调度守护（root：renice/taskset 需 CAP_SYS_NICE）
# 规范：功能.qd/算力调度（进程调度之上的算力层，ai-first 优先供给 ai.qd）
LOG=/奇点OS/运行/logs/qd-power.log
mkdir -p /奇点OS/运行/logs
/usr/bin/python3 /奇点OS/运行/算力调度.py policy ai-first >>"$LOG" 2>&1
while true; do
  /usr/bin/python3 /奇点OS/运行/算力调度.py stats >>"$LOG" 2>&1
  sleep 60
done
