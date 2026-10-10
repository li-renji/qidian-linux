/*
 * module-engine.c — 奇点OS 模块引擎 v0.4（第四次重构版）
 * 主系统 = 最大模块容器：本引擎是主系统的调度器，所有系统级组件
 * 以 .qd 模块形式挂在 /modules 下，模块之间只存在调用关系，可热替换。
 *
 * v0.4 变更（对齐开发文档 v1.9 全部已设计项）：
 *   - MEMO 授权校验闭环（7C.4）：MEMO READ 需 eye、WRITE 需 pen，未授权拒绝
 *   - 统一任务管理器（7C.3）：TASK REGISTER/HEARTBEAT/DONE/LIST + MEM REQUEST/RELEASE/STATUS
 *     permanent 转交用户确认（me> 待确认 y/n，管理器不自行批准）；借用超时（90s）强制回收
 *   - 资源管理.qds 持久化（type=resource：资源池/分配记录/审计）
 *   - config get/set（7A.4）：模块配置 .qds 读写（如 ai.qd 的 memory_max）
 *   - SKILLS INSTALL/REMOVE（7A.4 + 7A.6）：安装时声明权限转用户确认后生效
 *   - 模块自检报错（阶段 8）：.qd 目录缺 .qds 契约时启动/扫描报错
 *   - 包管理/渲染器/用户态 拆为 /modules 下独立 .qd 模块
 *   - run <模块> / modules / pm 命令
 * 保留：6801 内部链路、授权工具、记忆、ai 命令、me> 控制台
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/reboot.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <poll.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <time.h>

#define MODULES_DIR   "/modules"
#define MAIN_DIR      "/子框架.qd/主框架.qd"
#define AUTH_FILE     MAIN_DIR "/授权.qds"
#define MEMO_DIR      MAIN_DIR "/记忆.qd"
#define MEMO_INDEX    MEMO_DIR "/索引.qds"
#define RES_FILE      MAIN_DIR "/资源管理.qds"
#define AI_CFG        "/子框架.qd/ai.qd/配置.qds"
#define AI_RUNTIME_CFG "/子框架.qd/ai.qd/配置"
#define REPO_DIR      "/仓库"
#define AI_SKILLS     "/子框架.qd/ai.qd/skills"
#define AI_PORT       6800
#define INT_PORT      6801
#define MAX_MODULES   256
#define MAX_TASKS     32
#define BUF           1024

/* ---------------- 模块扫描与契约解析 ---------------- */

struct module_entry {
    char name[128];       /* 目录/文件名 */
    char kind[8];         /* qd / qds / qdai / dir */
    char entry[256];      /* 可执行入口（.qd 模块契约） */
    char version[64];     /* 版本 */
    char desc[256];       /* 描述 */
    size_t size;
};

/* 纯 C 递归复制（定义见包管理区块；skills/install 等提前引用） */
static int copy_rec(const char *src, const char *dst);

/* 6801 请求解析（定义见 6801 区块；me> ai 本地执行提前引用） */
static void handle_6801_text(const char *buf, char *resp, size_t sz);

static struct module_entry modules[MAX_MODULES];
static int module_count = 0;

static const char *classify(const char *path)
{
    const char *dot = strrchr(path, '.');
    if (!dot) return "?";
    if (strcmp(dot, ".qd") == 0) return "qd";
    if (strcmp(dot, ".qds") == 0) return "qds";
    if (strcmp(dot, ".qdai") == 0) return "qdai";
    if (strcmp(dot, ".dir") == 0) return "dir";
    return "?";
}

/* 读取 .qd 模块目录内的 .qds 契约（type=guide） */
static void read_contract(const char *dirpath, char *entry, size_t esz,
                          char *version, size_t vsz, char *desc, size_t dsz)
{
    entry[0] = 0; version[0] = 0; desc[0] = 0;
    char p[512];
    snprintf(p, sizeof(p), "%s/.qds", dirpath);
    FILE *f = fopen(p, "r");
    if (!f) return;
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strncmp(line, "entry=", 6) == 0) snprintf(entry, esz, "%s", line + 6);
        else if (strncmp(line, "version=", 8) == 0) snprintf(version, vsz, "%s", line + 8);
        else if (strncmp(line, "desc=", 5) == 0) snprintf(desc, dsz, "%s", line + 5);
    }
    fclose(f);
}

/* 扫描 /modules：.qd 目录读取契约，.qds/.qdai 文件登记 */
static int scan_modules(void)
{
    DIR *d = opendir(MODULES_DIR);
    struct dirent *ent;
    if (!d) { fprintf(stderr, "模块引擎: 无法打开 %s (%s)\n", MODULES_DIR, strerror(errno)); return -1; }
    module_count = 0;
    while ((ent = readdir(d)) != NULL) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
        char path[256];
        snprintf(path, sizeof(path), "%s/%s", MODULES_DIR, ent->d_name);
        struct stat st;
        if (stat(path, &st) != 0) continue;
        const char *kind = classify(ent->d_name);
        if (strcmp(kind, "?") == 0 && !S_ISDIR(st.st_mode)) continue;
        if (module_count >= MAX_MODULES) break;
        snprintf(modules[module_count].name, sizeof(modules[module_count].name), "%s", ent->d_name);
        snprintf(modules[module_count].kind, sizeof(modules[module_count].kind), "%s",
                 S_ISDIR(st.st_mode) ? "dir" : kind);
        modules[module_count].size = (size_t)st.st_size;
        /* .qd 模块目录：读取目录内 .qds 契约（type=guide → name/version/desc/entry） */
        if (S_ISDIR(st.st_mode) && strcmp(kind, "qd") == 0) {
            /* 模块自检报错（阶段 8）：.qd 缺 .qds 契约即报错，驱动修复 */
            char contract[256];
            snprintf(contract, sizeof(contract), "%s/.qds", path);
            struct stat cst;
            int has_contract = (stat(contract, &cst) == 0);
            if (!has_contract) {
                printf("⚠ 模块自检失败: %s 缺少 .qds 契约（应含首行 type=guide）\n",
                       ent->d_name);
                fflush(stdout);
            } else {
                read_contract(path, modules[module_count].entry,
                              sizeof(modules[module_count].entry),
                              modules[module_count].version,
                              sizeof(modules[module_count].version),
                              modules[module_count].desc,
                              sizeof(modules[module_count].desc));
            }
        }
        module_count++;
    }
    closedir(d);
    return module_count;
}

/* ---------------- 授权工具（眼睛/笔/橡皮） ---------------- */

struct auth_state { int eye, pen, eraser; };
static struct auth_state g_auth = { 0, 0, 0 };

static void auth_load(void)
{
    FILE *f = fopen(AUTH_FILE, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strncmp(line, "eye=", 4) == 0) g_auth.eye = (line[4] == '1');
        else if (strncmp(line, "pen=", 4) == 0) g_auth.pen = (line[4] == '1');
        else if (strncmp(line, "eraser=", 7) == 0) g_auth.eraser = (line[7] == '1');
    }
    fclose(f);
}

static void auth_save(void)
{
    mkdir(MAIN_DIR, 0755);
    FILE *f = fopen(AUTH_FILE, "w");
    if (!f) return;
    fprintf(f, "type=auth\neye=%d\npen=%d\neraser=%d\n", g_auth.eye, g_auth.pen, g_auth.eraser);
    fclose(f);
}

static void auth_print(void)
{
    printf("授权工具状态（用户授权，AI 跨框架能力开关）:\n");
    printf("  眼睛(读)  : %s\n", g_auth.eye ? "✅ 已授权" : "❌ 未授权");
    printf("  笔(写)    : %s\n", g_auth.pen ? "✅ 已授权" : "❌ 未授权");
    printf("  橡皮(删)  : %s\n", g_auth.eraser ? "✅ 已授权" : "❌ 未授权");
}

/* ---------------- 统一任务管理器（7C.3）+ 资源管理.qds ---------------- */

struct task { int id; char name[64]; int mem_mb; time_t last_hb; int dead; };
static struct task tasks[MAX_TASKS];
static int task_count = 0;
static int next_task_id = 1;

/* 资源池（应用层可调度上限；骨架模式主 0.5G + ai.qd 0.5G 取 512M 池） */
static int res_pool_mb = 512;
static int res_used_borrow = 0;
static int res_permanent_mb = 0;

static void res_load(void)
{
    FILE *f = fopen(RES_FILE, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strncmp(line, "pool_mb=", 8) == 0) res_pool_mb = atoi(line + 8);
        else if (strncmp(line, "used_borrow=", 12) == 0) res_used_borrow = atoi(line + 12);
        else if (strncmp(line, "permanent=", 10) == 0) res_permanent_mb = atoi(line + 10);
    }
    fclose(f);
}

