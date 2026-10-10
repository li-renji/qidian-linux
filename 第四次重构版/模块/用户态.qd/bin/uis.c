/*
 * uis.c — 奇点OS 用户态模块（用户态.qd/bin/uis）v2.0
 *
 * 主系统的用户态 = 独立 .qd 模块（可替换）：
 *   - v1.0：基础用户命令（whoami/pwd/date/echo）
 *   - v1.1：主系统用户界面会话（绑定 /dev/tty0，VNC 可见，文本）
 *   - v2.0：图形会话（fb 自绘 + tty0 raw 键盘）：
 *           · 启动即渲染 fb 桌面（渲染器 fbdesktop，中文完整显示）
 *           · 命令输出捕获后经 渲染器 fbfile 整页绘制
 *           · 底部输入条（渲染器 fbbar）
 *           · 无 /dev/fb0 时自动回退文本会话（串口可用）
 *   - 模块间只有调用关系：本模块 fork+exec 调 渲染器/包管理，
 *     经 6800 调 AI 子系统，不复制任何模块实现
 *
 * 命令接口（模块契约）：
 *   uis help / whoami / pwd / date / echo <t>
 *   uis（无参数）→ 主系统用户界面会话（VNC，优先图形后端）
 */
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <stdlib.h>
#include <time.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/wait.h>
#include <errno.h>
#include <fcntl.h>
#include <termios.h>

#define AI_PORT 6800
#define BUF 4096
#define OUT_FILE "/tmp/uis_out.txt"

static void usage(void)
{
    printf("奇点OS 用户态模块 (uis) v2.0\n");
    printf("用法: uis <命令>\n");
    printf("  whoami / pwd / date / echo <t>\n");
    printf("  （无参数）进入主系统用户界面会话（VNC，图形后端优先）\n");
}

/* ---------------- 模块间调用：fork+exec 调 /modules 下的系统模块 ---------------- */
static int run_module(const char *mod, char *const argv[])
{
    pid_t pid = fork();
    if (pid < 0) { perror("fork"); return -1; }
    if (pid == 0) {
        execv(mod, argv);
        fprintf(stderr, "调用 %s 失败: %s\n", mod, strerror(errno));
        _exit(127);
    }
    int st;
    waitpid(pid, &st, 0);
    return st;
}

static void call_render(char *const argv[])
{
    run_module("/modules/渲染器.qd/bin/render", argv);
}

static void call_pm(char *const argv[])
{
    run_module("/modules/包管理.qd/bin/pm", argv);
}

/* ---------------- AI 子系统访问（6800） ---------------- */
static int ai_send(const char *cmd, char *out, size_t sz)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(AI_PORT);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(fd, (struct sockaddr *)&a, sizeof(a)) != 0) {
        close(fd);
        return -1;
    }
    struct timeval tv;
    tv.tv_sec = 45;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    send(fd, cmd, (int)strlen(cmd), 0);
    send(fd, "\n", 1, 0);
    size_t got = 0;
    out[0] = 0;
    while (got + 1 < sz) {
        ssize_t n = recv(fd, out + got, sz - got - 1, 0);
        if (n <= 0) break;
        got += (size_t)n;
    }
    out[got] = 0;
    close(fd);
    return got > 0 ? 0 : -1;
}

static void cmd_ai(const char *text)
{
    char req[BUF], resp[BUF];
    snprintf(req, sizeof(req), "CHAT %s", text);
    if (ai_send(req, resp, sizeof(resp)) != 0) {
        printf("AI 子系统不可达（6800 未监听？）\n");
        return;
    }
    printf("%s\n", resp);
}

static void cmd_ai_status(void)
{
    char resp[BUF];
    if (ai_send("STATUS", resp, sizeof(resp)) != 0) {
        printf("AI 子系统不可达（6800 未监听？）\n");
        return;
    }
    printf("%s\n", resp);
}

static void cmd_ai_config(void)
{
    char resp[BUF];
    if (ai_send("CONFIG", resp, sizeof(resp)) != 0) {
        printf("AI 子系统不可达（6800 未监听？）\n");
        return;
    }
    printf("%s\n", resp);
}

/* ---------------- 命令执行：输出捕获到 OUT_FILE ---------------- */
static FILE *cap_out = NULL;
static int saved_stdout = -1;

static void cap_open(void)
{
    if (cap_out) { fflush(stdout); dup2(saved_stdout, 1); close(saved_stdout); fclose(cap_out); cap_out = NULL; saved_stdout = -1; }
    cap_out = fopen(OUT_FILE, "w");
    if (cap_out) {
        fflush(stdout);
        saved_stdout = dup(1);
        dup2(fileno(cap_out), 1);
    }
}

