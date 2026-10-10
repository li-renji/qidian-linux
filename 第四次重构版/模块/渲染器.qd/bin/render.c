/*
 * render.c — 奇点OS 渲染器模块（渲染器.qd/bin/render）v2.0
 *
 * 主系统的渲染器 = 独立 .qd 模块（可替换）：
 *   - v1.0：文本渲染器（banner/card/list/status）
 *   - v1.1：+ tree（真实扫描目录树）+ desktop（主系统桌面，文本）
 *   - v2.0：+ fb 图形后端（自绘 /dev/fb0，点阵字形直接写像素）
 *           完整中文显示（绕过 fbcon 256/512 字形限制）
 *           命令：fbclear / fbdesktop / fbfile <文件> [标题] / fbbar <文本> / fbloc <x> <y> <文本>
 *   - 未来换 GPU 合成器时，只替换本模块即可（模块间只有调用关系）
 *
 * 命令接口（模块契约）：
 *   render help / banner <t> / card <t> [行...] / list <t> [项...]
 *   render status <k=v> [...] / tree [路径] [深度] / desktop
 *   render fbclear / fbdesktop / fbfile <文件> [标题] / fbbar <文本> / fbloc <x> <y> <文本>
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <linux/fb.h>
#include <stdint.h>

#define W 60
#define GLYPH_H 16
#define GLYPH_W 16
#define GLYPH_BYTES 32          /* 16x16 1bpp */
#define MAX_CMAP 0xA000         /* 码点范围 0x20-0x9FFF */

/* ---------------- 文本后端（v1.1 保留，串口/无 fb 环境可用） ---------------- */
static void rule(char c)
{
    for (int i = 0; i < W; i++) putchar(c);
    putchar('\n');
}

static void center(const char *s)
{
    int n = (int)strlen(s);
    int pad = (W - n) / 2;
    if (pad < 0) pad = 0;
    for (int i = 0; i < pad; i++) putchar(' ');
    puts(s);
}

static void usage(void)
{
    printf("奇点OS 渲染器模块 (render) v2.0\n");
    printf("用法: render <模式> [内容]\n");
    printf("  banner <标题> / card / list / status   文本组件\n");
    printf("  tree [路径] [深度] / desktop           真实结构（文本）\n");
    printf("  fbclear                                清屏（fb 图形后端）\n");
    printf("  fbdesktop                              主系统桌面（fb 自绘，中文完整显示）\n");
    printf("  fbfile <文件> [标题]                   绘制文本文件内容\n");
    printf("  fbbar <文本>                           底部状态/输入条\n");
    printf("  fbloc <x> <y> <文本>                   指定位置绘制一行\n");
    printf("渲染后端: 文本(VNC/串口) + fb自绘(需要 /dev/fb0)\n");
}

/* ---------------- fb 图形后端 ---------------- */
static int fb_fd = -1;
static uint8_t *fb_mem = NULL;
static long fb_line = 0;         /* 每行字节数 */
static unsigned fb_xres = 0, fb_yres = 0;
static unsigned fb_bpp = 32;
static uint32_t fg_pix = 0xFFFFFFFF, bg_pix = 0;

/* 字体：PSF2 解析 */
static uint8_t *glyphs = NULL;   /* 字形数据段 */
static int glyph_count = 0;
static unsigned short *cmap = NULL; /* 码点 → 字形 index */

static int fb_init(void)
{
    fb_fd = open("/dev/fb0", O_RDWR);
    if (fb_fd < 0) return -1;
    struct fb_var_screeninfo vi;
    if (ioctl(fb_fd, FBIOGET_VSCREENINFO, &vi) != 0) { close(fb_fd); fb_fd = -1; return -1; }
    struct fb_fix_screeninfo fi;
    if (ioctl(fb_fd, FBIOGET_FSCREENINFO, &fi) != 0) { close(fb_fd); fb_fd = -1; return -1; }
    fb_xres = vi.xres; fb_yres = vi.yres;
    fb_bpp = vi.bits_per_pixel;
    fb_line = fi.line_length;
    size_t sz = (size_t)fi.smem_len;
    fb_mem = (uint8_t *)mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_SHARED, fb_fd, 0);
    if (fb_mem == MAP_FAILED) { close(fb_fd); fb_fd = -1; fb_mem = NULL; return -1; }
    return 0;
}