static void res_save(const char *audit)
{
    mkdir(MAIN_DIR, 0755);
    FILE *f = fopen(RES_FILE, "w");
    if (!f) return;
    fprintf(f, "type=resource\npool_mb=%d\nused_borrow=%d\npermanent=%d\n",
            res_pool_mb, res_used_borrow, res_permanent_mb);
    if (audit && audit[0])
        fprintf(f, "audit: %s\n", audit);
    fclose(f);
}

static struct task *task_find(int id)
{
    for (int i = 0; i < task_count; i++)
        if (tasks[i].id == id && !tasks[i].dead) return &tasks[i];
    return NULL;
}

static int task_register(const char *name, int mem)
{
    if (task_count >= MAX_TASKS) return -1;
    tasks[task_count].id = next_task_id++;
    snprintf(tasks[task_count].name, sizeof(tasks[task_count].name), "%s", name);
    tasks[task_count].mem_mb = mem;
    tasks[task_count].last_hb = time(NULL);
    tasks[task_count].dead = 0;
    task_count++;
    return tasks[task_count - 1].id;
}

static void task_done(int id, char *resp, size_t sz)
{
    struct task *t = task_find(id);
    if (!t) { snprintf(resp, sz, "ERR 任务不存在或已结束: %d\n", id); return; }
    t->dead = 1;
    /* 回收该任务绑定的借用 */
    if (t->mem_mb > 0) {
        res_used_borrow -= t->mem_mb;
        if (res_used_borrow < 0) res_used_borrow = 0;
        char audit[256];
        snprintf(audit, sizeof(audit), "TASK DONE id=%d 回收借用 %dMB",
                 id, t->mem_mb);
        res_save(audit);
    }
    snprintf(resp, sz, "OK 任务 %d 完成，借用已回收\n", id);
}

/* 超时回收：心跳缺失超 3 周期（90s）→ 强制回收借用（7C.3 兜底） */
static void task_reap(void)
{
    time_t now = time(NULL);
    for (int i = 0; i < task_count; i++) {
        if (tasks[i].dead) continue;
        if (now - tasks[i].last_hb > 90) {
            int id = tasks[i].id;
            int mem = tasks[i].mem_mb;
            tasks[i].dead = 1;
            if (mem > 0) {
                res_used_borrow -= mem;
                if (res_used_borrow < 0) res_used_borrow = 0;
                char audit[256];
                snprintf(audit, sizeof(audit), "超时回收 task=%d (%s) 强制释放 %dMB",
                         id, tasks[i].name, mem);
                res_save(audit);
            }
            printf("⚠ 任务 %d (%s) 心跳超时，已强制回收借用内存\n", id, tasks[i].name);
            fflush(stdout);
        }
    }
}

static void mem_status(char *resp, size_t sz)
{
    snprintf(resp, sz,
             "资源池: %dMB | 已借出: %dMB | 永久分配: %dMB | 可用: %dMB | 任务: %d\n",
             res_pool_mb, res_used_borrow, res_permanent_mb,
             res_pool_mb - res_used_borrow - res_permanent_mb, task_count);
}

/* AI 发起的 MEM REQUEST；permanent 转用户确认（g_pending 机制） */
static int g_pending = 0;          /* 1 = 有待确认请求（me> 输入进入确认模式） */
static char g_pending_desc[256];   /* 待确认内容描述 */
static char g_pending_req[256];    /* 确认后要执行的指令（module-engine 内部处理） */

static void pending_prompt(void)
{
    printf("\n⚠ 待用户确认（模块界面 me>）：\n  %s\n  输入 y 同意 / n 拒绝：",
           g_pending_desc);
    fflush(stdout);
}

static void mem_request(int mb, const char *kind, const char *task_arg, char *resp, size_t sz)
{
    if (mb <= 0) { snprintf(resp, sz, "ERR 大小非法\n"); return; }
    if (strcmp(kind, "permanent") == 0) {
        /* 管理器不得自行批准：转用户确认（7C.3 安全约束） */
        if (g_pending) { snprintf(resp, sz, "ERR 已有待确认请求，请先在 me> 处理\n"); return; }
        g_pending = 1;
        snprintf(g_pending_desc, sizeof(g_pending_desc),
                 "AI 申请永久分配 %dMB（当前 memory_max=%dMB）", mb, res_permanent_mb + mb);
        snprintf(g_pending_req, sizeof(g_pending_req), "PERMANENT %d", mb);
        pending_prompt();
        snprintf(resp, sz, "PENDING 已转交用户确认（请在 me> 控制台输入 y/n）\n");
        return;
    }
    if (strcmp(kind, "borrow") == 0) {
        int tid = atoi(task_arg);
        struct task *t = task_find(tid);
        if (!t) { snprintf(resp, sz, "ERR 借用必须绑定有效 task_id\n"); return; }
        if (res_used_borrow + res_permanent_mb + mb > res_pool_mb) {
            snprintf(resp, sz, "ERR 资源池不足（可用 %dMB）\n",
                     res_pool_mb - res_used_borrow - res_permanent_mb);
            return;
        }
        t->mem_mb += mb;
        res_used_borrow += mb;
        char audit[256];
        snprintf(audit, sizeof(audit), "MEM REQUEST borrow task=%d %dMB", tid, mb);
        res_save(audit);
        snprintf(resp, sz, "OK 已借用 %dMB（task=%d），可用 %dMB\n", mb, tid,
                 res_pool_mb - res_used_borrow - res_permanent_mb);
        return;
    }
    snprintf(resp, sz, "ERR 类型须为 permanent 或 borrow\n");
}

static void mem_release(const char *req, char *resp, size_t sz)
{
    /* 释放指定借用：按任务名/大小匹配简化——找到持有该借用额度的任务并归还原额度 */
    int mb = atoi(req);
    if (mb <= 0) { snprintf(resp, sz, "ERR 格式: MEM RELEASE <大小MB>\n"); return; }
    if (res_used_borrow >= mb) {
        res_used_borrow -= mb;
        char audit[256];
        snprintf(audit, sizeof(audit), "MEM RELEASE %dMB", mb);
        res_save(audit);
        snprintf(resp, sz, "OK 已释放 %dMB，可用 %dMB\n", mb,
                 res_pool_mb - res_used_borrow - res_permanent_mb);
    } else {
        snprintf(resp, sz, "ERR 借出额度不足（已借出 %dMB）\n", res_used_borrow);
    }
}

/* ---------------- config get/set（7A.4：模块配置 .qds） ---------------- */

/* 定位模块配置：ai.qd 用 /子框架.qd/ai.qd/配置.qds；其他模块用 /modules/<模块>.qd/配置.qds */
static void cfg_path_for(const char *mod, char *out, size_t sz)
{
    if (strcmp(mod, "ai.qd") == 0 || strcmp(mod, "ai") == 0)
        snprintf(out, sz, "%s", AI_CFG);
    else {
        if (strstr(mod, ".qd"))
            snprintf(out, sz, "/modules/%s/配置.qds", mod);
        else
            snprintf(out, sz, "/modules/%s.qd/配置.qds", mod);
    }
}

static void cfg_get(const char *mod, char *resp, size_t sz)
{
    char p[BUF];
    size_t off = 0;
    resp[0] = 0;
    if (strcmp(mod, "ai.qd") == 0) {
        /* 合并显示：配置.qds（memory_max 等）+ ai_service 运行时配置 /配置（llm_* 等） */
        FILE *f = fopen(AI_RUNTIME_CFG, "r");
        if (f) {
            char line[512];
            while (fgets(line, sizeof(line), f) && off + 256 < sz) {
                if (strncmp(line, "llm_", 4) != 0) continue;
                size_t l = strlen(line);
                if (off + l >= sz) break;
                memcpy(resp + off, line, l);
                off += l;
            }
            fclose(f);
        }
        cfg_path_for(mod, p, sizeof(p));
        FILE *g = fopen(p, "r");
        if (g) {
            char line[512];
            while (fgets(line, sizeof(line), g) && off + 256 < sz) {
                size_t l = strlen(line);
                if (off + l >= sz) break;
                memcpy(resp + off, line, l);
                off += l;
            }
            fclose(g);
        }
        if (off == 0) snprintf(resp, sz, "（ai.qd 暂无配置）\n");
        return;
    }
    cfg_path_for(mod, p, sizeof(p));
    FILE *f = fopen(p, "r");
    if (!f) { snprintf(resp, sz, "ERR 模块 %s 无配置文件（%s）\n", mod, p); return; }
    char line[512];
    while (fgets(line, sizeof(line), f) && off + 256 < sz) {
        size_t l = strlen(line);
        memcpy(resp + off, line, l);
        off += l;
    }
    fclose(f);
    if (off == 0) snprintf(resp, sz, "（空配置）\n");
}

