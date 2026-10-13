# 奇点 Linux 发行版 · AI 本地 Agent 调用文档 v1.3

> 更新日期：2026-10-13
> 关联：主文档《奇点Linux发行版_开发文档_v1.13.md》（架构 / 引导链 / 构建）、《奇点Linux发行版_包管理模块统一文档_v1.4.md》（模块契约 / sys_deps / 两阶段安装 / 可选拉取模块）
> 适用：奇点 Linux 发行版的 AI.QD 子系统、AI 调用主系统链路、本地 Agent 调用规范
> 项目根（PRJ）：`D:\C盘\Users\人机\Desktop\旧版奇点OS\完整版\第四次重构版`

## 版本记录（Changelog）

| 版本 | 日期 | 变更要点 |
| --- | --- | --- |
| v1.1 | 2026-10-11 | 初版（自开发文档拆分）：AI.QD 子系统定位、.qdai 双用法与唯一调用通道、双域权限模型、6800 文本协议、云端 OpenAI 兼容对话、记忆挂主系统、内存申请协议 |
| v1.2 | 2026-10-13 | **（为什么）第四次重构能力升级：AI 服务从"裸文本聊天骨架"升级为"可对外暴露 OpenAI 兼容接口 + token 最小化 Agent"**。改了什么：① 文头关联文档版本修正（开发文档 v1.12、包管理 v1.3）；② §4 新增 4.4 小节：6802 HTTP API 完整定义（GET /v1/models、POST /v1/chat/completions、SSE 流式、loopback+api_key 鉴权、400/502/504 错误语义）与 6800/6801/6802 三端口职责划分；③ §5.1 补工具清单配置化（tools=memo,task,skills 开关）与 system prompt 最小化注入规则；④ §5.2 补历史压缩摘要注入（每轮只注入 N 条事实摘要）；⑤ §7 现状核对表同步 |
| v1.3 | 2026-10-13 | **（为什么）文档与现实对齐**：v1.2 把 6802 HTTP API、三端口职责、工具清单配置化、system prompt 最小化、历史压缩摘要全部标为"📐 设计定稿"——实际 ai_service v0.6 已全部实现。改了什么：① 文头关联文档版本更新（开发 v1.13、包管理 v1.4）；② §4.4 标"✅ 已实现"；③ §5.1/§5.2 工具清单 / 最小化注入 / 压缩摘要标"✅ 已实现"；④ §3.1 模块调用描述更正——module-engine **不解析 sys_deps**（原 v1.1"先读 sys_deps 校验依赖"为未实现描述）；⑤ §1.1 配置清单更正——`memory_max` 属主系统侧资源管理概念，不在 ai_service 配置键内；⑥ §7 状态表同步 ✅；⑦ 附 v1.3 审查修复记录 |

## 1. AI.QD 子系统定位（总则）

**AI.QD 是独立运行、具备强安全隔离能力的最小 AI 子系统。**

| 特性 | 落地 |
| --- | --- |
| 独立运行 | 不依赖主系统存活：chroot 专属隔离容器 + 常驻 AI 服务，主系统崩溃不影响 AI |
| 强安全隔离 | 专属 chroot 容器 + 双域权限模型（框架内最高 / 框架间默认隔离） |
| 最小 AI 子系统 | 只承载 AI 服务本体：模型运行环境、模型调度、系统级 Agent 操作能力 |

### 1.1 AI.QD 的构成

```
ai.qd/                          # AI.QD = 最小独立系统
├─ bin/
│   └─ ai_service               # AI 服务（常驻，TCP 127.0.0.1:6800，仅本机）
├─ etc/                         # 容器内配置（DNS 等）
├─ skills/                      # AI skills 仓库区（AI 能力定义）
├─ 配置                         # AI 配置（含云端 API key / base_url / model / llm_mode / http_enabled / http_port / tools 等）
└─ ai.qdai                      # AI 引导文件（用法一：引导 AI 定位主系统的 .qdai）
```

> **配置键说明（v1.3 按源码核账）**：ai_service 实际加载的配置键为 `api_key / base_url / model / llm_mode / llm_base_url / llm_model / http_enabled / http_port / http_listen / tools / tool_max_chars / summary_max_chars`。**`memory_max` 不属于 ai_service 配置键**——它是主系统侧统一任务管理器的资源概念（见开发文档 v1.13 §7C.3），由用户在 me> 控制台 `config set ai.qd memory_max <值>` 管理（v1.3 更正：原 v1.1 把 memory_max 列为 AI 配置键有误）。

