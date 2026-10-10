/*
 * ai_service.c v0.5 — 奇点OS ai.qd 云端 AI 服务
 *
 * 运行环境：ai.qd 专属隔离容器（chroot /ai.qd），musl 静态编译。
 * 职责：
 *   1. 首次运行：TUI 配置向导（终端界面：输入云端 API Key / 基址 / 模型 → 测试连接 → 保存）
 *   2. 对话模式：tty0 终端交互对话（调用 OpenAI 兼容云端 API）
 *   3. 常驻监听 TCP 127.0.0.1:6800（STATUS / HELLO / CHAT / CONFIG / HELP）
 *   4. 配置持久化：/ai.qd/配置
 *
 * 云端调用：fork + pipe 执行 /bin/wget（busybox，HTTPS POST），
 *           解析回复 JSON 中 choices[0].message.content。
 *
 * 交叉编译：
 *   x86_64-linux-musl-gcc -static -O2 -o ai_service ai_service.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <termios.h>
#include <dirent.h>
#include <poll.h>

#define PORT 6800
#define BUF_SIZE 8192
#define CFG_PATH "/配置"
#define WGET "/bin/curl"

#define ANSI_RESET   "\033[0m"
#define ANSI_BOLD    "\033[1m"
#define ANSI_CYAN    "\033[36m"
#define ANSI_GREEN   "\033[32m"
#define ANSI_YELLOW  "\033[33m"
#define ANSI_RED     "\033[31m"
#define ANSI_MAG     "\033[35m"
#define ANSI_CLS     "\033[2J\033[H"

static char g_key[256]   = "";
static char g_base[256]  = "https://api.xiaomimimo.com/v1";
static char g_model[128] = "mimo-v2.5-pro";
/* 本地 LLM 引擎（阶段 6）：llm_mode=cloud|local，llm_base_url/llm_model 为本地端点 */
static char g_llm_mode[16]   = "cloud";
static char g_llm_base[256]  = "";
static char g_llm_model[128] = "";
static volatile sig_atomic_t g_running = 1;

static void on_signal(int s) { (void)s; g_running = 0; }

/* ---------------- 配置持久化 ---------------- */

static void cfg_load(void)
{
    FILE *f = fopen(CFG_PATH, "r");
    if (!f) return;
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        char *eq = strchr(line, '=');
        if (!eq) continue;
        *eq = 0;
        char *v = eq + 1;
        v[strcspn(v, "\r\n")] = 0;
        if (strcmp(line, "api_key") == 0) snprintf(g_key, sizeof(g_key), "%s", v);
        else if (strcmp(line, "base_url") == 0) snprintf(g_base, sizeof(g_base), "%s", v);
        else if (strcmp(line, "model") == 0) snprintf(g_model, sizeof(g_model), "%s", v);
        else if (strcmp(line, "llm_mode") == 0) snprintf(g_llm_mode, sizeof(g_llm_mode), "%s", v);
        else if (strcmp(line, "llm_base_url") == 0) snprintf(g_llm_base, sizeof(g_llm_base), "%s", v);
        else if (strcmp(line, "llm_model") == 0) snprintf(g_llm_model, sizeof(g_llm_model), "%s", v);
    }
    fclose(f);
}

static void cfg_save(void)
{
    FILE *f = fopen(CFG_PATH, "w");
    if (!f) return;
    fprintf(f, "api_key=%s\nbase_url=%s\nmodel=%s\n"
               "llm_mode=%s\nllm_base_url=%s\nllm_model=%s\n",
            g_key, g_base, g_model, g_llm_mode, g_llm_base, g_llm_model);
    fclose(f);
}

/* ---------------- JSON 工具 ---------------- */

static void json_escape(const char *s, char *out, size_t outsz)
{
    size_t o = 0;
    for (; *s && o + 8 < outsz; s++) {
        unsigned char c = (unsigned char)*s;
        switch (c) {
        case '"':  out[o++] = '\\'; out[o++] = '"';  break;
        case '\\': out[o++] = '\\'; out[o++] = '\\'; break;
        case '\n': out[o++] = '\\'; out[o++] = 'n';  break;
        case '\r': out[o++] = '\\'; out[o++] = 'r';  break;
        case '\t': out[o++] = '\\'; out[o++] = 't';  break;
        default:
            if (c < 0x20) { out[o++] = ' '; }  /* 低字节控制符 → 空格 */
            else out[o++] = c;
        }
    }
    out[o] = 0;
}

