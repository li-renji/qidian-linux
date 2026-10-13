/*
 * desktop.c — 奇点OS 桌面模块（桌面.qd/bin/desktop）v1.0
 *
 * 转译桌面（借鉴 Xfce 组件架构，代码自研，不依赖 GTK/X11）：
 *   xfwm4        → 窗口列表 / 焦点 / 标题栏拖动 / 关闭按钮（本文件）
 *   xfdesktop    → 桌面图标（启动器，本文件）
 *   xfce4-panel  → 底部快捷栏（本文件）
 *   渲染          → fork+exec 渲染器.qd（fbscreen 场景原语，一次绘制）
 *   输入          → /dev/input/event*（evdev 鼠标）+ /dev/tty0（raw 键盘）
 *   光标          → 本进程 mmap /dev/fb0 自绘叠加（12x16 箭头，保存/恢复像素）
 *
 * 命令接口（模块契约）：
 *   desktop help
 *   desktop（无参数）→ 转译桌面会话（VNC 图形，需要 /dev/fb0 + 渲染器.qd）
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <errno.h>
#include <time.h>
#include <poll.h>
#include <termios.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <stdint.h>

/* ---------------- 常量 ---------------- */
#define RENDER "/modules/渲染器.qd/bin/render"
#define SCENE_FILE "/tmp/desk.scene"
#define WIN_MAX 4
#define ICON_N 6
#define ICON_W 72
#define ICON_H 62
#define ICON_X0 12
#define ICON_Y0 12
#define ICON_DX 80
#define ICON_DY 70
#define PANEL_H 28
#define WIN_W 470
#define WIN_H 300
#define WIN_BAR_H 22
#define IN_H 20                    /* 窗口输入条高 */
#define AI_PORT 6800
#define BUF 4096

/* 光标箭头 12x16（1=白色前景） */
static const uint8_t CUR_BITS[16] = {
    0b11110000, 0b10000000, 0b10010000, 0b10000000,
    0b10110000, 0b10000000, 0b10010000, 0b10000000,
    0b10001000, 0b10000000, 0b10001000, 0b10000000,
    0b10000100, 0b10000000, 0b10000100, 0b00000000,
};
/* 位从左到右：bit7..bit0；两字节一行共 16 列? 实际取 12 列 */
#define CUR_W 12
#define CUR_H 16

/* ---------------- fb（光标层） ---------------- */
static int fb_fd = -1;
static uint8_t *fb_mem = NULL;
static long fb_line = 0;
static unsigned scr_w = 1024, scr_h = 768;
static uint32_t *cur_save = NULL;      /* 光标覆盖区备份 */
static int cur_x = 40, cur_y = 40;
static int cur_vis = 1;

static int fb_open(void)
{
    fb_fd = open("/dev/fb0", O_RDWR);
    if (fb_fd < 0) { perror("桌面: open /dev/fb0 O_RDWR"); return -1; }
    struct fb_var_screeninfo vi;
    if (ioctl(fb_fd, FBIOGET_VSCREENINFO, &vi) != 0) { perror("桌面: FBIOGET_VSCREENINFO"); close(fb_fd); fb_fd = -1; return -1; }
    struct fb_fix_screeninfo fi;
    if (ioctl(fb_fd, FBIOGET_FSCREENINFO, &fi) != 0) { perror("桌面: FBIOGET_FSCREENINFO"); close(fb_fd); fb_fd = -1; return -1; }
    scr_w = vi.xres; scr_h = vi.yres;
    fb_line = fi.line_length;
    size_t sz = (size_t)fi.smem_len;
    fb_mem = (uint8_t *)mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_SHARED, fb_fd, 0);
    if (fb_mem == MAP_FAILED) { perror("桌面: mmap fb0"); close(fb_fd); fb_fd = -1; fb_mem = NULL; return -1; }
    cur_save = (uint32_t *)malloc(CUR_W * CUR_H * sizeof(uint32_t));
    return 0;
}

static void fb_px(int x, int y, uint32_t c)
{
    if (x < 0 || y < 0 || (unsigned)x >= scr_w || (unsigned)y >= scr_h) return;
    ((uint32_t *)(fb_mem + (size_t)y * fb_line))[x] = c;
}

static uint32_t fb_get(int x, int y)
{
    if (x < 0 || y < 0 || (unsigned)x >= scr_w || (unsigned)y >= scr_h) return 0;
    return ((uint32_t *)(fb_mem + (size_t)y * fb_line))[x];
}