> **主系统侧另有 `.qdai` 文件**（用法二，见 §2.2）：位于 `/子框架.qd/主框架.qd/` 下，用自然语言和 PY 脚本写明 AI 对主系统调用的细节与规范——这是 AI 调用主系统的**唯一依据**。

### 1.2 AI 能力三要素（用户确认）

1. **模型运行环境**：导入模型到指定位置即可用（本地 LLM 规划：MiniCPM-2B 文本 / SmolVLM2 视觉 / Laya 判断门）；
2. **模型调度**：按任务选择模型，按需加载——不用即不加载，不全部同时调用（极端环境省内存）；
3. **系统级 Agent 操作**：AI 可作为系统级 Agent，经授权通道操作主系统（读写删工具制）。

## 2. AI 内部文件与 .qdai 调用通道

**AI 调用主系统，只能通过 `.qdai` 文件完成，不得绕过。**`.qdai` 内有 AI 对当前系统调用的规范与细节。

### 2.1 `.qdai` 的两种用法

| 用法 | 位置 | 内容 | 作用 |
| --- | --- | --- | --- |
| 用法一：引导 | `ai.qd/ai.qdai`（AI.QD 内） | AI 的身份 / 资源 / 权限模型 / 框架连通 / 职责 / 约束 / 架构 / 升级路径（八段式） | 引导 AI：AI 从这里出发，定位到主系统里面的 `.qdai` |
| 用法二：调用规范 | `/子框架.qd/主框架.qd/<名>.qdai`（主系统内） | 用**自然语言 + PY 脚本**写明 AI 对当前系统调用的细节与规范 | AI 按此规范对主系统进行实际调用 |

### 2.2 调用通道（唯一路径）

```
AI（ai_service 执行体）
  → 读 ai.qd/ai.qdai（引导）                      # 用法一：定位主系统 .qdai
  → 定位并读 主系统 .qdai（自然语言 + PY 脚本）      # 用法二：获得调用规范与细节
  → 按 .qdai 规范经授权通道调用主系统               # 眼睛=读 / 笔=写 / 橡皮=删
```

- **引导文件**（ai.qd/ai.qdai）：告诉 AI"主系统的 `.qdai` 在哪里、是什么结构"，不包含调用细节本身；
- **调用规范文件**（主系统 .qdai）：包含 AI 对当前系统调用的完整细节——能调用什么、怎么调用（命令 / PY 脚本片段）、返回什么、边界是什么；
- AI 执行体（ai_service）负责读取这两个文件并执行调用；**铁律：所有对外能力必须经 ai_service 统一出口，不得绕过。**

### 2.3 AI 内部文件分工

| 文件 | 职责 |
| --- | --- |
| `ai.qdai`（引导） | AI 的约束与行为定义（八段式），并引导 AI 定位主系统 `.qdai` |
| 主系统 `.qdai`（规范） | AI 对主系统调用的细节与规范（自然语言 + PY 脚本） |
| `ai_service`（执行体） | 常驻监听 6800，读取上述文件，按规范执行调用 |

## 3. AI 调用主系统的路径

### 3.1 调用链（用户确认的语义）

```
用户 / 主系统界面 / 其他模块
   → ai.qd 内部（ai_service，TCP 6800）          ← 所有 AI 能力的统一入口
   → 读 ai.qd/ai.qdai（引导）                    ← 定位主系统 .qdai
   → 读 主系统 .qdai（自然语言 + PY 脚本）         ← 获取调用规范与细节
   → 经授权通道（眼睛=读 / 笔=写 / 橡皮=删）
   → 主系统（module-engine，6801 内部转发）
```

