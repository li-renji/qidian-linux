/*
 * pm.c — 奇点OS 包管理模块（包管理.qd/bin/pm）v1.0
 *
 * 主系统的包管理 = 独立 .qd 模块。module-engine 通过
 *   run 包管理 / pm <子命令>
 * 调用本程序。任何实现相同命令接口的模块都可以整体替换本模块。
 *
 * 命令接口（模块契约）：
 *   pm help                      帮助
 *   pm list                      已安装模块
 *   pm info <模块>               模块详情（读 .qds 契约）
 *   pm install <模块> [--repo R] 从仓库实体安装到 /modules（校验 .qds type=guide）
 *   pm remove <模块>             卸载
 *   pm repo list                 仓库实体列表
 *   pm repo add <仓库.qds>       登记仓库索引（复制到 /仓库/<名>.qds）
 *
 * 安装原则（文档 7A.1）：安装 = 受控地往 /modules 放 .qd，热发现由引擎扫描承担。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>
#include <sys/stat.h>
#include <errno.h>

#define MODULES_DIR "/modules"
#define REPO_DIR    "/仓库/实体"
#define REPO_IDX    "/仓库"
#define BUF         1024

static void usage(void)
{
    printf("奇点OS 包管理模块 (pm) v1.0\n");
    printf("用法: pm <子命令>\n");
    printf("  help                    本帮助\n");
    printf("  list                    已安装模块\n");
    printf("  info <模块>             模块详情（读 .qds 契约）\n");
    printf("  install <模块> [--repo <仓库>]   从仓库实体安装\n");
    printf("  remove <模块>           卸载\n");
    printf("  repo list               仓库实体列表\n");
    printf("  repo add <仓库.qds>     登记仓库索引\n");
}

/* 纯 C 递归复制（不依赖外部命令） */
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
    return 0;
}

/* 校验模块包：目录内必须有 .qds 且首行 type=guide（文档 2.2 契约） */
static int validate_module(const char *dirpath, char *name, size_t nsz,
                           char *entry, size_t esz, char *version, size_t vsz)
{
    char p[BUF];
    snprintf(p, sizeof(p), "%s/.qds", dirpath);
    FILE *f = fopen(p, "r");
    if (!f) return -1;
    char first[128];
    if (!fgets(first, sizeof(first), f)) { fclose(f); return -1; }
    first[strcspn(first, "\r\n")] = 0;
    if (strncmp(first, "type=guide", 10) != 0) { fclose(f); return -1; }
    char line[512];
    name[0] = 0; entry[0] = 0; version[0] = 0;
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (strncmp(line, "name=", 5) == 0) snprintf(name, nsz, "%s", line + 5);
        else if (strncmp(line, "entry=", 6) == 0) snprintf(entry, esz, "%s", line + 6);
        else if (strncmp(line, "version=", 8) == 0) snprintf(version, vsz, "%s", line + 8);
    }
    fclose(f);
    return 0;
}

static int cmd_list(void)
{
    DIR *d = opendir(MODULES_DIR);
    if (!d) { printf("模块目录不可用: %s\n", MODULES_DIR); return 1; }
    struct dirent *e;
    int n = 0;
    printf("已安装模块（/modules）:\n");
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
        char p[BUF], name[128], entry[256], ver[64];
        snprintf(p, sizeof(p), "%s/%s", MODULES_DIR, e->d_name);
        struct stat st;
        if (stat(p, &st) != 0) continue;
        if (S_ISDIR(st.st_mode) && validate_module(p, name, sizeof(name),
                                                   entry, sizeof(entry), ver, sizeof(ver)) == 0)
            printf("  ■ %-30s v%s\n", name[0] ? name : e->d_name, ver[0] ? ver : "?");
        else
            printf("  · %-30s %lu 字节\n", e->d_name, (unsigned long)st.st_size);
        n++;
    }
    closedir(d);
    printf("共 %d 项\n", n);
    return 0;
}

static int cmd_info(const char *name)
{
    char p[BUF];
    if (strstr(name, ".qd"))
        snprintf(p, sizeof(p), "%s/%s", MODULES_DIR, name);
    else
        snprintf(p, sizeof(p), "%s/%s.qd", MODULES_DIR, name);
    struct stat st;
    if (stat(p, &st) != 0) { printf("未安装: %s\n", name); return 1; }
    char mname[128], entry[256], ver[64];
    if (S_ISDIR(st.st_mode) &&
        validate_module(p, mname, sizeof(mname), entry, sizeof(entry), ver, sizeof(ver)) == 0) {
        printf("模块: %s\n", mname[0] ? mname : name);
        printf("版本: %s\n", ver[0] ? ver : "?");
        printf("入口: %s\n", entry[0] ? entry : "（无）");
        printf("位置: %s\n", p);
    } else {
        printf("模块: %s\n位置: %s（无 .qds 契约）\n", name, p);
    }
    return 0;
}