/* 按行改写 <键>=<值>，保留其余行 */
static void cfg_set(const char *mod, const char *key, const char *val, char *resp, size_t sz)
{
    char p[BUF];
    /* ai.qd 的 llm_* 键 → ai_service 运行时配置（容器内 /配置），对话动态生效 */
    if (strcmp(mod, "ai.qd") == 0 && strncmp(key, "llm_", 4) == 0)
        snprintf(p, sizeof(p), "%s", AI_RUNTIME_CFG);
    else
        cfg_path_for(mod, p, sizeof(p));
    char tmp[BUF + 64];
    snprintf(tmp, sizeof(tmp), "%s.tmp", p);
    FILE *in = fopen(p, "r");
    FILE *out = fopen(tmp, "w");
    if (!out) { snprintf(resp, sz, "ERR 无法写配置 %s\n", p); return; }
    int found = 0;
    if (in) {
        char line[512];
        while (fgets(line, sizeof(line), in)) {
            line[strcspn(line, "\r\n")] = 0;
            if (strncmp(line, key, strlen(key)) == 0 && line[strlen(key)] == '=') {
                fprintf(out, "%s=%s\n", key, val);
                found = 1;
            } else {
                fprintf(out, "%s\n", line);
            }
        }
        fclose(in);
    } else {
        fprintf(out, "type=config\n");
    }
    if (!found) fprintf(out, "%s=%s\n", key, val);
    fclose(out);
    rename(tmp, p);
    /* 审计：config set 用户直操作即生效（7A.4/7C.3） */
    char audit[256];
    snprintf(audit, sizeof(audit), "CONFIG SET %s.%s=%s (用户操作)", mod, key, val);
    res_save(audit);
    snprintf(resp, sz, "OK 已设置 %s.%s=%s（用户操作即生效，已写审计）\n",
             mod, key, val);
}

/* ---------------- SKILLS 安装（7A.4/7A.6） ---------------- */

/* 从 /仓库/实体/<skill>.qd 复制到 AI 容器 skills 区；声明权限转用户确认 */
static void skills_install(const char *skill, char *resp, size_t sz)
{
    char src[BUF], dst[BUF];
    if (strstr(skill, ".qd"))
        snprintf(src, sizeof(src), "/仓库/实体/%s", skill);
    else
        snprintf(src, sizeof(src), "/仓库/实体/%s.qd", skill);
    struct stat st;
    if (stat(src, &st) != 0 || !S_ISDIR(st.st_mode)) {
        snprintf(resp, sz, "ERR 仓库中无此技能: %s\n", skill);
        return;
    }
    mkdir(AI_SKILLS, 0755);
    snprintf(dst, sizeof(dst), "%s/%s", AI_SKILLS, skill);
    if (copy_rec(src, dst) != 0) { snprintf(resp, sz, "ERR 安装失败\n"); return; }
    /* 读取声明权限（skill.qds 内 permissions= 行） */
    char sp[BUF];
    snprintf(sp, sizeof(sp), "%s/skill.qds", src);
    FILE *f = fopen(sp, "r");
    char perm[64] = "";
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            line[strcspn(line, "\r\n")] = 0;
            if (strncmp(line, "permissions=", 12) == 0) {
                snprintf(perm, sizeof(perm), "%s", line + 12);
                break;
            }
        }
        fclose(f);
    }
    if (perm[0]) {
        /* 声明权限 = 请求 ≠ 授权（7A.6）：转用户确认 */
        if (g_pending) { snprintf(resp, sz, "ERR 已有待确认请求，请先在 me> 处理\n"); return; }
        g_pending = 1;
        snprintf(g_pending_desc, sizeof(g_pending_desc),
                 "技能 %s 声明需要权限: %s —— 是否授出？", skill, perm);
        snprintf(g_pending_req, sizeof(g_pending_req), "SKILLS_AUTH %s %s", skill, perm);
        pending_prompt();
        snprintf(resp, sz, "PENDING 已安装 %s；权限授予待用户确认（me> y/n）\n", skill);
    } else {
        snprintf(resp, sz, "OK 已安装技能 %s（未声明权限）\n", skill);
    }
}

static void skills_remove(const char *skill, char *resp, size_t sz)
{
    char p[BUF];
    snprintf(p, sizeof(p), "%s/%s", AI_SKILLS, skill);
    struct stat st;
    if (stat(p, &st) != 0) { snprintf(resp, sz, "ERR 未安装: %s\n", skill); return; }
    if (remove(p) != 0) { snprintf(resp, sz, "ERR 移除失败\n"); return; }
    snprintf(resp, sz, "OK 已移除技能 %s\n", skill);
}

/* 处理待确认结果（me> 输入 y/n）：permanent 与 skill 授权落地 */
static void pending_resolve(int agree)
{
    g_pending = 0;
    if (strncmp(g_pending_req, "PERMANENT ", 10) == 0) {
        int mb = atoi(g_pending_req + 10);
        if (agree) {
            res_permanent_mb += mb;
            char audit[256];
            snprintf(audit, sizeof(audit), "PERMANENT 批准 +%dMB（审批人=用户）", mb);
            res_save(audit);
            printf("✓ 已批准 AI 永久分配 %dMB（memory_max=%dMB）\n", mb, res_permanent_mb);
        } else {
            printf("✗ 已拒绝 AI 永久分配申请（不降级为借用）\n");
        }
    } else if (strncmp(g_pending_req, "SKILLS_AUTH ", 12) == 0) {
        char *sp = g_pending_req + 12;
        char *pp = strchr(sp, ' ');
        if (pp) *pp = 0;
        if (agree) {
            /* 逐个授出声明权限 */
            const char *perms = pp ? pp + 1 : "";
            if (strstr(perms, "eye")) g_auth.eye = 1;
            if (strstr(perms, "pen")) g_auth.pen = 1;
            if (strstr(perms, "eraser")) g_auth.eraser = 1;
            auth_save();
            printf("✓ 已为技能 %s 授出权限（%s）\n", sp, perms);
        } else {
            printf("✗ 已拒绝技能 %s 的权限请求（技能已装但无跨框架权限）\n", sp);
        }
    }
    fflush(stdout);
}

/* ---------------- 记忆（记忆.qd 挂主系统） ---------------- */

static void memo_init_dir(void)
{
    mkdir(MEMO_DIR, 0755);
}

static void memo_update_index(const char *date, const char *brief)
{
    char tmp[BUF];
    snprintf(tmp, sizeof(tmp), "%s.tmp", MEMO_INDEX);
    FILE *out = fopen(tmp, "w");
    FILE *in = fopen(MEMO_INDEX, "r");
    if (out) {
        fprintf(out, "type=memory\n");
        if (in) {
            char line[512];
            while (fgets(line, sizeof(line), in)) {
                if (strncmp(line, "type=", 5) == 0) continue;
                if (strncmp(line, date, strlen(date)) == 0) continue;
                fputs(line, out);
            }
            fclose(in);
        }
        fprintf(out, "%s %s\n", date, brief);
        fclose(out);
        rename(tmp, MEMO_INDEX);
    } else if (in) fclose(in);
}

static int cmd_memo(int argc, char **argv)
{
    if (argc < 2) { printf("用法: memo index|read|write|search\n"); return 0; }
    if (strcmp(argv[1], "index") == 0) {
        FILE *f = fopen(MEMO_INDEX, "r");
        if (!f) { printf("记忆索引为空（尚无记录）\n"); return 0; }
        char line[512];
        while (fgets(line, sizeof(line), f)) {
            if (strncmp(line, "type=", 5) == 0) continue;
            printf("  %s", line);
        }
        fclose(f);
        return 0;
    }
    if (strcmp(argv[1], "read") == 0 && argc >= 3) {
        char p[BUF];
        snprintf(p, sizeof(p), "%s/%s.qd/内容.md", MEMO_DIR, argv[2]);
        FILE *f = fopen(p, "r");
        if (!f) { printf("未找到 %s 的记忆记录\n", argv[2]); return 0; }
        char c;
        while ((c = (char)fgetc(f)) != EOF) putchar(c);
        fclose(f);
        return 0;
    }
    if (strcmp(argv[1], "write") == 0 && argc >= 3) {
        char p[BUF], dirp[BUF];
        snprintf(dirp, sizeof(dirp), "%s/%s.qd", MEMO_DIR, argv[2]);
        mkdir(dirp, 0755);
        snprintf(p, sizeof(p), "%s/内容.md", dirp);
        FILE *f = fopen(p, "w");
        if (!f) { printf("写入失败\n"); return 0; }
        for (int i = 3; i < argc; i++) {
            if (i > 3) fputc(' ', f);
            fputs(argv[i], f);
        }
        fputc('\n', f);
        fclose(f);
        char brief[128];
        snprintf(brief, sizeof(brief), "%.60s", argc > 3 ? argv[3] : "");
        for (int i = 4; i < argc && strlen(brief) < 60; i++)
            snprintf(brief + strlen(brief), sizeof(brief) - strlen(brief), " %s", argv[i]);
        memo_update_index(argv[2], brief);
        printf("已写入记忆 %s\n", argv[2]);
        return 0;
    }
    if (strcmp(argv[1], "search") == 0 && argc >= 3) {
        FILE *f = fopen(MEMO_INDEX, "r");
        if (!f) { printf("记忆索引为空\n"); return 0; }
        char line[512];
        printf("索引中命中 \"%s\":\n", argv[2]);
        while (fgets(line, sizeof(line), f)) {
            if (strncmp(line, "type=", 5) == 0) continue;
            if (strstr(line, argv[2])) printf("  %s", line);
        }
        fclose(f);
        return 0;
    }
    printf("用法: memo index|read <日期>|write <日期> <内容>|search <词>\n");
    return 0;
}