- **外部调用方**（主系统界面 ai 命令、me> 控制台）：连 6800 → ai_service 校验 → 转发 6801 → module-engine 执行；
- **AI 主动申请**（内部发起，如 MEM / MEMO）：ai_service 直接经 6801 发往 module-engine，无需再经 6800 绕回自己；
- **模块调用**（v1.1）：AI 调用主系统模块（如 `run <模块>`）时经 module-engine 执行；**v1.3 更正**：原 v1.1"module-engine 先读该模块 `.qds` 的 `sys_deps` 校验系统区依赖存在性"为**未实现描述**——module-engine 契约解析仅 entry/version/desc，**不解析 sys_deps**（见开发文档 v1.13 §7 与包管理文档 v1.4 §3.2）。当前调用行为：模块存在即 run、不存在即报"模块未安装"；依赖校验随模块总线（阶段 9）落地；
- **未授权即断开**：AI 无对应工具授权时，跨框架通道断开，调用被拒。

### 3.2 双域权限模型（强安全隔离的核心）

**域一：框架内权限**（ai.qd 容器内，天然拥有，不受限）

| 能力 | 说明 |
| --- | --- |
| 读 | 对框架内文件与状态的最大读取权 |
| 写 | 对框架内文件与配置的最大写入权 |
| 删 | 对框架内内容的最大删除权 |
| 服务 / 硬件 | 可调用系统服务与底层硬件（网络、内存、设备接口）——**仅限容器内** |

**域二：框架间权限**（跨框架访问主系统，默认隔离）

| 工具 | 对应能力 | 授权后 AI 能做什么 |
| --- | --- | --- |
| 眼睛（读） | 读 | 读取主系统屏幕与内容，进行可视化窗口理解 |
| 笔（写） | 写 | 对主系统进行写入、修改 |
| 橡皮（删） | 删 | 对主系统进行删除操作 |

- 授权唯一来源是用户，可随时撤销；撤销后 AI 立即回到隔离状态；
- 任何"声明需要权限"（如 skill 声明）只是请求，不等于授权。

## 4. AI 服务接口（TCP 6800）

### 4.1 基础指令（已实现）

| 指令 | 作用 | 返回 |
| --- | --- | --- |
| `STATUS` | 查询 AI 服务状态 | 如实报告（引擎 / 模型 / 就绪状态） |
| `HELLO` | 连通性测试 | 问候 + 能力摘要 |
| `ECHO <文本>` | 回显测试 | 原样返回 |
| `HELP` | 指令列表 | 全部支持指令 |
| `SKILLS LIST` | 已装 skills | skills 列表 |
| `SKILLS INSTALL <skill>` | 安装 skill | 结果 |
| `SKILLS REMOVE <skill>` | 卸载 skill | 结果 |

### 4.2 对话指令（已实现，云端链路）

- 主系统界面 `ai <消息>` / `me> ai <消息>`：连 6800 → ai_service 转发云端 OpenAI 兼容接口 → 回复上屏；
- 当前云端配置：endpoint `https://api.xiaomimimo.com/v1`（OpenAI 兼容 `/chat/completions`），默认模型 `mimo-v2.5-pro`；
- 云端 API key 存放：系统内 `/子框架.qd/ai.qd/配置`（容器外备份于构建机），**完整密钥不得写入文档 / 日志 / 回复**。

### 4.3 规划指令（已设计，随阶段落地）

| 指令 | 作用 | 落地阶段 |
| --- | --- | --- |
| `MEM REQUEST <大小> <permanent\|borrow> [task_id]` | 向主系统申请内存 | 阶段 5 |
| `MEM RELEASE <申请id>` | 释放内存 | 阶段 5 |
| `MEM STATUS` | 查看内存配额 | 阶段 5 |
| `MEMO SEARCH <关键词>` | 记忆检索（主系统侧日历索引） | 阶段 5 |
| `MEMO READ <日期>` | 读记忆详情 | 阶段 5 |
| `MEMO WRITE <条目>` | 写记忆 | 阶段 5 |
| `TASK ...` | 统一任务管理（经主系统任务管理器） | 阶段 5 |

### 4.4 对外 HTTP API（TCP 6802，v1.2 新增，**已实现**）

#### 4.4.1 三端口职责划分

| 端口 | 协议 | 职责 | 监听 |
| --- | --- | --- | --- |
| **6800** | 裸文本行 | **人 / 主系统对话入口**：`me> ai`、uis、终端直连的文本协议（STATUS/HELLO/ECHO/HELP/SKILLS/CHAT），零回归保留 | `127.0.0.1` 仅 loopback |
| **6801** | 内部 TCP | **AI ↔ 主系统内部工具转发**：ai_service 作客户端连 module-engine，转发 MEMO/MEM/TASK/SKILLS/CONFIG | `127.0.0.1` 内部 |
| **6802** | HTTP | **对外 API（OpenAI 兼容）**：`/v1/models`、`/v1/chat/completions`（SSE 流式），供外部 SDK / 其他程序调用模型 | 默认 `127.0.0.1:6802`（`http_enabled` 可关；`http_port` 可配） |