/* 加载 PSF2 字体（/usr/share/consolefonts/unifont-16.psf），mmap 直映 + cmap 磁盘缓存 */
static int font_init(void)
{
    /* cmap 缓存：/tmp/unifont.cmap（80KB，字体固定则内容固定） */
    cmap = (unsigned short *)malloc(MAX_CMAP * sizeof(unsigned short));
    if (!cmap) return -1;
    int fd = open("/tmp/unifont.cmap", O_RDONLY);
    if (fd >= 0) {
        ssize_t rd = read(fd, cmap, MAX_CMAP * sizeof(unsigned short));
        close(fd);
        if (rd == (ssize_t)(MAX_CMAP * sizeof(unsigned short))) {
            /* glyph 段 mmap 字体文件 */
            int ffd = open("/usr/share/consolefonts/unifont-16.psf", O_RDONLY);
            if (ffd < 0) { free(cmap); cmap = NULL; return -1; }
            struct stat fst;
            if (fstat(ffd, &fst) != 0 || fst.st_size < 32) { close(ffd); free(cmap); cmap = NULL; return -1; }
            uint8_t hdr[32];
            if (read(ffd, hdr, 32) != 32) { close(ffd); free(cmap); cmap = NULL; return -1; }
            unsigned int hdrsize, cnt;
            memcpy(&hdrsize, hdr + 8, 4);
            memcpy(&cnt, hdr + 16, 4);
            glyph_count = (int)cnt;
            glyphs = (uint8_t *)mmap(NULL, fst.st_size, PROT_READ, MAP_PRIVATE, ffd, 0);
            close(ffd);
            if (glyphs == MAP_FAILED) { free(cmap); cmap = NULL; return -1; }
            glyphs += hdrsize;
            return 0;
        }
    }
    /* 无缓存：完整解析 */
    FILE *f = fopen("/usr/share/consolefonts/unifont-16.psf", "rb");
    if (!f) { free(cmap); cmap = NULL; return -1; }
    uint8_t hdr[32];
    if (fread(hdr, 1, 32, f) != 32) { fclose(f); free(cmap); cmap = NULL; return -1; }
    if (hdr[0] != 0x72 || hdr[1] != 0xB5 || hdr[2] != 0x4A || hdr[3] != 0x86) { fclose(f); free(cmap); cmap = NULL; return -1; }
    unsigned int hdrsize, flags, cnt, charsize, height, width;
    memcpy(&hdrsize, hdr + 8, 4);
    memcpy(&flags, hdr + 12, 4);
    memcpy(&cnt, hdr + 16, 4);
    memcpy(&charsize, hdr + 20, 4);
    memcpy(&height, hdr + 24, 4);
    memcpy(&width, hdr + 28, 4);
    if (height != GLYPH_H || width != GLYPH_W || charsize != GLYPH_BYTES) { fclose(f); free(cmap); cmap = NULL; return -1; }
    glyph_count = (int)cnt;
    glyphs = (uint8_t *)malloc((size_t)cnt * GLYPH_BYTES);
    if (!glyphs) { fclose(f); free(cmap); cmap = NULL; return -1; }
    fseek(f, hdrsize, SEEK_SET);
    if (fread(glyphs, GLYPH_BYTES, cnt, f) != cnt) { fclose(f); free(glyphs); glyphs = NULL; free(cmap); cmap = NULL; return -1; }
    /* unicode 表：每字形一个 2B 码点 + 0xFFFF 终止；表尾 0xFFFF 0xFFFF */
    for (unsigned i = 0; i < MAX_CMAP; i++) cmap[i] = 0xFFFF;
    if (flags & 1) {
        long pos = (long)hdrsize + (long)cnt * GLYPH_BYTES;
        fseek(f, pos, SEEK_SET);
        unsigned idx = 0;
        int got;
        for (;;) {
            unsigned short cp;
            got = (int)fread(&cp, 2, 1, f);
            if (got != 1) break;
            if (cp == 0xFFFF) {
                /* 表尾检测：连续两个 0xFFFF；否则为字形分隔 */
                long save = ftell(f);
                unsigned short cp2;
                if (fread(&cp2, 2, 1, f) == 1 && cp2 == 0xFFFF) break;
                fseek(f, save, SEEK_SET);   /* 回退：cp2 是下一字形码点，重新读 */
                idx++;
                continue;
            }
            if (cp < MAX_CMAP && idx < (unsigned)glyph_count)
                cmap[cp] = (unsigned short)idx;
        }
    }
    fclose(f);
    /* 写 cmap 缓存 */
    int wfd = open("/tmp/unifont.cmap", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (wfd >= 0) {
        write(wfd, cmap, MAX_CMAP * sizeof(unsigned short));
        close(wfd);
    }
    return 0;
}

/* 画一个字符（1bpp 位图 blit 到 fb 像素） */
static void fb_char(int x, int y, unsigned cp, uint32_t fg, uint32_t bg)
{
    if (cp >= MAX_CMAP || cmap[cp] == 0xFFFF) {
        /* 缺字形：画方块 */
        cp = 0xFFFD;
        if (cp >= MAX_CMAP || cmap[cp] == 0xFFFF) return;
    }
    unsigned gi = cmap[cp];
    const uint8_t *g = glyphs + (size_t)gi * GLYPH_BYTES;
    for (int row = 0; row < GLYPH_H; row++) {
        int yy = y + row;
        if (yy < 0 || yy >= (int)fb_yres) continue;
        uint8_t b0 = g[row * 2], b1 = g[row * 2 + 1];
        uint32_t *linep = (uint32_t *)(fb_mem + (size_t)yy * fb_line);
        for (int c = 0; c < GLYPH_W; c++) {
            int xx = x + c;
            if (xx < 0 || xx >= (int)fb_xres) continue;
            uint8_t bit = (c < 8) ? ((b0 >> (7 - c)) & 1) : ((b1 >> (15 - c)) & 1);
            linep[xx] = bit ? fg : bg;
        }
    }
}

/* 画字符串（从 (x,y) 开始，全角/汉字占 2 格，按 utf-8 解析） */
static void fb_text(int x, int y, const char *s, uint32_t fg, uint32_t bg)
{
    int cx = x;
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        unsigned cp;
        if (p[0] < 0x80) { cp = p[0]; p += 1; }
        else if ((p[0] & 0xE0) == 0xC0) { cp = ((p[0] & 0x1F) << 6) | (p[1] & 0x3F); p += 2; }
        else if ((p[0] & 0xF0) == 0xE0) { cp = ((p[0] & 0x0F) << 12) | ((p[1] & 0x3F) << 6) | (p[2] & 0x3F); p += 3; }
        else if ((p[0] & 0xF8) == 0xF0) { cp = ((p[0] & 0x07) << 18) | ((p[1] & 0x3F) << 12) | ((p[2] & 0x3F) << 6) | (p[3] & 0x3F); p += 4; }
        else { cp = '?'; p += 1; }
        fb_char(cx, y, cp, fg, bg);
        cx += GLYPH_W;
    }
}

