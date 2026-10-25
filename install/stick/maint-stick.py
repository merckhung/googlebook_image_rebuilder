#!/usr/bin/env python3
"""Write one complete Googlebook installer-stick image to a regular file.

The image keeps the Googlebook partitions, then adds the 512 MiB XBOOTLDR
partition and the 7936 MiB kano-maint partition from maint-layout.py. XBOOTLDR
is a FAT32 volume (no owners) holding the maintenance kernel, initramfs, and
the loader entries. kano-maint is an ext4 filesystem owned by uid 0, with the
payload stick-payload.py installs. The ESP loader.conf on the output stick
defaults to the installer, or the data-preserving updater with
--default-action update. The source image is not modified.

The command writes only that file. It does not open a block device, and it
does not boot the result. It always leaves `<out>.not-self-tested` beside the
image. When the source image has that sibling, its text is copied in too.
install/write-usb.sh still treats the sibling as untested and requires the
word untested before a write.
1. Put the file on a USB stick with install/write-usb.sh. That script still
   asks for the disk serial and reads the image back. Do not pass --keep-data:
   install/gbos-update.py refuses a disk that has partitions the release does
   not have.
2. The maintenance payload is already on kano-maint. Boot the stick and choose
   Install Googlebook to the internal disk.
"""
import hashlib
import importlib.util
import os
import shutil
import stat
import struct
import sys
import tempfile
import uuid
import zlib

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))

# FAT32 for the 512 MiB XBOOTLDR partition. reserved=32, 8 sectors/cluster,
# two FATs of 1024 sectors: data starts at 2080 and holds 130812 clusters.
# 1022 FAT sectors would cover clusters 2..130813; 1024 is the next multiple
# of 4, which keeps the data region a whole number of clusters.
PART_SECTORS = 512 * 1024 * 1024 // 512
RESERVED = 32
SPC = 8
FATS = 2
FAT_SECTORS = 1024
DATA_START = RESERVED + FATS * FAT_SECTORS
DATA_SECTORS = PART_SECTORS - DATA_START
CLUSTER_COUNT = DATA_SECTORS // SPC
LAST_CLUSTER = CLUSTER_COUNT + 1
CLUSTER_BYTES = SPC * 512
BACKUP_BOOT = 6
FSINFO_SECTOR = 1
VOL_ID = 0x4F4E414B  # bytes KANO, fixed so two builds of the same inputs match
DATE_1980_01_01 = 0x0021
EFI_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
# No maintenance.conf. A plain Maintenance item boots the maintenance kernel
# without the installer's lockdown / blacklist / efi=noruntime protections.
ENTRY_NAMES = (
    "gbos-install.conf",
    "gbos-update.conf",
    "gbos-nvme.conf",
    "gbos-nvme-debug.conf",
)
ENTRY_PROTECTION_TOKENS = (
    "lockdown=integrity",
    "efi=noruntime",
    "module_blacklist=",
    "initcall_blacklist=",
)
XBOOTLDR_FILES = (
    ("vmlinuz-linux", b"VMLINU~1   "),
    ("initramfs-linux.img", b"INITRA~1IMG"),
)


def refuse(msg):
    print(f"make-installer-stick: {msg}", file=sys.stderr)
    raise SystemExit(2)


def usage():
    print(
        "make-installer-stick: usage: make-installer-stick.sh "
        "--image IMAGE --out FILE --xbootldr DIR --root DIR --pristine DIR "
        "[--disk-sectors N] [--default-action install|update]",
        file=sys.stderr,
    )
    raise SystemExit(2)


def load_mod(filename, modname):
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        refuse(f"cannot load {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LAYOUT = load_mod("maint-layout.py", "maint_layout")
ESP = load_mod("esp-fat.py", "esp_fat")
EXT4 = load_mod("stick-ext4.py", "stick_ext4")
PAYLOAD = load_mod("stick-payload.py", "stick_payload")

if DATA_SECTORS % SPC != 0 or DATA_START + CLUSTER_COUNT * SPC != PART_SECTORS:
    refuse("FAT32 geometry does not fill the XBOOTLDR partition")
if (LAST_CLUSTER + 1 + 127) // 128 > FAT_SECTORS:
    refuse("FAT32 table does not cover the XBOOTLDR clusters")


def take(argv, i, flag):
    if i + 1 >= len(argv) or not argv[i + 1] or argv[i + 1].startswith("--"):
        kind = "a number" if flag == "--disk-sectors" else "a path"
        refuse(f"{flag} needs {kind}")
    return argv[i + 1]


def parse(argv):
    if not argv:
        usage()
    image = out = xdir = root = pristine = disk = default_action = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--image":
            if image is not None:
                refuse("--image given twice")
            image = take(argv, i, a)
        elif a == "--out":
            if out is not None:
                refuse("--out given twice")
            out = take(argv, i, a)
        elif a == "--xbootldr":
            if xdir is not None:
                refuse("--xbootldr given twice")
            xdir = take(argv, i, a)
        elif a == "--root":
            if root is not None:
                refuse("--root given twice")
            root = take(argv, i, a)
        elif a == "--pristine":
            if pristine is not None:
                refuse("--pristine given twice")
            pristine = take(argv, i, a)
        elif a == "--disk-sectors":
            if disk is not None:
                refuse("--disk-sectors given twice")
            disk = take(argv, i, a)
        elif a == "--default-action":
            if default_action is not None:
                refuse("--default-action given twice")
            default_action = take(argv, i, a)
            if default_action not in ("install", "update"):
                refuse("--default-action must be install or update")
        else:
            refuse(f"unknown argument {a}")
        i += 2
    if image is None:
        refuse("--image is required")
    if out is None:
        refuse("--out is required")
    if xdir is None:
        refuse("--xbootldr is required")
    if root is None:
        refuse("--root is required")
    if pristine is None:
        refuse("--pristine is required")
    return image, out, xdir, root, pristine, disk, default_action or "install"


def disk_sectors_of(text):
    if text is None:
        return None
    if not text.isdecimal() or text[0] == "0" or len(text) > 16:
        refuse("--disk-sectors must be a positive integer")
    n = int(text)
    if n > 2**48:
        refuse("--disk-sectors is absurd")
    return n


def under_dev(path):
    real = os.path.realpath(path)
    return real == "/dev" or real.startswith("/dev/")


def reject_image(path):
    try:
        st = os.lstat(path)
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    if stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode):
        refuse(f"refusing to read a device: {path}")