/* ---------------- 6800 客户端（ai 命令：主系统 ↔ AI） ---------------- */

static int ai_chat(const char *text)
{
    /* 内部指令（6800 协议）：module-engine 本身就是主系统（6801 服务端），
     * 直接本地执行，避免走 6800→6801 绕圈导致主循环阻塞死锁 */
    if (strncmp(text, "MEMO ", 5) == 0 || strncmp(text, "MEM ", 4) == 0 ||
        strncmp(text, "TASK ", 5) == 0 || strncmp(text, "SKILLS ", 7) == 0 ||
        strncmp(text, "CONFIG ", 7) == 0 || strncmp(text, "AUTH ", 5) == 0 ||
        strcmp(text, "STATUS") == 0 || strcmp(text, "HELP") == 0 ||
        strcmp(text, "CONFIG") == 0 || strcmp(text, "HELLO") == 0) {
        char resp[BUF * 2];
        handle_6801_text(text, resp, sizeof(resp));
        printf("AI > %s\n", resp);
        return 0;
    }
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(AI_PORT);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { close(fd); return -1; }
    char req[BUF];
    snprintf(req, sizeof(req), "CHAT %s\n", text);
    write(fd, req, strlen(req));
    shutdown(fd, SHUT_WR);
    /* 读超时：防止 AI 服务异常时阻塞主循环；云端真实 API 响应 5-30s，给足 45s */
    struct timeval tv;
    tv.tv_sec = 45;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    char resp[BUF * 4];
    ssize_t n = read(fd, resp, sizeof(resp) - 1);
    close(fd);
    if (n <= 0) { printf("AI > （无响应或超时）\n"); return -1; }
    resp[n] = 0;
    printf("AI > %s\n", resp);
    return 0;
}

/* ---------------- 6801 内部链路（AI ↔ 主系统） ---------------- */

static void handle_6801(int fd)
{
    char buf[BUF];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    if (n <= 0) return;
    buf[n] = 0;
    buf[strcspn(buf, "\r\n")] = 0;

    char resp[BUF * 2];
    handle_6801_text(buf, resp, sizeof(resp));
    write(fd, resp, strlen(resp));
    close(fd);
}

/* 6801 请求解析（本地处理，抽出供 handle_6801 与 me> ai 复用） */
static void handle_6801_text(const char *buf, char *resp, size_t sz)
{
    resp[0] = 0;

    if (strncmp(buf, "AUTH STATUS", 11) == 0) {
        snprintf(resp, sz, "OK eye=%d pen=%d eraser=%d\n",
                 g_auth.eye, g_auth.pen, g_auth.eraser);
    } else if (strncmp(buf, "MEMO INDEX", 10) == 0) {
        FILE *f = fopen(MEMO_INDEX, "r");
        if (!f) { snprintf(resp, sz, "OK 空\n"); }
        else {
            char line[512];
            size_t off = 0;
            while (fgets(line, sizeof(line), f) && off + 256 < sz) {
                if (strncmp(line, "type=", 5) == 0) continue;
                size_t l = strlen(line);
                if (off + l + 1 > sz) break;
                memcpy(resp + off, line, l);
                off += l;
            }
            fclose(f);
            if (off == 0) snprintf(resp, sz, "OK 空\n");
        }
    } else if (strncmp(buf, "MEMO READ ", 10) == 0) {
        /* 7C.4：记忆读属眼睛域，需已授权 */
        if (!g_auth.eye) { snprintf(resp, sz, "ERR 未授权：记忆读属「眼睛」域，请用户在 me> 执行 grant eye\n"); return; }
        char p[BUF];
        snprintf(p, sizeof(p), "%s/%s.qd/内容.md", MEMO_DIR, buf + 10);
        FILE *f = fopen(p, "r");
        if (!f) snprintf(resp, sz, "ERR 无此记录\n");
        else {
            size_t off = 0;
            int c;
            while ((c = fgetc(f)) != EOF && off + 1 < sz) resp[off++] = (char)c;
            fclose(f);
            resp[off] = 0;
        }
    } else if (strncmp(buf, "MEMO WRITE ", 11) == 0) {
        /* 7C.4：记忆写属笔域，需已授权 */
        if (!g_auth.pen) { snprintf(resp, sz, "ERR 未授权：记忆写属「笔」域，请用户在 me> 执行 grant pen\n"); return; }
        char *sp = strchr(buf + 11, ' ');
        if (!sp) snprintf(resp, sz, "ERR 格式: MEMO WRITE <日期> <内容>\n");
        else {
            *sp = 0;
            char *date = buf + 11;
            char *content = sp + 1;
            char dirp[BUF], p[BUF];
            snprintf(dirp, sizeof(dirp), "%s/%s.qd", MEMO_DIR, date);
            mkdir(dirp, 0755);
            snprintf(p, sizeof(p), "%s/内容.md", dirp);
            FILE *f = fopen(p, "w");
            if (!f) snprintf(resp, sz, "ERR 写入失败\n");
            else {
                fprintf(f, "%s\n", content);
                fclose(f);
                char brief[128];
                snprintf(brief, sizeof(brief), "%.60s", content);
                memo_update_index(date, brief);
                snprintf(resp, sz, "OK 已写入记忆 %s\n", date);
            }
        }
    } else if (strncmp(buf, "MEMO SEARCH ", 12) == 0) {
        FILE *f = fopen(MEMO_INDEX, "r");
        if (!f) snprintf(resp, sz, "OK 空\n");
        else {
            char line[512];
            size_t off = 0;
            while (fgets(line, sizeof(line), f) && off + 256 < sz) {
                if (strncmp(line, "type=", 5) == 0) continue;
                if (strstr(line, buf + 12)) {
                    size_t l = strlen(line);
                    memcpy(resp + off, line, l);
                    off += l;
                }
            }
            fclose(f);
            if (off == 0) snprintf(resp, sz, "OK 无命中\n");
        }
    } else if (strncmp(buf, "HELLO", 5) == 0) {
        snprintf(resp, sz,
                 "你好，我是奇点OS 的 AI（ai.qd）。我可查授权（AUTH STATUS）、读写记忆（MEMO ...）、"
                 "申请内存（MEM ...）、管理任务（TASK ...）、管理技能（SKILLS ...）、配置（CONFIG ...）；"
                 "对话请用 CHAT <文本>。\n");
    } else if (strncmp(buf, "MEM REQUEST ", 12) == 0) {
        /* MEM REQUEST <大小> permanent|borrow <task_id> */
        char *sp = strchr(buf + 12, ' ');
        if (!sp) { snprintf(resp, sz, "ERR 格式: MEM REQUEST <大小> permanent|borrow <task_id>\n"); return; }
        *sp = 0;
        int mb = atoi(buf + 12);
        char *kind = sp + 1;
        char *rest = strchr(kind, ' ');
        if (rest) *rest = 0;
        mem_request(mb, kind, rest ? rest + 1 : "", resp, sz);
    } else if (strncmp(buf, "MEM STATUS", 10) == 0) {
        mem_status(resp, sz);
    } else if (strncmp(buf, "TASK REGISTER ", 14) == 0) {
        /* TASK REGISTER <任务名> <所需内存MB> */
        char *sp = strchr(buf + 14, ' ');
        if (!sp) { snprintf(resp, sz, "ERR 格式: TASK REGISTER <任务名> <内存MB>\n"); return; }
        *sp = 0;
        int id = task_register(buf + 14, atoi(sp + 1));
        if (id < 0) snprintf(resp, sz, "ERR 任务表满\n");
        else snprintf(resp, sz, "OK task_id=%d\n", id);
    } else if (strncmp(buf, "TASK HEARTBEAT ", 15) == 0) {
        struct task *t = task_find(atoi(buf + 15));
        if (!t) snprintf(resp, sz, "ERR 任务不存在: %s\n", buf + 15);
        else { t->last_hb = time(NULL); snprintf(resp, sz, "OK hb\n"); }
    } else if (strncmp(buf, "TASK DONE ", 10) == 0) {
        task_done(atoi(buf + 10), resp, sz);
    } else if (strncmp(buf, "TASK LIST", 9) == 0) {
        size_t off = 0;
        snprintf(resp + off, sz - off, "任务表 (%d):\n", task_count);
        off = strlen(resp);
        for (int i = 0; i < task_count; i++) {
            if (tasks[i].dead) continue;
            char one[256];
            snprintf(one, sizeof(one), "  #%d %s mem=%dMB hb_ago=%lds\n",
                     tasks[i].id, tasks[i].name, tasks[i].mem_mb,
                     (long)(time(NULL) - tasks[i].last_hb));
            size_t l = strlen(one);
            if (off + l >= sz) break;
            memcpy(resp + off, one, l);
            off += l;
        }
        if (off == strlen("任务表 (0):\n")) snprintf(resp + off, sz - off, "  （无存活任务）\n");
    } else if (strncmp(buf, "SKILLS INSTALL ", 15) == 0) {
        skills_install(buf + 15, resp, sz);
    } else if (strncmp(buf, "SKILLS REMOVE ", 14) == 0) {
        skills_remove(buf + 14, resp, sz);
    } else if (strncmp(buf, "SKILLS LIST", 11) == 0) {
        DIR *d = opendir(AI_SKILLS);
        if (!d) snprintf(resp, sz, "OK 空（无已装技能）\n");
        else {
            size_t off = 0;
            struct dirent *e;
            while ((e = readdir(d)) != NULL && off + 128 < sz) {
                if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
                size_t l = strlen(e->d_name);
                memcpy(resp + off, e->d_name, l);
                off += l;
                resp[off++] = '\n';
            }
            closedir(d);
            if (off == 0) snprintf(resp, sz, "OK 空（无已装技能）\n");
        }
    } else if (strncmp(buf, "CONFIG GET ", 11) == 0) {
        cfg_get(buf + 11, resp, sz);
    } else if (strncmp(buf, "CONFIG SET ", 11) == 0) {
        /* CONFIG SET <模块> <键> <值> */
        char *sp = strchr(buf + 11, ' ');
        if (!sp) { snprintf(resp, sz, "ERR 格式: CONFIG SET <模块> <键> <值>\n"); return; }
        *sp = 0;
        char *mod = buf + 11;
        char *rest = sp + 1;
        char *sp2 = strchr(rest, ' ');
        if (!sp2) { snprintf(resp, sz, "ERR 格式: CONFIG SET <模块> <键> <值>\n"); return; }
        *sp2 = 0;
        cfg_set(mod, rest, sp2 + 1, resp, sz);
    } else {
        snprintf(resp, sz, "ERR 未知内部指令: %s\n", buf);
    }
}