static void cap_close_show(void)
{
    if (cap_out) {
        fflush(stdout);
        if (saved_stdout >= 0) { dup2(saved_stdout, 1); close(saved_stdout); saved_stdout = -1; }
        fclose(cap_out); cap_out = NULL;
    }
}

/* 执行一条命令：stdout 重定向到 OUT_FILE（模块调用 + 自身 printf 都进文件） */
static int exec_captured(char *const argv[], const char *mod)
{
    int fd = open(OUT_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return -1;
    pid_t pid = fork();
    if (pid < 0) { close(fd); return -1; }
    if (pid == 0) {
        dup2(fd, 1);
        dup2(fd, 2);
        close(fd);
        execv(mod, argv);
        fprintf(stderr, "调用 %s 失败: %s\n", mod, strerror(errno));
        _exit(127);
    }
    close(fd);
    int st;
    waitpid(pid, &st, 0);
    return st;
}

/* 命令处理（返回 0 继续会话，1 退出） */
static int handle_line(char *line)
{
    if (strcmp(line, "exit") == 0 || strcmp(line, "quit") == 0 ||
        strcmp(line, "退出") == 0) return 1;
    if (strcmp(line, "help") == 0 || strcmp(line, "帮助") == 0) {
        cap_open();
        printf("奇点OS 主系统用户界面命令：\n");
        printf("  桌面            重绘主系统桌面（结构树+模块状态）\n");
        printf("  结构 [路径]     查看系统结构树（真实扫描，默认 /）\n");
        printf("  模块            列出系统模块（包管理 list）\n");
        printf("  安装 <模块>     从仓库安装模块（包管理 install）\n");
        printf("  卸载 <模块>     卸载模块（包管理 remove）\n");
        printf("  仓库            查看包仓库（包管理 repo list）\n");
        printf("  ai <话>         与 AI 子系统对话（6800）\n");
        printf("  ai 状态 / ai 配置\n");
        printf("  whoami/pwd/date 用户信息\n");
        printf("  退出            退出用户界面（回到子系统控制台）\n");
        printf("  帮助            本帮助\n");
        cap_close_show();
        return 0;
    }
    if (strcmp(line, "desktop") == 0 || strcmp(line, "桌面") == 0) {
        char *av[] = { "render", "fbdesktop", NULL };
        call_render(av);
        return 0;
    }
    if (strcmp(line, "module") == 0 || strcmp(line, "模块") == 0) {
        char *av[] = { "pm", "list", NULL };
        exec_captured(av, "/modules/包管理.qd/bin/pm");
        return 0;
    }
    if (strcmp(line, "repo") == 0 || strcmp(line, "仓库") == 0) {
        char *av[] = { "pm", "repo", "list", NULL };
        exec_captured(av, "/modules/包管理.qd/bin/pm");
        return 0;
    }
    if (strncmp(line, "tree", 4) == 0 || strncmp(line, "结构", 6) == 0) {
        char *sp = strchr(line, ' ');
        char *av[6]; int na = 0;
        av[na++] = "render"; av[na++] = "tree";
        if (sp) av[na++] = sp + 1;
        av[na] = NULL;
        exec_captured(av, "/modules/渲染器.qd/bin/render");
        return 0;
    }
    if (strncmp(line, "install", 7) == 0 || strncmp(line, "安装", 6) == 0) {
        char *sp = strchr(line, ' ');
        if (!sp) { printf("用法: 安装 <模块>\n"); return 0; }
        char *av[] = { "pm", "install", sp + 1, NULL };
        exec_captured(av, "/modules/包管理.qd/bin/pm");
        return 0;
    }
    if (strncmp(line, "remove", 6) == 0 || strncmp(line, "卸载", 6) == 0) {
        char *sp = strchr(line, ' ');
        if (!sp) { printf("用法: 卸载 <模块>\n"); return 0; }
        char *av[] = { "pm", "remove", sp + 1, NULL };
        exec_captured(av, "/modules/包管理.qd/bin/pm");
        return 0;
    }
    if (strncmp(line, "ai ", 3) == 0) {
        cap_open();
        cmd_ai(line + 3);
        cap_close_show();
        return 0;
    }
    if (strcmp(line, "ai") == 0 || strcmp(line, "ai 状态") == 0 || strcmp(line, "ai status") == 0) {
        cap_open();
        cmd_ai_status();
        cap_close_show();
        return 0;
    }
    if (strcmp(line, "ai 配置") == 0 || strcmp(line, "ai config") == 0) {
        cap_open();
        cmd_ai_config();
        cap_close_show();
        return 0;
    }
    if (strcmp(line, "whoami") == 0) {
        cap_open();
        printf("user\n");
        cap_close_show();
        return 0;
    }
    if (strcmp(line, "pwd") == 0) {
        cap_open();
        printf("/子框架.qd/主框架.qd\n");
        cap_close_show();
        return 0;
    }
    if (strcmp(line, "date") == 0) {
        cap_open();
        time_t t = time(NULL);
        printf("%s", ctime(&t));
        cap_close_show();
        return 0;
    }
    if (strncmp(line, "echo ", 5) == 0) {
        cap_open();
        printf("%s\n", line + 5);
        cap_close_show();
        return 0;
    }
    /* 未知命令：透传给 Linux shell 执行（ls/cat/free/ps 等通用命令）
       输出捕获到 OUT_FILE，由 Enter 处理后的 show_out 显示在 fb */
    {
        char *av[] = { "sh", "-c", line, NULL };
        int r = exec_captured(av, "/bin/sh");
        if (r != 0) {
            cap_open();
            printf("命令执行失败（code=%d），输入 帮助 查看可用命令\n", r);
            cap_close_show();
        }
        return 0;
    }
}

/* 渲染输出文件到 fb（若失败回退文本打印） */
static void show_out(const char *title)
{
    if (access("/dev/fb0", F_OK) == 0) {
        char *av[] = { "render", "fbfile", OUT_FILE, (char *)title, NULL };
        call_render(av);
    } else {
        FILE *f = fopen(OUT_FILE, "r");
        if (f) {
            char l[512];
            while (fgets(l, sizeof(l), f)) fputs(l, stdout);
            fclose(f);
        }
    }
}

/* ---------------- 图形会话（fb + tty0 raw 键盘） ---------------- */
static void session_text(void);   /* 前向声明：无 fb 时回退文本会话 */

static void fb_bar_show(const char *prompt, const char *input)
{
    char bar[512];
    snprintf(bar, sizeof(bar), "%s%s", prompt, input);
    char *av[] = { "render", "fbbar", bar, NULL };
    call_render(av);
}

static void session_fb(void)
{
    /* 打开 tty0 原始键盘输入 */
    int tty = open("/dev/tty0", O_RDWR);
    if (tty < 0) { session_text(); return; }
    struct termios old, raw;
    tcgetattr(tty, &old);
    raw = old;
    cfmakeraw(&raw);
    tcsetattr(tty, TCSAFLUSH, &raw);
    /* 隐藏光标 */
    write(tty, "\033[?25l", 6);

    /* 渲染桌面 */
    {
        char *av[] = { "render", "fbdesktop", NULL };
        call_render(av);
    }
    char input[256];
    int in_len = 0;
    input[0] = 0;
    for (;;) {
        fb_bar_show("奇点OS~$ ", input);
        /* 读一个字符 */
        unsigned char ch;
        ssize_t n = read(tty, &ch, 1);
        if (n <= 0) continue;
        if (ch == '\r' || ch == '\n') {
            if (in_len == 0) continue;
            char *av[] = { "render", "fbbar", "奇点OS~$ 执行中...", NULL };
            call_render(av);
            char line[256];
            snprintf(line, sizeof(line), "%s", input);
            int r = handle_line(line);
            if (r) break;
            show_out("奇点OS 主系统");
            in_len = 0;
            input[0] = 0;
            continue;
        }
        if (ch == 127 || ch == 8) {           /* Backspace */
            if (in_len > 0) { input[--in_len] = 0; }
            continue;
        }
        if (ch == 3) {                         /* Ctrl-C 清输入 */
            in_len = 0;
            input[0] = 0;
            continue;
        }
        if (ch == 1) {                         /* Ctrl-A：重绘桌面 */
            char *av[] = { "render", "fbdesktop", NULL };
            call_render(av);
            continue;
        }
        if (in_len < (int)sizeof(input) - 1) {
            input[in_len++] = ch;
            input[in_len] = 0;
        }
    }
    /* 恢复终端 */
    tcsetattr(tty, TCSAFLUSH, &old);
    write(tty, "\033[?25h", 6);
    close(tty);
}

/* ---------------- 文本会话（无 fb 时回退，串口可用） ---------------- */
static void desktop_help(void)
{
    printf("奇点OS 主系统用户界面命令：\n");
    printf("  桌面            重绘主系统桌面（结构树+模块状态）\n");
    printf("  结构 [路径]     查看系统结构树（真实扫描，默认 /）\n");
    printf("  模块            列出系统模块（包管理 list）\n");
    printf("  安装 <模块>     从仓库安装模块（包管理 install）\n");
    printf("  卸载 <模块>     卸载模块（包管理 remove）\n");
    printf("  仓库            查看包仓库（包管理 repo list）\n");
    printf("  ai <话>         与 AI 子系统对话（6800）\n");
    printf("  ai 状态 / ai 配置\n");
    printf("  whoami/pwd/date 用户信息\n");
    printf("  退出            退出用户界面（回到子系统控制台）\n");
    printf("  帮助            本帮助\n");
}

static void session_text(void)
{
    printf("\033[2J\033[H");
    {
        char *av[] = { "render", "desktop", NULL };
        call_render(av);
    }
    char line[512];
    for (;;) {
        printf("奇点OS~$ ");
        fflush(stdout);
        if (!fgets(line, sizeof(line), stdin)) break;
        line[strcspn(line, "\r\n")] = 0;
        if (strcmp(line, "exit") == 0 || strcmp(line, "quit") == 0 ||
            strcmp(line, "退出") == 0) break;
        if (strcmp(line, "help") == 0 || strcmp(line, "帮助") == 0) { desktop_help(); continue; }
        if (strcmp(line, "desktop") == 0 || strcmp(line, "桌面") == 0) {
            char *av[] = { "render", "desktop", NULL };
            call_render(av);
            continue;
        }
        if (strcmp(line, "module") == 0 || strcmp(line, "模块") == 0) {
            char *av[] = { "pm", "list", NULL };
            call_pm(av);
            continue;
        }
        if (strcmp(line, "repo") == 0 || strcmp(line, "仓库") == 0) {
            char *av[] = { "pm", "repo", "list", NULL };
            call_pm(av);
            continue;
        }
        if (strncmp(line, "tree", 4) == 0 || strncmp(line, "结构", 6) == 0) {
            char *sp = strchr(line, ' ');
            char *av[6]; int na = 0;
            av[na++] = "render"; av[na++] = "tree";
            if (sp) av[na++] = sp + 1;
            av[na] = NULL;
            call_render(av);
            continue;
        }
        if (strncmp(line, "install", 7) == 0 || strncmp(line, "安装", 6) == 0) {
            char *sp = strchr(line, ' ');
            if (!sp) { printf("用法: 安装 <模块>\n"); continue; }
            char *av[] = { "pm", "install", sp + 1, NULL };
            call_pm(av);
            continue;
        }
        if (strncmp(line, "remove", 6) == 0 || strncmp(line, "卸载", 6) == 0) {
            char *sp = strchr(line, ' ');
            if (!sp) { printf("用法: 卸载 <模块>\n"); continue; }
            char *av[] = { "pm", "remove", sp + 1, NULL };
            call_pm(av);
            continue;
        }
        if (strncmp(line, "ai ", 3) == 0) { cmd_ai(line + 3); continue; }
        if (strcmp(line, "ai") == 0) { cmd_ai_status(); continue; }
        if (strcmp(line, "ai 状态") == 0 || strcmp(line, "ai status") == 0) { cmd_ai_status(); continue; }
        if (strcmp(line, "ai 配置") == 0 || strcmp(line, "ai config") == 0) { cmd_ai_config(); continue; }
        if (strcmp(line, "whoami") == 0) { printf("user\n"); continue; }
        if (strcmp(line, "pwd") == 0) { printf("/子框架.qd/主框架.qd\n"); continue; }
        if (strcmp(line, "date") == 0) {
            time_t t = time(NULL);
            printf("%s", ctime(&t));
            continue;
        }
        if (strncmp(line, "echo ", 5) == 0) { printf("%s\n", line + 5); continue; }
        printf("未知命令: %s（输入 帮助）\n", line);
    }
    printf("用户界面退出，子系统控制台接管（VNC/串口 me>）\n");
}

static void session(void)
{
    if (access("/dev/fb0", F_OK) == 0)
        session_fb();
    else
        session_text();
}

int main(int argc, char **argv)
{
    if (argc < 2) { session(); return 0; }
    const char *c = argv[1];
    if (strcmp(c, "help") == 0) usage();
    else if (strcmp(c, "whoami") == 0) printf("user\n");
    else if (strcmp(c, "pwd") == 0) printf("/子框架.qd/主框架.qd\n");
    else if (strcmp(c, "date") == 0) {
        time_t t = time(NULL);
        printf("%s", ctime(&t));
    }
    else if (strcmp(c, "echo") == 0 && argc >= 3) {
        for (int i = 2; i < argc; i++) {
            if (i > 2) putchar(' ');
            fputs(argv[i], stdout);
        }
        putchar('\n');
    }
    else { usage(); return 1; }
    return 0;
}