static void cursor_save(int x, int y)
{
    for (int r = 0; r < CUR_H; r++)
        for (int c = 0; c < CUR_W; c++)
            cur_save[r * CUR_W + c] = fb_get(x + c, y + r);
}

static void cursor_draw(int x, int y)
{
    for (int r = 0; r < CUR_H; r++) {
        for (int c = 0; c < CUR_W; c++) {
            uint8_t bit;
            if (c < 8) bit = (CUR_BITS[r * 2] >> (7 - c)) & 1;
            else       bit = (CUR_BITS[r * 2 + 1] >> (15 - c)) & 1;
            if (bit) fb_px(x + c, y + r, 0x00FFFFFF);
        }
    }
}

static void cursor_move(int nx, int ny)
{
    if (nx < 0) nx = 0;
    if (ny < 0) ny = 0;
    if (nx >= (int)scr_w - CUR_W) nx = (int)scr_w - CUR_W;
    if (ny >= (int)scr_h - CUR_H) ny = (int)scr_h - CUR_H;
    if (nx == cur_x && ny == cur_y) return;
    /* 恢复旧区域 */
    for (int r = 0; r < CUR_H; r++)
        for (int c = 0; c < CUR_W; c++)
            fb_px(cur_x + c, cur_y + r, cur_save[r * CUR_W + c]);
    cursor_save(nx, ny);
    cursor_draw(nx, ny);
    cur_x = nx; cur_y = ny;
}

/* ---------------- 模块调用 ---------------- */
static int run_module(const char *mod, char *const argv[], const char *outfile)
{
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        if (outfile) {
            int fd = open(outfile, O_WRONLY | O_CREAT | O_TRUNC, 0644);
            if (fd >= 0) { dup2(fd, 1); dup2(fd, 2); close(fd); }
        }
        execv(mod, argv);
        _exit(127);
    }
    int st;
    waitpid(pid, &st, 0);
    return st;
}

static void render_call(char *const argv[])
{
    run_module(RENDER, argv, NULL);
}

/* ---------------- AI 访问（6800，与用户态相同协议） ---------------- */
static int ai_send(const char *cmd, char *out, size_t sz)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(AI_PORT);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(fd, (struct sockaddr *)&a, sizeof(a)) != 0) { close(fd); return -1; }
    struct timeval tv;
    tv.tv_sec = 45; tv.tv_usec = 0;
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

/* ---------------- 窗口模型（转译 xfwm4 最小窗口管理） ---------------- */
struct Win {
    int used;
    int x, y, w, h;
    int focused;                 /* 是否为焦点窗口 */
    int dragging;                /* 正在拖动 */
    int dx, dy;                  /* 按下点相对窗口原点 */
    char title[32];
    char outfile[256];
    int offset;                  /* 内容滚动行偏移 */
    char inbuf[256];
    int inlen;
    int is_ai;                   /* AI 对话窗口：输入自动加 CHAT */
};

static struct Win wins[WIN_MAX];
static int icon_focused[ICON_N] = {0};   /* hover 高亮 */

static struct Win *focused_win(void)
{
    for (int i = WIN_MAX - 1; i >= 0; i--)
        if (wins[i].used && wins[i].focused) return &wins[i];
    for (int i = WIN_MAX - 1; i >= 0; i--)
        if (wins[i].used) return &wins[i];
    return NULL;
}

static struct Win *win_at(int x, int y)
{
    for (int i = WIN_MAX - 1; i >= 0; i--) {
        struct Win *w = &wins[i];
        if (!w->used) continue;
        if (x >= w->x && x < w->x + w->w && y >= w->y && y < w->y + w->h)
            return w;
    }
    return NULL;
}

static void set_focus(struct Win *w)
{
    for (int i = 0; i < WIN_MAX; i++) wins[i].focused = 0;
    if (w) w->focused = 1;
}