/* 在 buf 中查找键 "key" 的字符串值，写入 out。成功返回 1。 */
static int json_get_string(const char *buf, const char *key, char *out, size_t outsz)
{
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(buf, pat);
    if (!p) return 0;
    p += strlen(pat);
    while (*p && *p != ':') p++;
    if (*p != ':') return 0;
    p++;
    while (*p && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    if (*p != '"') return 0;
    p++;
    size_t o = 0;
    while (*p && o + 1 < outsz) {
        if (*p == '\\' && p[1]) {
            p++;
            switch (*p) {
            case 'n': out[o++] = '\n'; break;
            case 't': out[o++] = '\t'; break;
            case 'r': out[o++] = '\r'; break;
            case '"': out[o++] = '"';  break;
            case '\\': out[o++] = '\\'; break;
            default:  out[o++] = *p;    break;
            }
            p++;
        } else if (*p == '"') {
            out[o] = 0;
            return 1;
        } else {
            out[o++] = *p++;
        }
    }
    out[o] = 0;
    return 0;
}

/* ---------------- 云端调用（wget POST，OpenAI 兼容） ---------------- */
/* 返回 0 成功；-1 fork/pipe 失败；-2 无响应（网络/鉴权/超时）；其他见 rep */

static int cloud_chat(const char *text, char *reply, size_t replysz)
{
    char body[BUF_SIZE], esc[BUF_SIZE], url[BUF_SIZE], auth[320];

    json_escape(text, esc, sizeof(esc));
    snprintf(body, sizeof(body),
             "{\"model\":\"%s\",\"messages\":[{\"role\":\"user\",\"content\":\"%s\"}],\"stream\":false}",
             g_model, esc);
    snprintf(url, sizeof(url), "%s/chat/completions", g_base);
    snprintf(auth, sizeof(auth), "Bearer %s", g_key);

    /* v0.3：独立静态 mycurl（libcurl）替代 busybox wget。
     * 已证实：musl 程序 fork+exec busybox wget 的网络请求在 QEMU/WHPX 下必崩（GPF 0x400206）；
     * 独立二进制 mycurl（libcurl 静态）无此问题，宿主端真实 API 已验证全绿。 */
    int pfd[2];
    if (pipe(pfd) != 0) return -1;

    fprintf(stderr, "\n[AI] 云端请求开始 (pid fork)...\n");
    pid_t pid = fork();
    if (pid < 0) { close(pfd[0]); close(pfd[1]); fprintf(stderr, "[AI] fork 失败\n"); return -1; }

    if (pid == 0) {
        dup2(pfd[1], 1);
        close(pfd[0]);
        close(pfd[1]);
        char ahdr[512];
        snprintf(ahdr, sizeof(ahdr), "Bearer %s", g_key);
        execl("/bin/mycurl", "mycurl", url, ahdr, body, (char *)NULL);
        _exit(127);
    }

    close(pfd[1]);
    size_t got = 0;
    reply[0] = 0;
    struct pollfd pfr = { .fd = pfd[0], .events = POLLIN };
    int deadline = 40;
    while (got + 1 < replysz) {
        int pr = poll(&pfr, 1, 1000);
        if (pr == 0) { if (--deadline <= 0) break; continue; }
        if (pr < 0) break;
        ssize_t n = read(pfd[0], reply + got, replysz - got - 1);
        if (n <= 0) break;
        got += (size_t)n;
        if (got >= replysz - 1) break;
    }
    reply[got] = 0;
    close(pfd[0]);
    int st = 0;
    waitpid(pid, &st, WNOHANG);
    if (deadline <= 0 || got == 0) kill(pid, SIGKILL);
    waitpid(pid, &st, 0);
    fprintf(stderr, "[AI] 请求结束: got=%zu deadline=%d child=%s\n",
            got, deadline,
            WIFSIGNALED(st) ? "SIGNALED" : (WIFEXITED(st) ? "EXITED" : "?"));

    if (got == 0) return -2;
    return 0;
}

/* ---------------- 智能对话（本地/云端引擎切换，阶段 6） ---------------- */
/* local 模式：临时切到 llm_base_url 端点；失败自动回退云端并在 note 说明 */
static int smart_chat(const char *text, char *reply, size_t replysz,
                      char *note, size_t notesz)
{
    note[0] = 0;
    cfg_load();   /* 每次对话前刷新配置：me> config set ai.qd llm_* 动态生效 */
    if (strcmp(g_llm_mode, "local") == 0 && g_llm_base[0]) {
        char old_base[256], old_model[128];
        snprintf(old_base, sizeof(old_base), "%s", g_base);
        snprintf(old_model, sizeof(old_model), "%s", g_model);
        snprintf(g_base, sizeof(g_base), "%s", g_llm_base);
        snprintf(g_model, sizeof(g_model), "%s", g_llm_model[0] ? g_llm_model : "default");
        int rc = cloud_chat(text, reply, replysz);
        snprintf(g_base, sizeof(g_base), "%s", old_base);
        snprintf(g_model, sizeof(g_model), "%s", old_model);
        if (rc == 0) {
            snprintf(note, notesz, "（本地引擎 %s | 模型 %s）", g_llm_base,
                     g_llm_model[0] ? g_llm_model : "default");
            return 0;
        }
        snprintf(note, notesz, "⚠ 本地引擎 %s 不可达，已回退云端。", g_llm_base);
    }
    int rc = cloud_chat(text, reply, replysz);
    if (rc == 0 && !note[0])
        snprintf(note, notesz, "（云端 %s | 模型 %s）", g_base, g_model);
    return rc;
}

/* ---------------- 终端输入 ---------------- */

static void term_mode(int fd, int canonical)
{
    struct termios t;
    if (tcgetattr(fd, &t) != 0) return;
    if (canonical) t.c_lflag |= (ECHO | ICANON);
    else t.c_lflag &= ~(ECHO | ICANON);
    tcsetattr(fd, TCSANOW, &t);
}

/* 不回显输入（用于 API Key），回显星号 */
static void read_line_secret(char *buf, size_t sz)
{
    term_mode(0, 0);
    size_t i = 0;
    buf[0] = 0;
    while (i + 1 < sz) {
        char c;
        ssize_t n = read(0, &c, 1);
        if (n <= 0) break;
        if (c == '\n' || c == '\r') break;
        if (c == 127 || c == 8) {
            if (i > 0) { i--; write(1, "\b \b", 3); }
            continue;
        }
        buf[i++] = c;
        write(1, "*", 1);
    }
    buf[i] = 0;
    term_mode(0, 1);
    write(1, "\n", 1);
}

/* 统一走 read(0) 逐字符读取（避免与 read_line_secret 混用 stdio 缓冲导致错乱） */
static void read_line(char *buf, size_t sz)
{
    size_t i = 0;
    buf[0] = 0;
    while (i + 1 < sz) {
        char c;
        ssize_t n = read(0, &c, 1);
        if (n <= 0) break;
        if (c == '\n' || c == '\r') break;
        if (c == 127 || c == 8) { if (i > 0) i--; continue; }
        buf[i++] = c;
    }
    buf[i] = 0;
}

/* ---------------- 配置向导（状态机版，6800 常驻不被阻塞） ---------------- */

enum { ST_WIZARD_KEY, ST_WIZARD_BASE, ST_WIZARD_MODEL, ST_WIZARD_TEST, ST_CHAT } g_state;
static int g_phase = 0;  /* 0=key 1=base 2=model */

static void wizard_prompt(void)
{
    if (g_phase == 0) {
        printf(ANSI_CLS);
        printf(ANSI_BOLD ANSI_CYAN
               "╔══════════════════════════════════════════════════════════╗\n"
               "║            奇点OS · AI 子系统（ai.qd）接入向导            ║\n"
               "╚══════════════════════════════════════════════════════════╝\n" ANSI_RESET);
        printf(ANSI_YELLOW "本向导将配置云端 AI 服务（OpenAI 兼容接口），配置后即可对话。\n\n" ANSI_RESET);
        printf(ANSI_GREEN "1) 云端 API Key（输入时以 * 显示，不会明文回显）：\n" ANSI_RESET);
        /* 提前关闭回显：canonical 模式下字符会在行缓冲内被 ECHO 回显（明文），
         * 必须在用户开始输入之前就关掉 ECHO，否则会出现明文 key 行 */
        term_mode(0, 0);
    } else if (g_phase == 1) {
        printf(ANSI_GREEN "2) API 基址 [%s]（直接回车使用默认）：\n" ANSI_RESET, g_base);
    } else if (g_phase == 2) {
        printf(ANSI_GREEN "3) 模型名 [%s]（直接回车使用默认）：\n" ANSI_RESET, g_model);
    }
    fflush(stdout);
}

/* 返回 1 表示向导全部完成；0 继续；-1 出错（key 为空） */
static int wizard_step(const char *line)
{
    switch (g_phase) {
    case 0:
        if (!line[0]) return -1;
        snprintf(g_key, sizeof(g_key), "%s", line);
        g_phase = 1;
        break;
    case 1:
        if (line[0]) snprintf(g_base, sizeof(g_base), "%s", line);
        g_phase = 2;
        break;
    case 2:
        if (line[0]) snprintf(g_model, sizeof(g_model), "%s", line);
        g_phase = 3;
        return 1;   /* 全部输入完成，进入测试 */
    default:
        return 1;
    }
    return 0;
}

static void wizard_test(void)
{
    printf(ANSI_YELLOW "\n正在测试连接云端 API ...\n" ANSI_RESET);
    fflush(stdout);
    char rep[BUF_SIZE];
    int rc = cloud_chat("你好，请只回复四个字：连接成功", rep, sizeof(rep));
    if (rc == 0) {
        char content[BUF_SIZE];
        if (json_get_string(rep, "content", content, sizeof(content))) {
            printf(ANSI_GREEN "✅ 连接成功，模型回复：%s\n" ANSI_RESET, content);
        } else {
            printf(ANSI_GREEN "✅ 连接成功（但未能解析回复内容）。\n" ANSI_RESET);
            printf("原始响应：%.300s\n", rep);
        }
    } else {
        printf(ANSI_RED "❌ 连接失败（错误码 %d）。常见原因：\n" ANSI_RESET, rc);
        printf("  1. 网络未通（检查 QEMU 网卡与 guest 网络配置）\n");
        printf("  2. API Key 无效或未生效\n");
        printf("  3. 基址/模型名不正确\n");
        if (rep[0]) printf("  原始响应：%.200s\n", rep);
        printf(ANSI_YELLOW "（配置仍将保存，可稍后输入 /reconfig 重新配置）\n" ANSI_RESET);
    }
    cfg_save();
    printf(ANSI_GREEN "✅ 配置已保存到 %s。\n" ANSI_RESET, CFG_PATH);
}

static void print_status(void)
{
    printf(ANSI_CYAN "[状态] " ANSI_RESET
           "奇点OS AI 服务 v0.5 | 引擎: %s | 模型: %s\n"
           "  基址: %s\n"
           "  本地LLM: %s%s%s\n"
           "  Key: %s%s%s\n"
           "  容器: ai.qd | 监听: 127.0.0.1:%d\n",
           g_llm_mode[0] ? g_llm_mode : "cloud", g_model, g_base,
           g_llm_mode[0] ? g_llm_mode : "cloud",
           (strcmp(g_llm_mode, "local") == 0 && g_llm_base[0]) ? " @ " : "",
           (strcmp(g_llm_mode, "local") == 0 && g_llm_base[0]) ? g_llm_base : "",
           g_key[0] ? g_key : "(未配置)",
           g_key[0] ? (strlen(g_key) > 8 ? "..." : "") : "",
           g_key[0] ? (strlen(g_key) > 8 ? g_key + strlen(g_key) - 4 : "") : "",
           PORT);
}

/* ---------------- 6801 内部链路（AI → 主系统） ---------------- */

#define INT_PORT 6801

/* ai_service 作为客户端连接 6801，把内部指令转发给主系统 module-engine */
static int forward_6801(const char *req, char *resp, size_t sz)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(INT_PORT);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { close(fd); return -1; }
    write(fd, req, strlen(req));
    shutdown(fd, SHUT_WR);
    ssize_t n = read(fd, resp, sz - 1);
    close(fd);
    if (n <= 0) { resp[0] = 0; return -1; }
    resp[n] = 0;
    return 0;
}