/* ---------------- 模块运行（run <模块>） ---------------- */

/* 纯 C 递归复制（skills/install 等使用；不依赖 busybox cp） */
static int copy_rec(const char *src, const char *dst)
{
    struct stat st;
    if (stat(src, &st) != 0) return -1;
    if (S_ISDIR(st.st_mode)) {
        mkdir(dst, 0755);
        DIR *d = opendir(src);
        if (!d) return -1;
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
            char s[512], dd[512];
            snprintf(s, sizeof(s), "%s/%s", src, e->d_name);
            snprintf(dd, sizeof(dd), "%s/%s", dst, e->d_name);
            if (copy_rec(s, dd) != 0) { closedir(d); return -1; }
        }
        closedir(d);
        return 0;
    }
    FILE *in = fopen(src, "rb");
    if (!in) return -1;
    FILE *out = fopen(dst, "wb");
    if (!out) { fclose(in); return -1; }
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), in)) > 0)
        fwrite(buf, 1, n, out);
    fclose(in);
    fclose(out);
    /* 保留源文件权限（可执行位等，模块入口需 +x） */
    chmod(dst, st.st_mode & 07777);
    return 0;
}

/* 按模块名找到 entry 路径；找不到且带 .qd 后缀则尝试目录内默认入口 */
static int find_entry(const char *name, char *out, size_t sz)
{
    int n = scan_modules();
    for (int i = 0; i < n; i++) {
        /* 目录名是 <模块>.qd：name 可能带或不带 .qd 后缀 */
        int hit = 0;
        if (strcmp(modules[i].name, name) == 0) hit = 1;
        else {
            size_t nl = strlen(name);
            size_t ml = strlen(modules[i].name);
            if (ml == nl + 3 && strncmp(modules[i].name, name, nl) == 0 &&
                strcmp(modules[i].name + nl, ".qd") == 0)
                hit = 1;
        }
        if (hit) {
            if (modules[i].entry[0])
                snprintf(out, sz, "%s/%s/%s", MODULES_DIR, modules[i].name, modules[i].entry);
            else
                snprintf(out, sz, "%s/%s/run", MODULES_DIR, modules[i].name);
            return 0;
        }
    }
    /* 直接拼路径兜底 */
    snprintf(out, sz, "%s/%s/run", MODULES_DIR, name);
    if (access(out, X_OK) == 0) return 0;
    snprintf(out, sz, "%s/%s", MODULES_DIR, name);
    return 0;
}

static int cmd_run(int argc, char **argv)
{
    if (argc < 2) { printf("用法: run <模块> [参数...]\n"); return 0; }
    char path[512];
    find_entry(argv[1], path, sizeof(path));
    if (access(path, X_OK) != 0) {
        printf("模块 %s 无可用入口（%s）\n", argv[1], path);
        return 0;
    }
    printf("▶ 运行模块 %s（%s）\n", argv[1], path);
    fflush(stdout);
    pid_t pid = fork();
    if (pid < 0) { printf("fork 失败\n"); return 0; }
    if (pid == 0) {
        /* 子进程：执行模块 entry，参数透传 */
        char *args[32];
        args[0] = (char *)argv[1];
        int ai = 1;
        for (int i = 2; i < argc && ai < 31; i++) args[ai++] = argv[i];
        args[ai] = NULL;
        execv(path, args);
        fprintf(stderr, "模块 %s 启动失败\n", argv[1]);
        _exit(127);
    }
    int st;
    waitpid(pid, &st, 0);
    printf("▶ 模块 %s 结束（code=%d）\n", argv[1],
           WIFEXITED(st) ? WEXITSTATUS(st) : -1);
    return 0;
}

/* ---------------- 命令 ---------------- */

/* 主系统用户界面：挂载到 /dev/tty0（VNC 主画面），由 用户态.qd 提供 */
static void kill_stale_uis(void)
{
    /* 监督循环重启主系统时，旧 uis（孤儿进程）会残留并争抢 tty0/fb0，
       必须清理，否则键盘输入被旧进程吃掉、显示互相覆盖 */
    DIR *d = opendir("/proc");
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9') continue;
        char cmd[64], buf[128];
        snprintf(cmd, sizeof(cmd), "/proc/%s/cmdline", e->d_name);
        int fd = open(cmd, O_RDONLY);
        if (fd < 0) continue;
        ssize_t n = read(fd, buf, sizeof(buf) - 1);
        close(fd);
        if (n <= 0) continue;
        buf[n] = 0;
        if (strcmp(buf, "uis") == 0) {
            int opid = atoi(e->d_name);
            kill(opid, SIGKILL);
            printf("清理残留用户界面进程 pid=%d\n", opid);
        }
    }
    closedir(d);
}

static void spawn_uis(void)
{
    int tty = open("/dev/tty0", O_RDWR);
    if (tty < 0) {
        printf("用户界面不可用: /dev/tty0 打不开（%s）\n", strerror(errno));
        return;
    }
    kill_stale_uis();
    pid_t pid = fork();
    if (pid < 0) { close(tty); printf("fork 用户界面失败\n"); return; }
    if (pid == 0) {
        dup2(tty, 0);
        dup2(tty, 1);
        dup2(tty, 2);
        if (tty > 2) close(tty);
        execl("/modules/用户态.qd/bin/uis", "uis", (char *)NULL);
        fprintf(stderr, "用户态模块缺失，主系统界面不可用\n");
        _exit(127);
    }
    close(tty);
    printf("主系统用户界面已挂载（VNC/tty0，pid=%d，由 用户态.qd 提供）\n", pid);
    fflush(stdout);
}