static void fb_rect(int x, int y, int w, int h, uint32_t color)
{
    for (int r = 0; r < h; r++) {
        int yy = y + r;
        if (yy < 0 || yy >= (int)fb_yres) continue;
        uint32_t *linep = (uint32_t *)(fb_mem + (size_t)yy * fb_line);
        for (int c = 0; c < w; c++) {
            int xx = x + c;
            if (xx < 0 || xx >= (int)fb_xres) continue;
            linep[xx] = color;
        }
    }
}

static void fb_clear(void)
{
    if (!fb_mem) return;
    for (unsigned y = 0; y < fb_yres; y++) {
        uint32_t *linep = (uint32_t *)(fb_mem + (size_t)y * fb_line);
        for (unsigned x = 0; x < fb_xres; x++) linep[x] = bg_pix;
    }
}

/* 计算字符串的像素宽度（utf-8 逐码点） */
static int fb_str_w(const char *s)
{
    int n = 0;
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        int step;
        if (p[0] < 0x80) step = 1;
        else if ((p[0] & 0xE0) == 0xC0) step = 2;
        else if ((p[0] & 0xF0) == 0xE0) step = 3;
        else if ((p[0] & 0xF8) == 0xF0) step = 4;
        else step = 1;
        p += step; n += GLYPH_W;
    }
    return n;
}

