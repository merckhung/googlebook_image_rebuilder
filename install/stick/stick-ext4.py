#!/usr/bin/env python3
"""Write a sparse 7936 MiB ext4 image of a directory, with uid 0 and no journal.

62 groups of 128 MiB (8321499136 bytes). That matches maint-layout.py. One more
group would make the representative installer image larger than 28*10**9 bytes.
This does not call mke2fs and does not need root. Holes stay unwritten, so the
stick file stays sparse. Symlinks are stored as symlinks (a short target lives
in the inode). read_path checks the result without debugfs.
"""
import os
import stat
import struct

__all__ = ("Ext4Error", "read_path", "write_tree")

BLOCK = 4096
BPG = 32768
GROUPS = 62  # must match maint-layout.KANO_MAINT_GROUPS
BLOCKS = GROUPS * BPG  # every group full; 8321499136 bytes
IPG = 8192
INODE_SIZE = 256
FIRST_INO = 11
IT_BLOCKS = (IPG * INODE_SIZE) // BLOCK  # 512


def _sparse_groups(groups):
    # ext4 sparse_super: group 0, group 1, and powers of 3, 5, and 7.
    found = {0, 1}
    for base in (3, 5, 7):
        value = base
        while value < groups:
            found.add(value)
            value *= base
    return found


SPARSE = _sparse_groups(GROUPS)
EXTENTS_FL = 0x80000
FILETYPE = 0x2
EXTENTS_INCOMPAT = 0x40
SPARSE_SUPER = 0x1
LARGE_FILE = 0x2
SB_UUID = bytes.fromhex("6b616e6f2d6d61696e742d6578743401")
EXTENT_MAGIC = 0xF30A


class Ext4Error(Exception):
    pass


def fail(msg):
    raise Ext4Error(msg)


def meta_blocks(group):
    # super + group descriptors, then the two bitmaps and the inode table.
    extra = 2 if group in SPARSE else 0
    return extra + 2 + IT_BLOCKS


def group_fields(group):
    base = group * BPG
    local = 0
    super_blk = gdt_blk = None
    if group in SPARSE:
        super_blk = base + local
        local += 1
        gdt_blk = base + local
        local += 1
    block_bitmap = base + local
    local += 1
    inode_bitmap = base + local
    local += 1
    inode_table = base + local
    return super_blk, gdt_blk, block_bitmap, inode_bitmap, inode_table