/* 打开窗口：标题 + 预置内容（来自命令输出或帮助文本） */
static struct Win *open_win(const char *title, const char *outfile, int is_ai)
{
    struct Win *slot = NULL;
    for (int i = 0; i < WIN_MAX; i++) {
        if (!wins[i].used) { slot = &wins[i]; break; }
    }
    if (!slot) {                 /* 全满：覆盖最旧（序号最小） */
        slot = &wins[0];
        slot->used = 0;
    }
    int nx = 120 + slot->x * 0;  /* 层叠偏移 */
    int ny = 90;
    for (int i = 0; i < WIN_MAX; i++) {
        if (wins[i].used) { nx = wins[i].x + 28; ny = wins[i].y + 24; }
    }
    if (nx + WIN_W > (int)scr_w - 20) nx = (int)scr_w - WIN_W - 20;
    if (ny + WIN_H > (int)scr_h - PANEL_H - 10) ny = (int)scr_h - WIN_H - PANEL_H - 10;
    if (nx < 10) nx = 10;
    if (ny < 10) ny = 10;
    memset(slot, 0, sizeof(*slot));
    slot->used = 1;
    slot->x = nx; slot->y = ny;
    slot->w = WIN_W; slot->h = WIN_H;
    snprintf(slot->title, sizeof(slot->title), "%s", title);
    snprintf(slot->outfile, sizeof(slot->outfile), "%s", outfile);
    slot->is_ai = is_ai;
    set_focus(slot);
    return slot;
}

static void close_win(struct Win *w)
{
    w->used = 0;
    set_focus(NULL);
}

