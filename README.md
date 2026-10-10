# 奇点 Linux 发行版（第四次重构版）

> 从零构建（LFS 思路）的模块化 Linux 发行版：**Linux 内核 + 奇点 OS 自制 rootfs + 自研 init + 模块化框架**。
> 不捆绑任何发行版（不依赖 Arch / Ubuntu），需要什么组件就单独编译源码，打包成 `.qd/.qds` 模块。
> 系统框架、模块规范与 AI 子系统均为此仓库的原创设计。

## 这是什么

奇点 Linux 发行版是一套**以模块化为核心**、面向人机互联 / AGI 场景的自制 Linux 系统：

- **模块体系 `.QD / .QDS / .QDai`**：
  - `.QD` 文件夹 = 一个模块（程序本体 + 调用逻辑打包在一起），系统启动时扫描全部 `.QD` 并加载其中的 `.QDS`；
  - `.QDS` 定义 QD 的 **AI 调用 / 系统调用 / 安全边界**（热发现 / 热插拔 / 热替换 / 调用隔离 / 统一生命周期 / 模块间总线）；
  - `.qdai` 是 AI 调用主系统的**唯一通道**（ai.qd 内为引导，主系统内为自然语言 + PY 脚本调用规范）。
- **AI.QD 子系统**：独立运行、强安全隔离的最小 AI 子系统（chroot 专属隔离容器 + 常驻 AI 服务），支持云端 API（OpenAI 兼容）与本地模型（按需加载、用一个调一个）。
- **自研控制链**：内核 → `/sbin/init`（1 号进程，无 systemd）→ `boot.qds` 引导 → ai.qd 子系统 → 主系统 module-engine（监督循环，崩溃自动重启）。
- **极端环境可跑**：仅 CPU、骨架模式系统总占用 ≤ 1G、带本地 AI ≤ 4G 整机内存。

## 系统架构

```
硬件
 └─ Linux 内核（6.1.115，自编译裁剪）
     └─ 奇点OS rootfs（musl 交叉静态编译，无宿主库污染）
         ├─ sbin/init           1 号进程（自研，无 systemd）
         ├─ boot.qds            引导文件（激活子系统）
         ├─ 子框架.qd/
         │   ├─ ai.qd/          AI 子系统（chroot 隔离 + ai_service 6800）
         │   └─ 主框架.qd/      主系统（module-engine + 记忆.qd + 资源管理）
         ├─ modules/            .QD 模块区（包管理 / 渲染器 / 用户态 …）
         └─ 仓库/               包仓库（索引 + 实体/）
```

## 文档

- [开发文档 v1.10](第四次重构版/文档/奇点Linux发行版_开发文档_v1.10.md) —— 发行版总纲（架构 / 引导链 / 构建 / 验证 / 路线图）
- [包管理模块统一文档](第四次重构版/文档/奇点Linux发行版_包管理模块统一文档_v1.0.md) —— .QD 打包 / .QDS 契约 / 热替换 / 仓库
- [AI 本地 Agent 调用文档](第四次重构版/文档/奇点Linux发行版_AI本地Agent调用文档_v1.0.md) —— AI.QD 子系统 / .qdai 双用法 / AI 调用主系统
- [开发文档 v1.9（历史版）](第四次重构版/文档/奇点OS_第四次重构版_开发文档_v1.9.md) —— 上一版全量记录与审查修复记录

## 仓库内容（第四次重构版源码）

```
第四次重构版/
├─ init/            自研 init（1 号进程，C 源码，musl 静态编译）
├─ ai.qd/           AI 子系统（ai.qdai 约束 + ai_service.c 常驻服务源码）
├─ 引导/            boot.qds 引导文件
├─ 模块引擎/        module-engine.c 主系统引擎（模块容器 / me> 控制台）
├─ 模块/            包管理.qd / 渲染器.qd / 用户态.qd（.qds 契约 + C 源码）
├─ scripts/         启动最小 demo.ps1（Windows 侧演示脚本）
└─ 文档/            开发文档 v1.10 / v1.9 / 包管理 / AI 调用
```

- 内核（6.1.115 自编译 bzImage）、rootfs、ext4 镜像是**本地构建产物，不入库**；完整构建流程见开发文档第 5 章。
- 旧版实现（Ubuntu 组件基底 + XFCE 双框架）已于 2026-10 仓库清理时移除，仅保留 git 历史可回溯。

## 快速开始（构建后 QEMU）

```
qemu-system-x86_64 -accel whpx -m 2048 -smp 2 \
  -vga virtio -kernel bzImage -hda sos-rootfs.img \
  -append 'root=/dev/sda rootfstype=ext4 rw console=ttyS0' \
  -display vnc=127.0.0.1:1
```

- 引导链自动运行：init → boot.qds → ai_service（就绪）→ module-engine（`me>` 控制台）；
- AI 对话：主系统界面 / 串口 `me> ai <消息>` → ai_service → 云端 OpenAI 兼容接口；
- 模块管理：`me> list / modules / run <模块> / install / remove / module start|stop`。

## 已知限制

- 模块总线（UDS）、统一生命周期监督、`.QDS` 安全边界解析为**已设计、逐步落地中**（见开发文档状态表 ✅/📐）；
- 本地 LLM（MiniCPM-2B 等）为纯 CPU 推理，按需加载、不用即不加载；
- 语音链（VAD/ASR/TTS）为实验性功能，默认关闭，需用户在设置中手动开启。

## 许可

本仓库尚未选择开源许可证。作者保留所有权利；如需商用、分发或二次发布，请联系作者获取授权。
