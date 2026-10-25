#!/usr/bin/env python3
"""Where the maintenance partitions sit on a stick, read from a Googlebook image's GPT.

The image file is a whole disk: its backup GPT is the last 33 sectors, past the
last Googlebook partition. On a stick those partitions are not copied. XBOOTLDR
starts at the first sector after the last image partition (kano-nvme-install.sh
END_SECT, 37244880 on the real image). The next 512 MiB is XBOOTLDR. kano-maint
is the next 62 ext4 groups of 128 MiB (7936 MiB). One more group would make
that representative image larger than 28*10**9 bytes. tests/harness/test_real_layout.py
uses the same ends (start XB, XBOOTLDR ends XB+512MiB, kano-maint ends
XB+512MiB+7936MiB). setup-stick.sh finds XBOOTLDR by PARTLABEL, not by number.

This script only reads the image and prints the plan. It does not write.
"""
import os
import stat
import struct
import sys
import uuid
import zlib

XBOOTLDR_SECTORS = 512 * 1024 * 1024 // 512          # 512 MiB; the FAT writer is sized for this
# 63 groups would put END_SECT 37244880 + XBOOTLDR + kano-maint + 33 sectors
# past 28*10**9 bytes. 62 is the largest whole-group size that still fits.
KANO_MAINT_GROUPS = 62
KANO_MAINT_SECTORS = KANO_MAINT_GROUPS * (128 * 1024 * 1024 // 512)  # 7936 MiB
MAINT_REGION_SECTORS = XBOOTLDR_SECTORS + KANO_MAINT_SECTORS
BACKUP_SECTORS = 33                                  # 32 entry sectors + header
REPRESENTATIVE_END_LBA = 37244880                    # kano-nvme-install.sh END_SECT
STICK_BUDGET_BYTES = 28 * 10**9                      # a stick that reports 28.0 GB
XBOOTLDR_TYPE = "bc13c2ff-59e6-4262-a352-b275fd6f7172"  # systemd XBOOTLDR
LINUX_TYPE = "0fc63daf-8483-4772-8e79-3d69d8477de4"      # generic Linux filesystem
RESERVED_NAMES = {"XBOOTLDR": "XBOOTLDR", "kano-maint": "kano-maint"}


def refuse(msg):
    print(f"make-maint: {msg}", file=sys.stderr)
    raise SystemExit(2)


def read_at(f, off, n, what):
    f.seek(off)
    b = f.read(n)
    if len(b) != n:
        refuse(f"short read of {what}")
    return b


def header(b, lba):
    if b[:8] != b"EFI PART":
        refuse(f"no GPT header at LBA {lba}")
    hsize, hcrc = struct.unpack_from("<II", b, 12)
    if not 92 <= hsize <= 512 or zlib.crc32(b[:16] + bytes(4) + b[20:hsize]) != hcrc:
        refuse(f"GPT header CRC at LBA {lba}")
    my, alt, first, last = struct.unpack_from("<QQQQ", b, 24)
    elba, n, esz, ecrc = struct.unpack_from("<QIII", b, 72)
    if my != lba:
        refuse(f"GPT header at LBA {lba} names LBA {my}")
    if n != 128 or esz != 128:
        refuse(f"GPT at LBA {lba} has {n} entries of {esz} bytes; only 128 of 128 is accepted")
    return {"my": my, "alt": alt, "first": first, "last": last, "elba": elba, "ecrc": ecrc, "disk": b[56:72]}


def load(path):
    try:
        st = os.lstat(path)
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    if stat.S_ISLNK(st.st_mode):
        refuse(f"{path} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        refuse(f"{path} is not a regular file")
    if st.st_size == 0 or st.st_size % 512 != 0:
        refuse(f"{path} is empty or not whole 512-byte sectors")
    sectors = st.st_size // 512
    if sectors < 68:
        refuse(f"{path} is too small for a GPT")
    with open(path, "rb") as f:
        mbr = read_at(f, 0, 512, "the MBR")
        if mbr[510:512] != b"\x55\xaa" or mbr[450] != 0xEE:
            refuse("no protective MBR")
        primary = header(read_at(f, 512, 512, "the primary GPT header"), 1)
        if primary["elba"] != 2:
            refuse("primary GPT entries are not at LBA 2")
        if primary["alt"] != sectors - 1:
            refuse("backup GPT is not at the last sector")
        if primary["first"] != 34 or primary["last"] != sectors - 34:
            refuse("GPT usable area overlaps a GPT")
        ents = read_at(f, 2 * 512, 128 * 128, "the primary GPT entries")
        if zlib.crc32(ents) != primary["ecrc"]:
            refuse("GPT entries CRC")
        backup = header(read_at(f, (sectors - 1) * 512, 512, "the backup GPT header"), sectors - 1)
        if backup["alt"] != 1 or backup["elba"] != sectors - BACKUP_SECTORS:
            refuse("backup GPT entries are not in the last 33 sectors")
        bents = read_at(f, backup["elba"] * 512, 128 * 128, "the backup GPT entries")
        if bents != ents or backup["ecrc"] != primary["ecrc"]:
            refuse("backup GPT entries differ from the primary")
        if any(backup[k] != primary[k] for k in ("first", "last", "disk")):
            refuse("backup GPT differs from the primary")
    parts = []
    for i in range(128):
        e = ents[i * 128:(i + 1) * 128]
        if e[:16] == bytes(16):
            continue
        num = i + 1
        if num >= 32:
            refuse(f"image already has partition {num}; maintenance partitions are added beside the image, not into it")
        try:
            uuid.UUID(bytes_le=e[:16])
        except ValueError:
            refuse(f"partition {num} has a bad type GUID")
        first, last, _attrs = struct.unpack_from("<QQQ", e, 32)
        try:
            name = e[56:128].decode("utf-16le").split("\0", 1)[0]
        except UnicodeDecodeError:
            refuse(f"partition {num} has a bad name")
        if not name:
            refuse(f"partition {num} has an empty name")
        if name in RESERVED_NAMES:
            refuse(f"image already has a partition named {name}")
        if first > last:
            refuse(f"partition {num} ({name}) ends at LBA {last} before it starts")
        if first < primary["first"]:
            refuse(f"partition {num} ({name}) starts at LBA {first}, before the usable area")
        if last > primary["last"]:
            refuse(f"partition {num} ({name}) ends at LBA {last}, past the usable area")
        parts.append({"num": num, "name": name, "first": first, "last": last})
    if not parts:
        refuse("image has no partitions")
    ordered = sorted(parts, key=lambda p: (p["first"], p["last"], p["num"]))
    for a, b in zip(ordered, ordered[1:]):
        if b["first"] <= a["last"]:
            refuse(f"partitions {a['num']} ({a['name']}) and {b['num']} ({b['name']}) overlap")
    return sectors, max(p["last"] for p in parts) + 1


def plan(sectors, end, disk):
    if end + MAINT_REGION_SECTORS + BACKUP_SECTORS < end:
        refuse("layout overflow")
    x_first = end
    k_first = end + XBOOTLDR_SECTORS
    maint_end = end + MAINT_REGION_SECTORS
    stick_min = maint_end + BACKUP_SECTORS
    rows = [
        ("image_sectors", sectors),
        ("googlebook_end_lba", end),
        ("googlebook_copy_sectors", end),
        ("xbootldr_name", "XBOOTLDR"),
        ("xbootldr_type", XBOOTLDR_TYPE),
        ("xbootldr_first", x_first),
        ("xbootldr_last", k_first - 1),
        ("xbootldr_sectors", XBOOTLDR_SECTORS),
        ("kano_maint_name", "kano-maint"),
        ("kano_maint_type", LINUX_TYPE),
        ("kano_maint_first", k_first),
        ("kano_maint_last", maint_end - 1),
        ("kano_maint_sectors", MAINT_REGION_SECTORS - XBOOTLDR_SECTORS),
        ("maint_end_lba", maint_end),
        ("stick_min_sectors", stick_min),
    ]
    if disk is not None:
        if disk < stick_min:
            refuse(f"disk has {disk} sectors; the maintenance layout needs {stick_min}")
        rows.append(("disk_sectors", disk))
        rows.append(("disk", "ok"))
    print("layout ok")
    for key, value in rows:
        print(f"{key} {value}")


def main(argv):
    image = None
    disk = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--image":
            if image is not None:
                refuse("--image given twice")
            if i + 1 >= len(argv) or not argv[i + 1]:
                refuse("--image needs a path")
            image = argv[i + 1]
            i += 2
        elif a == "--disk-sectors":
            if disk is not None:
                refuse("--disk-sectors given twice")
            if i + 1 >= len(argv) or not argv[i + 1]:
                refuse("--disk-sectors needs a number")
            disk = argv[i + 1]
            i += 2
        else:
            refuse(f"unknown argument {a}")
    if image is None:
        refuse("--image is required")
    if disk is not None:
        if not disk.isdecimal() or disk[0] == "0" or len(disk) > 16:
            refuse("--disk-sectors must be a positive integer")
        disk_n = int(disk)
        if disk_n > 2**48:
            refuse("--disk-sectors is absurd")
    else:
        disk_n = None
    sectors, end = load(image)
    plan(sectors, end, disk_n)


if __name__ == "__main__":
    main(sys.argv[1:])