/* 顶部标题栏 */
static void fb_title_bar(const char *left, const char *right)
{
    fb_rect(0, 0, fb_xres, GLYPH_H + 4, 0x00A8A8A8);   /* 灰色标题栏 */
    int hc = (GLYPH_H + 4 - GLYPH_H) / 2;
    fb_text(8, hc, left, 0x00000000, 0x00A8A8A8);
    int rw = fb_str_w(right);
    fb_text(fb_xres - rw - 8, hc, right, 0x00000000, 0x00A8A8A8);
    /* 标题栏下分隔线 */
    fb_rect(0, GLYPH_H + 4, fb_xres, 2, 0x00AAAAAA);
}

/* 底部输入条 */
static void fb_bar(const char *s)
{
    int bar_top = fb_yres - GLYPH_H - 4;
    fb_rect(0, bar_top, fb_xres, GLYPH_H + 4, 0x00A8A8A8);
    int hc = (GLYPH_H + 4 - GLYPH_H) / 2;
    fb_text(8, bar_top + hc, s, 0x00000000, 0x00A8A8A8);
}

/* ---------------- 结构树（真实扫描） ---------------- */
static void tree_rec(FILE *out, const char *path, int depth, int maxd, const char *prefix)
{
    if (depth > maxd) return;
    DIR *d = opendir(path);
    if (!d) return;
    struct dirent *e;
    struct stat st;
    char names[512][256];
    int isdir[512], n = 0;
    while ((e = readdir(d)) != NULL && n < 512) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
        char p[1024];
        snprintf(p, sizeof(p), "%s/%s", path, e->d_name);
        snprintf(names[n], sizeof(names[n]), "%s", e->d_name);
        isdir[n] = (stat(p, &st) == 0 && S_ISDIR(st.st_mode)) ? 1 : 0;
        n++;
    }
    closedir(d);
    for (int i = 0; i < n; i++)
        for (int j = i + 1; j < n; j++) {
            int swap = 0;
            if (isdir[j] && !isdir[i]) swap = 1;
            else if (isdir[j] == isdir[i] && strcmp(names[j], names[i]) < 0) swap = 1;
            if (swap) {
                char tn[256]; int ti;
                snprintf(tn, sizeof(tn), "%s", names[i]); ti = isdir[i];
                snprintf(names[i], sizeof(names[i]), "%s", names[j]); isdir[i] = isdir[j];
                snprintf(names[j], sizeof(names[j]), "%s", tn); isdir[j] = ti;
            }
        }
    for (int i = 0; i < n; i++) {
        int last = (i == n - 1);
        fprintf(out, "%s%s%s\n", prefix, last ? "└─ " : "├─ ", names[i]);
        if (isdir[i]) {
            char np[1024], nprefix[1024];
            snprintf(np, sizeof(np), "%s/%s", path, names[i]);
            snprintf(nprefix, sizeof(nprefix), "%s%s", prefix, last ? "   " : "│  ");
            tree_rec(out, np, depth + 1, maxd, nprefix);
        }
    }
}

static void mod_state(const char *m, char *out, size_t sz)
{
    char p[1024];
    snprintf(p, sizeof(p), "/modules/%s/.state", m);
    FILE *f = fopen(p, "r");
    if (!f) { snprintf(out, sz, "未启动"); return; }
    char line[128];
    out[0] = 0;
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strncmp(line, "value=", 6) == 0) snprintf(out, sz, "%s", line + 6);
    }
    fclose(f);
    if (!out[0]) snprintf(out, sz, "未启动");
}