static void banner(void)
{
    printf("\n");
    printf("==============================================\n");
    printf("  奇点OS 模块引擎 v0.4 (module-engine)\n");
    printf("  主系统 = 模块容器：系统级组件均为 .qd 模块\n");
    printf("  系统控制权已移交：内核 → init → 模块引擎\n");
    printf("==============================================\n");
}

static void cmd_help(void)
{
    printf("可用命令:\n");
    printf("  help               显示本帮助\n");
    printf("  list               列出 /modules 下所有模块\n");
    printf("  modules            列出模块 + .qds 契约（name/entry/version/desc）\n");
    printf("  run <模块> [参数]   运行模块（fork+exec 模块入口）\n");
    printf("  pm <参数>           包管理（转发 包管理.qd 模块）\n");
    printf("  ai <文本>          与 AI 服务对话（经 6800）\n");
    printf("  grant/revoke <eye|pen|eraser>   授权/撤销 AI 工具\n");
    printf("  auth status        查看授权工具状态\n");
    printf("  memo index|read|write|search   记忆操作\n");
    printf("  task list          任务管理器：查看任务表\n");
    printf("  mem status         资源池 / 借用 / 永久分配状态\n");
    printf("  config get <模块>   查看模块配置（如 ai.qd）\n");
    printf("  config set <模块> <键> <值>    修改模块配置（用户直操作即生效）\n");
    printf("  info <名>          查看模块详情\n");
    printf("  install <模块>     从 /仓库/实体/ 安装模块到 /modules（7A.4）\n");
    printf("  remove <模块>      卸载模块（移出 /modules）\n");
    printf("  repo add <仓库.qds> 登记仓库索引；repo list 列出仓库与来源状态\n");
    printf("  module start/stop <模块>   启动/停止模块（生命周期，独立子进程）\n");
    printf("  module status      模块运行状态一览\n");
    printf("  shell              启动维护 shell (busybox sh)\n");
    printf("  reboot / poweroff  重启 / 关机\n");
}

static void cmd_list(void)
{
    int n = scan_modules();
    if (n < 0) { printf("模块目录不可用（%s），请先挂载\n", MODULES_DIR); return; }
    printf("共发现 %d 个模块项:\n", n);
    for (int i = 0; i < n; i++)
        printf("  [%s] %-40s %lu 字节\n", modules[i].kind, modules[i].name, (unsigned long)modules[i].size);
}

static void cmd_modules(void)
{
    int n = scan_modules();
    if (n < 0) { printf("模块目录不可用（%s），请先挂载\n", MODULES_DIR); return; }
    printf("系统模块契约（/modules，type=guide）:\n");
    for (int i = 0; i < n; i++) {
        if (strcmp(modules[i].kind, "dir") == 0) {
            printf("  ■ %s v%s\n", modules[i].name, modules[i].version[0] ? modules[i].version : "?");
            if (modules[i].entry[0]) printf("      入口: %s\n", modules[i].entry);
            if (modules[i].desc[0])  printf("      说明: %s\n", modules[i].desc);
        } else {
            printf("  · %-40s [%s] %lu 字节\n", modules[i].name, modules[i].kind, (unsigned long)modules[i].size);
        }
    }
}

static void cmd_info(const char *name)
{
    int n = scan_modules();
    for (int i = 0; i < n; i++) {
        if (strcmp(modules[i].name, name) == 0) {
            printf("模块: %s\n类型: %s\n大小: %lu 字节\n",
                   modules[i].name, modules[i].kind, (unsigned long)modules[i].size);
            if (modules[i].entry[0])   printf("入口: %s\n", modules[i].entry);
            if (modules[i].version[0]) printf("版本: %s\n", modules[i].version);
            if (modules[i].desc[0])    printf("说明: %s\n", modules[i].desc);
            return;
        }
    }
    printf("未找到模块: %s\n", name);
}

/* ---------------- 包管理（7A.4）与模块总线生命周期（阶段 9） ---------------- */

/* 纯 C 递归删除目录 */
static int rm_rf(const char *path)
{
    struct stat st;
    if (lstat(path, &st) != 0) return -1;
    if (S_ISDIR(st.st_mode)) {
        DIR *d = opendir(path);
        if (!d) return -1;
        struct dirent *e;
        char p[BUF];
        while ((e = readdir(d)) != NULL) {
            if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
            snprintf(p, sizeof(p), "%s/%s", path, e->d_name);
            rm_rf(p);
        }
        closedir(d);
        return rmdir(path);
    }
    return unlink(path);
}

/* 找 /仓库/<仓库.qds> 里的 source: url 字段（网络未开始 → 不可用） */
static void repo_source_state(const char *repo_path, char *out, size_t outsz)
{
    FILE *f = fopen(repo_path, "r");
    if (!f) { snprintf(out, outsz, "未找到"); return; }
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strncmp(line, "source: url", 11) == 0) {
            snprintf(out, outsz, "source: url → 网络未开始，不可用（7A.5）");
            fclose(f);
            return;
        }
        if (strncmp(line, "source=", 7) == 0) {
            snprintf(out, outsz, "source=%s（网络未开始，不可用，7A.5）", line + 7);
            fclose(f);
            return;
        }
    }
    fclose(f);
    snprintf(out, outsz, "本地仓库（/仓库/实体/）");
}

/* install <模块>：/仓库/实体/<模块>.qd → /modules/（7A.4） */
static void cmd_install(const char *name)
{
    char src[BUF], dst[BUF];
    snprintf(src, sizeof(src), "/仓库/实体/%s.qd", name);
    struct stat st;
    if (stat(src, &st) != 0 || !S_ISDIR(st.st_mode)) {
        printf("ERR 仓库实体中无此模块: %s.qd\n", name);
        return;
    }
    snprintf(dst, sizeof(dst), "%s/%s.qd", MODULES_DIR, name);
    if (stat(dst, &st) == 0) { printf("已存在 %s.qd，请先 remove 或直接替换\n", name); return; }
    if (copy_rec(src, dst) != 0) { printf("ERR 安装失败\n"); return; }
    printf("已安装 %s.qd → /modules（热发现生效，list 可见）\n", name);
}

/* remove <模块>：移出 /modules（7A.4） */
static void cmd_remove(const char *name)
{
    char p[BUF];
    snprintf(p, sizeof(p), "%s/%s.qd", MODULES_DIR, name);
    if (rm_rf(p) != 0) { printf("ERR 移除失败（模块不存在或不可写）\n"); return; }
    printf("已移除 %s.qd（若在运行中请先 module stop）\n", name);
}

/* repo add <仓库.qds>：登记索引到 /仓库/；repo list：列出索引与来源状态 */
static void cmd_repo_add(const char *path)
{
    struct stat st;
    if (stat(path, &st) != 0) { printf("ERR 仓库索引不存在: %s\n", path); return; }
    FILE *f = fopen(path, "r");
    if (!f) { printf("ERR 无法读取 %s\n", path); return; }
    char line[512];
    int is_index = 0;
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strncmp(line, "type=index", 10) == 0) is_index = 1;
    }
    fclose(f);
    if (!is_index) { printf("ERR %s 首行不是 type=index，拒绝登记\n", path); return; }
    mkdir(REPO_DIR, 0755);
    char dst[BUF];
    const char *base = strrchr(path, '/');
    base = base ? base + 1 : path;
    snprintf(dst, sizeof(dst), "%s/%s", REPO_DIR, base);
    if (copy_rec(path, dst) != 0) { printf("ERR 登记失败\n"); return; }
    char srcstate[128];
    repo_source_state(dst, srcstate, sizeof(srcstate));
    printf("已登记仓库 %s（%s）\n", base, srcstate);
}

static void cmd_repo_list(void)
{
    DIR *d = opendir(REPO_DIR);
    if (!d) { printf("仓库目录不存在或为空（%s）\n", REPO_DIR); return; }
    struct dirent *e;
    int cnt = 0;
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
        if (strstr(e->d_name, ".qds") == NULL) continue;
        char p[BUF];
        snprintf(p, sizeof(p), "%s/%s", REPO_DIR, e->d_name);
        char st[128];
        repo_source_state(p, st, sizeof(st));
        printf("  · %-40s %s\n", e->d_name, st);
        cnt++;
    }
    closedir(d);
    if (cnt == 0) printf("  （空）\n");
}