def set_bit(bitmap, index):
    bitmap[index // 8] |= 1 << (index % 8)


def popcount(bitmap):
    return sum(byte.bit_count() for byte in bitmap)


def pwrite(fd, data, off, what):
    view = memoryview(data)
    while view:
        wrote = os.pwrite(fd, view, off)
        if wrote <= 0:
            fail(f"short write of {what}")
        off += wrote
        view = view[wrote:]


class Node:
    def __init__(self, name, mode):
        self.name = name
        self.mode = mode
        self.is_dir = stat.S_ISDIR(mode)
        self.is_link = stat.S_ISLNK(mode)
        self.is_file = stat.S_ISREG(mode)
        self.target = b""
        self.path = ""
        self.size = 0
        self.children = []
        self.ino = 0
        self.blocks = []
        self.dir_blobs = []
        self.leaf_blocks = []
        self.iblock = b""


def scan(path):
    children = []
    try:
        iterator = os.scandir(path)
    except OSError as err:
        fail(f"cannot read {path}: {err.strerror}")
    with iterator as listing:
        entries = sorted(listing, key=lambda item: item.name)
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as err:
            fail(f"cannot read {entry.name}: {err.strerror}")
        node = Node(entry.name, info.st_mode)
        if node.is_link:
            try:
                node.target = os.readlink(entry.path).encode("utf-8")
            except OSError as err:
                fail(f"cannot read link {entry.name}: {err.strerror}")
            node.size = len(node.target)
        elif node.is_dir:
            node.children = scan(entry.path)
        elif node.is_file:
            node.path = entry.path
            node.size = info.st_size
        else:
            fail(f"unexpected file type: {entry.name}")
        children.append(node)
    return children


def assign_inodes(root):
    nxt = FIRST_INO

    def walk(node, is_root):
        nonlocal nxt
        if is_root:
            node.ino = 2
        else:
            if nxt >= GROUPS * IPG:
                fail("kano-maint has too many files")
            node.ino = nxt
            nxt += 1
        if node.is_dir:
            for child in node.children:
                walk(child, False)

    walk(root, True)
    return nxt - 1


def dir_links(node):
    return 2 + sum(1 for child in node.children if child.is_dir)


def ftype(node):
    if node.is_dir:
        return 2
    if node.is_link:
        return 7
    if node.is_file:
        return 1
    fail(f"unexpected file type: {node.name}")


def pack_dir(entries):
    """entries are (inode, file_type, name). Each returned block is 4096 bytes."""
    encoded = []
    for ino, typ, name in entries:
        raw = name.encode("utf-8")
        if not raw or len(raw) > 255 or b"\x00" in raw or b"/" in raw:
            fail(f"bad directory name {name!r}")
        need = (8 + len(raw) + 3) & ~3
        encoded.append((ino, typ, raw, need))
    blocks = []
    current = []
    used = 0
    for item in encoded:
        if used + item[3] > BLOCK:
            if not current:
                fail("directory entry does not fit in one block")
            blocks.append(current)
            current = []
            used = 0
        current.append(item)
        used += item[3]
    if current:
        blocks.append(current)
    out = []
    for group in blocks:
        blob = bytearray()
        for index, (ino, typ, raw, need) in enumerate(group):
            rec = need
            if index + 1 == len(group):
                rec = BLOCK - len(blob)
            if rec < need:
                fail("directory block overflow")
            blob += struct.pack("<IHBB", ino, rec, len(raw), typ)
            blob += raw
            blob += bytes(rec - 8 - len(raw))
        if len(blob) != BLOCK:
            fail("directory block was not filled")
        out.append(bytes(blob))
    return out


def build_dirs(node):
    if not node.is_dir:
        return
    entries = [(node.ino, 2, "."), (node.parent_ino, 2, "..")]
    for child in sorted(node.children, key=lambda item: item.name):
        entries.append((child.ino, ftype(child), child.name))
        build_dirs(child)
    node.dir_blobs = pack_dir(entries)


def link_parents(root):
    root.parent_ino = root.ino

    def walk(node):
        for child in node.children:
            child.parent_ino = node.ino
            if child.is_dir:
                walk(child)

    walk(root)


def extent_runs(blocks):
    if not blocks:
        return []
    runs = []
    logical = 0
    start = blocks[0]
    length = 1
    for block in blocks[1:]:
        if block == start + length and length < 32768:
            length += 1
        else:
            runs.append((logical, length, start))
            logical += length
            start = block
            length = 1
    runs.append((logical, length, start))
    return runs


def extent_header(entries, maximum, depth):
    return struct.pack("<HHHHI", EXTENT_MAGIC, entries, maximum, depth, 0)


def encode_extent(logical, length, physical):
    return struct.pack("<IHHI", logical, length, (physical >> 32) & 0xFFFF, physical & 0xFFFFFFFF)


def encode_index(logical, leaf):
    return struct.pack("<IIHH", logical, leaf & 0xFFFFFFFF, (leaf >> 32) & 0xFFFF, 0)


def alloc_cursor(start=0):
    cursor = start

    def alloc():
        nonlocal cursor
        while True:
            if cursor >= BLOCKS:
                fail("kano-maint is full")
            group = cursor // BPG
            local = cursor % BPG
            if local < meta_blocks(group):
                cursor = group * BPG + meta_blocks(group)
                continue
            block = cursor
            cursor += 1
            return block

    return alloc


def assign_blocks(root, alloc):
    leaves = []

    def take_file(node, count):
        node.blocks = [alloc() for _ in range(count)]

    def walk(node):
        if node.is_dir:
            take_file(node, len(node.dir_blobs))
            for child in node.children:
                walk(child)
        elif node.is_file and node.size:
            take_file(node, (node.size + BLOCK - 1) // BLOCK)
        elif node.is_link and len(node.target) > 60:
            # A long link is stored like a file. The test links are shorter.
            node.blocks = [alloc()]

    walk(root)

    def finish(node):
        blocks = node.blocks
        runs = extent_runs(blocks)
        if len(runs) <= 4:
            raw = bytearray(extent_header(len(runs), 4, 0))
            for logical, length, physical in runs:
                raw += encode_extent(logical, length, physical)
            node.iblock = bytes(raw).ljust(60, b"\0")
            if node.is_link and len(node.target) <= 60:
                node.iblock = node.target.ljust(60, b"\0")
                node.blocks = []
            return
        leaf_max = (BLOCK - 12) // 12
        chunks = [runs[index:index + leaf_max] for index in range(0, len(runs), leaf_max)]
        if len(chunks) > 4:
            fail(f"{node.name} needs more than one extent index level")
        raw = bytearray(extent_header(len(chunks), 4, 1))
        for chunk in chunks:
            leaf = alloc()
            node.leaf_blocks.append(leaf)
            body = bytearray(extent_header(len(chunk), leaf_max, 0))
            for logical, length, physical in chunk:
                body += encode_extent(logical, length, physical)
            leaves.append((leaf, bytes(body).ljust(BLOCK, b"\0")))
            raw += encode_index(chunk[0][0], leaf)
        node.iblock = bytes(raw).ljust(60, b"\0")
        if node.is_link and len(node.target) <= 60:
            node.iblock = node.target.ljust(60, b"\0")
            node.blocks = []
            node.leaf_blocks = []

    def walk_finish(node):
        if node.is_dir or node.is_file or node.is_link:
            finish(node)
        if node.is_dir:
            for child in node.children:
                walk_finish(child)

    # finish() may allocate leaf blocks, so do it after data blocks exist.
    # The short-link case clears blocks that were never allocated. Long links
    # and files keep theirs. Directories always use extents.
    walk_finish(root)
    return leaves


def inode_bytes(node):
    if node.is_link and len(node.target) <= 60:
        flags = 0
        blocks_512 = 0
        links = 1
        size = len(node.target)
    else:
        flags = EXTENTS_FL
        data_blocks = len(node.blocks)
        blocks_512 = data_blocks * (BLOCK // 512)
        links = dir_links(node) if node.is_dir else 1
        size = data_blocks * BLOCK if node.is_dir else node.size
    raw = bytearray(INODE_SIZE)
    struct.pack_into("<H", raw, 0, node.mode & 0xFFFF)
    struct.pack_into("<I", raw, 4, size & 0xFFFFFFFF)
    struct.pack_into("<H", raw, 26, links)
    struct.pack_into("<I", raw, 28, blocks_512 & 0xFFFFFFFF)
    struct.pack_into("<I", raw, 32, flags)
    raw[40:100] = node.iblock[:60]
    struct.pack_into("<I", raw, 108, (size >> 32) & 0xFFFFFFFF)
    return bytes(raw)


def superblock(group, free_blocks, free_inodes):
    sb = bytearray(1024)
    struct.pack_into("<I", sb, 0x00, GROUPS * IPG)
    struct.pack_into("<I", sb, 0x04, BLOCKS)
    struct.pack_into("<I", sb, 0x0C, free_blocks)
    struct.pack_into("<I", sb, 0x10, free_inodes)
    struct.pack_into("<I", sb, 0x18, 2)
    struct.pack_into("<I", sb, 0x1C, 2)
    struct.pack_into("<I", sb, 0x20, BPG)
    struct.pack_into("<I", sb, 0x24, BPG)
    struct.pack_into("<I", sb, 0x28, IPG)
    struct.pack_into("<H", sb, 0x36, 20)
    struct.pack_into("<H", sb, 0x38, 0xEF53)
    struct.pack_into("<H", sb, 0x3A, 1)
    struct.pack_into("<H", sb, 0x3C, 1)
    struct.pack_into("<I", sb, 0x4C, 1)
    struct.pack_into("<I", sb, 0x54, FIRST_INO)
    struct.pack_into("<H", sb, 0x58, INODE_SIZE)
    struct.pack_into("<H", sb, 0x5A, group)
    struct.pack_into("<I", sb, 0x60, FILETYPE | EXTENTS_INCOMPAT)
    struct.pack_into("<I", sb, 0x64, SPARSE_SUPER | LARGE_FILE)
    sb[0x68:0x78] = SB_UUID
    sb[0x78:0x88] = b"kano-maint\0\0\0\0\0\0"
    return bytes(sb)


def open_readable(path, mode, name):
    """Open a regular file. A missing owner-read bit is added only for the
    read. The caller restores `saved` afterwards. `mode` is the mode scan
    already stored for the inode, and this function does not replace it.
    """
    saved = stat.S_IMODE(mode)
    granted = False
    if saved & 0o400 == 0:
        try:
            os.chmod(path, saved | 0o400, follow_symlinks=False)
        except OSError as err:
            fail(f"cannot read {name}: {err.strerror}")
        granted = True
    try:
        file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as err:
        if granted:
            try:
                os.chmod(path, saved, follow_symlinks=False)
            except OSError as restore_err:
                fail(f"cannot restore the mode of {name}: {restore_err.strerror}")
        fail(f"cannot read {name}: {err.strerror}")
    return file_fd, granted, saved


def write_tree(fd, part_off, part_len, root_path):
    if part_len != BLOCKS * BLOCK:
        fail("kano-maint is not 8321499136 bytes")
    if not os.path.isdir(root_path) or os.path.islink(root_path):
        fail("maintenance root is not a directory")
    root = Node("", 0o040755)
    root.is_dir = True
    root.is_link = False
    root.is_file = False
    root.children = scan(root_path)
    assign_inodes(root)
    link_parents(root)
    build_dirs(root)
    alloc = alloc_cursor()
    leaves = assign_blocks(root, alloc)

    block_bitmaps = [bytearray(BLOCK) for _ in range(GROUPS)]
    inode_bitmaps = [bytearray(BLOCK) for _ in range(GROUPS)]
    for group in range(GROUPS):
        for local in range(meta_blocks(group)):
            set_bit(block_bitmaps[group], local)
    for index in range(FIRST_INO - 1):
        set_bit(inode_bitmaps[0], index)

    used_blocks = []

    def mark_tree(node):
        for block in node.blocks:
            used_blocks.append(block)
        for block in node.leaf_blocks:
            used_blocks.append(block)
        if node.is_dir:
            for child in node.children:
                mark_tree(child)

    mark_tree(root)
    for block in used_blocks:
        group, local = divmod(block, BPG)
        set_bit(block_bitmaps[group], local)

    inodes = {}

    def collect(node):
        inodes[node.ino] = inode_bytes(node)
        if node.is_dir:
            for child in node.children:
                collect(child)

    collect(root)
    for number in inodes:
        group = (number - 1) // IPG
        bit = (number - 1) % IPG
        set_bit(inode_bitmaps[group], bit)

    free_blocks = []
    free_inodes = []
    used_dirs = [0] * GROUPS
    for group in range(GROUPS):
        used = popcount(block_bitmaps[group])
        if used > BPG:
            fail("block bitmap overflow")
        free_blocks.append(BPG - used)
        free_inodes.append(IPG - popcount(inode_bitmaps[group]))
    for number, node_bytes in inodes.items():
        mode = struct.unpack_from("<H", node_bytes, 0)[0]
        if stat.S_ISDIR(mode):
            used_dirs[(number - 1) // IPG] += 1
    if sum(free_blocks) + sum(popcount(item) for item in block_bitmaps) != BLOCKS:
        fail("block accounting does not add up")
    # e2fsck requires every bit past the last inode in the bitmap block to be set.
    # Those bits are not inodes, so they are filled in after the free-inode count.
    pad_from = IPG // 8
    for group in range(GROUPS):
        inode_bitmaps[group][pad_from:] = b"\xff" * (BLOCK - pad_from)

    descriptors = bytearray(BLOCK)
    for group in range(GROUPS):
        _super, _gdt, block_bitmap, inode_bitmap, inode_table = group_fields(group)
        struct.pack_into(
            "<IIIHHHHIHHHH",
            descriptors,
            group * 32,
            block_bitmap,
            inode_bitmap,
            inode_table,
            free_blocks[group] & 0xFFFF,
            free_inodes[group] & 0xFFFF,
            used_dirs[group] & 0xFFFF,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    total_free_blocks = sum(free_blocks)
    total_free_inodes = sum(free_inodes)

    def put_block(block, data):
        if len(data) > BLOCK:
            fail("block is larger than 4096")
        pwrite(fd, data, part_off + block * BLOCK, "kano-maint")

    for group in SPARSE:
        super_blk, gdt_blk, _bb, _ib, _it = group_fields(group)
        # The superblock sits 1024 bytes into its block. Leave the leading zeros.
        pwrite(fd, superblock(group, total_free_blocks, total_free_inodes), part_off + super_blk * BLOCK + 1024, "the ext4 superblock")
        put_block(gdt_blk, descriptors)

    for group in range(GROUPS):
        _super, _gdt, block_bitmap, inode_bitmap, inode_table = group_fields(group)
        put_block(block_bitmap, bytes(block_bitmaps[group]))
        if any(inode_bitmaps[group]):
            put_block(inode_bitmap, bytes(inode_bitmaps[group]))
        table = {}
        for number, raw in inodes.items():
            if (number - 1) // IPG != group:
                continue
            index = (number - 1) % IPG
            table.setdefault(index // 16, bytearray(BLOCK))
            slot = (index % 16) * INODE_SIZE
            table[index // 16][slot:slot + INODE_SIZE] = raw
        for index, blob in table.items():
            put_block(inode_table + index, bytes(blob))

    def write_data(node):
        if node.is_dir:
            for block, blob in zip(node.blocks, node.dir_blobs):
                put_block(block, blob)
        elif node.is_file and node.blocks:
            file_fd, granted, saved = open_readable(node.path, node.mode, node.name)
            try:
                remaining = node.size
                file_off = 0
                for block in node.blocks:
                    take = min(remaining, BLOCK)
                    got = os.pread(file_fd, take, file_off)
                    if len(got) != take:
                        fail(f"short read of {node.name}")
                    pwrite(fd, got, part_off + block * BLOCK, node.name)
                    remaining -= take
                    file_off += take
            finally:
                os.close(file_fd)
                if granted:
                    try:
                        os.chmod(node.path, saved, follow_symlinks=False)
                    except OSError as err:
                        fail(f"cannot restore the mode of {node.name}: {err.strerror}")
        elif node.is_link and node.blocks:
            put_block(node.blocks[0], node.target.ljust(BLOCK, b"\0"))
        if node.is_dir:
            for child in node.children:
                write_data(child)

    for leaf, blob in leaves:
        put_block(leaf, blob)
    write_data(root)


def read_inode(fd, part_off, number):
    if number < 1:
        fail("bad inode")
    group = (number - 1) // IPG
    index = (number - 1) % IPG
    _super, _gdt, _bb, _ib, inode_table = group_fields(group)
    block = inode_table + index // 16
    slot = (index % 16) * INODE_SIZE
    raw = os.pread(fd, INODE_SIZE, part_off + block * BLOCK + slot)
    if len(raw) != INODE_SIZE:
        fail("short read of an inode")
    return raw


def parse_extents(fd, part_off, iblock, depth_limit=1):
    if len(iblock) < 12:
        fail("extent header is short")
    magic, entries, _maximum, depth, _gen = struct.unpack_from("<HHHHI", iblock, 0)
    if magic != EXTENT_MAGIC:
        fail("inode is not an extent file")
    if depth == 0:
        runs = []
        for index in range(entries):
            logical, length, hi, lo = struct.unpack_from("<IHHI", iblock, 12 + index * 12)
            if length == 0 or length > 32768:
                fail("bad extent length")
            runs.append((logical, length, lo | (hi << 32)))
        return runs
    if depth != 1 or depth_limit < 1:
        fail("extent tree is deeper than this reader")
    runs = []
    for index in range(entries):
        logical, lo, hi, _unused = struct.unpack_from("<IIHH", iblock, 12 + index * 12)
        leaf = lo | (hi << 32)
        blob = os.pread(fd, BLOCK, part_off + leaf * BLOCK)
        if len(blob) != BLOCK:
            fail("short read of an extent leaf")
        if logical != struct.unpack_from("<I", blob, 12)[0] and entries:
            pass
        runs.extend(parse_extents(fd, part_off, blob, 0))
    return runs


def read_blocks(fd, part_off, runs, size):
    if size > 8 << 20:
        fail("read_path refuses a file larger than 8 MiB")
    out = bytearray(size)
    filled = 0
    for logical, length, physical in runs:
        for step in range(length):
            start = (logical + step) * BLOCK
            if start >= size:
                return bytes(out)
            take = min(BLOCK, size - start)
            got = os.pread(fd, take, part_off + (physical + step) * BLOCK)
            if len(got) != take:
                fail("short read of file data")
            out[start:start + take] = got
            filled += take
    if filled < size:
        fail("file extents are shorter than the inode")
    return bytes(out)


def dir_entries(blob):
    found = []
    pos = 0
    while pos + 8 <= len(blob):
        ino, rec, name_len, typ = struct.unpack_from("<IHBB", blob, pos)
        if rec < 8 or pos + rec > len(blob):
            fail("directory entry is out of the block")
        if ino:
            name = blob[pos + 8:pos + 8 + name_len]
            found.append((ino, typ, name.decode("utf-8")))
        pos += rec
        if blob[pos:pos + 4] == b"\0\0\0\0" and pos + 8 > len(blob):
            break
    return found


def read_path(fd, part_off, part_len, relpath):
    """Return (mode, uid, gid, data_or_target, is_link) for a path in the filesystem."""
    if part_len != BLOCKS * BLOCK:
        fail("kano-maint is not 8321499136 bytes")
    magic = os.pread(fd, 2, part_off + 1024 + 0x38)
    if magic != b"\x53\xef":
        fail("kano-maint has no ext4 superblock")
    number = 2
    parts = [part for part in relpath.split("/") if part]
    is_link = False
    data = b""
    for depth, name in enumerate(parts):
        raw = read_inode(fd, part_off, number)
        mode = struct.unpack_from("<H", raw, 0)[0]
        if not stat.S_ISDIR(mode):
            fail(f"{relpath} is not a directory")
        size = struct.unpack_from("<I", raw, 4)[0]
        runs = parse_extents(fd, part_off, raw[40:100])
        blob = read_blocks(fd, part_off, runs, size)
        match = [item for item in dir_entries(blob) if item[2] == name]
        if len(match) != 1:
            fail(f"kano-maint has no {relpath}")
        number = match[0][0]
        if depth + 1 != len(parts):
            continue
    raw = read_inode(fd, part_off, number)
    mode = struct.unpack_from("<H", raw, 0)[0]
    uid = struct.unpack_from("<H", raw, 2)[0] | (struct.unpack_from("<H", raw, 120)[0] << 16)
    gid = struct.unpack_from("<H", raw, 24)[0] | (struct.unpack_from("<H", raw, 122)[0] << 16)
    size = struct.unpack_from("<I", raw, 4)[0] | (struct.unpack_from("<I", raw, 108)[0] << 32)
    flags = struct.unpack_from("<I", raw, 32)[0]
    if stat.S_ISLNK(mode) and not flags & EXTENTS_FL and size <= 60:
        data = bytes(raw[40:40 + size])
        is_link = True
    elif stat.S_ISDIR(mode):
        data = b""
    else:
        runs = parse_extents(fd, part_off, raw[40:100]) if size or flags & EXTENTS_FL else []
        data = read_blocks(fd, part_off, runs, size) if size else b""
        is_link = stat.S_ISLNK(mode)
    return mode, uid, gid, data, is_link
