#!/bin/bash
# qd-os-start.sh —— 系统框架服务链（规范 12 章：模块系统→shm_ipc→microsrv→进程调度）
# 由 qd-os.service 托管；界面层(qwm/ui_*)在用户会话 .xinitrc，不在此服务
/usr/bin/python3 /奇点OS/运行/shm_ipc.py daemon &
/usr/bin/python3 /奇点OS/运行/microsrv.py daemon &
# 进程调度/算力调度为管理 CLI：注册状态（不常驻）
/usr/bin/python3 /奇点OS/运行/进程调度.py status >/dev/null 2>&1
# 锚点：永不退出的守护（G.12 教训：禁止 exec 可被关闭的应用）
exec sleep infinity