/* 收集桌面内容到文件（文本渲染 + fb 共用） */
static void build_desktop(FILE *out)
{
    fprintf(out, "┌─ 系统结构（真实扫描 / 顶层目录 + 系统模块）%s\n", "─┐");
    tree_rec(out, "/", 0, 0, "");
    fprintf(out, "\n┌─ 系统模块（/modules）%s\n", "─┐");
    DIR *d = opendir("/modules");
    if (d) {
        struct dirent *e;
        int shown = 0;
        while ((e = readdir(d)) != NULL) {
            if (strstr(e->d_name, ".qd") == NULL) continue;
            char p[1024], line[256];
            snprintf(p, sizeof(p), "/modules/%s/.qds", e->d_name);
            char name[128] = "?", ver[64] = "?", desc[256] = "";
            FILE *f = fopen(p, "r");
            if (f) {
                while (fgets(line, sizeof(line), f)) {
                    line[strcspn(line, "\r\n")] = 0;
                    if (strncmp(line, "name=", 5) == 0) snprintf(name, sizeof(name), "%s", line + 5);
                    if (strncmp(line, "version=", 8) == 0) snprintf(ver, sizeof(ver), "%s", line + 8);
                    if (strncmp(line, "desc=", 5) == 0) snprintf(desc, sizeof(desc), "%s", line + 5);
                }
                fclose(f);
            }
            char st[64];
            mod_state(e->d_name, st, sizeof(st));
            fprintf(out, "  %s  v%s  [%s]  %s\n", name, ver, st, desc);
            shown = 1;
        }
        closedir(d);
        if (!shown) fprintf(out, "  （无系统模块）\n");
    }
    long mem_total = 0, mem_free = 0;
    FILE *mi = fopen("/proc/meminfo", "r");
    if (mi) {
        char k[64]; long v; char u[16];
        while (fscanf(mi, "%63s %ld %15s", k, &v, u) == 3) {
            if (strcmp(k, "MemTotal:") == 0) mem_total = v;
            if (strcmp(k, "MemFree:") == 0) mem_free = v;
            if (mem_total && mem_free) break;
        }
        fclose(mi);
    }
    fprintf(out, "  内存: %ld MB 总量 / %ld MB 空闲\n", mem_total / 1024, mem_free / 1024);
}

/* fb 桌面：标题栏 + 内容区 + 底部提示条 */
static void do_fb_desktop(void)
{
    if (fb_init() != 0 || font_init() != 0) {
        printf("fb 后端不可用（无 /dev/fb0 或字体缺失）\n");
        return;
    }
    fb_clear();
    fb_title_bar("奇点OS 主系统（主框架）", "render v2.0 fb 自绘");
    /* 内容区写临时文件后整页绘制 */
    char tmp[] = "/tmp/render_desk.XXXXXX";
    int fd = mkstemp(tmp);
    if (fd >= 0) {
        FILE *f = fdopen(fd, "w");
        build_desktop(f);
        fclose(f);
        /* 逐行绘制（每行 16px，从 y=24 开始） */
        FILE *rf = fopen(tmp, "r");
        if (rf) {
            char line[512];
            int y = 24;
            while (fgets(line, sizeof(line), rf) && y + GLYPH_H < (int)(fb_yres - GLYPH_H - 8)) {
                line[strcspn(line, "\r\n")] = 0;
                fb_text(8, y, line, 0xFFFFFFFF, 0x00000000);
                y += GLYPH_H;
            }
            fclose(rf);
        }
        unlink(tmp);
    }
    fb_bar(" 输入 help 查看命令 · 输入 退出 返回控制台 · AI 对话: ai <话>");
}

