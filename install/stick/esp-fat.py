#!/usr/bin/env python3
"""Read and patch a FAT12/16/32 ESP, and format the small FAT12 the tests use.

The product never imports the test GPT helper. A real Googlebook ESP is an
ordinary FAT volume, not the fixed XBOOTLDR geometry, so the reader follows
the BIOS parameter block. patch_file rewrites one existing file in place and
returns the byte ranges it changed; it does not allocate clusters.
"""
import os
import struct

__all__ = ("EspError", "format_fat12", "list_files", "patch_file", "read_file")


class EspError(Exception):
    pass


def fail(msg):
    raise EspError(msg)


def pread(fd, n, off, what):
    got = os.pread(fd, n, off)
    if len(got) != n:
        fail(f"short read of {what}")
    return got


def pwrite(fd, data, off, what):
    wrote = os.pwrite(fd, data, off)
    if wrote != len(data):
        fail(f"short write of {what}")


def sfn_checksum(sfn):
    total = 0
    for byte in sfn:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def lfn_entries(name, checksum):
    chars = [ord(ch) for ch in name]
    if any(ch > 0xFFFF for ch in chars):
        fail(f"{name} is not a FAT name")
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
    struct.pack_into("<H", entry, 20, (cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", entry, 26, cluster & 0xFFFF)
    struct.pack_into("<I", entry, 28, size)
    return bytes(entry)


def sfn_name(sfn, nt):
    """8.3 directory entry with no long name. mkfs.fat writes these for names that fit."""
    if len(sfn) != 11:
        fail("ESP short name is the wrong length")
    raw = bytearray(sfn)
    if raw[0] == 0x05:
        raw[0] = 0xE5
    try:
        text = bytes(raw).decode("ascii")
    except UnicodeDecodeError:
        fail("ESP short name is not ASCII")
    base, ext = text[:8].rstrip(" "), text[8:].rstrip(" ")
    if not base or " " in base or " " in ext:
        fail("ESP short name is not a file name")
    if nt & 0x08:
        base = base.lower()
    if nt & 0x10:
        ext = ext.lower()
    return base + "." + ext if ext else base


def lfn_text(entries, sfn):
    expect = sfn_checksum(sfn)
    ordered = sorted(entries, key=lambda entry: entry[0] & 0x1F)
    seqs = [entry[0] & 0x1F for entry in ordered]
    if seqs != list(range(1, len(ordered) + 1)) or not (ordered[-1][0] & 0x40):
        fail("broken long file name")
    chars = []
    for entry in ordered:
        if entry[11] != 0x0F or entry[13] != expect or entry[26:28] != b"\x00\x00":
            fail("long file name checksum mismatch")
        raw = entry[1:11] + entry[14:26] + entry[28:32]
        for offset in range(0, 26, 2):
            unit = struct.unpack_from("<H", raw, offset)[0]
            if unit == 0:
                return "".join(chars)
            if unit == 0xFFFF:
                fail("padding inside a long file name")
            chars.append(chr(unit))
    return "".join(chars)


def clean_fat(text):
    out = []
    for ch in text.upper():
        if ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in "$%'-_@~`!(){}^#&":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def split_name(name):
    if "." in name:
        return name.rsplit(".", 1)
    return name, ""


def assign_sfns(nodes):
    used = set()
    pending = []
    for node in nodes:
        base, ext = split_name(node["name"])
        body, ending = clean_fat(base), clean_fat(ext)[:3]
        if len(body) <= 8 and len(ext) <= 3 and len(clean_fat(ext)) <= 3:
            sfn = (body.ljust(8) + ending.ljust(3)).encode("ascii")
            if len(sfn) == 11 and sfn[0] not in (0x00, 0x20) and sfn not in used:
                used.add(sfn)
                node["sfn"] = sfn
                continue
        pending.append((body, ending, node))
    groups = {}
    for body, ending, node in pending:
        groups.setdefault((body[:6], ending), []).append((body, node))
    for (stem, ending), group in groups.items():
        group.sort(key=lambda item: (len(item[0]), item[1]["name"]))
        number = 1
        for _body, node in group:
            while True:
                tail = f"~{number}"
                number += 1
                if number > 1000:
                    fail("too many similar FAT names")
                cut = 8 - len(tail)
                sfn = (stem[:cut] + tail).ljust(8).encode("ascii") + ending.ljust(3).encode("ascii")
                if len(sfn) == 11 and sfn not in used:
                    used.add(sfn)
                    node["sfn"] = sfn
                    break


def parse_boot(boot, length):
    if len(boot) != 512 or boot[510:512] != b"\x55\xaa":
        fail("ESP has no FAT boot sector")
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    nfats = boot[16]
    root_ents = struct.unpack_from("<H", boot, 17)[0]
    tot16 = struct.unpack_from("<H", boot, 19)[0]
    fatsz16 = struct.unpack_from("<H", boot, 22)[0]
    tot32 = struct.unpack_from("<I", boot, 32)[0]
    if bps != 512 or spc == 0 or nfats == 0 or reserved == 0:
        fail("ESP FAT geometry is not supported")
    tot = tot16 or tot32
    fatsz = fatsz16 or struct.unpack_from("<I", boot, 36)[0]
    if tot == 0 or fatsz == 0 or tot * bps > length:
        fail("ESP FAT geometry is not supported")
    root_bytes = root_ents * 32
    root_sectors = (root_bytes + bps - 1) // bps
    data_sectors = tot - (reserved + nfats * fatsz + root_sectors)
    if data_sectors < spc:
        fail("ESP FAT has no data clusters")
    clusters = data_sectors // spc
    if fatsz16 == 0 and root_ents == 0:
        kind = 32
    elif clusters < 4085:
        kind = 12
    elif clusters < 65525:
        kind = 16
    else:
        kind = 32
    fat_off = reserved * bps
    data_off = (reserved + nfats * fatsz + root_sectors) * bps
    if kind == 32:
        root_cluster = struct.unpack_from("<I", boot, 44)[0]
        if root_cluster < 2:
            fail("ESP FAT32 root cluster is missing")
        eoc = 0x0FFFFFF8
    else:
        root_cluster = None
        eoc = 0xFF8 if kind == 12 else 0xFFF8
    return {
        "bps": bps,
        "spc": spc,
        "nfats": nfats,
        "fatsz": fatsz,
        "kind": kind,
        "fat_off": fat_off,
        "root_off": fat_off + nfats * fatsz * bps,
        "root_bytes": root_bytes,
        "root_cluster": root_cluster,
        "data_off": data_off,
        "cluster_bytes": spc * bps,
        "clusters": clusters,
        "eoc": eoc,
        "limit": tot * bps,
    }


def fat_value(fat, kind, index):
    if kind == 12:
        off = index + index // 2
        if off + 1 >= len(fat):
            fail("ESP FAT entry is outside the table")
        raw = fat[off] | (fat[off + 1] << 8)
        return (raw >> 4) & 0x0FFF if index & 1 else raw & 0x0FFF
    if kind == 16:
        return struct.unpack_from("<H", fat, index * 2)[0]
    return struct.unpack_from("<I", fat, index * 4)[0] & 0x0FFFFFFF


def set_fat12(fat, index, value):
    value &= 0x0FFF
    off = index + index // 2
    if index & 1:
        fat[off] = (fat[off] & 0x0F) | ((value << 4) & 0xF0)
        fat[off + 1] = (value >> 4) & 0xFF
    else:
        fat[off] = value & 0xFF
        fat[off + 1] = (fat[off + 1] & 0xF0) | ((value >> 8) & 0x0F)


def cluster_chain(fat, geo, start):
    kind = geo["kind"]
    eoc = geo["eoc"]
    current = start
    seen = []
    while current < eoc:
        if current < 2 or current >= geo["clusters"] + 2 or current in seen or len(seen) > geo["clusters"]:
            fail("ESP cluster chain is broken")
        seen.append(current)
        nxt = fat_value(fat, kind, current)
        if nxt >= eoc:
            break
        if nxt == 0:
            fail("ESP cluster chain is broken")
        current = nxt
    return seen


def load(fd, part_off, length):
    boot = pread(fd, 512, part_off, "the ESP boot sector")
    geo = parse_boot(boot, length)
    fat_bytes = geo["fatsz"] * geo["bps"]
    fat = pread(fd, fat_bytes, part_off + geo["fat_off"], "the ESP FAT")
    return geo, fat


def region_entries(blob, base):
    found = []
    pending = []
    for index in range(0, len(blob), 32):
        entry = blob[index:index + 32]
        if len(entry) < 32 or entry[0] == 0:
            break
        if entry[0] == 0xE5:
            pending = []
            continue
        if entry[11] == 0x0F:
            pending.append(entry)
            continue
        attr = entry[11]
        if attr & 0x08:
            pending = []
            continue
        sfn = bytes(entry[:11])
        if sfn[:1] == b"." and not pending:
            name = sfn.decode("ascii").rstrip(" ")
        elif not pending:
            name = sfn_name(sfn, entry[12])
        else:
            name = lfn_text(pending, sfn)
            pending = []
        cluster = struct.unpack_from("<H", entry, 26)[0] | (struct.unpack_from("<H", entry, 20)[0] << 16)
        size = struct.unpack_from("<I", entry, 28)[0]
        found.append({
            "name": name,
            "sfn": sfn,
            "cluster": cluster,
            "size": size,
            "attr": attr,
            "entry_off": base + index,
            "is_dir": bool(attr & 0x10),
        })
    if pending:
        fail("ESP long name without a file")
    return found


def directory_regions(fd, part_off, geo, fat, cluster):
    if cluster is None:
        blob = pread(fd, geo["root_bytes"], part_off + geo["root_off"], "the ESP root directory")
        return [(part_off + geo["root_off"], blob)]
    regions = []
    for number in cluster_chain(fat, geo, cluster):
        off = part_off + geo["data_off"] + (number - 2) * geo["cluster_bytes"]
        blob = pread(fd, geo["cluster_bytes"], off, "an ESP directory")
        regions.append((off, blob))
    return regions


def list_dir(fd, part_off, geo, fat, cluster):
    found = []
    for base, blob in directory_regions(fd, part_off, geo, fat, cluster):
        found.extend(region_entries(blob, base))
    return found


def lookup(fd, part_off, length, relpath):
    geo, fat = load(fd, part_off, length)
    parts = [part for part in relpath.split("/") if part]
    if not parts:
        fail("ESP path is empty")
    entries = list_dir(fd, part_off, geo, fat, geo["root_cluster"])
    current = None
    for depth, name in enumerate(parts):
        match = [entry for entry in entries if entry["name"] == name]
        if len(match) != 1:
            fail(f"ESP has no {relpath}")
        current = match[0]
        if depth + 1 == len(parts):
            return geo, fat, current
        if not current["is_dir"]:
            fail(f"ESP path {relpath} is not a directory")
        entries = list_dir(fd, part_off, geo, fat, current["cluster"])
    fail(f"ESP has no {relpath}")


def read_file_at(fd, part_off, geo, fat, entry):
    if entry["is_dir"]:
        fail(f"{entry['name']} is a directory")
    if entry["cluster"] < 2 and entry["size"]:
        fail(f"{entry['name']} has no cluster")
    left = entry["size"]
    if left == 0:
        return b""
    out = bytearray()
    for number in cluster_chain(fat, geo, entry["cluster"]):
        if not left:
            break
        take = min(left, geo["cluster_bytes"])
        off = part_off + geo["data_off"] + (number - 2) * geo["cluster_bytes"]
        out += pread(fd, take, off, entry["name"])
        left -= take
    if left:
        fail(f"{entry['name']} is shorter than its directory entry")
    return bytes(out)


def read_file(fd, part_off, length, relpath):
    geo, fat, entry = lookup(fd, part_off, length, relpath)
    return read_file_at(fd, part_off, geo, fat, entry)


def list_files(fd, part_off, length, reldir):
    geo, fat = load(fd, part_off, length)
    entries = list_dir(fd, part_off, geo, fat, geo["root_cluster"])
    if reldir:
        for name in [part for part in reldir.split("/") if part]:
            match = [entry for entry in entries if entry["name"] == name and entry["is_dir"]]
            if len(match) != 1:
                fail(f"ESP has no {reldir}")
            entries = list_dir(fd, part_off, geo, fat, match[0]["cluster"])
    files = {}
    for entry in entries:
        if entry["name"] in (".", "..") or entry["is_dir"]:
            continue
        files[entry["name"]] = read_file_at(fd, part_off, geo, fat, entry)
    return files


def patch_file(fd, part_off, length, relpath, data):
    """Write data over an existing file. Return (absolute offset, length) ranges changed."""
    if not isinstance(data, (bytes, bytearray)):
        fail("ESP file is not bytes")
    geo, fat, entry = lookup(fd, part_off, length, relpath)
    if entry["is_dir"]:
        fail(f"{relpath} is a directory")
    chain = cluster_chain(fat, geo, entry["cluster"]) if entry["cluster"] >= 2 else []
    capacity = len(chain) * geo["cluster_bytes"]
    if len(data) > capacity:
        fail(f"{relpath} does not fit in its ESP clusters")
    changed = []
    left = len(data)
    offset = 0
    for number in chain:
        if not left:
            break
        take = min(left, geo["cluster_bytes"])
        at = part_off + geo["data_off"] + (number - 2) * geo["cluster_bytes"]
        pwrite(fd, data[offset:offset + take], at, relpath)
        changed.append((at, take))
        offset += take
        left -= take
    if entry["size"] != len(data):
        at = entry["entry_off"] + 28
        pwrite(fd, struct.pack("<I", len(data)), at, relpath)
        changed.append((at, 4))
    return changed


def format_fat12(sectors, files):
    """Build a FAT12 partition image. files maps slash-separated paths to bytes."""
    if sectors < 64 or sectors > 65535:
        fail("FAT12 image size is not supported")
    bps, spc, reserved, nfats, root_entries = 512, 1, 1, 2, 16
    root_sectors = ((root_entries * 32) + bps - 1) // bps
    fat_sectors = 1
    clusters = 0
    while True:
        data_sectors = sectors - reserved - nfats * fat_sectors - root_sectors
        if data_sectors <= 0:
            fail("FAT12 partition is too small")
        clusters = data_sectors // spc
        if clusters >= 4085:
            fail("partition is too big for FAT12")
        need = ((clusters + 2) * 3 + 1) // 2
        need_sectors = (need + bps - 1) // bps
        if need_sectors <= fat_sectors:
            break
        fat_sectors = need_sectors
    image = bytearray(sectors * bps)
    boot = bytearray(512)
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", boot, 11, bps)
    boot[13] = spc
    struct.pack_into("<H", boot, 14, reserved)
    boot[16] = nfats
    struct.pack_into("<H", boot, 17, root_entries)
    struct.pack_into("<H", boot, 19, sectors)
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 22, fat_sectors)
    struct.pack_into("<H", boot, 24, 63)
    struct.pack_into("<H", boot, 26, 255)
    boot[38] = 0x29
    boot[43:54] = b"EFI-SYSTEM "
    boot[54:62] = b"FAT12   "
    boot[510:512] = b"\x55\xaa"
    image[0:512] = boot
    fat = bytearray(fat_sectors * bps)
    set_fat12(fat, 0, 0xFF8)
    set_fat12(fat, 1, 0xFFF)
    nxt = 2

    def take(count):
        nonlocal nxt
        if nxt + count - 1 >= clusters + 2:
            fail("FAT12 files do not fit")
        chain = list(range(nxt, nxt + count))
        for cluster in chain[:-1]:
            set_fat12(fat, cluster, cluster + 1)
        set_fat12(fat, chain[-1], 0xFFF)
        nxt = chain[-1] + 1
        return chain

    def build(paths):
        root = {"name": "", "is_dir": True, "data": b"", "children": []}
        for path, data in sorted(paths.items()):
            if not isinstance(data, (bytes, bytearray)):
                fail(f"{path} is not bytes")
            node = root
            parts = [part for part in path.split("/") if part]
            if not parts:
                fail("empty FAT path")
            for part in parts[:-1]:
                child = next((item for item in node["children"] if item["name"] == part), None)
                if child is None:
                    child = {"name": part, "is_dir": True, "data": b"", "children": []}
                    node["children"].append(child)
                elif not child["is_dir"]:
                    fail(f"{path} is both a file and a directory")
                node = child
            name = parts[-1]
            if any(item["name"] == name for item in node["children"]):
                fail(f"duplicate FAT path {path}")
            node["children"].append({"name": name, "is_dir": False, "data": bytes(data), "children": []})
        return root

    def alloc(node):
        assign_sfns(node["children"])
        for child in node["children"]:
            if child["is_dir"]:
                child["chain"] = take(1)
                alloc(child)
            else:
                count = max(1, (len(child["data"]) + spc * bps - 1) // (spc * bps)) if child["data"] else 0
                child["chain"] = take(count) if count else []

    def dir_bytes(node, self_cluster, parent_cluster, is_root):
        blob = bytearray()
        if not is_root:
            blob += sfn_entry(b".          ", self_cluster, 0, 0x10)
            blob += sfn_entry(b"..         ", parent_cluster, 0, 0x10)
        for child in node["children"]:
            blob += b"".join(lfn_entries(child["name"], sfn_checksum(child["sfn"])))
            cluster = child["chain"][0] if child["chain"] else 0
            attr = 0x10 if child["is_dir"] else 0x20
            size = 0 if child["is_dir"] else len(child["data"])
            blob += sfn_entry(child["sfn"], cluster, size, attr)
        room = root_entries * 32 if is_root else spc * bps
        if len(blob) > room:
            fail("FAT12 directory does not fit")
        return bytes(blob).ljust(room, b"\0")

    root = build(files)
    alloc(root)
    data_off = (reserved + nfats * fat_sectors + root_sectors) * bps

    def place(node, self_cluster, parent_cluster, is_root):
        blob = dir_bytes(node, self_cluster, parent_cluster, is_root)
        if is_root:
            at = (reserved + nfats * fat_sectors) * bps
            image[at:at + len(blob)] = blob
        else:
            at = data_off + (self_cluster - 2) * spc * bps
            image[at:at + len(blob)] = blob
        for child in node["children"]:
            if child["is_dir"]:
                place(child, child["chain"][0], self_cluster if not is_root else 0, False)
            else:
                data = child["data"]
                for cluster in child["chain"]:
                    take_n = min(len(data), spc * bps)
                    at = data_off + (cluster - 2) * spc * bps
                    image[at:at + take_n] = data[:take_n]
                    data = data[take_n:]

    place(root, 0, 0, True)
    for copy in range(nfats):
        at = (reserved + copy * fat_sectors) * bps
        image[at:at + len(fat)] = fat
    return bytes(image)