/* 模块生命周期状态机：/modules/<模块>.qd/.state + .pid（name 可带或不带 .qd） */
static void mod_state_path(const char *name, char *p, size_t sz, const char *file)
{
    const char *n = name;
    static char nb[128];
    if (strstr(name, ".qd") == NULL) {
        snprintf(nb, sizeof(nb), "%s.qd", name);
        n = nb;
    }
    snprintf(p, sz, "%s/%s/%s", MODULES_DIR, n, file);
}

static const char *mod_read_state(const char *name, char *buf, size_t sz)
{
    char p[BUF];
    mod_state_path(name, p, sizeof(p), ".state");
    FILE *f = fopen(p, "r");
    if (!f) return "stopped";   /* 无状态文件 = 未启动 */
    char line[128];
    buf[0] = 0;
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strncmp(line, "value=", 6) == 0) snprintf(buf, sz, "%s", line + 6);
    }
    fclose(f);
    return buf[0] ? buf : "stopped";
}

static void cmd_module_start(const char *name)
{
    struct stat st;
    char p[BUF], run[BUF], state[BUF];
    snprintf(p, sizeof(p), "%s/%s.qd", MODULES_DIR, name);
    if (stat(p, &st) != 0 || !S_ISDIR(st.st_mode)) { printf("ERR 模块不存在: %s\n", name); return; }
    int n = scan_modules();
    const char *entry = NULL;
    char qname[128];
    snprintf(qname, sizeof(qname), "%s.qd", name);
    for (int i = 0; i < n; i++) {
        if (strcmp(modules[i].name, name) == 0 || strcmp(modules[i].name, qname) == 0) {
            if (modules[i].entry[0]) { entry = modules[i].entry; break; }
        }
    }
    snprintf(run, sizeof(run), "%s/%s", p, entry ? entry : "bin/run");
    if (stat(run, &st) != 0 || !(st.st_mode & (S_IXUSR | S_IXGRP | S_IXOTH))) {
        printf("ERR 模块 %s 无可执行入口 %s（契约 entry= 未定义或不可执行）\n", name, run);
        return;
    }
    /* 调用隔离（阶段 9）：模块以独立子进程运行，主系统不加载模块代码 */
    pid_t pid = fork();
    if (pid < 0) { printf("ERR fork 失败\n"); return; }
    if (pid == 0) {
        execl(run, run, (char *)NULL);
        _exit(127);
    }
    char pidf[BUF];
    mod_state_path(name, pidf, sizeof(pidf), ".pid");
    FILE *fp = fopen(pidf, "w");
    if (fp) { fprintf(fp, "%d\n", pid); fclose(fp); }
    mod_state_path(name, state, sizeof(state), ".state");
    fp = fopen(state, "w");
    if (fp) { fprintf(fp, "type=state\nvalue=active\npid=%d\n", pid); fclose(fp); }
    printf("已启动 %s（pid=%d，独立子进程，调用隔离）\n", name, pid);
}

static void cmd_module_stop(const char *name)
{
    char pidf[BUF], state[BUF];
    mod_state_path(name, pidf, sizeof(pidf), ".pid");
    FILE *f = fopen(pidf, "r");
    if (f) {
        int pid = 0;
        if (fscanf(f, "%d", &pid) == 1 && pid > 1) kill(pid, SIGTERM);
        fclose(f);
        unlink(pidf);
    }
    mod_state_path(name, state, sizeof(state), ".state");
    FILE *fp = fopen(state, "w");
    if (fp) { fprintf(fp, "type=state\nvalue=stopped\n"); fclose(fp); }
    printf("已停止 %s\n", name);
}

static void cmd_module_status(void)
{
    int n = scan_modules();
    char buf[128];
    printf("模块生命周期（/modules/.state）:\n");
    for (int i = 0; i < n; i++) {
        if (strcmp(modules[i].kind, "dir") != 0) continue;
        const char *st = mod_read_state(modules[i].name, buf, sizeof(buf));
        printf("  %s  [%s] %s\n", modules[i].name, st,
               strcmp(st, "active") == 0 ? "运行中" : "已停止/未启动");
    }
}

/* 把 me> 输入行拆成 argc/argv */
static int split_line(char *line, char **argv, int max)
{
    int n = 0;
    char *p = line;
    while (*p && n < max) {
        while (*p == ' ') p++;
        if (!*p) break;
        argv[n++] = p;
        while (*p && *p != ' ') p++;
        if (*p) *p++ = 0;
    }
    return n;
}

static void run_cmd(char *line)
{
    /* 待确认模式：输入 y/n 处理挂起的用户确认请求（permanent / skill 授权） */
    if (g_pending) {
        if (strcmp(line, "y") == 0 || strcmp(line, "Y") == 0) {
            pending_resolve(1);
            return;
        }
        if (strcmp(line, "n") == 0 || strcmp(line, "N") == 0) {
            pending_resolve(0);
            return;
        }
        printf("（待确认中，请输入 y 同意 / n 拒绝）\n");
        return;
    }
    char *argv[32];
    int argc = split_line(line, argv, 32);
    if (argc == 0) return;
    const char *c = argv[0];
    if (strcmp(c, "help") == 0) cmd_help();
    else if (strcmp(c, "list") == 0) cmd_list();
    else if (strcmp(c, "modules") == 0) cmd_modules();
    else if (strcmp(c, "info") == 0 && argc >= 2) cmd_info(argv[1]);
    else if (strcmp(c, "run") == 0) cmd_run(argc, argv);
    else if (strcmp(c, "pm") == 0) {
        /* 包管理 = 独立模块：转发给 包管理.qd */
        char args[32][128];
        int n = 0;
        snprintf(args[n++], sizeof(args[0]), "包管理");
        for (int i = 1; i < argc && n < 31; i++) {
            snprintf(args[n], sizeof(args[0]), "%s", argv[i]);
            n++;
        }
        char *a2[32];
        for (int i = 0; i < n; i++) a2[i] = args[i];
        char path[512];
        find_entry("包管理", path, sizeof(path));
        if (access(path, X_OK) != 0) { printf("包管理模块未安装\n"); return; }
        pid_t pid = fork();
        if (pid == 0) {
            char *ea[32];
            ea[0] = a2[0];
            for (int i = 1; i < n; i++) ea[i] = a2[i];
            ea[n] = NULL;
            execv(path, ea);
            _exit(127);
        }
        int st;
        waitpid(pid, &st, 0);
    }
    else if (strcmp(c, "ai") == 0 && argc >= 2) {
        char text[BUF];
        text[0] = 0;
        for (int i = 1; i < argc; i++) {
            if (i > 1) strncat(text, " ", sizeof(text) - strlen(text) - 1);
            strncat(text, argv[i], sizeof(text) - strlen(text) - 1);
        }
        if (ai_chat(text) != 0) printf("AI 服务不可达（6800 未监听？）\n");
    }
    else if (strcmp(c, "grant") == 0 && argc >= 2) {
        if (strcmp(argv[1], "eye") == 0) g_auth.eye = 1;
        else if (strcmp(argv[1], "pen") == 0) g_auth.pen = 1;
        else if (strcmp(argv[1], "eraser") == 0) g_auth.eraser = 1;
        else { printf("工具: eye / pen / eraser\n"); return; }
        auth_save();
        printf("已授予 AI「%s」权限\n", argv[1]);
    }
    else if (strcmp(c, "revoke") == 0 && argc >= 2) {
        if (strcmp(argv[1], "eye") == 0) g_auth.eye = 0;
        else if (strcmp(argv[1], "pen") == 0) g_auth.pen = 0;
        else if (strcmp(argv[1], "eraser") == 0) g_auth.eraser = 0;
        else { printf("工具: eye / pen / eraser\n"); return; }
        auth_save();
        printf("已撤销 AI「%s」权限\n", argv[1]);
    }
    else if (strcmp(c, "auth") == 0 && argc >= 2 && strcmp(argv[1], "status") == 0) auth_print();
    else if (strcmp(c, "memo") == 0) cmd_memo(argc, argv);
    else if (strcmp(c, "task") == 0 && argc >= 3 && strcmp(argv[1], "register") == 0) {
        int id = task_register(argv[2], argc >= 4 ? atoi(argv[3]) : 0);
        if (id < 0) printf("ERR 任务表满\n");
        else printf("OK task_id=%d\n", id);
    }
    else if (strcmp(c, "task") == 0 && argc >= 3 && strcmp(argv[1], "heartbeat") == 0) {
        struct task *t = task_find(atoi(argv[2]));
        if (!t) printf("ERR 任务不存在: %s\n", argv[2]);
        else { t->last_hb = time(NULL); printf("OK hb\n"); }
    }
    else if (strcmp(c, "task") == 0 && argc >= 3 && strcmp(argv[1], "done") == 0) {
        char r[BUF];
        task_done(atoi(argv[2]), r, sizeof(r));
        printf("%s", r);
    }
    else if (strcmp(c, "task") == 0 && argc >= 2 && strcmp(argv[1], "list") == 0) {
        printf("任务表 (%d):\n", task_count);
        for (int i = 0; i < task_count; i++) {
            if (tasks[i].dead) continue;
            printf("  #%d %s mem=%dMB hb_ago=%lds\n",
                   tasks[i].id, tasks[i].name, tasks[i].mem_mb,
                   (long)(time(NULL) - tasks[i].last_hb));
        }
    }
    else if (strcmp(c, "mem") == 0 && argc >= 3 && strcmp(argv[1], "request") == 0) {
        /* mem request <大小> permanent|borrow [task_id] */
        char r[BUF];
        mem_request(atoi(argv[2]), argc >= 4 ? argv[3] : "",
                    argc >= 5 ? argv[4] : "", r, sizeof(r));
        printf("%s", r);
    }
    else if (strcmp(c, "mem") == 0 && argc >= 3 && strcmp(argv[1], "release") == 0) {
        char r[BUF];
        mem_release(argv[2], r, sizeof(r));
        printf("%s", r);
    }
    else if (strcmp(c, "mem") == 0 && argc >= 2 && strcmp(argv[1], "status") == 0) {
        char r[BUF];
        mem_status(r, sizeof(r));
        printf("%s", r);
    }
    else if (strcmp(c, "config") == 0 && argc >= 3 && strcmp(argv[1], "get") == 0) {
        char r[BUF * 2];
        cfg_get(argv[2], r, sizeof(r));
        printf("%s", r);
    }
    else if (strcmp(c, "config") == 0 && argc >= 5 && strcmp(argv[1], "set") == 0) {
        char r[BUF * 2];
        cfg_set(argv[2], argv[3], argv[4], r, sizeof(r));
        printf("%s", r);
    }
    else if (strcmp(c, "skills") == 0 && argc >= 3 && strcmp(argv[1], "install") == 0) {
        char r[BUF * 2];
        skills_install(argv[2], r, sizeof(r));
        printf("%s", r);
    }
    else if (strcmp(c, "skills") == 0 && argc >= 3 && strcmp(argv[1], "remove") == 0) {
        char r[BUF * 2];
        skills_remove(argv[2], r, sizeof(r));
        printf("%s", r);
    }
    else if (strcmp(c, "skills") == 0 && argc >= 2 && strcmp(argv[1], "list") == 0) {
        DIR *d = opendir(AI_SKILLS);
        if (!d) printf("（无已装技能）\n");
        else {
            struct dirent *e;
            while ((e = readdir(d)) != NULL) {
                if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
                printf("  %s\n", e->d_name);
            }
            closedir(d);
        }
    }
    else if (strcmp(c, "install") == 0 && argc >= 2) cmd_install(argv[1]);
    else if (strcmp(c, "remove") == 0 && argc >= 2) cmd_remove(argv[1]);
    else if (strcmp(c, "repo") == 0 && argc >= 3 && strcmp(argv[1], "add") == 0) cmd_repo_add(argv[2]);
    else if (strcmp(c, "repo") == 0 && argc >= 2 && strcmp(argv[1], "list") == 0) cmd_repo_list();
    else if (strcmp(c, "module") == 0 && argc >= 3 && strcmp(argv[1], "start") == 0) cmd_module_start(argv[2]);
    else if (strcmp(c, "module") == 0 && argc >= 3 && strcmp(argv[1], "stop") == 0) cmd_module_stop(argv[2]);
    else if (strcmp(c, "module") == 0 && argc >= 2 && strcmp(argv[1], "status") == 0) cmd_module_status();
    else if (strcmp(c, "shell") == 0) {
        printf("启动维护 shell...\n");
        execl("/bin/sh", "sh", NULL);
        perror("shell 启动失败");
    }
    else if (strcmp(c, "reboot") == 0) { printf("重启系统...\n"); sync(); reboot(0x4321fedc); }
    else if (strcmp(c, "poweroff") == 0) { printf("关机...\n"); sync(); reboot(0x4321fedc + 1); }
    else if (c[0] == 0) return;
    else printf("未知命令: %s（输入 help）\n", c);
}