static int cmd_install(const char *name)
{
    char src[BUF], dst[BUF];
    if (strstr(name, ".qd"))
        snprintf(src, sizeof(src), "%s/%s", REPO_DIR, name);
    else
        snprintf(src, sizeof(src), "%s/%s.qd", REPO_DIR, name);
    struct stat st;
    if (stat(src, &st) != 0) { printf("仓库中无此模块: %s（%s）\n", name, REPO_DIR); return 1; }
    if (!S_ISDIR(st.st_mode)) { printf("仓库实体必须是 .qd 模块目录: %s\n", name); return 1; }
    /* 契约校验（type=guide） */
    char mname[128], entry[256], ver[64];
    if (validate_module(src, mname, sizeof(mname), entry, sizeof(entry), ver, sizeof(ver)) != 0) {
        printf("✗ 模块缺少合法 .qds 契约（需首行 type=guide）: %s\n", name);
        return 1;
    }
    printf("校验通过: %s v%s（入口 %s）\n", mname[0] ? mname : name,
           ver[0] ? ver : "?", entry[0] ? entry : "无");
    mkdir(MODULES_DIR, 0755);
    if (strstr(name, ".qd"))
        snprintf(dst, sizeof(dst), "%s/%s", MODULES_DIR, name);
    else
        snprintf(dst, sizeof(dst), "%s/%s.qd", MODULES_DIR, name);
    if (copy_rec(src, dst) != 0) { printf("✗ 安装失败\n"); return 1; }
    printf("✓ 已安装 %s → %s\n", mname[0] ? mname : name, MODULES_DIR);
    return 0;
}

static int cmd_remove(const char *name)
{
    char path[BUF];
    if (strstr(name, ".qd"))
        snprintf(path, sizeof(path), "%s/%s", MODULES_DIR, name);
    else
        snprintf(path, sizeof(path), "%s/%s.qd", MODULES_DIR, name);
    struct stat st;
    if (stat(path, &st) != 0) { printf("未安装: %s\n", name); return 1; }
    if (S_ISDIR(st.st_mode)) {
        /* 递归删除 */
        DIR *d = opendir(path);
        if (d) {
            struct dirent *e;
            while ((e = readdir(d)) != NULL) {
                if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
                char c[BUF];
                snprintf(c, sizeof(c), "%s/%s", path, e->d_name);
                remove(c);
            }
            closedir(d);
        }
    }
    if (remove(path) != 0) { printf("✗ 移除失败: %s\n", path); return 1; }
    printf("✓ 已卸载 %s\n", name);
    return 0;
}

static int cmd_repo_list(void)
{
    DIR *d = opendir(REPO_DIR);
    if (!d) { printf("仓库实体目录不可用（%s）\n", REPO_DIR); return 1; }
    struct dirent *e;
    int n = 0;
    printf("仓库实体（/仓库/实体/）:\n");
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
        printf("  %s\n", e->d_name);
        n++;
    }
    closedir(d);
    if (n == 0) printf("  （空）\n");
    return 0;
}

static int cmd_repo_add(const char *path)
{
    struct stat st;
    if (stat(path, &st) != 0) { printf("索引不存在: %s\n", path); return 1; }
    mkdir(REPO_IDX, 0755);
    /* 复制为 /仓库/<文件名> */
    const char *base = strrchr(path, '/');
    base = base ? base + 1 : path;
    char dst[BUF];
    snprintf(dst, sizeof(dst), "%s/%s", REPO_IDX, base);
    if (copy_rec(path, dst) != 0) { printf("✗ 登记失败\n"); return 1; }
    printf("✓ 已登记仓库索引: %s\n", dst);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) { usage(); return 0; }
    const char *c = argv[1];
    if (strcmp(c, "help") == 0) usage();
    else if (strcmp(c, "list") == 0) return cmd_list();
    else if (strcmp(c, "info") == 0 && argc >= 3) return cmd_info(argv[2]);
    else if (strcmp(c, "install") == 0 && argc >= 3) return cmd_install(argv[2]);
    else if (strcmp(c, "remove") == 0 && argc >= 3) return cmd_remove(argv[2]);
    else if (strcmp(c, "repo") == 0 && argc >= 3 && strcmp(argv[2], "list") == 0) return cmd_repo_list();
    else if (strcmp(c, "repo") == 0 && argc >= 4 && strcmp(argv[2], "add") == 0) return cmd_repo_add(argv[3]);
    else { usage(); return 1; }
    return 0;
}
