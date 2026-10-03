# qd-loader 原生化规划（规范 20.2）

> 目标：用 C 原生 ELF 替代 Python 版 `qd-run.py`，把 .qds 冷启动从
> ~69ms 压到 ≤10ms。**本阶段只出规划，不写 C 代码。**

## 1. 现状与瓶颈

### 1.1 链路
内核 `binfmt_misc` 识别 `.qds` 的 `SSDQ` magic → 调用
`/奇点OS/运行/qd-run.py` → `import qd_loader`（Python 解释器冷启动 + 模块加载）→
校验 256B 文件头（magic/sha256 自校验）→ 提取 payload 到 `tempfile` →
`os.execvpe('/usr/bin/python3', ...)` 再次拉起 Python 执行 payload。

### 1.2 实测数据
来源：`/奇点OS/运行/logs/slo_report.json`（2026-09-25）
- `qds_cold_start`：avg **68.9ms**，p50 69.0ms，max 74.8ms（10 次，样本 `眼睛.qds status`）。
- 规范 20.1 红线：qds 冷启动 ≤ **10ms**。**当前不达标（fail）**。

### 1.3 瓶颈分解（定性）
| 阶段 | 估计耗时 | 原因 |
|---|---|---|
| Python 解释器启动 | ~30-40ms | 解释器初始化、site 模块 |
| `import qd_loader` + struct/hashlib/json | ~10-15ms | 纯 Python 模块加载 |
| 读文件 + sha256 自校验 | ~5ms | 256B 文件头，可忽略 |
| tempfile 落盘 + exec python3 | ~15-20ms | payload 写临时文件 + 二次 exec 解释器 |
| **合计** | **~69ms** | 与实测吻合 |

根因：**两次 Python 解释器启动**（loader 一次、payload 一次）+ 临时文件落盘。

## 2. C 原生 ELF 方案

### 2.1 职责（严格对齐 qd_loader.py 契约）
1. **QDSS 头校验**：读 256B 头，校验
   - magic `0x51445353`（QDSS）
   - 前 232B 的 SHA256 前 24 字节 == 头偏移 232:256（自校验）
   - 文件总长度 == 256 + bin_size + config_size + sig_size
   任一失败 exit code=5（INTERNAL）。
2. **payload 提取**：按 `bin_offset=256` / `bin_size` 切出 payload。
   - `payload_type==1`（Python）：直接 `execl("/usr/bin/python3", "python3", "-", ...)`
     并把 payload 通过 **stdin 管道**传给 python3（**省掉 tempfile 落盘**）。
   - `payload_type==0`（ELF）：payload 写入 `memfd_create(MFD_CLOEXEC)`，
     再 `fexecve(memfd, argv, env)`（不落盘、更安全）。
3. **QD_CONFIG 注入**：把命令行参数打包成
   `QD_CONFIG={"args":[...]}` 写入环境变量，与 Python 版**完全一致**；
   同时注入 `QD_SERVICE_NAME=<name>`、`QD_MODULE_DIR=<dirname>`。
4. **错误码**：exit code 对齐规范 9.6（0/1/3/4/5），错误信息写 stderr。

### 2.2 关键系统调用
- `memfd_create(2)` + `fexecve(2)`（ELF payload，零临时文件）
- `pipe(2)` + `execl("/usr/bin/python3","python3","-",...)` + `write(pipe, payload)`（Python payload）
- `SHA256`：用系统库 `libcrypto`（OpenSSL）或内置小实现，只算 232B。

### 2.3 binfmt_misc 切换
- 现状：`binfmt_misc` 解释器指向 `qd-run.py`。
- 原生版：编译出 `qd-run-native`（静态/动态小 ELF），把 binfmt 登记改指
  `qd-run-native`，并保留 `qd-run.py` 作为调试通道。

## 3. 迁移步骤
1. **编写 C**：`/奇点OS/运行/qd-run-native.c`（单文件，<300 行），实现头校验+提取+exec。
2. **编译**：`gcc -O2 -o qd-run-native qd-run-native.c -lcrypto`（或无依赖小 SHA256）。
3. **对照测试**：用同一组 .qds（眼睛/笔/橡皮/模块自检）跑 native 与 Python 版，
   比对 stdout JSON 完全一致、QD_CONFIG 环境变量一致。
4. **压测**：`slo_measure.py` 切到 native 后重测冷启动，确认 ≤10ms。
5. **切换 binfmt**：更新 `/proc/sys/fs/binfmt_misc/register` 指向 native；
   保留 `qd-run.py` 不删除（调试/回退）。
6. **灰度**：先切用户态 .qds（工具.qd），观察 1 个版本周期无异常再切系统 .qds。

## 4. 验收标准
- [ ] `qd-run-native <sample.qds> status` 输出与 `qd-run.py` 逐字节一致（JSON）。
- [ ] 冷启动 avg ≤ **10ms**（`slo_measure.py:qds_cold_start.pass == true`）。
- [ ] `QD_CONFIG` 环境变量契约不变（payload 内 `os.environ['QD_CONFIG']` 解析一致）。
- [ ] 头校验失败时 exit=5，且不执行 payload。
- [ ] 不引入临时文件（`/tmp/qd_run_*` 不再增长）。

## 5. 风险与回退
| 风险 | 等级 | 缓解/回退 |
|---|---|---|
| C 版头校验与 Python 版行为不一致（大小端/字段偏移） | 高 | 迁移步骤 3 做逐字节对照；不一致即不切 binfmt |
| `fexecve`/`memfd` 在旧内核不可用 | 中 | 降级到 pipe 给 python3 的路径；ELF payload 回退 tempfile |
| payload 依赖 `QD_MODULE_DIR` 等环境变量 | 低 | C 版逐一注入，对照 `qd-run.py` 的 env 集合 |
| native 版崩溃导致所有 .qds 无法启动 | 高 | **binfmt 一键回退**：把登记指回 `qd-run.py`；保留 Python 版常驻不删 |
| 不碰 AI 框架主逻辑 / systemd unit / 安全配置 | — | 仅新增 C 源码与编译产物，不改服务定义 |

## 6. 本阶段结论（ch20）
- 实测冷启动 68.9ms，距 10ms 红线差 ~7 倍，瓶颈是双 Python 解释器 + tempfile 落盘。
- 原生 C 版路径清晰（memfd/fexecve + pipe-to-python），但属高风险重构，
  按硬约束**本阶段不落地 C 代码**，待规划评审通过后单独排期实施。
