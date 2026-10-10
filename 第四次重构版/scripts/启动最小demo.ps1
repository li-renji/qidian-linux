# 奇点OS 最小 demo 启动器（第四次重构版：自制内核 + rootfs + init + 云端AI）
# 通道: VNC 127.0.0.1:5901（AI 配置向导/对话界面）, 串口日志 D:\iso\sos\serial.log
# 网络: QEMU user 模式（guest eth0=10.0.2.15 → 云端 API）
$QEMU = 'D:\C盘\Users\人机\Desktop\新版奇点OS\完整版\release\奇点OS\qd\qemu\qemu-system-x86_64.exe'
$KERNEL = 'D:\iso\sos\bzImage'
$DISK = 'D:\iso\sos\sos-rootfs.img'

& $QEMU @(
    '-accel', 'whpx',
    '-m', '1024',
    '-smp', '2',
    '-kernel', $KERNEL,
    '-hda', $DISK,
    '-netdev', 'user,id=net0',
    '-device', 'e1000,netdev=net0',
    '-append', 'root=/dev/sda rootfstype=ext4 rw console=ttyS0',
    '-serial', 'file:D:\iso\sos\serial.log',
    '-display', 'vnc=127.0.0.1:1',
    '-name', '奇点OS-云端AI demo'
)