/* 本地扫描 ai.qd 容器内 /skills 下的技能模块 */
static void skills_list(char *out, size_t sz)
{
    DIR *d = opendir("/skills");
    size_t off = 0;
    if (!d) {
        snprintf(out, sz, "skills 目录为空（/skills 未创建或无技能）\n");
        return;
    }
    struct dirent *ent;
    out[0] = 0;
    while ((ent = readdir(d)) != NULL) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
        size_t l = strlen(ent->d_name);
        if (off + l + 8 < sz) {
            memcpy(out + off, ent->d_name, l);
            off += l;
            out[off++] = '\n';
        }
    }
    closedir(d);
    if (off == 0) snprintf(out, sz, "（无技能）\n");
    out[off] = 0;
}

/* ---------------- 6800 socket 指令 ---------------- */

/* 统一指令处理：TUI 对话与 6800 socket 共用同一解析 */
static void process_command(const char *buf, char *resp, size_t sz)
{
    resp[0] = 0;

    if (strncmp(buf, "STATUS", 6) == 0) {
        snprintf(resp, sz,
                 "奇点OS AI 服务 v0.5 | 状态: 就绪 | 引擎: 云端API | 模型: %s | Key: %s\n",
                 g_model, g_key[0] ? "已配置" : "未配置");
    } else if (strncmp(buf, "HELLO", 5) == 0) {
        snprintf(resp, sz,
                 "你好，我是奇点OS 的 AI（ai.qd）。当前引擎为云端 API，可对话（CHAT <文本>）；\n"
                 "我可查授权（AUTH STATUS）、读写记忆（MEMO INDEX/READ/WRITE/SEARCH）、\n"
                 "申请内存（MEM REQUEST/STATUS）、管理任务（TASK ...）、列/装/卸技能（SKILLS LIST/INSTALL/REMOVE）、配置（CONFIG GET/SET）。\n");
    } else if (strncmp(buf, "CHAT ", 5) == 0) {
        char rep[BUF_SIZE];
        char note[256];
        int rc = smart_chat(buf + 5, rep, sizeof(rep), note, sizeof(note));
        if (rc != 0) {
            snprintf(resp, sz, "ERR chat rc=%d %s\n", rc, note);
        } else {
            char content[BUF_SIZE];
            if (json_get_string(rep, "content", content, sizeof(content)))
                snprintf(resp, sz, "%s%s\n", note[0] ? note : "", content);
            else
                snprintf(resp, sz, "ERR parse: %.400s\n", rep);
        }
    } else if (strncmp(buf, "AUTH STATUS", 11) == 0) {
        char r[BUF_SIZE];
        if (forward_6801("AUTH STATUS", r, sizeof(r)) != 0)
            snprintf(resp, sz, "ERR 主系统 6801 不可达（主系统未就绪）\n");
        else
            snprintf(resp, sz, "AI 权限（主系统侧）: %s", r);
    } else if (strncmp(buf, "MEMO ", 5) == 0) {
        /* MEMO INDEX / READ <日期> / WRITE <日期> <内容> / SEARCH <词> → 6801 转发主系统
         * 授权校验在主系统侧完成（7C.4：READ 需 eye、WRITE 需 pen） */
        char r[BUF_SIZE * 2];
        if (forward_6801(buf, r, sizeof(r)) != 0)
            snprintf(resp, sz, "ERR 主系统 6801 不可达\n");
        else
            snprintf(resp, sz, "%s", r);
    } else if (strncmp(buf, "MEM ", 4) == 0) {
        /* MEM REQUEST <大小> permanent|borrow <task_id> / MEM RELEASE / MEM STATUS → 6801 */
        char r[BUF_SIZE * 2];
        if (forward_6801(buf, r, sizeof(r)) != 0)
            snprintf(resp, sz, "ERR 主系统 6801 不可达\n");
        else
            snprintf(resp, sz, "%s", r);
    } else if (strncmp(buf, "TASK ", 5) == 0) {
        /* TASK REGISTER/HEARTBEAT/DONE/LIST → 6801 */
        char r[BUF_SIZE * 2];
        if (forward_6801(buf, r, sizeof(r)) != 0)
            snprintf(resp, sz, "ERR 主系统 6801 不可达\n");
        else
            snprintf(resp, sz, "%s", r);
    } else if (strncmp(buf, "SKILLS LIST", 11) == 0) {
        char r[BUF_SIZE];
        skills_list(r, sizeof(r));
        snprintf(resp, sz, "AI 技能（ai.qd/skills）:\n%s", r);
    } else if (strncmp(buf, "SKILLS ", 7) == 0) {
        /* SKILLS INSTALL <技能> / SKILLS REMOVE <技能> → 6801（权限确认在主系统 me>） */
        char r[BUF_SIZE * 2];
        if (forward_6801(buf, r, sizeof(r)) != 0)
            snprintf(resp, sz, "ERR 主系统 6801 不可达\n");
        else
            snprintf(resp, sz, "%s", r);
    } else if (strncmp(buf, "CONFIG GET", 10) == 0 || strncmp(buf, "CONFIG SET", 10) == 0) {
        char r[BUF_SIZE * 2];
        if (forward_6801(buf, r, sizeof(r)) != 0)
            snprintf(resp, sz, "ERR 主系统 6801 不可达\n");
        else
            snprintf(resp, sz, "%s", r);
    } else if (strncmp(buf, "CONFIG", 6) == 0) {
        snprintf(resp, sz, "llm_mode=%s\nllm_base_url=%s\nllm_model=%s\nmodel=%s\nbase_url=%s\nkey=%s\n",
                 g_llm_mode[0] ? g_llm_mode : "cloud", g_llm_base, g_llm_model,
                 g_model, g_base, g_key[0] ? "已配置" : "未配置");
    } else if (strncmp(buf, "HELP", 4) == 0) {
        snprintf(resp, sz,
                 "指令: STATUS / HELLO / CHAT <文本> / AUTH STATUS /\n"
                 "      MEMO INDEX|READ|WRITE|SEARCH / MEM REQUEST|RELEASE|STATUS /\n"
                 "      TASK REGISTER|HEARTBEAT|DONE|LIST / SKILLS LIST|INSTALL|REMOVE /\n"
                 "      CONFIG GET|SET / CONFIG / HELP\n");
    } else {
        snprintf(resp, sz, "未知指令: %s（输入 HELP 查看）\n", buf);
    }
}