/* ---------------- 命令执行（输出写 outfile） ---------------- */
static void exec_cmd(struct Win *w, const char *line)
{
    char cmd[512];
    snprintf(cmd, sizeof(cmd), "%s", line);
    /* 去掉首尾空白 */
    while (*cmd == ' ') memmove(cmd, cmd + 1, strlen(cmd));
    int n = (int)strlen(cmd);
    while (n > 0 && (cmd[n-1] == ' ' || cmd[n-1] == '\r')) cmd[--n] = 0;

    if (cmd[0] == 0) { w->inlen = 0; w->inbuf[0] = 0; w->offset = 0; return; }

    int is_ai_win = w->is_ai;
    if (is_ai_win && strncmp(cmd, "ai ", 3) != 0 && strcmp(cmd, "ai 状态") != 0 &&
        strcmp(cmd, "ai") != 0 && strcmp(cmd, "exit") != 0 && strcmp(cmd, "退出") != 0) {
        /* AI 窗口：输入直接作为对话内容 */
        char full[600];
        snprintf(full, sizeof(full), "CHAT %s", cmd);
        char resp[BUF];
        if (ai_send(full, resp, sizeof(resp)) != 0) {
            FILE *f = fopen(w->outfile, "w");
            if (f) { fprintf(f, "AI 子系统不可达（6800 未监听？）\n"); fclose(f); }
        } else {
            FILE *f = fopen(w->outfile, "w");
            if (f) { fprintf(f, "你: %s\n\n%s\n", cmd, resp); fclose(f); }
        }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }

    if (strcmp(cmd, "exit") == 0 || strcmp(cmd, "quit") == 0 || strcmp(cmd, "退出") == 0) {
        close_win(w);
        return;
    }
    if (strcmp(cmd, "help") == 0 || strcmp(cmd, "帮助") == 0) {
        FILE *f = fopen(w->outfile, "w");
        if (f) {
            fprintf(f, "奇点OS 桌面命令：\n");
            fprintf(f, "  桌面         重绘桌面\n");
            fprintf(f, "  结构 [路径]  系统结构树（真实扫描）\n");
            fprintf(f, "  模块         模块列表（包管理 list）\n");
            fprintf(f, "  仓库         包仓库（repo list）\n");
            fprintf(f, "  安装 <模块>  安装模块\n");
            fprintf(f, "  卸载 <模块>  卸载模块\n");
            fprintf(f, "  ai <话>      与 AI 子系统对话\n");
            fprintf(f, "  ai 状态 / ai 配置\n");
            fprintf(f, "  whoami/pwd/date/echo\n");
            fprintf(f, "  其余命令    透传给 Linux shell 执行\n");
            fprintf(f, "  退出 / exit  关闭窗口\n");
            fclose(f);
        }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strcmp(cmd, "桌面") == 0 || strcmp(cmd, "desktop") == 0) {
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strncmp(cmd, "结构", 6) == 0 || strncmp(cmd, "tree", 4) == 0) {
        char *av[6]; int na = 0;
        av[na++] = "render"; av[na++] = "tree";
        char *sp = strchr(cmd, ' ');
        if (sp) av[na++] = sp + 1;
        av[na] = NULL;
        run_module(RENDER, av, w->outfile);
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strcmp(cmd, "模块") == 0 || strcmp(cmd, "module") == 0) {
        char *av[] = { "pm", "list", NULL };
        run_module("/modules/包管理.qd/bin/pm", av, w->outfile);
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strcmp(cmd, "仓库") == 0 || strcmp(cmd, "repo") == 0) {
        char *av[] = { "pm", "repo", "list", NULL };
        run_module("/modules/包管理.qd/bin/pm", av, w->outfile);
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strncmp(cmd, "安装", 6) == 0 || strncmp(cmd, "install", 7) == 0) {
        char *sp = strchr(cmd, ' ');
        if (!sp) {
            FILE *f = fopen(w->outfile, "w");
            if (f) { fprintf(f, "用法: 安装 <模块>\n"); fclose(f); }
        } else {
            char *av[] = { "pm", "install", sp + 1, NULL };
            run_module("/modules/包管理.qd/bin/pm", av, w->outfile);
        }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strncmp(cmd, "卸载", 6) == 0 || strncmp(cmd, "remove", 6) == 0) {
        char *sp = strchr(cmd, ' ');
        if (!sp) {
            FILE *f = fopen(w->outfile, "w");
            if (f) { fprintf(f, "用法: 卸载 <模块>\n"); fclose(f); }
        } else {
            char *av[] = { "pm", "remove", sp + 1, NULL };
            run_module("/modules/包管理.qd/bin/pm", av, w->outfile);
        }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strncmp(cmd, "ai ", 3) == 0 || strcmp(cmd, "ai") == 0 || strcmp(cmd, "ai 状态") == 0) {
        char full[600];
        if (strcmp(cmd, "ai") == 0 || strcmp(cmd, "ai 状态") == 0)
            snprintf(full, sizeof(full), "STATUS");
        else
            snprintf(full, sizeof(full), "CHAT %s", cmd + 3);
        char resp[BUF];
        if (ai_send(full, resp, sizeof(resp)) != 0) {
            FILE *f = fopen(w->outfile, "w");
            if (f) { fprintf(f, "AI 子系统不可达（6800 未监听？）\n"); fclose(f); }
        } else {
            FILE *f = fopen(w->outfile, "w");
            if (f) { fprintf(f, "%s\n", resp); fclose(f); }
        }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strcmp(cmd, "whoami") == 0) {
        FILE *f = fopen(w->outfile, "w");
        if (f) { fprintf(f, "user\n"); fclose(f); }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strcmp(cmd, "pwd") == 0) {
        FILE *f = fopen(w->outfile, "w");
        if (f) { fprintf(f, "/子框架.qd/主框架.qd\n"); fclose(f); }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strcmp(cmd, "date") == 0) {
        FILE *f = fopen(w->outfile, "w");
        if (f) {
            time_t t = time(NULL);
            fprintf(f, "%s", ctime(&t));
            fclose(f);
        }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    if (strncmp(cmd, "echo ", 5) == 0) {
        FILE *f = fopen(w->outfile, "w");
        if (f) { fprintf(f, "%s\n", cmd + 5); fclose(f); }
        w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
        return;
    }
    /* 其余：透传 Linux shell */
    char *av[] = { "sh", "-c", cmd, NULL };
    int r = run_module("/bin/sh", av, w->outfile);
    if (r != 0) {
        FILE *f = fopen(w->outfile, "w");
        if (f) { fprintf(f, "命令执行失败（code=%d），输入 帮助 查看可用命令\n", r); fclose(f); }
    }
    w->inlen = 0; w->inbuf[0] = 0; w->offset = 0;
}

/* ---------------- 场景生成与渲染 ---------------- */
static void gen_scene(void)
{
    FILE *f = fopen(SCENE_FILE, "w");
    if (!f) return;
    fprintf(f, "bg 101820\n");
    /* 顶部状态条（时间） */
    {
        time_t t = time(NULL);
        struct tm *tm = localtime(&t);
        char ts[64];
        snprintf(ts, sizeof(ts), "%04d-%02d-%02d %02d:%02d", tm->tm_year + 1900,
                 tm->tm_mon + 1, tm->tm_mday, tm->tm_hour, tm->tm_min);
        fprintf(f, "rect 0 0 %u 26 1E2A36\n", scr_w);
        fprintf(f, "text 10 4 FFFFFF 1E2A36 奇点OS 转译桌面 v1.0\n");
        fprintf(f, "text %u 4 FFFFFF 1E2A36 %s\n", scr_w - 200, ts);
    }
    /* 图标 */
    const char *icons[ICON_N] = { "终端", "系统结构", "模块", "仓库", "AI对话", "帮助" };
    for (int i = 0; i < ICON_N; i++) {
        int ix = ICON_X0 + i * ICON_DX;
        int iy = ICON_Y0;
        fprintf(f, "icon %d %d %d %s\n", ix, iy, icon_focused[i], icons[i]);
    }
    /* 窗口（焦点最后画，z 序最高） */
    for (int i = 0; i < WIN_MAX; i++) {
        struct Win *w = &wins[i];
        if (!w->used) continue;
        fprintf(f, "win %d %d %d %d %d %s %s %d\n",
                w->x, w->y, w->w, w->h, w->focused ? 1 : 0, w->title,
                w->outfile, w->offset);
    }
    /* 面板（转译 xfce4-panel：快捷按钮） */
    fprintf(f, "panel %u 终端 结构 模块 仓库 AI\n", scr_h - PANEL_H);
    fclose(f);
    char *av[] = { "render", "fbscreen", (char *)SCENE_FILE, NULL };
    render_call(av);
}

/* 画焦点窗口输入行（单独一次渲染，避免整场景重画） */
static void draw_input_line(struct Win *w)
{
    if (!w) return;
    char bar[512];
    snprintf(bar, sizeof(bar), "> %s%s", w->inbuf, w->inlen ? "" : " ");
    int iny = w->y + w->h - IN_H;
    char xb[16], yb[16];
    snprintf(xb, sizeof(xb), "%d", w->x + 6);
    snprintf(yb, sizeof(yb), "%d", iny);
    char *av[] = { "render", "fbtext", xb, yb, "FFFFFF", "181818", bar, NULL };
    render_call(av);
}

/* ---------------- 输入设备 ---------------- */
#define MOUSE_MAX 4
static int mouse_fds[MOUSE_MAX];
static int mouse_n = 0;
static int hb = 0;

/* 打开全部 evdev 指针设备（PS/2 相对 + usb-tablet 绝对都收集）：
   具备按键(BTN_LEFT) + 相对(REL_X/REL_Y)或绝对(ABS_X/ABS_Y)指针能力即视为指针设备 */
static void mouse_open(void)
{
    for (int i = 0; i < 16 && mouse_n < MOUSE_MAX; i++) {
        char p[64];
        snprintf(p, sizeof(p), "/dev/input/event%d", i);
        int fd = open(p, O_RDONLY | O_NONBLOCK);
        if (fd < 0) continue;
        unsigned long bits_rel = 0, bits_abs = 0;
        unsigned char bits_key[48] = {0};  /* 覆盖 key code 0-383（BTN_LEFT=272）*/
        if (ioctl(fd, EVIOCGBIT(EV_REL, sizeof(bits_rel)), &bits_rel) >= 0 &&
            ioctl(fd, EVIOCGBIT(EV_ABS, sizeof(bits_abs)), &bits_abs) >= 0 &&
            ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(bits_key)), bits_key) >= 0) {
            int is_ptr =
                ((bits_rel & (1UL << REL_X)) && (bits_rel & (1UL << REL_Y))) ||
                ((bits_abs & (1UL << ABS_X)) && (bits_abs & (1UL << ABS_Y)));
            int has_btn = (bits_key[BTN_LEFT / 8] & (1u << (BTN_LEFT % 8))) != 0;
            if (is_ptr && has_btn) {
                mouse_fds[mouse_n++] = fd;
                printf("桌面: 指针设备%d = %s（is_ptr=%d has_btn=%d）\n", mouse_n, p, is_ptr, has_btn);
                fflush(stdout);
                continue;
            }
            printf("桌面: %s 已打开，is_ptr=%d has_btn=%d（跳过）\n", p, is_ptr, has_btn);
            fflush(stdout);
        }
        close(fd);
    }
    if (mouse_n == 0) {
        printf("桌面: 未找到 evdev 指针设备，键盘可用\n");
        /* 诊断：/dev 根目录与 devtmpfs 挂载状态 */
        printf("桌面: --- /proc/mounts dev 行 ---\n");
        FILE *mf = fopen("/proc/mounts", "r");
        if (mf) {
            char line[256];
            while (fgets(line, sizeof(line), mf)) {
                if (strstr(line, "/dev") || strstr(line, "devtmpfs")) printf("桌面: %s", line);
            }
            fclose(mf);
        }
        printf("桌面: --- /dev 目录内容 ---\n");
        DIR *dv = opendir("/dev");
        if (dv) {
            struct dirent *e;
            while ((e = readdir(dv)) != NULL) {
                if (e->d_name[0] == '.') continue;
                printf("桌面: /dev/%s\n", e->d_name);
            }
            closedir(dv);
        } else {
            printf("桌面: /dev 目录打不开 errno=%d\n", errno);
        }
        fflush(stdout);
    }
}