int main(int argc, char **argv)
{
    banner();
    auth_load();
    res_load();
    memo_init_dir();
    int n = scan_modules();
    if (n < 0) printf("警告: %s 不存在或不可读，系统模块不可用\n", MODULES_DIR);
    else printf("模块引擎就绪：发现 %d 个模块项（输入 help 查看命令）\n", n);

    /* 非交互模式：argv[1] 为命令（供脚本调用） */
    if (argc > 1) {
        char line[BUF];
        snprintf(line, sizeof(line), "%s", argv[1]);
        for (int i = 2; i < argc; i++) {
            strncat(line, " ", sizeof(line) - strlen(line) - 1);
            strncat(line, argv[i], sizeof(line) - strlen(line) - 1);
        }
        run_cmd(line);
        return 0;
    }

    /* 6801 内部链路（AI ↔ 主系统） */
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    /* 6801 仅主系统内部使用，不随 exec 泄漏给模块子进程 */
    fcntl(srv, F_SETFD, FD_CLOEXEC);
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(INT_PORT);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(srv, (struct sockaddr *)&a, sizeof(a)) == 0 && listen(srv, 4) == 0) {
        printf("内部链路就绪：AI ↔ 主系统 127.0.0.1:%d\n", INT_PORT);
        fflush(stdout);
    } else {
        fprintf(stderr, "6801 监听失败: %s\n", strerror(errno));
        fflush(stderr);
    }

    /* 主系统用户界面：挂载到 /dev/tty0（VNC 主画面） */
    spawn_uis();

    /* 交互 + 6801 双通道 poll */
    char line[BUF];
    size_t linelen = 0;
    struct stat modst;
    int have_modst = (stat(MODULES_DIR, &modst) == 0);
    time_t mod_mtime = have_modst ? modst.st_mtime : 0;
    time_t hotplug_next = time(NULL) + 2;
    for (;;) {
        struct pollfd pf[2];
        pf[0].fd = 0;
        pf[0].events = POLLIN;
        pf[1].fd = srv;
        pf[1].events = POLLIN;
        /* 1000ms 超时：定期执行任务管理器心跳超时回收（7C.3）+ 模块热插拔检测（阶段 9） */
        int pr = poll(pf, 2, 1000);
        if (pr < 0) break;
        task_reap();

        /* 热插拔检测：/modules 目录 mtime 变化 → 重扫并打印差异（阶段 9） */
        if (time(NULL) >= hotplug_next) {
            hotplug_next = time(NULL) + 2;
            struct stat nst;
            if (stat(MODULES_DIR, &nst) == 0 && nst.st_mtime != mod_mtime) {
                int old_count = module_count;
                char old_names[MAX_MODULES][128];
                for (int i = 0; i < old_count; i++)
                    snprintf(old_names[i], sizeof(old_names[i]), "%s", modules[i].name);
                int new_count = scan_modules();
                printf("\n[热插拔] /modules 变化：%d → %d 项\n", old_count, new_count);
                for (int i = 0; i < new_count; i++) {
                    int existed = 0;
                    for (int j = 0; j < old_count; j++)
                        if (strcmp(modules[i].name, old_names[j]) == 0) { existed = 1; break; }
                    if (!existed) printf("  新增: %s\n", modules[i].name);
                }
                for (int j = 0; j < old_count; j++) {
                    int gone = 1;
                    for (int i = 0; i < new_count; i++)
                        if (strcmp(modules[i].name, old_names[j]) == 0) { gone = 0; break; }
                    if (gone) printf("  移除: %s\n", old_names[j]);
                }
                mod_mtime = nst.st_mtime;
                printf("me> ");
                fflush(stdout);
            }
        }

        if (pf[1].revents & POLLIN) {
            int c = accept(srv, NULL, NULL);
            if (c >= 0) handle_6801(c);
        }
        if (pf[0].revents & POLLIN) {
            char tmp[128];
            ssize_t n = read(0, tmp, sizeof(tmp) - 1);
            if (n <= 0) break;
            for (ssize_t i = 0; i < n; i++) {
                if (tmp[i] == '\n' || tmp[i] == '\r') {
                    if (linelen > 0) {
                        line[linelen] = 0;
                        printf("me> %s\n", line);
                        run_cmd(line);
                        linelen = 0;
                        printf("me> ");
                        fflush(stdout);
                    }
                } else if (linelen + 1 < sizeof(line)) {
                    line[linelen++] = tmp[i];
                }
            }
        }
    }
    return 0;
}