static void handle_client(int fd)
{
    char buf[BUF_SIZE];
    size_t len = 0;
    /* 循环读直到换行或缓冲满（长文本对话不会被 TCP 分片截断） */
    while (len + 1 < sizeof(buf)) {
        ssize_t n = read(fd, buf + len, 1);
        if (n <= 0) break;
        if (buf[len] == '\n') break;
        len++;
    }
    buf[len] = 0;
    buf[strcspn(buf, "\r\n")] = 0;

    char resp[BUF_SIZE * 2];
    process_command(buf, resp, sizeof(resp));
    write(fd, resp, strlen(resp));
}

/* ---------------- 主程序 ---------------- */

int main(int argc, char **argv)
{
    int daemon_mode = 0;
    for (int i = 1; i < argc; i++)
        if (strcmp(argv[i], "--daemon") == 0) daemon_mode = 1;

    /* stdout 对 /dev/tty0 是全缓冲，不设无缓冲会导致 printf 卡在缓冲区不显示 */
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);

    cfg_load();

    /* TCP 6800 常驻服务 */
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { perror("socket"); return 1; }
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(PORT);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind"); return 1;
    }
    if (listen(srv, 8) < 0) { perror("listen"); return 1; }

    /* 状态机：无配置 → 向导（不阻塞 6800 常驻服务）；有配置 → 直接对话
     * daemon 模式（主系统 VNC 主画面）：无配置也不进向导（stdin=/dev/null 无可交互），
     * 直接常驻，配置通过 me> config set ai.qd 写入 */
    g_state = (g_key[0] || daemon_mode) ? ST_CHAT : ST_WIZARD_KEY;
    if (g_state == ST_WIZARD_KEY) {
        wizard_prompt();
    } else {
        printf(ANSI_BOLD "奇点OS AI 服务 v0.5 就绪（引擎: 云端API | 模型: %s | 监听 127.0.0.1:%d%s）\n" ANSI_RESET,
               g_model, PORT, daemon_mode ? " | daemon" : "");
        printf(ANSI_GREEN "AI 服务常驻运行；对话经 6800 或主系统界面 ai 命令。\n" ANSI_RESET);
        fflush(stdout);
    }

    /* 主循环：同时服务 终端（向导/对话） 与 6800 socket */
    while (g_running) {
        struct pollfd fds[2];
        fds[0].fd = 0;         fds[0].events = POLLIN;
        fds[1].fd = srv;       fds[1].events = POLLIN;

        int pr = poll(fds, 2, 1000);
        if (pr < 0) {
            if (errno == EINTR) continue;
            break;
        }

        if (fds[1].revents & POLLIN) {
            struct sockaddr_in cli;
            socklen_t clen = sizeof(cli);
            int fd = accept(srv, (struct sockaddr *)&cli, &clen);
            if (fd >= 0) {
                handle_client(fd);
                close(fd);
            }
        }

        if (fds[0].revents & POLLIN) {
            char line[BUF_SIZE];
            switch (g_state) {
            case ST_WIZARD_KEY: {
                /* 输入 API Key：关回显，星号显示 */
                char key[512];
                read_line_secret(key, sizeof(key));
                int st = wizard_step(key);
                if (st == -1) {
                    printf(ANSI_RED "⚠ Key 不能为空，请重新输入：\n" ANSI_RESET);
                    fflush(stdout);
                } else {
                    g_state = ST_WIZARD_BASE;
                    wizard_prompt();
                }
                break;
            }
            case ST_WIZARD_BASE:
            case ST_WIZARD_MODEL: {
                read_line(line, sizeof(line));
                if (wizard_step(line) == 1) {
                    g_state = ST_WIZARD_TEST;
                    cfg_save();   /* 向导完成：持久化 key/base_url/model 到 /ai.qd/配置 */
                }
                wizard_prompt();
                break;
            }
            case ST_WIZARD_TEST: {
                wizard_test();
                g_state = ST_CHAT;
                printf(ANSI_GREEN "进入对话模式。输入 /help 查看指令。\n" ANSI_RESET);
                fflush(stdout);
                break;
            }
            case ST_CHAT: {
                read_line(line, sizeof(line));
                if (!line[0]) break;
                if (strcmp(line, "/quit") == 0 || strcmp(line, "/exit") == 0) {
                    printf("再见。\n");
                    g_running = 0;
                    break;
                }
                if (strcmp(line, "/reconfig") == 0) {
                    g_state = ST_WIZARD_KEY;
                    g_phase = 0;
                    wizard_prompt();
                    break;
                }
                if (strcmp(line, "/status") == 0) {
                    print_status();
                    fflush(stdout);
                    break;
                }
                if (strcmp(line, "/clear") == 0) {
                    printf(ANSI_CLS);
                    fflush(stdout);
                    break;
                }
                if (strcmp(line, "/help") == 0) {
                    printf("指令: /quit /reconfig /status /clear\n"
                           "内部指令（本地执行，不发云端）: HELP / STATUS / AUTH STATUS / MEMO INDEX|READ|WRITE|SEARCH / SKILLS LIST / CONFIG\n"
                           "其他输入为对话内容，发送云端 AI。\n");
                    fflush(stdout);
                    break;
                }
                /* 内部指令（6800 协议）在 TUI 中也本地执行，不当作对话发云端 */
                if (strncmp(line, "AUTH ", 5) == 0 ||
                    strncmp(line, "MEMO ", 5) == 0 ||
                    strncmp(line, "MEM ", 4) == 0 ||
                    strncmp(line, "TASK ", 5) == 0 ||
                    strncmp(line, "SKILLS ", 7) == 0 ||
                    strncmp(line, "CONFIG ", 7) == 0 ||
                    strcmp(line, "STATUS") == 0 ||
                    strcmp(line, "HELP") == 0 ||
                    strcmp(line, "CONFIG") == 0 ||
                    strcmp(line, "HELLO") == 0) {
                    char resp[BUF_SIZE * 2];
                    process_command(line, resp, sizeof(resp));
                    printf(ANSI_CYAN "[本地] " ANSI_RESET "%s", resp);
                    fflush(stdout);
                    break;
                }
                printf(ANSI_YELLOW "AI 思考中...\n" ANSI_RESET);
                fflush(stdout);
                char rep[BUF_SIZE];
                char note[256];
                int rc = smart_chat(line, rep, sizeof(rep), note, sizeof(note));
                if (rc != 0) {
                    printf(ANSI_RED "⚠ 调用失败（错误码 %d）。%s\n" ANSI_RESET, rc, note);
                    if (rep[0]) printf("原始响应: %.300s\n", rep);
                    break;
                }
                if (note[0]) printf("%s\n", note);
                char content[BUF_SIZE];
                if (json_get_string(rep, "content", content, sizeof(content))) {
                    printf(ANSI_CYAN "奇点OS AI > " ANSI_RESET "%s\n", content);
                } else {
                    printf("（未能解析模型回复）原始响应: %.400s\n", rep);
                }
                fflush(stdout);
                break;
            }
            }
        }
    }

    close(srv);
    printf("AI 服务停止\n");
    return 0;
}