static void handle_mouse(struct input_event *ev)
{
    static int btn_down = 0;
    if (ev->type == EV_ABS) {
        /* usb-tablet / 绝对坐标鼠标：映射 0-32767 → 屏幕 */
        if (ev->code == ABS_X) cursor_move((int)((long)ev->value * scr_w / 32767), cur_y);
        else if (ev->code == ABS_Y) cursor_move(cur_x, (int)((long)ev->value * scr_h / 32767));
        return;
    }
    if (ev->type == EV_REL) {
        if (ev->code == REL_X) cursor_move(cur_x + ev->value, cur_y);
        else if (ev->code == REL_Y) cursor_move(cur_x, cur_y + ev->value);
        else if (ev->code == REL_WHEEL) {
            struct Win *w = focused_win();
            if (w) {
                int d = ev->value > 0 ? -3 : 3;
                if (w->offset + d < 0) w->offset = 0; else w->offset += d;
                gen_scene();
                cursor_save(cur_x, cur_y);
                cursor_draw(cur_x, cur_y);
            }
        }
        return;
    }
    if (ev->type != EV_KEY) return;
    if (ev->code == BTN_LEFT) {
        if (ev->value == 1) btn_down = 1;       /* 按下 */
        else if (ev->value == 0) {              /* 释放 */
            btn_down = 0;
            struct Win *w = focused_win();
            if (w) w->dragging = 0;
        }
        if (ev->value == 1) {
            int x = cur_x, y = cur_y;
            /* 1) 面板按钮 */
            if (y >= (int)scr_h - PANEL_H) {
                int idx = (x - 6) / 98;
                if (idx >= 0 && idx < 5) {
                    const char *titles[5] = { "终端", "系统结构", "模块", "仓库", "AI对话" };
                    const char *files[5] = {
                        "/tmp/win_help.txt", "/tmp/win_tree.txt",
                        "/tmp/win_mod.txt", "/tmp/win_repo.txt", "/tmp/win_ai.txt"
                    };
                    int is_ai[5] = { 0, 0, 0, 0, 1 };
                    FILE *f = fopen(files[idx], "w");
                    if (f) {
                        if (idx == 0) fprintf(f, "奇点OS 终端（转译桌面）\n输入 帮助 查看命令，其余命令透传 Linux shell。\n");
                        else if (idx == 4) fprintf(f, "AI 对话窗口：直接输入对话内容，按回车发送（6800）。\n");
                        fclose(f);
                    }
                    struct Win *w2 = open_win(titles[idx], files[idx], is_ai[idx]);
                    if (w2 && idx == 1) {
                        char *av[] = { "render", "tree", "/", NULL };
                        run_module(RENDER, av, w2->outfile);
                    } else if (w2 && idx == 2) {
                        char *av[] = { "pm", "list", NULL };
                        run_module("/modules/包管理.qd/bin/pm", av, w2->outfile);
                    } else if (w2 && idx == 3) {
                        char *av[] = { "pm", "repo", "list", NULL };
                        run_module("/modules/包管理.qd/bin/pm", av, w2->outfile);
                    }
                    gen_scene();
                    cursor_save(cur_x, cur_y);
                    cursor_draw(cur_x, cur_y);
                }
                return;
            }
            /* 2) 窗口（z 序检查） */
            struct Win *w = win_at(x, y);
            if (w) {
                set_focus(w);
                if (y < w->y + WIN_BAR_H + 2) {        /* 标题栏 */
                    if (x >= w->x + w->w - 26) {       /* × 关闭 */
                        close_win(w);
                    } else {
                        w->dragging = 1;
                        w->dx = x - w->x;
                        w->dy = y - w->y;
                    }
                }
                gen_scene();
                cursor_save(cur_x, cur_y);
                cursor_draw(cur_x, cur_y);
                return;
            }
            /* 3) 桌面图标 */
            for (int i = 0; i < ICON_N; i++) {
                int ix = ICON_X0 + i * ICON_DX;
                if (x >= ix && x < ix + ICON_W && y >= ICON_Y0 && y < ICON_Y0 + ICON_H) {
                    const char *titles[ICON_N] = { "终端", "系统结构", "模块", "仓库", "AI对话", "帮助" };
                    const char *files[ICON_N] = {
                        "/tmp/win_help.txt", "/tmp/win_tree.txt",
                        "/tmp/win_mod.txt", "/tmp/win_repo.txt", "/tmp/win_ai.txt",
                        "/tmp/win_help2.txt"
                    };
                    int is_ai[ICON_N] = { 0, 0, 0, 0, 1, 0 };
                    if (i == 0) {
                        FILE *f = fopen(files[i], "w");
                        if (f) {
                            fprintf(f, "奇点OS 终端（转译桌面）\n输入 帮助 查看命令，其余命令透传 Linux shell。\n");
                            fclose(f);
                        }
                    } else if (i == 4) {
                        FILE *f = fopen(files[i], "w");
                        if (f) {
                            fprintf(f, "AI 对话窗口：直接输入对话内容，按回车发送（6800）。\n");
                            fclose(f);
                        }
                    } else if (i == 5) {
                        FILE *f = fopen(files[i], "w");
                        if (f) {
                            fprintf(f, "奇点OS 转译桌面 v1.0\n");
                            fprintf(f, "桌面图标与底部面板可打开应用窗口；\n");
                            fprintf(f, "窗口可拖动标题栏移动、点 × 关闭、滚轮滚动内容；\n");
                            fprintf(f, "窗口内输入命令回车执行（帮助 查看命令列表）。\n");
                            fclose(f);
                        }
                    }
                    struct Win *w2 = open_win(titles[i], files[i], is_ai[i]);
                    if (w2 && i == 1) {
                        char *av[] = { "render", "tree", "/", NULL };
                        run_module(RENDER, av, w2->outfile);
                    } else if (w2 && i == 2) {
                        char *av[] = { "pm", "list", NULL };
                        run_module("/modules/包管理.qd/bin/pm", av, w2->outfile);
                    } else if (w2 && i == 3) {
                        char *av[] = { "pm", "repo", "list", NULL };
                        run_module("/modules/包管理.qd/bin/pm", av, w2->outfile);
                    } else if (w2 && i == 5) {
                        w2->is_ai = 0;
                    }
                    gen_scene();
                    cursor_save(cur_x, cur_y);
                    cursor_draw(cur_x, cur_y);
                    return;
                }
            }
        }
    }
}