def reject_out(path):
    if under_dev(path):
        refuse("refusing to write under /dev")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    except OSError as e:
        refuse(f"cannot write {path}: {e.strerror}")
    if st is not None:
        if stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode):
            refuse(f"refusing to write a device: {path}")
        refuse("output already exists")
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(parent):
        refuse("output directory does not exist")


def load_image(path):
    saved = LAYOUT.refuse

    def wrapped(msg):
        refuse(msg)

    LAYOUT.refuse = wrapped
    try:
        return LAYOUT.load(path)
    finally:
        LAYOUT.refuse = saved


def pread_exact(fd, n, off, what):
    got = os.pread(fd, n, off)
    if len(got) != n:
        refuse(f"short read of {what}")
    return got


def pwrite_exact(fd, data, off, what):
    wrote = os.pwrite(fd, data, off)
    if wrote != len(data):
        refuse(f"short write of {what}")


def check_xbootldr(path):
    """The two kernel files, before the image or the disk size is considered."""
    try:
        st = os.lstat(path)
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    if stat.S_ISLNK(st.st_mode):
        refuse(f"{path} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        refuse(f"{path} is not a directory")
    try:
        dirfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    try:
        clusters = 1
        for name, sfn in XBOOTLDR_FILES:
            if len(sfn) != 11:
                refuse(f"short name for {name} is not 8.3")
            try:
                st = os.lstat(name, dir_fd=dirfd)
            except FileNotFoundError:
                refuse(f"xbootldr is missing {name}")
            except OSError as e:
                refuse(f"cannot read {name}: {e.strerror}")
            if stat.S_ISLNK(st.st_mode):
                refuse(f"{name} is a symlink")
            if not stat.S_ISREG(st.st_mode):
                refuse(f"{name} is not a regular file")
            if st.st_size == 0:
                refuse(f"{name} is empty")
            if st.st_size > 0xFFFFFFFF:
                refuse(f"{name} is too large for FAT32")
            clusters += (st.st_size + CLUSTER_BYTES - 1) // CLUSTER_BYTES
        if clusters > CLUSTER_COUNT:
            refuse("XBOOTLDR files do not fit in the 512 MiB partition")
    finally:
        os.close(dirfd)


def collect_dir(path, fds):
    nodes = []
    try:
        iterator = os.scandir(path)
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    with iterator as listing:
        entries = sorted(listing, key=lambda item: item.name)
    for entry in entries:
        if entry.name in (".", ".."):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as e:
            refuse(f"cannot read {entry.name}: {e.strerror}")
        node = {"name": entry.name, "chain": [], "cluster": 0, "data": b"", "fd": None, "size": 0}
        if stat.S_ISLNK(info.st_mode):
            refuse(f"{entry.name} is a symlink")
        if stat.S_ISDIR(info.st_mode):
            node["is_dir"] = True
            node["children"] = collect_dir(entry.path, fds)
        elif stat.S_ISREG(info.st_mode):
            if info.st_size == 0:
                refuse(f"{entry.name} is empty")
            if info.st_size > 0xFFFFFFFF:
                refuse(f"{entry.name} is too large for FAT32")
            try:
                fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as e:
                refuse(f"cannot read {entry.name}: {e.strerror}")
            fds.append(fd)
            node["is_dir"] = False
            node["children"] = []
            node["fd"] = fd
            node["size"] = info.st_size
        else:
            refuse(f"{entry.name} is not a regular file")
        nodes.append(node)
    return nodes


def node_named(nodes, name):
    for node in nodes:
        if node["name"] == name:
            return node
    return None


def require_tree(nodes):
    for name in ("vmlinuz-linux", "initramfs-linux.img", "loader"):
        if node_named(nodes, name) is None:
            refuse(f"xbootldr is missing {name}")
    loader = node_named(nodes, "loader")
    if not loader["is_dir"]:
        refuse("loader is not a directory")
    entries = node_named(loader["children"], "entries")
    if entries is None or not entries["is_dir"]:
        refuse("xbootldr is missing entries")
    found = sorted(child["name"] for child in entries["children"])
    allowed = sorted(ENTRY_NAMES)
    if found != allowed:
        extra = [name for name in found if name not in ENTRY_NAMES]
        missing = [name for name in ENTRY_NAMES if name not in found]
        if extra:
            refuse(
                "xbootldr has a plain maintenance entry or another unexpected "
                "boot entry: " + " ".join(extra)
            )
        refuse("xbootldr is missing " + " ".join(missing))
    for name in ("gbos-install.conf", "gbos-update.conf"):
        node = node_named(entries["children"], name)
        if node is None or node["is_dir"] or node["fd"] is None:
            refuse(f"xbootldr is missing {name}")
        blob = os.pread(node["fd"], node["size"], 0)
        if len(blob) != node["size"]:
            refuse(f"short read of {name}")
        try:
            text = blob.decode("ascii")
        except UnicodeError:
            refuse(f"{name} is not ASCII")
        lines = text.splitlines()
        options = [line for line in lines if line.startswith("options ")]
        if (
            "linux /vmlinuz-linux" not in lines
            or len(options) != 1
            or any(token not in options[0] for token in ENTRY_PROTECTION_TOKENS)
        ):
            refuse(f"{name} is missing a firmware protection")
        if name == "gbos-install.conf" and not options[0].endswith(" gbos.install=1"):
            refuse("install entry lost gbos.install=1")
        if name == "gbos-update.conf" and (
            not options[0].endswith(" gbos.install=1 gbos.update=1")
        ):
            refuse("update entry lost gbos.update=1")


def assign_tree(nodes):
    try:
        ESP.assign_sfns(nodes)
    except ESP.EspError as err:
        refuse(str(err))
    for node in nodes:
        sfn = node.get("sfn")
        if not isinstance(sfn, bytes) or len(sfn) != 11:
            refuse(f"short name for {node['name']} is not 8.3")
        if node["is_dir"]:
            assign_tree(node["children"])


def count_files(nodes):
    total = 0
    for node in nodes:
        if node["is_dir"]:
            total += count_files(node["children"])
        else:
            total += 1
    return total


def sfn_checksum(sfn):
    total = 0
    for byte in sfn:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def lfn_entries(name, checksum):
    chars = [ord(ch) for ch in name]
    if any(ch > 0xFFFF for ch in chars):
        refuse(f"{name} is not a FAT name")
    slots = (len(chars) + 12) // 13
    entries = []
    for seq in range(1, slots + 1):
        chunk = []
        base = (seq - 1) * 13
        for i in range(13):
            idx = base + i
            if idx < len(chars):
                chunk.append(chars[idx])
            elif idx == len(chars):
                chunk.append(0)
            else:
                chunk.append(0xFFFF)
        entry = bytearray(32)
        entry[0] = seq | (0x40 if seq == slots else 0)
        for i in range(5):
            struct.pack_into("<H", entry, 1 + 2 * i, chunk[i])
        entry[11] = 0x0F
        entry[13] = checksum
        for i in range(6):
            struct.pack_into("<H", entry, 14 + 2 * i, chunk[5 + i])
        for i in range(2):
            struct.pack_into("<H", entry, 28 + 2 * i, chunk[11 + i])
        entries.append(bytes(entry))
    entries.reverse()
    return entries


def sfn_entry(sfn, cluster, size, attr):
    entry = bytearray(32)
    entry[0:11] = sfn
    entry[11] = attr
    struct.pack_into("<H", entry, 16, DATE_1980_01_01)
    struct.pack_into("<H", entry, 18, DATE_1980_01_01)
    struct.pack_into("<H", entry, 20, (cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", entry, 24, DATE_1980_01_01)
    struct.pack_into("<H", entry, 26, cluster & 0xFFFF)
    struct.pack_into("<I", entry, 28, size)
    return bytes(entry)


def directory_bytes(nodes, self_cluster, parent_cluster, is_root):
    blob = bytearray()
    if not is_root:
        blob += sfn_entry(b".          ", self_cluster, 0, 0x10)
        blob += sfn_entry(b"..         ", parent_cluster, 0, 0x10)
    for node in nodes:
        blob += b"".join(lfn_entries(node["name"], sfn_checksum(node["sfn"])))
        attr = 0x10 if node["is_dir"] else 0x20
        size = 0 if node["is_dir"] else node["size"]
        blob += sfn_entry(node["sfn"], node["cluster"], size, attr)
    if len(blob) > CLUSTER_BYTES:
        refuse("XBOOTLDR directory does not fit in one cluster")
    return bytes(blob).ljust(CLUSTER_BYTES, b"\0")


def allocate_tree(nodes):
    fat = [0] * (FAT_SECTORS * (512 // 4))
    fat[0] = 0x0FFFFFF8
    fat[1] = 0x0FFFFFFF
    fat[2] = 0x0FFFFFFF
    nxt = 3

    def take_clusters(count):
        nonlocal nxt
        if nxt + count - 1 > LAST_CLUSTER:
            refuse("XBOOTLDR files do not fit in the 512 MiB partition")
        chain = list(range(nxt, nxt + count))
        for cluster in chain[:-1]:
            fat[cluster] = cluster + 1
        fat[chain[-1]] = 0x0FFFFFFF
        nxt = chain[-1] + 1
        return chain

    def alloc_dirs(items):
        for node in items:
            if node["is_dir"]:
                node["chain"] = take_clusters(1)
                node["cluster"] = node["chain"][0]
                alloc_dirs(node["children"])

    def alloc_files(items):
        for node in items:
            if node["is_dir"]:
                alloc_files(node["children"])
            else:
                count = (node["size"] + CLUSTER_BYTES - 1) // CLUSTER_BYTES
                node["chain"] = take_clusters(count)
                node["cluster"] = node["chain"][0]

    alloc_dirs(nodes)
    alloc_files(nodes)

    def fill(items, self_cluster, parent_cluster, is_root):
        for node in items:
            if node["is_dir"]:
                node["data"] = fill(node["children"], node["cluster"], self_cluster, False)
        return directory_bytes(items, self_cluster, parent_cluster, is_root)

    root_data = fill(nodes, 2, 0, True)
    raw = b"".join(struct.pack("<I", value) for value in fat)
    if len(raw) != FAT_SECTORS * 512:
        refuse("FAT32 table is the wrong size")
    used = 1

    def used_clusters(items):
        total = 0
        for node in items:
            total += len(node["chain"])
            if node["is_dir"]:
                total += used_clusters(node["children"])
        return total

    used += used_clusters(nodes)
    free_count = CLUSTER_COUNT - used
    return raw, root_data, free_count, nxt


def boot_sector(hidden):
    if hidden > 0xFFFFFFFF:
        refuse("XBOOTLDR starts past the FAT32 hidden-sector field")
    sector = bytearray(512)
    sector[0:3] = b"\xeb\x58\x90"
    sector[3:11] = b"MSWIN4.1"
    struct.pack_into("<H", sector, 11, 512)
    sector[13] = SPC
    struct.pack_into("<H", sector, 14, RESERVED)
    sector[16] = FATS
    sector[21] = 0xF8
    struct.pack_into("<H", sector, 24, 63)
    struct.pack_into("<H", sector, 26, 255)
    struct.pack_into("<I", sector, 28, hidden)
    struct.pack_into("<I", sector, 32, PART_SECTORS)
    struct.pack_into("<I", sector, 36, FAT_SECTORS)
    struct.pack_into("<I", sector, 44, 2)
    struct.pack_into("<H", sector, 48, FSINFO_SECTOR)
    struct.pack_into("<H", sector, 50, BACKUP_BOOT)
    sector[64] = 0x80
    sector[66] = 0x29
    struct.pack_into("<I", sector, 67, VOL_ID)
    sector[71:82] = b"XBOOTLDR   "
    sector[82:90] = b"FAT32   "
    sector[510:512] = b"\x55\xaa"
    return bytes(sector)


def fsinfo(free_count, next_free):
    sector = bytearray(512)
    struct.pack_into("<I", sector, 0, 0x41615252)
    struct.pack_into("<I", sector, 484, 0x61417272)
    struct.pack_into("<I", sector, 488, free_count)
    struct.pack_into("<I", sector, 492, next_free)
    struct.pack_into("<I", sector, 508, 0xAA550000)
    return bytes(sector)


def copy_prefix(image_fd, out_fd, sectors):
    left = sectors * 512
    off = 0
    while left:
        chunk = min(left, 1024 * 1024)
        got = pread_exact(image_fd, chunk, off, "the image")
        pwrite_exact(out_fd, got, off, "the image prefix")
        off += chunk
        left -= chunk


def write_member(out_fd, src_fd, x_first, chain, size):
    left = size
    for cluster in chain:
        take_n = min(left, CLUSTER_BYTES)
        got = pread_exact(src_fd, take_n, size - left, "an XBOOTLDR file")
        off = (x_first + DATA_START + (cluster - 2) * SPC) * 512
        pwrite_exact(out_fd, got, off, "an XBOOTLDR cluster")
        left -= take_n
    if left:
        refuse("short write of an XBOOTLDR file")


def write_tree_fat(out_fd, x_first, nodes):
    for node in nodes:
        if node["is_dir"]:
            off = (x_first + DATA_START + (node["cluster"] - 2) * SPC) * 512
            pwrite_exact(out_fd, node["data"], off, "an XBOOTLDR directory")
            write_tree_fat(out_fd, x_first, node["children"])
        else:
            write_member(out_fd, node["fd"], x_first, node["chain"], node["size"])


def write_fat(out_fd, x_first, nodes, fat, root_data, free_count, next_free):
    boot = boot_sector(x_first)
    info = fsinfo(free_count, next_free if next_free <= LAST_CLUSTER else 0xFFFFFFFF)
    base = x_first * 512
    pwrite_exact(out_fd, boot, base, "the FAT32 boot sector")
    pwrite_exact(out_fd, info, base + FSINFO_SECTOR * 512, "the FAT32 FSInfo sector")
    pwrite_exact(out_fd, boot, base + BACKUP_BOOT * 512, "the FAT32 backup boot sector")
    pwrite_exact(out_fd, info, base + (BACKUP_BOOT + 1) * 512, "the FAT32 backup FSInfo sector")
    pwrite_exact(out_fd, fat, base + RESERVED * 512, "the first FAT")
    pwrite_exact(out_fd, fat, base + (RESERVED + FAT_SECTORS) * 512, "the second FAT")
    pwrite_exact(out_fd, root_data, base + DATA_START * 512, "the FAT32 root directory")
    write_tree_fat(out_fd, x_first, nodes)


def ranges_match(image_fd, out_fd, start, end, skip):
    skip = sorted(skip)
    off = start
    index = 0
    while off < end:
        while index < len(skip) and skip[index][0] + skip[index][1] <= off:
            index += 1
        if index < len(skip) and skip[index][0] <= off < skip[index][0] + skip[index][1]:
            off = skip[index][0] + skip[index][1]
            continue
        limit = end
        if index < len(skip) and skip[index][0] < limit:
            limit = skip[index][0]
        chunk = min(limit - off, 1024 * 1024)
        if chunk <= 0:
            continue
        if pread_exact(image_fd, chunk, off, "the image") != pread_exact(out_fd, chunk, off, "the stick"):
            refuse("read-back of the Googlebook bytes differs from the image")
        off += chunk


def lfn_text(entries, sfn):
    expect = sfn_checksum(sfn)
    ordered = sorted(entries, key=lambda entry: entry[0] & 0x1F)
    seqs = [entry[0] & 0x1F for entry in ordered]
    if seqs != list(range(1, len(ordered) + 1)) or not (ordered[-1][0] & 0x40):
        refuse("read-back found a broken long file name")
    chars = []
    for entry in ordered:
        if entry[11] != 0x0F or entry[13] != expect or entry[26:28] != b"\x00\x00":
            refuse("read-back found a long file name checksum mismatch")
        raw = entry[1:11] + entry[14:26] + entry[28:32]
        for offset in range(0, 26, 2):
            unit = struct.unpack_from("<H", raw, offset)[0]
            if unit == 0:
                return "".join(chars)
            if unit == 0xFFFF:
                refuse("read-back found padding inside a long file name")
            chars.append(chr(unit))
    return "".join(chars)


def parse_directory(blob):
    found = []
    pending = []
    for index in range(0, len(blob), 32):
        entry = blob[index:index + 32]
        if entry[0] == 0:
            break
        if entry[11] == 0x0F:
            pending.append(entry)
            continue
        sfn = bytes(entry[:11])
        cluster = struct.unpack_from("<H", entry, 26)[0] | (struct.unpack_from("<H", entry, 20)[0] << 16)
        size = struct.unpack_from("<I", entry, 28)[0]
        if sfn in (b".          ", b"..         "):
            if pending:
                refuse("read-back found a long name on a dot entry")
            name = "." if sfn == b".          " else ".."
            found.append({"name": name, "sfn": sfn, "cluster": cluster, "size": size, "attr": entry[11], "dot": True})
            pending = []
            continue
        if not pending:
            refuse("read-back found a short name without a long name")
        name = lfn_text(pending, sfn)
        pending = []
        found.append({"name": name, "sfn": sfn, "cluster": cluster, "size": size, "attr": entry[11], "dot": False})
    if pending:
        refuse("read-back found a long name without a file")
    return found


def check_fat_headers(out_fd, x_first, fat):
    base = x_first * 512
    boot = pread_exact(out_fd, 512, base, "the FAT32 boot sector")
    if boot[510:512] != b"\x55\xaa" or boot[82:90] != b"FAT32   ":
        refuse("read-back found no FAT32 boot sector")
    if pread_exact(out_fd, 512, base + BACKUP_BOOT * 512, "the FAT32 backup boot sector") != boot:
        refuse("read-back found a FAT32 backup boot sector that differs")
    info = pread_exact(out_fd, 512, base + 512, "the FAT32 FSInfo sector")
    if info[0:4] != b"RRaA" or info[484:488] != b"rrAa" or info[510:512] != b"\x55\xaa":
        refuse("read-back found no FAT32 FSInfo sector")
    disk_fat = pread_exact(out_fd, FAT_SECTORS * 512, base + RESERVED * 512, "the first FAT")
    second = pread_exact(out_fd, FAT_SECTORS * 512, base + (RESERVED + FAT_SECTORS) * 512, "the second FAT")
    if disk_fat != second or disk_fat != fat:
        refuse("read-back found two different FAT copies")
    return [struct.unpack_from("<I", disk_fat, i)[0] for i in range(0, len(disk_fat), 4)]


def files_match(out_fd, x_first, nodes, cluster, data, fat_entries, parent_cluster):
    off = (x_first + DATA_START + (cluster - 2) * SPC) * 512
    disk = pread_exact(out_fd, CLUSTER_BYTES, off, "an XBOOTLDR directory")
    if disk != data:
        refuse("read-back of an XBOOTLDR directory differs")
    parsed = parse_directory(disk)
    dots = [item for item in parsed if item["dot"]]
    names = [item for item in parsed if not item["dot"]]
    if parent_cluster is None:
        if dots:
            refuse("read-back found a dot entry in the XBOOTLDR root")
    else:
        if [item["name"] for item in dots] != [".", ".."]:
            refuse("read-back found broken dot entries")
        if dots[0]["cluster"] != cluster or dots[1]["cluster"] != parent_cluster:
            refuse("read-back found a dot entry pointing at the wrong cluster")
        if any(item["attr"] != 0x10 or item["size"] != 0 for item in dots):
            refuse("read-back found a dot entry that is not a directory")
    if [item["name"] for item in names] != [node["name"] for node in nodes]:
        refuse("read-back found different XBOOTLDR file names")
    for item, node in zip(names, nodes):
        if item["sfn"] != node["sfn"] or item["cluster"] != node["cluster"]:
            refuse(f"read-back name of {node['name']} differs")
        if node["is_dir"]:
            if item["size"] != 0 or not item["attr"] & 0x10:
                refuse(f"read-back of {node['name']} is not a directory")
            files_match(out_fd, x_first, node["children"], node["cluster"], node["data"], fat_entries, cluster)
            continue
        if item["size"] != node["size"] or item["attr"] & 0x10:
            refuse(f"read-back size of {node['name']} differs")
        left = node["size"]
        current = node["cluster"]
        seen = 0
        while left:
            if current < 2 or current > LAST_CLUSTER or seen > CLUSTER_COUNT:
                refuse(f"read-back cluster chain of {node['name']} is broken")
            take_n = min(left, CLUSTER_BYTES)
            at = (x_first + DATA_START + (current - 2) * SPC) * 512
            disk_bytes = pread_exact(out_fd, take_n, at, node["name"])
            src = pread_exact(node["fd"], take_n, node["size"] - left, node["name"])
            if disk_bytes != src:
                refuse(f"read-back bytes of {node['name']} differ")
            left -= take_n
            nxt = fat_entries[current] & 0x0FFFFFFF
            seen += 1
            if left == 0:
                if nxt < 0x0FFFFFF8:
                    refuse(f"read-back chain of {node['name']} is too long")
                break
            if nxt >= 0x0FFFFFF8:
                refuse(f"read-back chain of {node['name']} ended early")
            current = nxt


def gpt_header(my, alt, elba, disk_guid, sectors, entries):
    header = bytearray(struct.pack(
        "<8sIIIIQQQQ16sQIII",
        b"EFI PART", 0x10000, 92, 0, 0,
        my, alt, 34, sectors - 34,
        disk_guid, elba, 128, 128, zlib.crc32(entries),
    ))
    struct.pack_into("<I", header, 16, zlib.crc32(header))
    return bytes(header) + bytes(512 - 92)


def protective_mbr(sectors):
    mbr = bytearray(512)
    mbr[446:462] = struct.pack(
        "<B3sB3sII", 0, b"\x00\x02\x00", 0xEE, b"\xff\xff\xff", 1, min(sectors - 1, 0xFFFFFFFF)
    )
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr)


def partition_entry(type_text, part_guid, first, last, name):
    entry = bytearray(128)
    entry[0:16] = uuid.UUID(type_text).bytes_le
    entry[16:32] = part_guid.bytes_le
    struct.pack_into("<QQQ", entry, 32, first, last, 0)
    encoded = name.encode("utf-16le")
    if len(encoded) > 72:
        refuse(f"partition name {name} is too long")
    entry[56:56 + len(encoded)] = encoded
    return bytes(entry)


def stick_table(source, x_first, k_first, k_last, kano_guid):
    # Numbers 32 and 33 are the maintenance partitions. A hole below 32,
    # such as the unused number 11 on a Googlebook image, stays empty.
    # layout already refuses an image that uses number 32 or higher.
    table = bytearray(source)
    added = (
        (31, LAYOUT.XBOOTLDR_TYPE, x_first, k_first - 1, "XBOOTLDR", uuid.uuid4()),
        (32, LAYOUT.LINUX_TYPE, k_first, k_last, "kano-maint", kano_guid),
    )
    for index, _type_text, _first, _last, _name, _guid in added:
        slot = table[index * 128:index * 128 + 16]
        if slot != bytes(16):
            refuse(
                f"image already has partition {index + 1}; maintenance "
                "partitions are added beside the image, not into it"
            )
    for index, type_text, first, last, name, part_guid in added:
        table[index * 128:(index + 1) * 128] = partition_entry(type_text, part_guid, first, last, name)
    return bytes(table)


def find_esp(entries):
    found = None
    for index in range(128):
        entry = entries[index * 128:(index + 1) * 128]
        if entry[:16] == bytes(16):
            continue
        try:
            type_text = str(uuid.UUID(bytes_le=entry[:16]))
            name = entry[56:128].decode("utf-16le").split("\0", 1)[0]
        except (ValueError, UnicodeDecodeError):
            refuse(f"partition {index + 1} has a bad GPT entry")
        if name != "EFI-SYSTEM":
            continue
        if type_text != EFI_TYPE:
            refuse("EFI-SYSTEM partition has the wrong type")
        if found is not None:
            refuse("image has more than one EFI-SYSTEM partition")
        first, last = struct.unpack_from("<QQ", entry, 32)
        if first > last:
            refuse("EFI-SYSTEM partition ends before it starts")
        found = (first, last)
    if found is None:
        refuse("image has no EFI-SYSTEM partition")
    return found


def read_regular(path, mode, name):
    """Read one regular file.

    A missing owner-read bit is added only while this process reads, then
    the saved mode is put back on that inode. The caller stores `mode` from
    before the change. chmod does not follow a symlink; the caller already
    required a regular file. dbus-daemon-launch-helper is mode 4110.
    """
    saved = stat.S_IMODE(mode)
    granted = False
    if saved & 0o400 == 0:
        try:
            os.chmod(path, saved | 0o400, follow_symlinks=False)
        except OSError as e:
            refuse(f"cannot read {name}: {e.strerror}")
        granted = True
    try:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            refuse(f"cannot read {name}: {e.strerror}")
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        if granted:
            try:
                os.chmod(path, saved, follow_symlinks=False)
            except OSError as e:
                refuse(f"cannot restore the mode of {name}: {e.strerror}")


def copy_tree(src, dst):
    try:
        info = os.lstat(src)
    except OSError as e:
        refuse(f"cannot read {src}: {e.strerror}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        refuse(f"{src} is not a directory")
    os.mkdir(dst, stat.S_IMODE(info.st_mode) or 0o755)
    try:
        iterator = os.scandir(src)
    except OSError as e:
        refuse(f"cannot read {src}: {e.strerror}")
    with iterator as listing:
        entries = sorted(listing, key=lambda item: item.name)
    for entry in entries:
        target = os.path.join(dst, entry.name)
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as e:
            refuse(f"cannot read {entry.name}: {e.strerror}")
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), target)
        elif stat.S_ISDIR(st.st_mode):
            copy_tree(entry.path, target)
        elif stat.S_ISREG(st.st_mode):
            data = read_regular(entry.path, st.st_mode, entry.name)
            file_mode = stat.S_IMODE(st.st_mode) or 0o644
            out = os.open(
                target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                file_mode,
            )
            try:
                view = memoryview(data)
                while view:
                    wrote = os.write(out, view)
                    if wrote <= 0:
                        refuse(f"short write of {entry.name}")
                    view = view[wrote:]
                os.fchmod(out, file_mode)
            finally:
                os.close(out)
        else:
            refuse(f"unexpected file type: {entry.name}")


def write_extracted(directory, files):
    for name, data in files.items():
        if "/" in name or name in (".", ".."):
            refuse(f"ESP entry name is not a single file: {name}")
        dest = os.path.join(directory, name)
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        try:
            view = memoryview(data)
            while view:
                wrote = os.write(fd, view)
                if wrote <= 0:
                    refuse(f"short write of {name}")
                view = view[wrote:]
        finally:
            os.close(fd)


def sha256_file(path):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while True:
            chunk = os.read(fd, 8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def write_sha(path, digest):
    text = f"{digest}  {os.path.basename(path)}\n".encode("ascii")
    fd = os.open(path + ".sha256", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    try:
        view = memoryview(text)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                refuse("short write of the sha256 file")
            view = view[wrote:]
        os.fsync(fd)
    finally:
        os.close(fd)


MARKER_LIMIT = 4096
STICK_UNTESTED_LINE = b"installer stick was not booted in QEMU by make-installer-stick\n"


def marker_path(path):
    return path + ".not-self-tested"


def read_source_marker(image):
    """Return the source marker bytes, or empty when the source has none.

    A symlink is not followed. A non-regular file or more than MARKER_LIMIT
    bytes is refused. Existence, not the text, is what write-usb.sh checks.
    """
    path = marker_path(image)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return b""
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    if stat.S_ISLNK(st.st_mode):
        refuse(f"{path} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        refuse(f"{path} is not a regular file")
    if st.st_size > MARKER_LIMIT:
        refuse(f"{path} is larger than {MARKER_LIMIT} bytes")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    try:
        data = os.read(fd, MARKER_LIMIT + 1)
    finally:
        os.close(fd)
    if len(data) > MARKER_LIMIT:
        refuse(f"{path} is larger than {MARKER_LIMIT} bytes")
    return data


def untested_marker_bytes(source_reason):
    """The stick was not booted. A source reason is kept after that line."""
    text = STICK_UNTESTED_LINE
    if source_reason:
        text += source_reason
        if not source_reason.endswith(b"\n"):
            text += b"\n"
    return text


def reject_marker_dest(out):
    path = marker_path(out)
    if under_dev(path):
        refuse("refusing to write under /dev")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as e:
        refuse(f"cannot write {path}: {e.strerror}")
    if stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode):
        refuse(f"refusing to write a device: {path}")
    refuse(f"output marker already exists: {path}")


def write_untested_marker(out, source_reason):
    path = marker_path(out)
    data = untested_marker_bytes(source_reason)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        refuse(f"output marker already exists: {path}")
    except OSError as e:
        refuse(f"cannot write {path}: {e.strerror}")
    try:
        view = memoryview(data)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                refuse("short write of the untested marker")
            view = view[wrote:]
        os.fchmod(fd, 0o644)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.close(fd)


def check_owners(out_fd, part_off, part_len):
    try:
        mode, uid, gid, _data, is_link = EXT4.read_path(
            out_fd, part_off, part_len, "usr/local/sbin/gbos-install"
        )
        link_mode, link_uid, link_gid, target, link = EXT4.read_path(
            out_fd, part_off, part_len,
            "etc/systemd/system/multi-user.target.wants/gbos-installer.service",
        )
    except EXT4.Ext4Error as err:
        refuse(str(err))
    if uid != 0 or gid != 0 or is_link or not stat.S_ISREG(mode):
        refuse("kano-maint owner is not uid 0")
    if (
        link_uid != 0 or link_gid != 0 or not link or not stat.S_ISLNK(link_mode)
        or target != b"/etc/systemd/system/gbos-installer.service"
    ):
        refuse("kano-maint owner is not uid 0")


def write_stick(image, out, xdir, root, pristine, disk_text, default_action="install"):
    if default_action not in ("install", "update"):
        refuse("default action must be install or update")
    loader_text = PAYLOAD.esp_loader_text()
    if default_action == "update":
        if loader_text.count("default gbos-install.conf\n") != 1:
            refuse("unexpected installer default entry")
        loader_text = loader_text.replace("default gbos-install.conf\n", "default gbos-update.conf\n")
    reject_image(image)
    reject_out(out)
    reject_marker_dest(out)
    source_reason = read_source_marker(image)
    if LAYOUT.XBOOTLDR_SECTORS != PART_SECTORS:
        refuse("XBOOTLDR size changed; the FAT32 writer is sized for 512 MiB")
    check_xbootldr(xdir)
    sectors, end = load_image(image)
    x_first = end
    k_first = end + LAYOUT.XBOOTLDR_SECTORS
    maint_end = end + LAYOUT.MAINT_REGION_SECTORS
    stick_min = maint_end + LAYOUT.BACKUP_SECTORS
    chosen = disk_sectors_of(disk_text)
    disk = stick_min if chosen is None else chosen
    if disk < stick_min:
        refuse(f"disk has {disk} sectors; the maintenance layout needs {stick_min}")
    k_last = maint_end - 1
    if not 34 <= x_first <= k_last <= disk - 34:
        refuse("maintenance partitions fall outside the stick GPT")
    part_off = k_first * 512
    part_len = (k_last - k_first + 1) * 512
    work = tempfile.mkdtemp(prefix="gbos-stick-")
    fds = []
    tmp = out + ".gbos-partial"
    out_fd = None
    try:
        if os.path.lexists(tmp):
            refuse("temporary output already exists")
        root_copy = os.path.join(work, "root")
        esp_dir = os.path.join(work, "esp", "loader", "entries")
        copy_tree(root, root_copy)
        os.makedirs(esp_dir)
        image_fd = os.open(image, os.O_RDONLY | os.O_NOFOLLOW)
        fds.append(image_fd)
        source_entries = pread_exact(image_fd, 128 * 128, 1024, "the image GPT")
        esp_first, esp_last = find_esp(source_entries)
        esp_len = (esp_last - esp_first + 1) * 512
        partuuid = uuid.uuid4()
        try:
            extracted = ESP.list_files(image_fd, esp_first * 512, esp_len, "loader/entries")
            write_extracted(esp_dir, extracted)
            PAYLOAD.apply({
                "--root": root_copy,
                "--pristine": pristine,
                "--xbootldr": xdir,
                "--esp-dir": os.path.join(work, "esp"),
                "--image": image,
                "--partuuid": str(partuuid),
            })
        except (PAYLOAD.PayloadError, ESP.EspError) as err:
            refuse(str(err))
        children = collect_dir(xdir, fds)
        require_tree(children)
        assign_tree(children)
        fat, root_data, free_count, next_free = allocate_tree(children)
        file_count = count_files(children)
        out_fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o644)
        try:
            os.ftruncate(out_fd, disk * 512)
            copy_prefix(image_fd, out_fd, end)
            table = stick_table(source_entries, x_first, k_first, k_last, partuuid)
            disk_guid = uuid.uuid4().bytes_le
            primary = gpt_header(1, disk - 1, 2, disk_guid, disk, table)
            backup = gpt_header(disk - 1, 1, disk - 33, disk_guid, disk, table)
            mbr = protective_mbr(disk)
            pwrite_exact(out_fd, mbr, 0, "the protective MBR")
            pwrite_exact(out_fd, primary, 512, "the primary GPT header")
            pwrite_exact(out_fd, table, 1024, "the primary GPT entries")
            write_fat(out_fd, x_first, children, fat, root_data, free_count, next_free)
            try:
                EXT4.write_tree(out_fd, part_off, part_len, root_copy)
                changed = ESP.patch_file(
                    out_fd, esp_first * 512, esp_len, "loader/loader.conf",
                    loader_text.encode("ascii"),
                )
            except (EXT4.Ext4Error, ESP.EspError) as err:
                refuse(str(err))
            pwrite_exact(out_fd, table, (disk - 33) * 512, "the backup GPT entries")
            pwrite_exact(out_fd, backup, (disk - 1) * 512, "the backup GPT header")
            if pread_exact(out_fd, 512, 0, "the protective MBR") != mbr:
                refuse("read-back of the protective MBR differs")
            if pread_exact(out_fd, 512, 512, "the primary GPT header") != primary:
                refuse("read-back of the primary GPT header differs")
            if pread_exact(out_fd, len(table), 1024, "the primary GPT entries") != table:
                refuse("read-back of the primary GPT entries differs")
            if pread_exact(out_fd, len(table), (disk - 33) * 512, "the backup GPT entries") != table:
                refuse("read-back of the backup GPT entries differs")
            if pread_exact(out_fd, 512, (disk - 1) * 512, "the backup GPT header") != backup:
                refuse("read-back of the backup GPT header differs")
            ranges_match(image_fd, out_fd, 34 * 512, end * 512, changed)
            fat_entries = check_fat_headers(out_fd, x_first, fat)
            files_match(out_fd, x_first, children, 2, root_data, fat_entries, None)
            if pread_exact(out_fd, 512, k_first * 512, "kano-maint") != bytes(512):
                refuse("kano-maint boot sector is not empty")
            if pread_exact(out_fd, 2, part_off + 1024 + 0x38, "the ext4 superblock") != b"\x53\xef":
                refuse("kano-maint has no ext4 superblock")
            if pread_exact(out_fd, 512, k_last * 512, "kano-maint") != bytes(512):
                refuse("kano-maint end sector is not empty")
            check_owners(out_fd, part_off, part_len)
            try:
                if ESP.read_file(out_fd, esp_first * 512, esp_len, "loader/loader.conf") != loader_text.encode("ascii"):
                    refuse("ESP loader.conf was not patched")
            except ESP.EspError as err:
                refuse(str(err))
            os.fsync(out_fd)
        except BaseException:
            os.close(out_fd)
            out_fd = None
            os.unlink(tmp)
            raise
        os.close(out_fd)
        out_fd = None
        if os.path.lexists(out):
            os.unlink(tmp)
            refuse("output already exists")
        try:
            write_untested_marker(out, source_reason)
        except BaseException:
            os.unlink(tmp)
            raise
        try:
            os.link(tmp, out)
        except OSError as link_err:
            try:
                os.unlink(marker_path(out))
            except OSError:
                pass
            os.unlink(tmp)
            if os.path.exists(out):
                refuse("output already exists")
            raise link_err
        os.unlink(tmp)
        digest = sha256_file(out)
        write_sha(out, digest)
        parent = os.path.dirname(os.path.abspath(out)) or "."
        dirfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        print("stick ok")
        print(f"image_sectors {sectors}")
        print(f"googlebook_copy_sectors {end}")
        print(f"xbootldr_first {x_first}")
        print(f"kano_maint_first {k_first}")
        print(f"disk_sectors {disk}")
        print(f"minimum_stick_sectors {stick_min}")
        print(f"xbootldr files {file_count}")
        print("kano-maint ext4")
        print("owners uid 0")
        print(f"sha256 {digest}")
        print(f"default_action {default_action}")
        print("step 1: this command writes only a file and does not open a block device")
        print(
            "step 2: put that file on a USB stick with install/write-usb.sh, which still "
            "asks for the disk serial and reads the image back. Do not pass --keep-data: "
            "install/gbos-update.py refuses a disk that has partitions the release does not have"
        )
        print(
            "step 3: the maintenance payload is already on kano-maint. "
            + ("Boot the stick and choose Update Googlebook on the internal disk (keep data)"
               if default_action == "update" else "Boot the stick and choose Install Googlebook to the internal disk")
        )
    finally:
        if out_fd is not None:
            os.close(out_fd)
        for fd in fds:
            os.close(fd)
        shutil.rmtree(work, ignore_errors=True)


def main(argv):
    image, out, xdir, root, pristine, disk, default_action = parse(argv)
    write_stick(image, out, xdir, root, pristine, disk, default_action)


if __name__ == "__main__":
    main(sys.argv[1:])
