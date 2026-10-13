/*
 * render.c — 奇点OS 渲染器模块（渲染器.qd/bin/render）v2.1
 *
 * 主系统的渲染器 = 独立 .qd 模块（可替换）：
 *   - v1.0：文本渲染器（banner/card/list/status）
 *   - v1.1：+ tree（真实扫描目录树）+ desktop（主系统桌面，文本）
 *   - v2.0：+ fb 图形后端（自绘 /dev/fb0，点阵字形直接写像素）
 *           完整中文显示（绕过 fbcon 256/512 字形限制）
 *           命令：fbclear / fbdesktop / fbfile <文件> [标题] / fbbar <文本> / fbloc <x> <y> <文本>
 *   - v2.1：+ 桌面合成原语（转译 Xfce 架构给 桌面.qd 使用）：
 *           fbbg <色>           全屏背景
 *           fbrect <x> <y> <w> <h> <色>      填充矩形（像素坐标）
 *           fbtext <x> <y> <fg> <bg> <文本>  指定像素位置文本
 *           fbicon <x> <y> <聚焦> <标签>     桌面图标（边框/底色/方块/标签）
 *           fbwin <x> <y> <w> <h> <聚焦> <标题> <内容文件> [行偏移]   窗口
 *           fbpanel <y> <项...> 底部快捷栏
 *           fbscreen <场景文件>  一次绘制完整场景（每行一条原语，供桌面合成器调用）
 *   - 未来换 GPU 合成器时，只替换本模块即可（模块间只有调用关系）
 *
 * 命令接口（模块契约）：
 *   render help / banner <t> / card <t> [行...] / list <t> [项...]
 *   render status <k=v> [...] / tree [路径] [深度] / desktop
 *   render fbclear / fbdesktop / fbfile <文件> [标题] / fbbar <文本> / fbloc <x> <y> <文本>
 *   render fbbg <色> / fbrect <x> <y> <w> <h> <色> / fbtext <x> <y> <fg> <bg> <文本>
 *   render fbicon <x> <y> <聚焦> <标签> / fbwin <x> <y> <w> <h> <聚焦> <标题> <文件> [偏移]
 *   render fbpanel <y> <项...> / fbscreen <场景文件>
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
#define FONT_PATH "/usr/share/consolefonts/unifont-16.psf"
#define CMAP_CACHE "/tmp/unifont.cmap"

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
    printf("奇点OS 渲染器模块 (render) v2.1\n");
    printf("用法: render <模式> [内容]\n");
    printf("  文本组件: banner <标题> / card / list / status\n");
    printf("  tree [路径] [深度] / desktop           真实结构（文本）\n");
    printf("  fb 基础: fbclear / fbdesktop / fbfile <文件> [标题] / fbbar <文本> / fbloc <x> <y> <文本>\n");
    printf("  桌面合成: fbbg <色> / fbrect <x> <y> <w> <h> <色> / fbtext <x> <y> <fg> <bg> <文本>\n");
    printf("            fbicon <x> <y> <聚焦> <标签> / fbwin <x> <y> <w> <h> <聚焦> <标题> <文件> [偏移]\n");
    printf("            fbpanel <y> <项...> / fbscreen <场景文件>\n");
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

/* 加载 PSF2 字体（unifont-16.psf），mmap 直映 + cmap 磁盘缓存（含指纹校验） */
static int font_init(void)
{
    /* 先取当前字体文件大小作为缓存指纹（字体更换 → 指纹变化 → 缓存失效重建） */
    struct stat cur_fst;
    if (stat(FONT_PATH, &cur_fst) != 0 || cur_fst.st_size < 32) return -1;

    cmap = (unsigned short *)malloc(MAX_CMAP * sizeof(unsigned short));
    if (!cmap) return -1;
    int fd = open(CMAP_CACHE, O_RDONLY);
    if (fd >= 0) {
        /* 缓存格式：uint32 字体文件大小 | uint32 glyph 数 | 80KB cmap */
        uint32_t cached_size = 0, cached_cnt = 0;
        ssize_t rh = read(fd, &cached_size, 4);
        ssize_t rn = read(fd, &cached_cnt, 4);
        ssize_t rd = read(fd, cmap, MAX_CMAP * sizeof(unsigned short));
        close(fd);
        if (rh == 4 && rn == 4 && rd == (ssize_t)(MAX_CMAP * sizeof(unsigned short))
            && cached_size == (uint32_t)cur_fst.st_size) {
            /* 指纹匹配：glyph 段 mmap 字体文件 */
            int ffd = open(FONT_PATH, O_RDONLY);
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
        /* 指纹不匹配：继续走完整解析（重建缓存） */
    }
    /* 无缓存或指纹不匹配：完整解析 */
    FILE *f = fopen(FONT_PATH, "rb");
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
    /* 写 cmap 缓存（指纹头：字体文件大小 + glyph 数，供下次校验） */
    int wfd = open(CMAP_CACHE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (wfd >= 0) {
        uint32_t fsz = (uint32_t)cur_fst.st_size;
        uint32_t gcnt = (uint32_t)glyph_count;
        write(wfd, &fsz, 4);
        write(wfd, &gcnt, 4);
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
    if (gi >= (unsigned)glyph_count) {
        /* 索引越界（缓存/字体不匹配兜底）：画方块 */
        cp = 0xFFFD;
        if (cp >= MAX_CMAP || cmap[cp] == 0xFFFF) return;
        gi = cmap[cp];
        if (gi >= (unsigned)glyph_count) return;
    }
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

/* ---------------- v2.1 桌面合成原语 ---------------- */

/* 解析颜色: RRGGBB / 0xRRGGBB / 0xAARRGGBB → uint32 */
static uint32_t parse_color(const char *s)
{
    unsigned long v = strtoul(s, NULL, 16);
    return (uint32_t)v;
}

/* fbbg <色>：全屏背景 */
static void do_fbbg(const char *col)
{
    if (fb_init() != 0) { printf("fb 后端不可用\n"); return; }
    bg_pix = parse_color(col);
    fb_clear();
}

/* fbrect <x> <y> <w> <h> <色>：填充矩形（像素坐标） */
static void do_fbrect(int x, int y, int w, int h, const char *col)
{
    if (fb_init() != 0) { printf("fb 后端不可用\n"); return; }
    fb_rect(x, y, w, h, parse_color(col));
}

/* fbtext <x> <y> <fg> <bg> <文本...>：指定像素位置文本 */
static void do_fbtext(int x, int y, const char *fg, const char *bg, const char *s)
{
    if (fb_init() != 0 || font_init() != 0) { printf("fb 后端不可用\n"); return; }
    fb_text(x, y, s, parse_color(fg), parse_color(bg));
}

/* fbicon <x> <y> <聚焦> <标签...>：桌面图标（转译 xfdesktop 图标） */
static void do_fbicon(int x, int y, int focused, const char *label)
{
    if (fb_init() != 0 || font_init() != 0) { printf("fb 后端不可用\n"); return; }
    const int ICON_W = 72, ICON_H = 62;
    uint32_t border = focused ? 0x005A9BD5 : 0x004A4A4A;
    uint32_t fill   = focused ? 0x001E3A55 : 0x00141414;
    uint32_t square = focused ? 0x006FA8DC : 0x00555555;
    fb_rect(x, y, ICON_W, ICON_H, fill);
    fb_rect(x, y, ICON_W, 2, border);                       /* 上 */
    fb_rect(x, y + ICON_H - 2, ICON_W, 2, border);          /* 下 */
    fb_rect(x, y, 2, ICON_H, border);                       /* 左 */
    fb_rect(x + ICON_W - 2, y, 2, ICON_H, border);          /* 右 */
    /* 图标方块（28x28，居中偏上） */
    int sx = x + (ICON_W - 28) / 2;
    fb_rect(sx, y + 4, 28, 28, square);
    /* 标签：白字，居中，y+40 起 */
    int lw = fb_str_w(label);
    int tx = x + (ICON_W - lw) / 2;
    if (tx < x + 2) tx = x + 2;
    fb_text(tx, y + 40, label, 0x00FFFFFF, fill);
}

/* fbwin <x> <y> <w> <h> <聚焦> <标题> <内容文件> [行偏移]：窗口（转译 xfwm4 窗口） */
static void do_fbwin(int x, int y, int w, int h, int focused, const char *title,
                     const char *file, int offset)
{
    if (fb_init() != 0 || font_init() != 0) { printf("fb 后端不可用\n"); return; }
    if (w < 60 || h < 40) return;
    uint32_t border = focused ? 0x005A9BD5 : 0x004A4A4A;
    uint32_t fill   = 0x00242424;
    uint32_t bar    = focused ? 0x002E5B8A : 0x003A3A3A;
    uint32_t barfg  = focused ? 0x00FFFFFF : 0x00AAAAAA;
    /* 背景 + 边框 */
    fb_rect(x, y, w, h, fill);
    fb_rect(x, y, w, 2, border);
    fb_rect(x, y + h - 2, w, 2, border);
    fb_rect(x, y, 2, h, border);
    fb_rect(x + w - 2, y, 2, h, border);
    /* 标题栏（22px） */
    const int BAR_H = 22;
    fb_rect(x + 2, y + 2, w - 4, BAR_H, bar);
    int tw = fb_str_w(title);
    if (tw > w - 40) tw = w - 40;
    fb_text(x + 6, y + 2 + (BAR_H - GLYPH_H) / 2, title, barfg, bar);
    /* 关闭按钮 ×（标题栏右端 20px 宽） */
    fb_text(x + w - 20, y + 2 + (BAR_H - GLYPH_H) / 2, "×", 0x00E06060, bar);
    /* 内容区：从标题栏下 4px 起，逐行画 contentfile（最多 (h-26-BAR_H-6)/16 行） */
    int content_top = y + 2 + BAR_H + 4;
    int maxlines = (h - (content_top - y) - 6) / GLYPH_H;
    if (maxlines < 1) maxlines = 1;
    FILE *f = fopen(file, "r");
    if (!f) {
        fb_text(x + 6, content_top, "(内容不可用)", 0x00FF8080, fill);
        return;
    }
    char line[512];
    int ln = 0, drawn = 0;
    while (fgets(line, sizeof(line), f)) {
        if (ln < offset) { ln++; continue; }
        if (drawn >= maxlines) break;
        line[strcspn(line, "\r\n")] = 0;
        /* 截断超宽行 */
        if (fb_str_w(line) > w - 14) {
            char cut[256];
            int keep = 0;
            while (keep < (int)sizeof(cut) - 1) {
                unsigned char c = (unsigned char)line[keep];
                if (c < 0x80) {
                    if (fb_str_w(cut) + GLYPH_W > w - 14) break;
                    cut[keep] = line[keep]; keep++;
                } else {
                    if (fb_str_w(cut) + 2 * GLYPH_W > w - 14) break;
                    cut[keep] = line[keep];
                    if (keep + 1 < (int)sizeof(cut)) { cut[keep + 1] = line[keep + 1]; keep += 2; }
                    else break;
                }
            }
            cut[keep] = 0;
            fb_text(x + 6, content_top + drawn * GLYPH_H, cut, 0x00DDDDDD, fill);
        } else {
            fb_text(x + 6, content_top + drawn * GLYPH_H, line, 0x00DDDDDD, fill);
        }
        drawn++;
    }
    fclose(f);
    /* 输入行底色（底部）——桌面合成器在其上叠 fbtext 输入内容 */
    int iny = y + h - GLYPH_H - 4;
    fb_rect(x + 4, iny - 2, w - 8, GLYPH_H + 4, 0x00181818);
}

/* fbpanel <y> <项...>：底部快捷栏（转译 xfce4-panel） */
static void do_fbpanel(int y, int argc, char **argv)
{
    if (fb_init() != 0 || font_init() != 0) { printf("fb 后端不可用\n"); return; }
    const int PANEL_H = 28, BTN_W = 96;
    fb_rect(0, y, fb_xres, PANEL_H, 0x002A2A2A);
    fb_rect(0, y, fb_xres, 1, 0x00444444);
    for (int i = 0; i < argc; i++) {
        int bx = 6 + i * (BTN_W + 2);
        fb_rect(bx, y + 3, BTN_W, PANEL_H - 6, 0x00363636);
        const char *s = argv[i];
        int sw = fb_str_w(s);
        if (sw > BTN_W - 8) sw = BTN_W - 8;
        fb_text(bx + (BTN_W - sw) / 2, y + (PANEL_H - GLYPH_H) / 2, s, 0x00E8E8E8, 0x00363636);
        if (i < argc - 1)
            fb_rect(bx + BTN_W, y + 6, 1, PANEL_H - 12, 0x00444444);
    }
}

/* fbscreen <场景文件>：一次绘制完整场景。每行一条原语：
 *   bg <色>
 *   rect <x> <y> <w> <h> <色>
 *   text <x> <y> <fg> <bg> <文本...>
 *   icon <x> <y> <聚焦> <标签...>
 *   win <x> <y> <w> <h> <聚焦> <标题> <文件> [行偏移]
 *   panel <y> <项...>
 * 行内空白字符分隔；文本类原语取"命令名之后全部剩余"作为文本。
 */
static void do_fbscreen(const char *path)
{
    if (fb_init() != 0 || font_init() != 0) { printf("fb 后端不可用\n"); return; }
    FILE *f = fopen(path, "r");
    if (!f) { printf("场景文件不存在: %s\n", path); return; }
    char line[1024];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (line[0] == 0 || line[0] == '#') continue;
        char *save = NULL;
        char *cmd = strtok_r(line, " \t", &save);
        if (!cmd) continue;
        if (strcmp(cmd, "bg") == 0) {
            char *c = strtok_r(NULL, " \t", &save);
            if (c) { bg_pix = parse_color(c); fb_clear(); }
        } else if (strcmp(cmd, "rect") == 0) {
            char *xs = strtok_r(NULL, " \t", &save);
            char *ys = strtok_r(NULL, " \t", &save);
            char *ws = strtok_r(NULL, " \t", &save);
            char *hs = strtok_r(NULL, " \t", &save);
            char *cs = strtok_r(NULL, " \t", &save);
            if (xs && ys && ws && hs && cs)
                fb_rect(atoi(xs), atoi(ys), atoi(ws), atoi(hs), parse_color(cs));
        } else if (strcmp(cmd, "text") == 0) {
            char *xs = strtok_r(NULL, " \t", &save);
            char *ys = strtok_r(NULL, " \t", &save);
            char *fs = strtok_r(NULL, " \t", &save);
            char *bs = strtok_r(NULL, " \t", &save);
            char *rest = strtok_r(NULL, "", &save);
            if (xs && ys && fs && bs && rest) {
                while (*rest == ' ' || *rest == '\t') rest++;
                fb_text(atoi(xs), atoi(ys), rest, parse_color(fs), parse_color(bs));
            }
        } else if (strcmp(cmd, "icon") == 0) {
            char *xs = strtok_r(NULL, " \t", &save);
            char *ys = strtok_r(NULL, " \t", &save);
            char *fs = strtok_r(NULL, " \t", &save);
            char *rest = strtok_r(NULL, "", &save);
            if (xs && ys && fs && rest) {
                while (*rest == ' ' || *rest == '\t') rest++;
                do_fbicon(atoi(xs), atoi(ys), atoi(fs) ? 1 : 0, rest);
            }
        } else if (strcmp(cmd, "win") == 0) {
            char *xs = strtok_r(NULL, " \t", &save);
            char *ys = strtok_r(NULL, " \t", &save);
            char *ws = strtok_r(NULL, " \t", &save);
            char *hs = strtok_r(NULL, " \t", &save);
            char *fs = strtok_r(NULL, " \t", &save);
            char *ts = strtok_r(NULL, " \t", &save);
            char *fl = strtok_r(NULL, " \t", &save);
            char *os = strtok_r(NULL, " \t", &save);
            if (xs && ys && ws && hs && fs && ts && fl)
                do_fbwin(atoi(xs), atoi(ys), atoi(ws), atoi(hs), atoi(fs) ? 1 : 0,
                         ts, fl, os ? atoi(os) : 0);
        } else if (strcmp(cmd, "panel") == 0) {
            char *ys = strtok_r(NULL, " \t", &save);
            if (!ys) continue;
            char *items[16]; int n = 0;
            char *it;
            while ((it = strtok_r(NULL, " \t", &save)) != NULL && n < 16)
                items[n++] = it;
            do_fbpanel(atoi(ys), n, items);
        }
    }
    fclose(f);
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
    fb_title_bar("奇点OS 主系统（主框架）", "render v2.1 fb 自绘");
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
    if (title) fb_title_bar(title, "render v2.1");
    else fb_title_bar("奇点OS 主系统", "render v2.1");
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
        if (fb_init() != 0 || font_init() != 0) { printf("fb 后端不可用\n"); return 1; }
        fb_bar(argv[2]);
        return 0;
    }
    if (strcmp(m, "fbloc") == 0 && argc >= 5) {
        do_fb_loc(atoi(argv[2]), atoi(argv[3]), argv[4]);
        return 0;
    }

    /* ---- v2.1 桌面合成原语 ---- */
    if (strcmp(m, "fbbg") == 0 && argc >= 3) { do_fbbg(argv[2]); return 0; }
    if (strcmp(m, "fbrect") == 0 && argc >= 7) {
        do_fbrect(atoi(argv[2]), atoi(argv[3]), atoi(argv[4]), atoi(argv[5]), argv[6]);
        return 0;
    }
    if (strcmp(m, "fbtext") == 0 && argc >= 6) {
        do_fbtext(atoi(argv[2]), atoi(argv[3]), argv[4], argv[5], argv[6]);
        return 0;
    }
    if (strcmp(m, "fbicon") == 0 && argc >= 5) {
        do_fbicon(atoi(argv[2]), atoi(argv[3]), atoi(argv[4]) ? 1 : 0, argv[5]);
        return 0;
    }
    if (strcmp(m, "fbwin") == 0 && argc >= 9) {
        do_fbwin(atoi(argv[2]), atoi(argv[3]), atoi(argv[4]), atoi(argv[5]),
                 atoi(argv[6]) ? 1 : 0, argv[7], argv[8], argc >= 10 ? atoi(argv[9]) : 0);
        return 0;
    }
    if (strcmp(m, "fbpanel") == 0 && argc >= 4) {
        do_fbpanel(atoi(argv[2]), argc - 3, &argv[3]);
        return 0;
    }
    if (strcmp(m, "fbscreen") == 0 && argc >= 3) {
        do_fbscreen(argv[2]);
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
        center("renderer v2.1 · 真实显示系统结构");
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
