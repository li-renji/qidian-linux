#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qd-pack.py - .qds 打包工具（严格按奇点OS规范 4.2 节）
将一个 ELF 二进制 + 内嵌配置打包为 .qds 系统单文件

文件布局（严格固定，不可变）：
  偏移 0        : 文件头（固定 256 字节）
  偏移 256      : ELF 二进制（bin_size 字节）
  偏移 256+bin  : 内嵌配置（JSON, config_size 字节）
  末尾          : 签名（可选）

用法:
  python3 qd-pack.py <elf> <config.json> -o <output.qds>
  python3 qd-pack.py <elf> --no-config -o <output.qds>
"""
import json
import struct
import sys
import hashlib
import argparse

MAGIC = 0x51445353  # "QDSS"
HEADER_SIZE = 256

def pack(elf_path, config_path, out_path, arch=1, name="qd-service",
         entry_symbol="qd_service_main", permissions=0,
         min_os_version=(1 << 16) | 0, api_level=1, payload_type=0):
    if payload_type == 1:  # Python payload
        with open(elf_path, 'r', encoding='utf-8') as f:
            elf = f.read().encode('utf-8')
    else:  # ELF payload
        with open(elf_path, 'rb') as f:
            elf = f.read()

    if config_path and config_path != '--no-config':
        with open(config_path, 'r', encoding='utf-8') as f:
            config_json = json.dumps(json.load(f), ensure_ascii=False).encode('utf-8')
    else:
        config_json = b''

    bin_size = len(elf)
    config_size = len(config_json)

    # 文件头 256 字节
    header = bytearray(HEADER_SIZE)
    struct.pack_into('<I', header, 0, MAGIC)
    struct.pack_into('<H', header, 4, 1)          # header_version
    struct.pack_into('<H', header, 6, 1)          # format_version
    name_b = name.encode('utf-8')[:63]
    header[8:8+len(name_b)] = name_b
    entry_b = entry_symbol.encode('utf-8')[:63]
    header[72:72+len(entry_b)] = entry_b
    struct.pack_into('<I', header, 136, arch)     # arch: 1=x86_64
    struct.pack_into('<I', header, 140, HEADER_SIZE)  # bin_offset = 256
    struct.pack_into('<Q', header, 144, bin_size)
    struct.pack_into('<Q', header, 152, HEADER_SIZE + bin_size)  # config_offset
    struct.pack_into('<Q', header, 160, config_size)
    struct.pack_into('<Q', header, 168, HEADER_SIZE + bin_size + config_size)  # signature_offset
    struct.pack_into('<Q', header, 176, 0)        # signature_size
    struct.pack_into('<Q', header, 184, permissions)
    struct.pack_into('<I', header, 192, min_os_version)
    struct.pack_into('<I', header, 196, api_level)
    struct.pack_into('<I', header, 200, payload_type)  # 0=ELF 1=Python
    # 保留字段全 0（已默认）
    # sha256_tail: 前232字节的SHA256前24字节
    sha = hashlib.sha256(bytes(header[:232])).digest()[:24]
    header[232:232+24] = sha

    with open(out_path, 'wb') as f:
        f.write(bytes(header))
        f.write(elf)
        f.write(config_json)

    size = HEADER_SIZE + bin_size + config_size
    print(f"[OK] 已打包 {out_path}")
    print(f"     name={name} entry={entry_symbol} arch={'x86_64' if arch==1 else 'aarch64' if arch==0 else '?'}")
    print(f"     bin={bin_size}B config={config_size}B 总大小={size}B")
    return size

def verify(path):
    """验证 .qds 文件完整性"""
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < HEADER_SIZE:
        print(f"[错误] 文件太小: {len(data)}B < 256B")
        return False
    magic, hver, fver = struct.unpack_from('<IHH', data, 0)
    if magic != MAGIC:
        print(f"[错误] magic 不符: 0x{magic:08X} (期望 0x51445353)")
        return False
    name = data[8:72].split(b'\0')[0].decode('utf-8', errors='replace')
    bin_offset, bin_size = struct.unpack_from('<IQ', data, 140)
    config_offset, config_size = struct.unpack_from('<QQ', data, 152)
    sig_offset, sig_size = struct.unpack_from('<QQ', data, 168)
    expected = 256 + bin_size + config_size + sig_size
    if len(data) != expected:
        print(f"[错误] 大小不匹配: 实际{len(data)}B 期望{expected}B")
        return False
    sha = hashlib.sha256(data[:232]).digest()[:24]
    if sha != data[232:256]:
        print(f"[错误] 文件头自校验失败")
        return False
    print(f"[OK] 有效 .qds: name={name} hver={hver} fver={fver}")
    print(f"     bin_offset={bin_offset} bin_size={bin_size}")
    print(f"     config_offset={config_offset} config_size={config_size}")
    print(f"     sig_offset={sig_offset} sig_size={sig_size}")
    return True

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='QDSS 打包工具')
    sub = parser.add_subparsers(dest='cmd')
    p = sub.add_parser('pack', help='打包 ELF+配置 为 .qds')
    p.add_argument('elf')
    p.add_argument('config', nargs='?', default=None)
    p.add_argument('-o', '--output', required=True)
    p.add_argument('--name', default='qd-service')
    p.add_argument('--entry', default='qd_service_main')
    p.add_argument('--arch', type=int, default=1)
    p.add_argument('--perms', type=int, default=0)
    p.add_argument('--py', action='store_true', help='payload 为 Python 脚本')
    v = sub.add_parser('verify', help='验证 .qds 文件')
    v.add_argument('file')
    args = parser.parse_args()

    if args.cmd == 'pack':
        pack(args.elf, args.config, args.output, arch=args.arch,
             name=args.name, entry_symbol=args.entry, permissions=args.perms,
             payload_type=(1 if args.py else 0))
    elif args.cmd == 'verify':
        ok = verify(args.file)
        sys.exit(0 if ok else 1)
    else:
        parser.print_help()