> 三端口语义钉死：**6800 = 人/主系统对话，6801 = 内部工具转发，6802 = 对外 API**。三者互不重叠——6800 不承载转发（那是 6801），6802 不做内部工具调用（那是 6801）。

#### 4.4.2 端点定义

```
GET  /v1/models
POST /v1/chat/completions
```

- `GET /v1/models` 返回当前可用模型（模型名取 `g_model` / `llm_model` 当前生效值，兼容 OpenAI SDK 的模型列表查询）：

```json
{"object":"list","data":[{"id":"<当前模型>","object":"model","owned_by":"qd"}]}
```

- `POST /v1/chat/completions` 请求体与 OpenAI 一致：`{"model":"...","messages":[{"role":"user","content":"..."}],"stream":true|false}`。第一版只消费 `user` 消息（与现有 cloud_chat 对齐）；`system` 消息可选合并进内置 system prompt（见 §5.1）。
- **非流式响应**：

```json
{"id":"...","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"..."},"finish_reason":"stop"}]}
```

- **流式响应（`stream:true`，SSE）**：逐块发送，结束以 `data: [DONE]` 收尾：

```
data: {"choices":[{"delta":{"content":"你好"},"index":0}]}

data: {"choices":[{"delta":{"content":"，世界"},"index":0}]}

data: [DONE]

```

#### 4.4.3 鉴权策略（loopback 免鉴权 / 对外强制 Bearer）

- **默认仅 loopback**：绑定 `127.0.0.1:6802`，对外零暴露，不校验密钥（OpenAI SDK 兼容模式下接受并忽略 Bearer 头）；
- **配置开启"对外监听"**（绑定非 loopback 地址 / `http_enabled` 对外）：**强制校验 `Authorization: Bearer <api_key>`**，与 ai.qd 配置 `api_key` 比对，不符返回 401；
- 配置键：`http_enabled`（on/off）、`http_port`（默认 6802）、`http_host`（默认 127.0.0.1）、`api_key`。**api_key 只存系统内 `/子框架.qd/ai.qd/配置`，文档 / 日志 / 回复中一律用 `<你的API Key>` 占位，禁止写出真实密钥**。

#### 4.4.4 错误语义

| 状态码 | 含义 | 触发 |
| --- | --- | --- |
| `400` | 请求错误 | 缺 `model` / `messages`，或请求体非法 |
| `401` | 未授权 | 对外监听下 Bearer 缺失或 `api_key` 不匹配 |
| `502` | 上游失败 | 云端 / 本地 LLM 上游调用失败（网络错误、上游非 2xx） |
| `504` | 上游超时 | 沿用 40s deadline，超时 SIGKILL 后返回 |

> 模型路由对调用方透明：无论上游是云端 mimo（`api.xiaomimimo.com/v1`，`mimo-v2.5-pro`）还是本地 llama.cpp 端点，外部只面对 6802 一个 OpenAI 兼容入口（网关语义）。functions/tools 字段列为二期，与 §5.1 工具 schema 注入同一批落地。

## 5. 本地 Agent 调用规范

### 5.1 模型接入（现状 + 规划）

- **现状（云端）**：ai_service 已接云端 OpenAI 兼容 API（mimo-v2.5-pro），对话可跑；
- **规划（本地）**：本地 LLM（MiniCPM-2B）经 llama.cpp 接入，推理引擎作为 ai_service 的后端之一；
- **调度原则（用户确认）**：模型按需加载、用一个调一个、不全部同时调用；不用时一点不调用；
- **架构分流（用户确认）**：内核识别 x86 架构激活奇点 Linux 开发版路径；识别 ARM 架构激活 MNN 路径——让模型跑出最大 / 最多 token。

#### 工具清单配置化（v1.2 新增，**已实现**——移植 phi-agent feature-flags 思路）

此前 MEMO/MEM/TASK/SKILLS 等工具能力**从未以任何形式交给模型**（模型永远只收到一句用户文本，工具只能人手敲协议触发）。v1.2 起改为配置开关，**v1.3 核账：ai_service v0.6 已实现**：