static void handle_key(unsigned char ch)
{
    struct Win *w = focused_win();
    if (!w) {
        if (ch == 1) gen_scene();
        return;
    }
    if (w->dragging) return;
    if (ch == '\r' || ch == '\n') {
        if (w->inlen == 0) return;
        char line[256];
        snprintf(line, sizeof(line), "%s", w->inbuf);
        exec_cmd(w, line);
        gen_scene();
        cursor_save(cur_x, cur_y);
        cursor_draw(cur_x, cur_y);
        draw_input_line(focused_win());
        return;
    }
    if (ch == 127 || ch == 8) {
        if (w->inlen > 0) w->inbuf[--w->inlen] = 0;
        draw_input_line(w);
        return;
    }
    if (ch == 3) {
        w->inlen = 0; w->inbuf[0] = 0;
        draw_input_line(w);
        return;
    }
    if (ch == 1) {
        gen_scene();
        cursor_save(cur_x, cur_y);
        cursor_draw(cur_x, cur_y);
        return;
    }
    if (w->inlen < (int)sizeof(w->inbuf) - 1) {
        w->inbuf[w->inlen++] = ch;
        w->inbuf[w->inlen] = 0;
        draw_input_line(w);
    }
}

/* ---------------- 会话主循环 ---------------- */
static void session(void)
{
    /* fb 与 tty 都是可重试资源：本进程是 PID 1 图形会话，任何情况不得退出
       （exit → Attempted to kill init → kernel panic）。失败则提示并重试。 */
    for (;;) {
        if (fb_open() == 0) break;   /* 成功(0)才跳出；失败(-1)提示后重试 */
        printf("桌面: /dev/fb0 打开失败，3 秒后重试...\n");
        fflush(stdout);
        sleep(3);
    }
    int tty = open("/dev/tty0", O_RDWR);
    if (tty < 0) {
        printf("桌面: /dev/tty0 不可用，3 秒后重试...\n");
        fflush(stdout);
        sleep(3);
        close(fb_fd);
        fb_mem = NULL;
        session();               /* 重试整个会话初始化 */
        return;
    }
    struct termios old, raw;
    tcgetattr(tty, &old);
    raw = old;
    cfmakeraw(&raw);
    tcsetattr(tty, TCSAFLUSH, &raw);
    write(tty, "\033[?25l", 6);              /* 隐藏系统光标 */

    mouse_open();

    gen_scene();
    cursor_save(cur_x, cur_y);
    cursor_draw(cur_x, cur_y);

    struct pollfd pf[1 + MOUSE_MAX];
    pf[0].fd = tty; pf[0].events = POLLIN;
    for (int k = 0; k < mouse_n; k++) { pf[1 + k].fd = mouse_fds[k]; pf[1 + k].events = POLLIN; }

    for (;;) {
        int nf = 1 + mouse_n;
        if (poll(pf, (nfds_t)nf, 100) <= 0) {
            /* 超时：刷新顶部时间（低频心跳：每 60 秒一条，作存活证明） */
            if (++hb % 600 == 0) {
                printf("桌面: 心跳 %d 秒\n", hb / 10);
                fflush(stdout);
            }
            gen_scene();
            cursor_save(cur_x, cur_y);
            cursor_draw(cur_x, cur_y);
            draw_input_line(focused_win());
            continue;
        }
        if (pf[0].revents & POLLIN) {
            unsigned char ch;
            while (read(tty, &ch, 1) == 1) handle_key(ch);
        }
        for (int k = 0; k < mouse_n; k++) {
            if (!(pf[1 + k].revents & POLLIN)) continue;
            struct input_event ev;
            while (read(mouse_fds[k], &ev, sizeof(ev)) == (ssize_t)sizeof(ev)) {
                /* 拖动窗口（相对/绝对移动都支持） */
                if ((ev.type == EV_REL && (ev.code == REL_X || ev.code == REL_Y)) ||
                    (ev.type == EV_ABS && (ev.code == ABS_X || ev.code == ABS_Y))) {
                    struct Win *w = focused_win();
                    if (w && w->dragging) {
                        w->x = cur_x - w->dx;
                        w->y = cur_y - w->dy;
                        if (w->x < 0) w->x = 0;
                        if (w->y < 0) w->y = 0;
                        gen_scene();
                        cursor_save(cur_x, cur_y);
                        cursor_draw(cur_x, cur_y);
                        draw_input_line(w);
                        continue;
                    }
                }
                handle_mouse(&ev);
                /* hover 高亮：图标区（指针移动事件） */
                if (ev.type == EV_REL || ev.type == EV_ABS) {
                    int changed = 0;
                    for (int i = 0; i < ICON_N; i++) {
                        int ix = ICON_X0 + i * ICON_DX;
                        int on = (cur_x >= ix && cur_x < ix + ICON_W &&
                                  cur_y >= ICON_Y0 && cur_y < ICON_Y0 + ICON_H);
                        if (on != icon_focused[i]) {
                            icon_focused[i] = on;
                            changed = 1;
                        }
                    }
                    if (changed) {
                        gen_scene();
                        cursor_save(cur_x, cur_y);
                        cursor_draw(cur_x, cur_y);
                        draw_input_line(focused_win());
                    }
                }
            }
        }
    }
    tcsetattr(tty, TCSAFLUSH, &old);
    write(tty, "\033[?25h", 6);
    close(tty);
    for (int k = 0; k < mouse_n; k++) close(mouse_fds[k]);
}

static void usage(void)
{
    printf("奇点OS 桌面模块 (desktop) v1.0（转译桌面：窗口/图标/面板，evdev 鼠标 + tty0 键盘）\n");
    printf("用法: desktop help\n");
    printf("      desktop（无参数）→ 转译桌面会话（VNC 图形，需 /dev/fb0 + 渲染器.qd）\n");
}

int main(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "help") == 0) { usage(); return 0; }
    if (argc >= 2) { usage(); return 1; }
    session();
    /* 兜底：桌面会话作为 PID 1 不得退出（否则内核 panic） */
    for (;;) sleep(60);
    return 0;
}
