/* 奇点OS 自研 init —— 系统 1 号进程（第四次重构版）
 *
 * 启动流程：内核挂载 rootfs → 执行 /sbin/init →
 *   init 挂载内核虚拟文件系统 → 启动模块引擎 → 模块引擎扫描 /modules 下 .qd/.qds
 *
 * 编译（musl 静态，禁止用宿主 glibc）：
 *   x86_64-linux-musl-gcc init.c -o sos-rootfs/sbin/init -static
 *
 * 注意（相对原方案的两处修正）：
 *   1. 头文件是 <sys/mount.h>，不是 <mount.h>（原方案会编译失败）
 *   2. /dev 挂 devtmpfs 而不是 tmpfs：tmpfs 会覆盖预先 mknod 的节点，
 *      导致 console/null 丢失；devtmpfs 由内核自动生成设备节点
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <string.h>
#include <errno.h>

/* 打印并保持存活（1 号进程不允许退出） */
static void hang(const char *msg)
{
    fprintf(stderr, "奇点OS: %s (errno=%d)\n", msg, errno);
    for (;;) pause();
}

int main(int argc, char **argv)
{
    /* 0. 建立 /dev 等基础目录（rootfs 里已有，双保险） */
    mkdir("/dev", 0755);
    mkdir("/proc", 0755);
    mkdir("/sys", 0755);
    mkdir("/tmp", 0755);

    /* 1. 挂载内核虚拟文件系统 */
    if (mount("proc", "/proc", "proc", 0, NULL) != 0)
        hang("挂载 /proc 失败");
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0)
        hang("挂载 /sys 失败");
    if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0) {
        /* devtmpfs 不可用时退回 tmpfs + 手动建最小节点 */
        if (mount("tmpfs", "/dev", "tmpfs", 0, NULL) != 0)
            hang("挂载 /dev 失败");
    }

    /* 1.1 手动补建基础设备节点（双保险：devtmpfs 动态生成失败时兜底）
     *  tty0 设备号是 (4,0)（VT 虚拟终端，VNC 显示的就是它，ai_service 界面依赖）；
     *  /dev/tty 是 (5,0)、/dev/console 是 (5,1) —— 不要弄混 */
    mknod("/dev/null", S_IFCHR | 0666, makedev(1, 3));
    mknod("/dev/console", S_IFCHR | 0600, makedev(5, 1));
    mknod("/dev/tty", S_IFCHR | 0666, makedev(5, 0));
    mknod("/dev/tty0", S_IFCHR | 0600, makedev(4, 0));
    mknod("/dev/tty1", S_IFCHR | 0600, makedev(4, 1));
    mknod("/dev/tty2", S_IFCHR | 0600, makedev(4, 2));
    chmod("/dev/tty0", 0622);
    chmod("/dev/tty1", 0622);
    chmod("/dev/tty2", 0622);
    chmod("/dev/console", 0622);

    printf("奇点OS init 启动成功\n");
    printf("正在加载引导文件 boot.qds...\n");
    fflush(stdout);

    /* 2. 执行根目录引导文件：连接第一个框架 ai.qd，启动 AI 子系统，
     *    再由引导文件转交主系统框架（module-engine） */
    if (access("/boot.qds", X_OK) == 0) {
        execl("/boot.qds", "boot.qds", NULL);
        hang("引导文件启动失败");
    }

    /* 引导文件缺失时回退：直接启动主系统（子框架.qd/主框架.qd/module-engine） */
    printf("引导文件未就绪，回退加载主系统...\n");
    if (access("/子框架.qd/主框架.qd/module-engine", X_OK) == 0) {
        execl("/子框架.qd/主框架.qd/module-engine", "module-engine", NULL);
        hang("主系统启动失败");
    }

    printf("模块引擎未就绪，进入维护 shell\n");
    if (access("/bin/sh", X_OK) == 0)
        execl("/bin/sh", "sh", NULL);

    hang("无可用 shell");
    return 0;
}