/* fb 文件绘制：整页多行文本 */
static void do_fb_file(const char *path, const char *title)
{
    if (fb_init() != 0 || font_init() != 0) {
        printf("fb 后端不可用（无 /dev/fb0 或字体缺失）\n");
        return;
    }
    fb_clear();
    if (title) fb_title_bar(title, "render v2.0");
    else fb_title_bar("奇点OS 主系统", "render v2.0");
    FILE *f = fopen(path, "r");
    if (!f) {
        fb_text(8, 24, "(文件不存在)", 0xFFFFFFFF, 0x00000000);
        fb_bar(" 输入 help 查看命令");
        return;
    }
    char line[512];
    int y = 24;
    while (fgets(line, sizeof(line), f) && y + GLYPH_H < (int)(fb_yres - GLYPH_H - 8)) {
        line[strcspn(line, "\r\n")] = 0;
        fb_text(8, y, line, 0xFFFFFFFF, 0x00000000);
        y += GLYPH_H;
    }
    fclose(f);
    fb_bar(" 输入 help 查看命令");
}

/* fb 单行定位绘制 */
static void do_fb_loc(int x, int y, const char *s)
{
    if (fb_init() != 0 || font_init() != 0) { printf("fb 后端不可用\n"); return; }
    fb_text(x * GLYPH_W, y * GLYPH_H, s, 0xFFFFFFFF, 0x00000000);
}

/* ---------------- main ---------------- */
int main(int argc, char **argv)
{
    if (argc < 2) { usage(); return 0; }
    const char *m = argv[1];

    if (strcmp(m, "help") == 0) { usage(); return 0; }

    if (strcmp(m, "fbclear") == 0) {
        if (fb_init() != 0) { printf("fb 后端不可用\n"); return 1; }
        fb_clear();
        return 0;
    }
    if (strcmp(m, "fbdesktop") == 0) { do_fb_desktop(); return 0; }
    if (strcmp(m, "fbfile") == 0 && argc >= 3) {
        do_fb_file(argv[2], argc >= 4 ? argv[3] : NULL);
        return 0;
    }
    if (strcmp(m, "fbbar") == 0 && argc >= 3) {
        if (fb_init() != 0) { printf("fb 后端不可用\n"); return 1; }
        fb_bar(argv[2]);
        return 0;
    }
    if (strcmp(m, "fbloc") == 0 && argc >= 5) {
        do_fb_loc(atoi(argv[2]), atoi(argv[3]), argv[4]);
        return 0;
    }

    if (strcmp(m, "banner") == 0 && argc >= 3) {
        rule('=');
        center(argv[2]);
        rule('=');
        return 0;
    }
    if (strcmp(m, "card") == 0 && argc >= 3) {
        rule('-');
        center(argv[2]);
        rule('-');
        for (int i = 3; i < argc; i++)
            printf("  %s\n", argv[i]);
        rule('-');
        return 0;
    }
    if (strcmp(m, "list") == 0 && argc >= 3) {
        rule('-');
        printf("  %s\n", argv[2]);
        rule('-');
        for (int i = 3; i < argc; i++)
            printf("  · %s\n", argv[i]);
        rule('-');
        return 0;
    }
    if (strcmp(m, "status") == 0) {
        rule('=');
        center("系统状态");
        rule('=');
        for (int i = 2; i < argc; i++) {
            char *eq = strchr(argv[i], '=');
            if (eq) {
                *eq = 0;
                printf("  %-20s %s\n", argv[i], eq + 1);
            } else {
                printf("  %s\n", argv[i]);
            }
        }
        rule('=');
        return 0;
    }
    if (strcmp(m, "tree") == 0) {
        const char *path = argc >= 3 ? argv[2] : "/";
        int maxd = argc >= 4 ? atoi(argv[3]) : 3;
        if (maxd < 1) maxd = 1;
        if (maxd > 6) maxd = 6;
        if (!path || path[0] == 0) path = "/";
        printf("系统结构（真实扫描 %s，深度≤%d）\n", path, maxd);
        tree_rec(stdout, path, 0, maxd, "");
        printf("\n");
        return 0;
    }
    if (strcmp(m, "desktop") == 0) {
        printf("\033[2J\033[H");
        rule('=');
        center("奇点OS 主系统（主框架）");
        center("renderer v2.0 · 真实显示系统结构");
        rule('=');
        printf("\n");
        build_desktop(stdout);
        printf("\n操作：用户界面由 用户态.qd 提供，输入 help 查看命令\n");
        rule('=');
        return 0;
    }

    usage();
    return 1;
}
