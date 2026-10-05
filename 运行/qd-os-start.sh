#!/bin/bash
# qd-os-start.sh —— 系统框架服务链（规范 12 章：模块运行时→shm_ipc→microsrv→进程调度）
# 由 qd-os.service 托管；界面层为模块化桌面（图形图像桌面.qd），在用户会话 .xinitrc 启动
# 2026-10-05 重构：模块运行时 qdd 纳入开机服务链（此前模块化管理器不随系统启动=形同虚设）
/usr/bin/python3 /奇点OS/系统/功能.qd/bin/qdd.py daemon &   # 模块运行时：热发现/生命周期/总线/隔离拦截
/usr/bin/python3 /奇点OS/运行/shm_ipc.py daemon &           # 共享内存 IPC
/usr/bin/python3 /奇点OS/运行/microsrv.py daemon &          # 低功耗常驻微服务
# 进程调度/算力调度为管理 CLI：注册状态（不常驻）
/usr/bin/python3 /奇点OS/运行/进程调度.py status >/dev/null 2>&1
# 锚点：永不退出的守护（G.12 教训：禁止 exec 可被关闭的应用）
exec sleep infinity