- ai.qd 配置新增 `tools=memo,task,skills`（逗号分隔的启用清单）——**未列出的工具不向模型注入 schema、不暴露**（对应 phi-agent"不注册即不暴露"，零未用工具 token）；
- `tool_max_chars`（默认 4000）统一截断 6801 转发回来的工具结果，截断带 `...(truncated)` 标记；
- 工具清单经 6802 的 functions/tools 字段暴露给外部 SDK 调用方（二期），内部则拼进 system prompt。

#### system prompt 最小化注入规则（v1.2 新增，**已实现**）

- **不整份回灌**：ai.qdai（~2KB 八段式约束）此前从不注入请求；若直接整文件注入又会变成每轮固定 2KB 开销——因此采用 phi-agent 式精简注入，不是整文件注入；
- **拆最小片段、按需拼接**：system prompt 拆为身份 / 能力 / 边界三个最小片段，每片段 <50 token，按当前启用的 `tools` 清单与场景拼接（PromptFragment 思路）；
- 第一版只注入"当前模型该知道的最小上下文"，避免调用方每轮重述背景。

### 5.2 记忆：挂主系统（不在 AI 容器内）

- AI 统一记忆位于主系统：`/子框架.qd/主框架.qd/记忆.qd`（日历索引 + 详情）；
- AI 读写记忆需向主系统申请（MEMO 协议），不直接落盘到容器内——**减少 ai.qd 一层的存储与运行内存**；
- 记忆检索流程：MEMO SEARCH → 主系统提供简介（日历）→ 命中候选 → 读详情；多个候选无法确认时丢给用户确认。

#### 历史压缩摘要注入（v1.2 新增，**已实现**——phi-agent 式事实注入）

此前对话历史一条不存、每轮单轮无状态。v1.2 起多轮上下文按"摘要"而非"全量"注入：

- **每轮只注入 N 条事实摘要**（如 `history_facts_n=10`）：把上一轮及更早对话压缩成 N 条关键事实摘要，注入下一条 user/system 消息；**不回灌完整历史数组**；
- **事实注入（TurnFact）**：上一轮工具调用（6801 转发 MEMO/TASK 等）返回的结果，只把其中的"事实"摘要注入下一轮，不把整段原始返回塞进上下文；
- **压缩中间件**：每次模型调用前对对话历史做压缩，配套 `run_compact_session`（对应 phi-agent CompressionMiddleware）；
- 与记忆.qd 分工：长期记忆走主系统 MEMO 协议（日历索引 + 详情，防短路），会话内短期上下文走本摘要机制——两者都遵守"不一次性拉全量进上下文"。

### 5.3 内存：向主系统申请

ai.qd 识别到自身运行内存不足时，向主系统统一任务管理器申请（经 6801 → module-engine）：

| 类型 | 语义 | 结束方式 |
| --- | --- | --- |
| `permanent`（永久） | 长期获得该额度（持久化，等价提高 memory_max） | 须经用户确认（用户在 me> 执行 config set 落地，写入审计段） |
| `borrow`（借用） | 临时借用，绑定 task_id | 任务完成自动释放归还资源池 |

- `memory_max` 的唯一合法修改入口：用户在模块界面 `config set ai.qd memory_max <值>`（用户直接操作即生效）；
- 极端环境预算：骨架 ≤1G / 本地 AI ≥3.5G / 主系统 0.5G。

### 5.4 状态如实报告（铁律）

- 未接入本地模型前，不得谎称已具备推理能力；
- `STATUS` 必须如实报告：当前引擎（云端 / 本地）、模型、就绪状态；
- 就绪判定：boot.qds 轮询 `/tmp/ai.log`，匹配 `grep -q "就绪"`（ai_service 启动成功时 stdout / stderr 双写）。

## 6. AI 与主系统的关系（架构语义）

- **主系统的 Agent 可以调用 ai.qd 的 API 对主系统进行操作**：主系统界面 `ai` 命令 → 6800 → ai_service → 模型推理 → 经授权通道执行主系统操作；
- **ai.qd 也可以作为常驻系统级 AI 独立存在**：不依赖桌面 / 界面，终端、其他模块、甚至另一台奇点 OS 都能通过约定通道调它；
- 桌面只是 AI 的"脸"，不是 AI 的"身体"——AI.QD 自身有独立的常驻服务、记忆库与知识库。

## 7. 现状核对表

| 项 | 状态 |
| --- | --- |
| AI.QD chroot 隔离容器 + ai_service 常驻 | ✅ 已实现并验证 |
| 6800 基础指令（STATUS/HELLO/CHAT/CONFIG/HELP/SKILLS） | ✅ 已实现并验证（v0.6：STATUS/HELLO/CHAT/CONFIG/HELP/SKILLS LIST\|INSTALL\|REMOVE） |
| 云端 API 对话（OpenAI 兼容，mimo-v2.5-pro） | ✅ 已实现并验证 |
| ai.qdai 八段式约束（引导：身份/资源/权限/连通/职责/约束/架构/升级） | ✅ 已实现（引导 AI 定位主系统 .qdai） |
| 主系统 .qdai（自然语言 + PY 脚本调用规范） | 📐 待建（随阶段 5 落地，AI 调用主系统的唯一依据） |
| 双域权限模型（框架内 / 框架间 + 工具授权） | ✅ 已定义；授权工具接口（grant/revoke/auth status）已在 module-engine v0.5 实现，AI 侧落地 📐 阶段 2 |
| SKILLS 仓库与指令 | ✅ 已实现（仓库实体就绪） |
| 6801 内部链路（ai_service → module-engine） | ✅ 已实现（module-engine 侧：v0.5 已绑定 6801 loopback 监听，FD_CLOEXEC）；ai_service 侧 6801 转发闭环 📐 阶段 5 |
| MEM / MEMO / TASK 协议 | 📐 已设计（v1.4/v1.5）；**module-engine 侧 memo/task/mem 命令已实现**，ai_service 侧转发闭环随阶段 5 |
| 本地 LLM 接入（MiniCPM-2B 等） | 📐 规划（阶段 3） |
| 6802 HTTP OpenAI 兼容 API（/v1/models、/v1/chat/completions SSE、loopback/对外 Bearer 鉴权、400/401/502/504） | ✅ 已实现（v0.6：配置键 http_enabled/http_port/http_listen 已加载，代码实现） |
| 三端口职责分离（6800 对话 / 6801 内部转发 / 6802 对外 API） | ✅ 已实现（6800/6802 在 ai_service；6801 在 module-engine） |
| 工具清单配置化（tools=memo,task,skills 开关 + tool_max_chars 截断） | ✅ 已实现（v0.6：tools/tool_max_chars 配置键 + 截断注入） |
| system prompt 最小化注入（片段 <50 token、按需拼接） | ✅ 已实现（v0.6：身份/能力/边界三片段最小化注入） |
| 历史压缩摘要注入（summary_max_chars，每轮只注入摘要） | ✅ 已实现（v0.6：进程内滚动历史摘要） |

> 记号：✅ 已实现并验证；📐 已设计待落地。

## 附：v1.3 审查修复记录

| #  | 问题 | 修复 |
| -- | --- | --- |
| 81 | §7 现状表把 6802 HTTP API、三端口职责、工具清单配置化、system prompt 最小化、历史压缩摘要全部标"📐 设计定稿，随阶段 13 / Agent 极简升级落地"——**实际 ai_service v0.6 已全部实现**（6802 监听、http_enabled/http_port 配置键、tools/tool_max_chars/summary_max_chars 配置键并注入） | §7 对应行改 ✅ 已实现（v0.6）；§4.4/§5.1/§5.2 小节标题同步标"已实现" |
| 82 | §3.1 模块调用"module-engine 先读 .qds 的 sys_deps 校验依赖存在性，缺失即报错"——**module-engine 不解析 sys_deps**（read_contract 仅 entry/version/desc） | 更正为"当前不校验依赖；模块存在即 run、缺失报'模块未安装'；依赖校验随模块总线阶段 9 落地" |
| 83 | §1.1 AI 配置"含云端 API key / memory_max"——**memory_max 不是 ai_service 配置键**（属主系统侧资源管理概念） | 配置清单改为实际键（api_key/base_url/model/llm_mode/http_*/tools/...），memory_max 说明归位主系统 me> config set |
| 84 | 文头关联文档版本滞后 | 更新为开发文档 v1.13、包管理文档 v1.4 |
