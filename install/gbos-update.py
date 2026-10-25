#!/usr/bin/env python3
"""Update an installed Googlebook (USB stick or internal disk) to a newer release and keep the user's data.

The disk has two slots, a and b, the way Android lays them out: the dynamic partitions <name>_a / <name>_b in super
(one LP metadata describes both), and boot_x, init_boot_x, vendor_boot_x, vbmeta_x, pvmfw_x, recovery_x, ota_recovery_x.
A release image fills slot a. An update writes the new release into the slot that is NOT started by default, while
the running one stays untouched, then points the boot menu at it:
  - the dynamic partitions go to free space in super (the old ones of that slot are dropped), the physical slot
    partitions are overwritten, the LP metadata is rewritten with the other slot's extents unchanged;
  - the ESP gets /gbos/<slot>/initrd.img (the release's ramdisk, lz4, plus its bootconfig with
    androidboot.slot_suffix=_<slot> and this disk's boot_a GUID) and, when the kernel differs from every kernel already
    there, /gbos/<slot>/kernel.efi;
  - the release's entries (lapis-quiet.conf, lapis-debug.conf; releases before v0.2 also lapis.conf) start the new slot;
    the running slot's quiet and serial-debug entries become prev-<entry> "... (previous version)" when the ESP has room
    for both slots' files, and a running-slot entry the release no longer has (the plain lapis.conf) is removed.
Never written: userdata, metadata, misc, frp_persist, desktop_security_persist, desktop_security_storage, the GPT, and
every partition that is not a slot partition, super or the ESP. Nothing else of the disk changes.

Refused before anything is written: a disk whose partition table differs from the release's (except where userdata
starts), another AVB key, another model, another KeyMint/Gatekeeper selection (the lock screen and keystore keys
depend on them), an older Google build, too little room in super, a release that fails its checksums, a FAT whose
size field reaches past its partition.

Order of the writes (each step is reported; a rerun after a stop finishes the same target slot):
  state file -> [installer stick: its internal-disk entries all start the running slot from the stick's own copy, and
  nothing on the stick points at the target slot] -> the target slot's old entries and files removed -> dynamic
  partitions -> physical slot partitions -> LP metadata -> new boot files -> [installer stick: its menu starts the new
  slot, "(previous version)" the running one] -> the disk's menu switched -> state file removed.
When the ESP cannot hold both versions' boot files (on the KANO's 64 MiB ESP: every Google build with a new kernel), the
old ones are removed before the new ones are written (--without-fallback, or an installer stick that keeps the running
version): from then until the menu is switched, the disk's own menu cannot start either version, and a stop there says
so; the rerun finishes it. (Only when the new files would still fit next to the old ones but two previous-version
entries would not, i.e. within a few KiB, the new files go first and the old ones after the switch.)

usage:
  gbos-update.py inspect  <disk>
  gbos-update.py plan     <disk> <source> [--mode stick|nvme]
  gbos-update.py apply    <disk> <source> --mode stick|nvme [--rdev MAJ:MIN [--syspath P] [--bytes N]]
                          [--without-fallback] [--stick-boot <XBOOTLDR device or image@@offset>]
  gbos-update.py rollback <disk> [--rdev MAJ:MIN [--syspath P] [--bytes N]] [--stick-boot ...] [--allow-build-change]
  <source>: --image <release.img> (checked against <release.img>.sha256)
         or --pristine <dir> --super <device holding the release's super> (the installer stick's data)
<disk> may be a regular file (tests). On a block device every write goes through one O_EXCL descriptor checked
against --rdev (and, when given, the realpath of /sys/dev/block/MAJ:MIN and the size in bytes); every written range of
a partition is read back (page cache dropped) and compared by SHA-256, every FAT file is read back after mcopy.
Exit: 0 done, 1 failed after writing started (run the same command again), 2 refused (nothing written).
"""
import hashlib, os, re, shutil, stat, struct, subprocess, sys, tempfile, uuid, zlib

SECTOR = 512
MIB = 1 << 20
SLOT_PHYS = ("boot", "init_boot", "vendor_boot", "vbmeta", "pvmfw", "recovery", "ota_recovery")
NEVER = ("userdata", "metadata", "misc", "frp_persist", "desktop_security_persist", "desktop_security_storage")
ESP = "EFI-SYSTEM"
ENTRIES = ("lapis-quiet.conf", "lapis-debug.conf", "lapis.conf")    # every entry name a release may have
REQUIRED_ENTRIES = ("lapis-quiet.conf", "lapis-debug.conf")           # lapis.conf (plain) was dropped in v0.2
PREV = "prev-"
PREV_TITLE = " (previous version)"
STATE = "/gbos/update-state.txt"
LIFT = "initcall_blacklist=nvme_init,mei_me_driver_init"
LZ4_LEGACY = b"\x02\x21\x4c\x18"
BCMAGIC = b"#BOOTCONFIG\n"
FAIL_AT = os.environ.get("GBOS_UPDATE_TEST_FAIL_AT", "")   # test hook: stop after the named step (exit 1)
WRITING = []                                                # set at the first change to the disk
WINDOW = []                                                 # set while the disk's own menu cannot start any version
STICK = []                                                  # set when the installer stick's menu is part of the change
HOLD = []                                                   # descriptors held O_EXCL until the end


class Refuse(Exception):
    """Nothing was written."""


class Failed(Exception):
    """Writing had started."""


def say(*a):
    print(*a, flush=True)


def run(cmd, data=None, text=False):
    r = subprocess.run(cmd, input=data, capture_output=True, text=text, env=dict(os.environ, MTOOLS_SKIP_CHECK="1"))
    if r.returncode:
        err = r.stderr if text else r.stderr.decode(errors="replace")
        raise RuntimeError(f"{' '.join(map(str, cmd))} failed: {err.strip()[:300]}")
    return r.stdout


# ------------------------------------------------------------------ disks

class Disk:
    """A disk or disk image. write=True: block devices are opened O_EXCL and checked against rdev."""

    def __init__(self, path, write=False, rdev=None, syspath=None, nbytes=None):
        self.path = os.path.realpath(path)
        st = os.stat(self.path)
        self.blk = stat.S_ISBLK(st.st_mode)
        if not self.blk and not stat.S_ISREG(st.st_mode):
            raise Refuse(f"{path} is neither a block device nor a regular file")
        flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
        if write and self.blk:
            flags |= os.O_EXCL
        try:
            self.fd = os.open(self.path, flags)
        except OSError as e:
            raise Refuse(f"cannot open {self.path}{' exclusively' if write and self.blk else ''}: {e.strerror}")
        fst = os.fstat(self.fd)
        if self.blk and rdev is not None and f"{os.major(fst.st_rdev)}:{os.minor(fst.st_rdev)}" != rdev:
            raise Refuse(f"{self.path} is {os.major(fst.st_rdev)}:{os.minor(fst.st_rdev)}, not the confirmed {rdev}")
        if write and self.blk and rdev is None:
            raise Refuse("a block device is only written with --rdev (its confirmed major:minor)")
        if self.blk and syspath is not None:
            sp = os.path.realpath(f"/sys/dev/block/{os.major(fst.st_rdev)}:{os.minor(fst.st_rdev)}")
            if sp != syspath:
                raise Refuse(f"{self.path} is {sp} in sysfs, not the confirmed {syspath}")
        self.size = os.lseek(self.fd, 0, os.SEEK_END)
        if nbytes is not None and self.size != int(nbytes):
            raise Refuse(f"{self.path} has {self.size} bytes, not the confirmed {nbytes}")
        self.write = write
        self.forbidden = []          # (start, end, why): byte ranges no update may write

    def guard(self, ranges):
        self.forbidden = list(ranges)

    def pread(self, off, n):
        out = bytearray()
        while len(out) < n:
            b = os.pread(self.fd, min(n - len(out), 16 << 20), off + len(out))
            if not b:
                raise RuntimeError(f"{self.path}: short read at {off + len(out)}")
            out += b
        return bytes(out)

    def pwrite(self, off, data):
        assert self.write
        for a, b, why in self.forbidden:
            if off < b and off + len(data) > a:
                raise Failed(f"refusing to write {off}+{len(data)}: it overlaps {why}")
        mv = memoryview(data)
        while mv:
            n = os.pwrite(self.fd, mv, off)
            off += n
            mv = mv[n:]

    def sync_drop(self):
        os.fsync(self.fd)
        if self.blk:
            import fcntl
            fcntl.ioctl(self.fd, 0x1261)  # BLKFLSBUF: the next reads come from the disk
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)

    def sha(self, off, n):
        h = hashlib.sha256()
        left = n
        while left:
            b = self.pread(off + n - left, min(left, 16 << 20))
            h.update(b)
            left -= len(b)
        return h.hexdigest()

    def vol(self, off):
        """mtools image spec of a FAT file system at byte offset off."""
        return f"{self.path}@@{off}"


def gpt_read(disk):
    """Primary GPT of a Disk: {name: dict(n, start, len, type, guid, attrs)}; checks CRCs and names."""
    mbr, hd = disk.pread(0, 512), disk.pread(512, 512)
    if mbr[510:512] != b"\x55\xaa" or mbr[450] != 0xEE:
        raise Refuse(f"{disk.path}: no protective MBR")
    if hd[:8] != b"EFI PART":
        raise Refuse(f"{disk.path}: no GPT")
    hsize, hcrc = struct.unpack_from("<II", hd, 12)
    if not 92 <= hsize <= 512 or zlib.crc32(hd[:16] + bytes(4) + hd[20:hsize]) != hcrc:
        raise Refuse(f"{disk.path}: GPT header CRC is wrong")
    lba, n, esz, ecrc = struct.unpack_from("<QIII", hd, 72)
    if esz < 128 or n * esz > MIB:
        raise Refuse(f"{disk.path}: odd GPT entry table")
    t = disk.pread(lba * 512, n * esz)
    if zlib.crc32(t) != ecrc:
        raise Refuse(f"{disk.path}: GPT entries CRC is wrong")
    out = {}
    for i in range(n):
        e = t[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16):
            continue
        first, last, attrs = struct.unpack_from("<QQQ", e, 32)
        name = e[56:128].decode("utf-16le").rstrip("\0")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,36}", name) or name in out:
            raise Refuse(f"{disk.path}: partition {i + 1} has an odd or repeated name {name!r}")
        out[name] = dict(n=i + 1, start=first * 512, len=(last + 1 - first) * 512, type=str(uuid.UUID(bytes_le=e[:16])),
                         guid=str(uuid.UUID(bytes_le=e[16:32])), attrs=attrs)
    for need in ("super", ESP, "boot_a", "userdata") + tuple(f"{p}_{s}" for p in SLOT_PHYS for s in "ab"):
        if need not in out:
            raise Refuse(f"{disk.path}: no {need} partition (not a Googlebook disk of this layout)")
    return out


# ------------------------------------------------------------------ LP metadata (super)

GEO_MAGIC, HDR_MAGIC = 0x616C4467, 0x414C5030
RESERVED = 4096
GEO_SIZE = 4096


class LP:
    """LP metadata of one super: geometry, header template, groups and block-device tables kept as bytes,
    partitions as dicts with explicit extents (sector, count) relative to super."""

    @staticmethod
    def geometry(raw):
        magic, ssize = struct.unpack_from("<II", raw, 0)
        if magic != GEO_MAGIC or ssize < 52:
            raise Refuse("super: no LP geometry")
        g = bytearray(raw[:ssize]); csum = bytes(g[8:40]); g[8:40] = bytes(32)
        if hashlib.sha256(g).digest() != csum:
            raise Refuse("super: LP geometry checksum")
        mx, slots, lbs = struct.unpack_from("<III", raw, 40)
        return mx, slots, lbs

    @classmethod
    def read(cls, rd, size):
        """rd(off, n) reads super; size = super bytes. Reads metadata slot 0 (primary, else backup) and checks the
        other slots: a slot that differs means an interrupted write (reported, the newest valid one is not guessed)."""
        g1 = rd(RESERVED, GEO_SIZE)
        g2 = rd(RESERVED + GEO_SIZE, GEO_SIZE)
        mx, slots, lbs = cls.geometry(g1)
        if cls.geometry(g2) != (mx, slots, lbs):
            raise Refuse("super: the two LP geometry copies differ")
        copies = {}
        for s in range(slots):
            for kind, base in (("primary", RESERVED + 2 * GEO_SIZE), ("backup", RESERVED + 2 * GEO_SIZE + slots * mx)):
                raw = rd(base + s * mx, mx)
                try:
                    copies[(s, kind)] = cls.parse(raw)
                except Refuse as e:
                    copies[(s, kind)] = e
        good = [v for v in copies.values() if not isinstance(v, Refuse)]
        if not good:
            raise Refuse("super: no valid LP metadata copy")
        first = copies.get((0, "primary"))
        if isinstance(first, Refuse):
            first = copies.get((0, "backup"))
        if isinstance(first, Refuse):
            raise Refuse("super: slot 0 LP metadata is damaged in both copies")
        lp = first
        lp.mx, lp.slots, lp.lbs, lp.size = mx, slots, lbs, size
        lp.consistent = all(not isinstance(v, Refuse) and v.blob == first.blob for v in copies.values())
        lp.validate()
        return lp

    @classmethod
    def parse(cls, raw):
        magic, major, minor, hsz = struct.unpack_from("<IHHI", raw, 0)
        if magic != HDR_MAGIC or not 128 <= hsz <= 512:
            raise Refuse("super: no LP header")
        h = bytearray(raw[:hsz]); csum = bytes(h[12:44]); h[12:44] = bytes(32)
        if hashlib.sha256(h).digest() != csum:
            raise Refuse("super: LP header checksum")
        tsize = struct.unpack_from("<I", raw, 44)[0]
        t = raw[hsz:hsz + tsize]
        if len(t) != tsize or hashlib.sha256(t).digest() != raw[48:80]:
            raise Refuse("super: LP tables checksum")
        self = cls()
        self.header = bytes(raw[:hsz]); self.blob = bytes(raw[:hsz + tsize])
        d = lambda o: struct.unpack_from("<III", raw, o)
        pt, ex, gr, bd = d(80), d(92), d(104), d(116)
        if pt[2] != 52 or ex[2] != 24 or gr[2] != 48 or bd[2] != 64:
            raise Refuse("super: unknown LP table entry sizes")
        for off, num, esz in (pt, ex, gr, bd):
            if off + num * esz > tsize:
                raise Refuse("super: LP table outside the tables")
        self.groups_raw = t[gr[0]:gr[0] + gr[1] * 48]
        self.bdevs_raw = t[bd[0]:bd[0] + bd[1] * 64]
        self.groups = [t[gr[0] + i * 48:gr[0] + i * 48 + 36].split(b"\0")[0].decode() for i in range(gr[1])]
        self.group_max = [struct.unpack_from("<Q", t, gr[0] + i * 48 + 40)[0] for i in range(gr[1])]
        exts = [struct.unpack_from("<QIQI", t, ex[0] + i * 24) for i in range(ex[1])]
        self.parts = []
        for i in range(pt[1]):
            o = pt[0] + i * 52
            name = t[o:o + 36].split(b"\0")[0].decode()
            attrs, fe, ne, gi = struct.unpack_from("<IIII", t, o + 36)
            if fe + ne > len(exts) or gi >= len(self.groups):
                raise Refuse(f"super: LP partition {name} points outside the tables")
            el = []
            for j in range(fe, fe + ne):
                nsec, ttype, data, src = exts[j]
                if ttype != 0 or src != 0:
                    raise Refuse(f"super: LP partition {name} has a non-linear extent")
                el.append((data, nsec))
            self.parts.append(dict(name=name, attrs=attrs, group=gi, extents=el))
        if bd[1] != 1:
            raise Refuse("super: expected one LP block device")
        first, align, aoff, bsize = struct.unpack_from("<QIIQ", self.bdevs_raw, 0)
        self.first, self.align, self.bsize = first, align, bsize
        return self

    def validate(self):
        if self.align % SECTOR or self.first * SECTOR < RESERVED + 2 * GEO_SIZE + 2 * self.slots * self.mx or \
                self.bsize != self.size:
            raise Refuse("super: LP block device does not match the super partition")
        spans = sorted((a, a + n, p["name"]) for p in self.parts for a, n in p["extents"])
        for a, b, name in spans:
            if a < self.first or b * SECTOR > self.size:
                raise Refuse(f"super: extent of {name} outside the data area")
        # extents of the two slots may not overlap; within one LP table, overlapping extents mean damage
        for (a1, b1, n1), (a2, b2, n2) in zip(spans, spans[1:]):
            if a2 < b1:
                raise Refuse(f"super: extents of {n1} and {n2} overlap")

    def part(self, name):
        return next((p for p in self.parts if p["name"] == name), None)

    def size_of(self, name):
        p = self.part(name)
        return sum(n for _, n in p["extents"]) * SECTOR if p else 0

    def serialize(self):
        names = [p["name"] for p in self.parts]
        if len(set(names)) != len(names):
            raise RuntimeError("LP: repeated partition name")
        ptab, etab = bytearray(), bytearray()
        for p in self.parts:
            nm = p["name"].encode()
            if len(nm) > 35:
                raise RuntimeError(f"LP: partition name too long: {p['name']}")
            ptab += nm.ljust(36, b"\0") + struct.pack("<IIII", p["attrs"], len(etab) // 24, len(p["extents"]), p["group"])
            for a, n in p["extents"]:
                etab += struct.pack("<QIQI", n, 0, a, 0)
        tables = bytes(ptab) + bytes(etab) + self.groups_raw + self.bdevs_raw
        h = bytearray(self.header)
        struct.pack_into("<I", h, 44, len(tables))
        h[48:80] = hashlib.sha256(tables).digest()
        o = 0
        for at, num, esz in ((80, len(self.parts), 52), (92, len(etab) // 24, 24), (104, len(self.groups), 48),
                             (116, len(self.bdevs_raw) // 64, 64)):
            struct.pack_into("<III", h, at, o, num, esz)
            o += num * esz
        h[12:44] = bytes(32)
        h[12:44] = hashlib.sha256(h).digest()
        blob = bytes(h) + tables
        if len(blob) > self.mx:
            raise RuntimeError("LP: metadata larger than a metadata slot")
        return blob

    def slot_offsets(self):
        """(super-relative byte offset) of every metadata copy, in the order they are rewritten: the other slots'
        backup then primary, slot 0 (the one the running slot a may read) last."""
        p0 = RESERVED + 2 * GEO_SIZE
        b0 = p0 + self.slots * self.mx
        order = list(range(1, self.slots)) + [0]
        return [off for s in order for off in (b0 + s * self.mx, p0 + s * self.mx)]


def allocate(lp, sizes, keep):
    """Linear extents for sizes {name: bytes} in super, avoiding every extent of the partitions in keep and the
    metadata area; 1 MiB aligned, largest first, one extent when a gap is big enough."""
    al = max(lp.align // SECTOR, 1)
    used = sorted((a, a + n) for p in lp.parts if p["name"] in keep for a, n in p["extents"])
    free, pos = [], lp.first
    for a, b in used + [(lp.size // SECTOR, lp.size // SECTOR)]:
        start = -(-pos // al) * al
        if a > start:
            free.append([start, a])
        pos = max(pos, b)
    out = {}
    for name, nbytes in sorted(sizes.items(), key=lambda kv: -kv[1]):
        need = -(-nbytes // SECTOR)      # an extent only has to START aligned; its length need not be a multiple
        gap = next((g for g in free if g[1] - g[0] >= need), None)
        ext = []
        if gap:
            ext.append((gap[0], need))
            gap[0] = -(-(gap[0] + need) // al) * al
        else:
            left = need
            for g in free:
                if left == 0:
                    break
                take = min(left, g[1] - g[0])
                if take <= 0:
                    continue
                ext.append((g[0], take))
                left -= take
                g[0] = -(-(g[0] + take) // al) * al
            if left:
                raise Refuse(f"super has no room for {name} ({nbytes // MIB} MiB) next to the running slot")
        free = [g for g in free if g[1] > g[0]]
        out[name] = ext
    return out


# ------------------------------------------------------------------ AVB, bootconfig, ramdisk, entries

def vbmeta_key(raw):
    if raw[:4] != b"AVB0":
        raise Refuse("vbmeta: no AVB header")
    auth, aux = struct.unpack_from(">QQ", raw, 12)
    po, ps = struct.unpack_from(">QQ", raw, 64)
    a = 256 + auth + po
    if ps == 0 or a + ps > len(raw):
        raise Refuse("vbmeta: no public key")
    return hashlib.sha256(raw[a:a + ps]).hexdigest()


def split_initrd(d):
    """(ramdisk bytes, bootconfig lines) of an initrd that ends in a bootconfig."""
    if not d.endswith(BCMAGIC):
        raise Refuse("initrd has no bootconfig")
    end = len(d) - len(BCMAGIC)
    size, csum = struct.unpack_from("<II", d, end - 8)
    start = end - 8 - size
    bc = d[start:start + size]
    if start < 0 or (sum(bc) & 0xFFFFFFFF) != csum:
        raise Refuse("initrd bootconfig checksum")
    return d[:start], bc


def bc_get(bc, key):
    hits = [l for l in bc.split(b"\n") if l.startswith(key.encode() + b"=")]
    if len(hits) != 1:
        raise Refuse(f"bootconfig: {len(hits)} lines of {key}")
    return hits[0].split(b"=", 1)[1].decode()


def bc_set(bc, key, value):
    lines = bc.split(b"\n")
    hits = [i for i, l in enumerate(lines) if l.startswith(key.encode() + b"=")]
    if len(hits) != 1:
        raise Refuse(f"bootconfig: {len(hits)} lines of {key}")
    lines[hits[0]] = key.encode() + b"=" + value.encode()
    return b"\n".join(lines)


def bc_blob(bc):
    return bc + struct.pack("<II", len(bc), sum(bc) & 0xFFFFFFFF) + BCMAGIC


def ramdisk_plain(rd):
    if rd[:4] == LZ4_LEGACY:
        return run(["lz4", "-d", "-c"], rd)
    return rd


def ramdisk_prop(rd, key):
    """A property from system/etc/ramdisk/build.prop inside an (lz4 or plain) newc ramdisk."""
    body = ramdisk_plain(rd)
    off = 0
    while True:
        off = body.find(b"070701", off)
        if off < 0:
            break
        h = body[off:off + 110]
        try:
            ns, fs = int(h[94:102], 16), int(h[54:62], 16)
        except ValueError:
            off += 6
            continue
        nend = (off + 110 + ns + 3) & ~3
        name = body[off + 110:off + 110 + ns - 1]
        if name == b"system/etc/ramdisk/build.prop":
            for line in body[nend:nend + fs].decode(errors="replace").splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
            break
        off = (nend + fs + 3) & ~3
    raise Refuse(f"ramdisk has no {key}")


def build_id(fp):
    m = re.fullmatch(r"[^/]+/[^/]+/[^:]+:[^/]+/[^/]+/(\d+):\w+/[\w-]+", fp)
    if not m:
        raise Refuse(f"odd build fingerprint {fp!r}")
    return int(m.group(1))


def entry_parse(text):
    e = {"initrd": []}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        k, _, v = line.partition(" ")
        if k == "initrd":
            e["initrd"].append(v.strip())
        else:
            e[k] = v.strip()
    if not e.get("title") or not e.get("linux") or not e["initrd"] or "options" not in e:
        raise Refuse("boot entry without title, linux, initrd or options")
    return e


def entry_text(title, linux, initrd, options):
    return f"title {title}\nlinux {linux}\n" + "".join(f"initrd {i}\n" for i in initrd) + f"options {options}\n"


def lift(options):
    """NVMe entries: only the NVMe initcall block lifted; module_blacklist and efi=noruntime must stay."""
    toks = options.split()
    bl = [x for x in toks if x.startswith("module_blacklist=")]
    if toks.count(LIFT) != 1 or "efi=noruntime" not in toks or len(bl) != 1 or \
            {"nvme", "nvme_core"} & set(bl[0][17:].split(",")):
        raise Refuse("a release entry cannot be changed for the internal disk safely")
    return " ".join("initcall_blacklist=mei_me_driver_init" if t == LIFT else t for t in toks)


def model_of(title):
    m = re.match(r"Googlebook (\S+) ", title)
    if not m:
        raise Refuse(f"boot entry title {title!r} does not name a model")
    return m.group(1)


# ------------------------------------------------------------------ FAT volumes (mtools)

def fat_fits(boot_sector, length, what):
    """Refuse a FAT whose BPB says it is larger than its partition: mtools would write past the partition's end."""
    bps = struct.unpack_from("<H", boot_sector, 11)[0]
    total = struct.unpack_from("<H", boot_sector, 19)[0] or struct.unpack_from("<I", boot_sector, 32)[0]
    if boot_sector[510:512] != b"\x55\xaa" or bps not in (512, 1024, 2048, 4096) or not total:
        raise Refuse(f"{what}: not a FAT file system")
    if total * bps > length:
        raise Refuse(f"{what}: its FAT says {total * bps} bytes, but the partition has {length}")


class Fat:
    def __init__(self, spec, tmp):
        self.spec, self.tmp = spec, tmp
        self.blkfd = None            # a block device's held descriptor (open_stick)

    def sync_drop(self):
        """mtools wrote through its own descriptor: flush the device, so the checks that follow read the disk"""
        if self.blkfd is not None:
            import fcntl
            os.fsync(self.blkfd)
            fcntl.ioctl(self.blkfd, 0x1261)  # BLKFLSBUF

    def ls(self, path):
        r = subprocess.run(["mdir", "-b", "-i", self.spec, "::" + path], capture_output=True, text=True,
                           env=dict(os.environ, MTOOLS_SKIP_CHECK="1"))
        if r.returncode:
            return []
        return [l.strip().rstrip("/").split("/")[-1] for l in r.stdout.splitlines() if l.strip().rstrip("/")]

    def exists(self, path):
        d, _, f = path.rpartition("/")
        return f in self.ls(d or "/")

    def read(self, path):
        t = os.path.join(self.tmp, "fat-read")
        if os.path.exists(t):
            os.unlink(t)
        run(["mcopy", "-n", "-i", self.spec, "::" + path, t])
        with open(t, "rb") as f:
            return f.read()

    def write(self, path, data):
        d = path.rpartition("/")[0]
        if d:
            parts = d.strip("/").split("/")
            for i in range(len(parts)):
                sub = "/" + "/".join(parts[:i + 1])
                if not self.exists(sub):
                    run(["mmd", "-i", self.spec, "::" + sub])
        t = os.path.join(self.tmp, "fat-write")
        with open(t, "wb") as f:
            f.write(data)
        run(["mcopy", "-o", "-i", self.spec, t, "::" + path])
        if self.read(path) != data:
            raise Failed(f"{path} on {self.spec} reads back different")

    def delete(self, path):
        if self.exists(path):
            run(["mdel", "-i", self.spec, "::" + path])

    def free(self):
        out = run(["minfo", "-i", self.spec, "::"], text=True)
        spc = int(re.search(r"cluster size:\s*(\d+) sectors", out).group(1))
        bps = int(re.search(r"sector size:\s*(\d+)", out).group(1))
        mdir = run(["mdir", "-i", self.spec, "::/"], text=True)
        free = int(re.search(r"([\d ]+) bytes free", mdir).group(1).replace(" ", ""))
        return free, spc * bps


# ------------------------------------------------------------------ sources (a release)

class Release:
    """What an update needs from a release: its GPT layout, its slot-a partitions, its super, its ESP files."""

    def __init__(self, layout, read_phys, super_rd, super_size, esp_fat, ident):
        self.layout, self.read_phys, self.super_rd, self.super_size, self.esp, self.ident = \
            layout, read_phys, super_rd, super_size, esp_fat, ident
        self.lp = LP.read(super_rd, super_size)
        self.dyn = {p["name"][:-2]: p for p in self.lp.parts if p["name"].endswith("_a") and p["extents"]}
        if "system" not in self.dyn or "vendor" not in self.dyn:
            raise Refuse("release: super has no system_a or vendor_a")
        self.kernel = esp_fat.read("/kernel.efi")
        initrd = esp_fat.read("/initrd.img")
        self.ramdisk, self.bc = split_initrd(initrd)
        if bc_get(self.bc, "androidboot.slot_suffix") != "_a":
            raise Refuse("release: its initrd is not for slot a")
        self.entries = {n: entry_parse(esp_fat.read("/loader/entries/" + n).decode()) for n in ENTRIES
                        if n in REQUIRED_ENTRIES or esp_fat.exists("/loader/entries/" + n)}
        self.loader = esp_fat.read("/loader/loader.conf").decode()
        self.vbmeta = b"".join(read_phys("vbmeta_a"))
        self.key = vbmeta_key(self.vbmeta)
        self.fingerprint = ramdisk_prop(self.ramdisk, "ro.bootimage.build.fingerprint")
        self.build = build_id(self.fingerprint)
        self.model = model_of(self.entries["lapis-quiet.conf"]["title"])
        if {model_of(e["title"]) for e in self.entries.values()} != {self.model}:
            raise Refuse("release: its entries name different models")


def image_release(path, tmp, check_sha=True):
    if check_sha:
        sp = path + ".sha256"
        if not os.path.isfile(sp):
            raise Refuse(f"{sp} is missing")
        want = open(sp).read().split()[0]
        say(f"checking {os.path.basename(path)} against its .sha256 (reads the whole image)...")
        img = Disk(path)
        if img.sha(0, img.size) != want:
            raise Refuse(f"{path} does not match its .sha256")
    img = Disk(path)
    lay = gpt_read(img)

    def read_phys(name):
        p = lay[name]
        for o in range(0, p["len"], 16 << 20):
            yield img.pread(p["start"] + o, min(16 << 20, p["len"] - o))

    sp = lay["super"]
    esp_t = os.path.join(tmp, "release-esp.img")
    with open(esp_t, "wb") as f:
        for b in read_phys(ESP):
            f.write(b)
    rel = Release(lay, read_phys, lambda o, n: img.pread(sp["start"] + o, n), sp["len"], Fat(esp_t, tmp),
                  f"image {os.path.basename(path)}")
    return rel


def pristine_release(pdir, superdev, tmp):
    """The installer stick's data: manifest + pristine partition files (checked by SHA-256) + super on the stick."""
    lines = open(os.path.join(pdir, "manifest.txt")).read().splitlines()
    img = lines[0].split()
    lay, sha = {}, {}
    for line in lines[2:]:
        _, n, name, start, length, attrs, typ, s, kind = line.split()
        lay[name] = dict(n=int(n), start=int(start), len=int(length), type=typ, attrs=int(attrs, 16), kind=kind)
        sha[name] = s
    sdev = Disk(superdev)
    if sdev.size != lay["super"]["len"]:
        raise Refuse("the stick's super is not the size the installer data says")
    say("checking the release's super on this stick against the installer data...")
    if sdev.sha(0, sdev.size) != sha["super"]:
        raise Refuse("super on this stick does not match the installer data")

    def read_phys(name):
        p = subprocess.Popen(["zstd", "-q", "-dc", os.path.join(pdir, f"p{lay[name]['n']}.img.zst")], stdout=subprocess.PIPE)
        h, n = hashlib.sha256(), 0
        while True:
            b = p.stdout.read(16 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            yield b
        if p.wait() or h.hexdigest() != sha[name] or n != lay[name]["len"]:
            raise Refuse(f"installer data for {name} is damaged")

    esp_t = os.path.join(tmp, "release-esp.img")
    with open(esp_t, "wb") as f:
        for b in read_phys(ESP):
            f.write(b)
    for name in [f"{p}_a" for p in SLOT_PHYS]:     # check every file now, before anything is written
        for _ in read_phys(name):
            pass
    return Release(lay, read_phys, lambda o, n: sdev.pread(o, n), sdev.size, Fat(esp_t, tmp), f"installer data {img[1]}")


# ------------------------------------------------------------------ the installed disk

SELECT = ("androidboot.vendor.apex.com.android.hardware.keymint", "androidboot.vendor.apex.com.android.hardware.gatekeeper",
          "androidboot.hypervisor.protected_vm.supported")


class Installed:
    """What is on the disk: partition table, super metadata, boot menu, which slot starts by default and the facts of
    that slot (Google build, AVB key, model, KeyMint/Gatekeeper selection). While an update is unfinished, the facts
    come from /gbos/update-state.txt, written before its first change."""

    def __init__(self, disk, tmp):
        self.disk = disk
        self.gpt = gpt_read(disk)
        self.super = self.gpt["super"]
        self.lp = LP.read(lambda o, n: disk.pread(self.super["start"] + o, n), self.super["len"])
        esp = self.gpt[ESP]
        fat_fits(disk.pread(esp["start"], 512), esp["len"], "the disk's ESP")     # before mtools reads or writes it
        self.fat = Fat(disk.vol(esp["start"]), tmp)
        self.entries, self.slot_of = {}, {}
        for name in self.fat.ls("/loader/entries"):
            if not name.endswith(".conf"):
                continue
            try:
                e = entry_parse(self.fat.read("/loader/entries/" + name).decode())
            except (Refuse, RuntimeError, UnicodeDecodeError):
                self.slot_of[name] = None
                continue
            self.entries[name] = e
            try:
                _, bc = split_initrd(self.fat.read(e["initrd"][-1]))
                self.slot_of[name] = bc_get(bc, "androidboot.slot_suffix").lstrip("_")
            except (Refuse, RuntimeError):
                self.slot_of[name] = None        # file missing or damaged (an interrupted update)
        try:
            m = re.search(r"^default\s+(\S+)", self.fat.read("/loader/loader.conf").decode(), re.M)
        except RuntimeError:
            m = None
        self.default = m.group(1) if m else None
        st = self.fat.read(STATE).decode() if self.fat.exists(STATE) else ""
        if st:
            kv = dict(l.split(" ", 1) for l in st.splitlines() if " " in l)
            self.resume = kv.get("updating")
            if self.resume not in ("a", "b"):
                raise Refuse(f"{STATE} is damaged")
            self.current = "b" if self.resume == "a" else "a"
            self.build, self.fingerprint = int(kv["build"]), kv["fingerprint"]
            self.key, self.model = kv["key"], kv["model"]
            self.select = {k: kv["select." + k] for k in SELECT}
        else:
            self.resume = None
            self.current = self.slot_of.get(self.default)
            if self.current not in ("a", "b"):
                raise Refuse("cannot tell which slot starts by default (damaged boot menu, no update in progress)")
            cur = self.entries[self.default]
            rd, bc = split_initrd(self.fat.read(cur["initrd"][-1]))
            self.fingerprint = ramdisk_prop(rd, "ro.bootimage.build.fingerprint")
            self.build = build_id(self.fingerprint)
            vb = self.gpt[f"vbmeta_{self.current}"]
            self.key = vbmeta_key(disk.pread(vb["start"], vb["len"]))
            self.model = model_of(cur["title"])
            self.select = {k: bc_get(bc, k) for k in SELECT}
        self.target = "b" if self.current == "a" else "a"

    def state_text(self, release):
        return "".join(f"{k} {v}\n" for k, v in [("updating", self.target), ("from", self.current), ("build", self.build),
                                                 ("fingerprint", self.fingerprint), ("key", self.key), ("model", self.model),
                                                 ("release", release)] + [("select." + k, v) for k, v in self.select.items()])

    def facts(self):
        prev = {self.slot_of[n] for n in self.entries if n.startswith(PREV)} - {None}
        return [f"disk {self.disk.path}", f"model {self.model}", f"current slot {self.current} ({self.default})",
                f"previous slot on the menu: {','.join(sorted(prev)) or 'none'}", f"google build {self.build} ({self.fingerprint})",
                f"avb key sha256 {self.key}", f"super metadata copies consistent: {self.lp.consistent}",
                f"update in progress: {self.resume or 'no'}"]


# ------------------------------------------------------------------ plan and apply

def check(inst, rel, mode):
    """Every reason to refuse; nothing is written here."""
    for name, p in rel.layout.items():
        q = inst.gpt.get(name)
        if q is None:
            raise Refuse(f"the disk has no {name}; this release needs a fresh install")
        same = (q["n"], q["type"]) == (p["n"], p["type"]) and (name == "userdata" or (q["start"], q["len"]) == (p["start"], p["len"]))
        if not same:
            raise Refuse(f"partition {name} differs between the disk and the release; this release needs a fresh install")
    if set(inst.gpt) != set(rel.layout):
        raise Refuse("the disk has partitions the release does not have; this release needs a fresh install")
    if rel.model != inst.model:
        raise Refuse(f"the release is for {rel.model}, the disk holds Googlebook for {inst.model}")
    if rel.key != inst.key:
        raise Refuse("the release is signed with another AVB key than the installed system. Build the update with the "
                     "same work directory (its base/keys/vbmeta.pem); otherwise only a fresh install works (data lost)")
    for k in SELECT:
        if bc_get(rel.bc, k) != inst.select[k]:
            raise Refuse(f"the release changes {k}; the lock screen and keystore keys would stop working")
    if rel.build < inst.build:
        raise Refuse(f"the release has an older Google build ({rel.build}) than the disk ({inst.build}); "
                     "going back needs a fresh install")
    keep = {p["name"] for p in inst.lp.parts if not p["name"].endswith("_" + inst.target)}
    sizes = {f"{b}_{inst.target}": sum(n for _, n in p["extents"]) * SECTOR for b, p in rel.dyn.items()}
    for b, p in rel.dyn.items():
        if inst.lp.part(f"{b}_{inst.target}") is None and inst.lp.part(f"{b}_{inst.current}") is None:
            raise Refuse(f"the release has a new dynamic partition {b}; this release needs a fresh install")
    gi = inst.lp.groups.index(f"google_dynamic_partitions_{inst.target}") if f"google_dynamic_partitions_{inst.target}" in inst.lp.groups else None
    if gi is None:
        raise Refuse("super has no group for the target slot")
    if sum(sizes.values()) > inst.lp.group_max[gi] and inst.lp.group_max[gi]:
        raise Refuse("the release's partitions are larger than the slot's group in super")
    ext = allocate(inst.lp, sizes, keep)
    return dict(sizes=sizes, extents=ext, group=gi)


def boot_files(inst, rel, mode, tmp):
    """The target slot's ESP files and entries, the running slot's entries as prev-<entry>, and whether the ESP has
    room for both slots' files (otherwise the previous version cannot stay on this disk's menu)."""
    t, c = inst.target, inst.current
    bc = bc_set(rel.bc, "androidboot.slot_suffix", "_" + t)
    bc = bc_set(bc, "androidboot.boot_part_uuid", inst.gpt["boot_a"]["guid"])
    raw = os.path.join(tmp, "ramdisk.cpio")
    with open(raw, "wb") as f:
        f.write(ramdisk_plain(rel.ramdisk))
    initrd = run(["lz4", "-l", "-12", "--favor-decSpeed", "-c", raw]) + bc_blob(bc)
    ks = hashlib.sha256(rel.kernel).hexdigest()
    running = {n: e for n, e in inst.entries.items() if inst.slot_of.get(n) == c}
    kernel_path = None
    for p in sorted({e["linux"] for e in running.values()}):   # the running slot's kernel, when it is the same
        if hashlib.sha256(inst.fat.read(p)).hexdigest() == ks:
            kernel_path = p
            break
    files = {f"/gbos/{t}/initrd.img": initrd}
    if kernel_path is None:
        kernel_path = f"/gbos/{t}/kernel.efi"
        files[kernel_path] = rel.kernel
    entries = {}
    for n, e in rel.entries.items():
        opts = lift(e["options"]) if mode == "nvme" else e["options"]
        entries[n] = entry_text(e["title"], kernel_path, [f"/gbos/{t}/initrd.img"], opts)
    loader = "default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n" if mode == "nvme" else rel.loader
    prev = {}
    for n in ENTRIES:
        e = running.get(n)
        if e is not None and n in rel.entries:
            prev[PREV + n] = entry_text(e["title"] + PREV_TITLE, e["linux"], e["initrd"], e["options"])
    # running-slot entries the release no longer has (the plain lapis.conf of releases before v0.2): off the menu
    stale = sorted(n for n in running if not n.startswith(PREV) and n not in rel.entries)
    keep_files = {x for e in running.values() for x in [e["linux"], *e["initrd"]]}
    drop_files = {x for n, e in inst.entries.items() if inst.slot_of.get(n) != c for x in [e["linux"], *e["initrd"]]}
    drop_files |= {f"/gbos/{t}/{f}" for f in inst.fat.ls(f"/gbos/{t}")}     # leftovers of an unfinished update
    drop_files -= keep_files
    free, cl = inst.fat.free()
    rup = lambda n: -(-n // cl) * cl
    reclaim = sum(rup(len(inst.fat.read(x))) for x in drop_files if inst.fat.exists(x))
    need = sum(rup(len(v)) for v in files.values()) + rup(4096) * (len(entries) + len(prev) + 4)
    fits = free + reclaim >= need + 4 * cl
    if os.environ.get("GBOS_UPDATE_TEST") and os.environ.get("GBOS_UPDATE_TEST_NO_PREV_ROOM"):
        fits = False                                 # test hook: as if the ESP had no room for the previous version
    # without room for a previous version: can the new files still be written before the old ones are removed?
    new_first = free + reclaim >= sum(rup(len(v)) for v in files.values()) + rup(4096) * (len(entries) + 4) + 4 * cl
    return dict(files=files, entries=entries, prev=prev, stale=stale, loader=loader, drop=sorted(drop_files), keep=sorted(keep_files),
                fits=fits, new_first=new_first, free=free, reclaim=reclaim, need=need, cluster=cl, kernel=rel.kernel,
                initrd=initrd, running=running)


def plan_text(inst, rel, chk, bf):
    t = inst.target
    lines = [f"release: {rel.ident}, google build {rel.build}, model {rel.model}",
             f"update: slot {t} gets the release; slot {inst.current} (running) stays as it is",
             "super: " + ", ".join(f"{n} {s // MIB} MiB" for n, s in sorted(chk["sizes"].items())),
             "slot partitions: " + ", ".join(f"{p}_{t}" for p in SLOT_PHYS),
             f"boot files: {', '.join(bf['files'])} ({'kernel shared with the running slot' if len(bf['files']) == 1 else 'new kernel'})",
             "previous version kept on this disk's boot menu: " + ("yes" if bf["fits"] and bf["prev"] else
             "NO (the ESP has no room for both)" if bf["prev"] else "NO (the running version's boot files are gone)")
             + f" (free {bf['free'] // 1024} KiB, reclaimed {bf['reclaim'] // 1024} KiB, needed {bf['need'] // 1024} KiB)"
             + ("" if bf["fits"] else "; the new boot files are written " + ("before the old ones are removed" if bf["new_first"]
                else "only after the old ones are removed (the disk's own menu cannot start either version until the update finishes)")),
             "never written: " + ", ".join(NEVER) + ", the partition table, every other partition"]
    if rel.build != inst.build:
        lines.append(f"NOTE: new Google build {inst.build} -> {rel.build}. Once the new version has started, Android has"
                     " upgraded /data; the previous version may then not start with it")
    return lines


def write_stream(disk, ranges, chunks):
    """Write a stream of byte chunks across [(byte offset, length)] ranges; returns the SHA-256 of what was written."""
    h = hashlib.sha256()
    ri, roff = 0, 0
    for b in chunks:
        h.update(b)
        mv = memoryview(b)
        while mv:
            if ri >= len(ranges):
                raise Failed("more data than room")
            off, ln = ranges[ri]
            n = min(len(mv), ln - roff)
            disk.pwrite(off + roff, mv[:n])
            mv, roff = mv[n:], roff + n
            if roff == ln:
                ri, roff = ri + 1, 0
    return h.hexdigest()


def readback(disk, ranges, total):
    h = hashlib.sha256()
    left = total
    for off, ln in ranges:
        n = min(ln, left)
        for o in range(0, n, 16 << 20):
            h.update(disk.pread(off + o, min(16 << 20, n - o)))
        left -= n
    return h.hexdigest()


def step(name):
    say(f"ok   {name}")
    if FAIL_AT and FAIL_AT == name.split(":")[0]:
        raise Failed(f"test hook: stopped after {name}")


def guard_ranges(inst):
    """Byte ranges an update never writes: the partition table areas and every partition except super, the ESP
    (written through mtools only) and the target slot's partitions."""
    g = inst.gpt
    first = min(p["start"] for p in g.values())
    last = max(p["start"] + p["len"] for p in g.values())
    out = [(0, first, "the partition table"), (last, inst.disk.size, "the backup partition table")]
    allowed = {"super"} | {f"{p}_{inst.target}" for p in SLOT_PHYS}
    out += [(p["start"], p["start"] + p["len"], name) for name, p in g.items() if name not in allowed]
    # inside super: the running slot's extents
    for q in inst.lp.parts:
        if not q["name"].endswith("_" + inst.target):
            out += [(inst.super["start"] + a * SECTOR, inst.super["start"] + (a + n) * SECTOR, q["name"]) for a, n in q["extents"]]
    return out


def stick_entries(sb):
    """{name: (entry or None, slot or None)} of the installer stick's internal-disk entries (gbos-nvme*.conf); the slot
    comes from the androidboot.slot_suffix in the bootconfig of the initrd the entry starts."""
    out = {}
    for n in sb.ls("/loader/entries"):
        if not (n.startswith("gbos-nvme") and n.endswith(".conf")):
            continue
        try:
            e = entry_parse(sb.read("/loader/entries/" + n).decode())
        except (Refuse, RuntimeError, UnicodeDecodeError):
            out[n] = (None, None)
            continue
        try:
            _, bc = split_initrd(sb.read(e["initrd"][-1]))
            out[n] = (e, bc_get(bc, "androidboot.slot_suffix").lstrip("_"))
        except (Refuse, RuntimeError):
            out[n] = (e, None)
    return out


def stick_source(sb, inst, c):
    """Where the stick's copy of the running slot c comes from: the internal disk's own running quiet entry (what slot
    c really holds; a copy on the stick may be older, after an update from another stick), or, when its files are
    gone (a rerun after a stop inside the --without-fallback window), the stick's entry for slot c that the first
    attempt wrote. Returns (entry, on_stick); refuses before anything is written when there is neither."""
    run_e = inst.entries.get(inst.default)
    if run_e is not None and inst.slot_of.get(inst.default) == c:
        return run_e, False
    ents = stick_entries(sb)
    for n in ("gbos-nvme.conf", "gbos-nvme-prev.conf"):
        e, s = ents.get(n, (None, None))
        if e is not None and s == c:
            return e, True
    raise Refuse("cannot find the running version's boot files, neither on the internal disk nor on the installer stick")


def stick_copy(sb, inst, src, on_stick, c):
    """{path on the stick: data} of the copy of slot c under /gbos-nvme/<c>/ (src's files, read from where they are)"""
    read = sb.read if on_stick else inst.fat.read
    return {f"/gbos-nvme/{c}/{os.path.basename(x)}": read(x) for x in [src["linux"], *src["initrd"]]}


def stick_needs(sb, bf, t, copy):
    """Room on the stick's boot partition for the copy of the running slot (what is not there yet) and the target
    slot's new files; the target slot's old files there are removed first."""
    free, cl = sb.free()
    rup = lambda n: -(-n // cl) * cl
    copy_c = sum(rup(len(d)) for y, d in copy.items() if not (sb.exists(y) and sb.read(y) == d))
    reclaim = sum(rup(len(sb.read(f"/gbos-nvme/{t}/{f}"))) for f in sb.ls(f"/gbos-nvme/{t}"))
    return free + reclaim >= copy_c + rup(len(bf["kernel"])) + rup(len(bf["initrd"])) + 16 * cl


def stick_prepare(sb, t, c, src, copy):
    """Before any slot is written: the stick's 'Googlebook (internal disk)' and '(previous version)' entries start the
    running slot c from the stick's copy under /gbos-nvme/<c>/, and nothing there points at the target slot t (its
    entries and files are removed). Runs again unchanged after a stop at any later step: the stick's entries then
    start slot c again until the rerun has written slot t."""
    for y, data in copy.items():
        if not sb.exists(y) or sb.read(y) != data:
            sb.write(y, data)
    linux = f"/gbos-nvme/{c}/{os.path.basename(src['linux'])}"
    initrd = [f"/gbos-nvme/{c}/{os.path.basename(x)}" for x in src["initrd"]]
    sb.write("/loader/entries/gbos-nvme-prev.conf",
             entry_text("Googlebook (internal disk, previous version)", linux, initrd, src["options"]).encode())
    sb.write("/loader/entries/gbos-nvme.conf", entry_text("Googlebook (internal disk)", linux, initrd, src["options"]).encode())
    for n, (e, s) in stick_entries(sb).items():
        if n not in ("gbos-nvme.conf", "gbos-nvme-prev.conf") and s != c:
            sb.delete("/loader/entries/" + n)      # e.g. the serial-debug entry of slot t (rewritten at the stick step)
    for f in sb.ls(f"/gbos-nvme/{t}"):
        sb.delete(f"/gbos-nvme/{t}/{f}")
    sb.sync_drop()
    left = stick_entries(sb)
    if (any(s != c for _, s in left.values()) or left.get("gbos-nvme.conf", (None, None))[1] != c
            or left.get("gbos-nvme-prev.conf", (None, None))[1] != c):
        raise Failed("the installer stick still points at the slot being written")


def stick_menu(sb, bf, t, c):
    """After the target slot is written and read back: the stick's 'Googlebook (internal disk)' entries start slot t
    from /gbos-nvme/<t>/; 'previous version' (written by stick_prepare) keeps starting slot c; files no entry uses any
    more are removed. Idempotent."""
    kp, ip = f"/gbos-nvme/{t}/kernel.efi", f"/gbos-nvme/{t}/initrd.img"
    sb.write(kp, bf["kernel"])
    sb.write(ip, bf["initrd"])
    for n, src, title in (("gbos-nvme.conf", "lapis-quiet.conf", "Googlebook (internal disk)"),
                          ("gbos-nvme-debug.conf", "lapis-debug.conf", "Googlebook (internal disk) serial-debug")):
        e = entry_parse(bf["entries"][src])
        sb.write("/loader/entries/" + n, entry_text(title, kp, [ip], e["options"]).encode())
    sb.sync_drop()
    ents = stick_entries(sb)
    if ents.get("gbos-nvme-prev.conf", (None, None))[1] != c or ents["gbos-nvme.conf"][1] != t:
        raise Failed("the installer stick's menu is not as written")
    used = {x for e, _ in ents.values() if e is not None for x in [e["linux"], *e["initrd"]]}
    for sub in ("", "/a", "/b"):
        for f in sb.ls("/gbos-nvme" + sub):
            x = f"/gbos-nvme{sub}/{f}"
            if f.endswith((".efi", ".img")) and x not in used:
                sb.delete(x)
    sb.sync_drop()


def open_stick(stick_boot, tmp):
    """The installer stick's boot partition, held O_EXCL (nothing can mount it while mtools writes it) and its FAT
    size field checked against the partition."""
    sb = Fat(stick_boot, tmp)
    dev, _, off = stick_boot.partition("@@")
    off = int(off or 0)
    if stat.S_ISBLK(os.stat(dev).st_mode):
        try:
            fd = os.open(dev, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)
        except OSError as e:
            raise Refuse(f"the stick's boot partition {dev} is mounted or in use ({e.strerror})")
        HOLD.append(fd)
        sb.blkfd = fd
        length = os.lseek(fd, 0, os.SEEK_END) - off
        bs = os.pread(fd, 512, off)
    else:
        length = os.path.getsize(dev) - off
        with open(dev, "rb") as fh:
            fh.seek(off)
            bs = fh.read(512)
    fat_fits(bs, length, "the installer stick's boot partition")
    return sb


def apply(disk, rel, mode, without_fallback, stick_boot, tmp):
    inst = Installed(disk, tmp)
    chk = check(inst, rel, mode)
    bf = boot_files(inst, rel, mode, tmp)
    for l in plan_text(inst, rel, chk, bf):
        say("     " + l)
    sb = open_stick(stick_boot, tmp) if stick_boot else None
    if sb is not None:
        STICK.append(True)
    t, c = inst.target, inst.current
    if sb is not None:
        stick_src, on_stick = stick_source(sb, inst, c)
        copy = stick_copy(sb, inst, stick_src, on_stick, c)
        if not stick_needs(sb, bf, t, copy):
            raise Refuse("the installer stick's boot partition has no room for the internal disk's boot files")
    if not bf["fits"] and bf["prev"] and not without_fallback and sb is None:
        raise Refuse("the ESP has no room to keep the previous version; run again with --without-fallback "
                     "(if the update then stops halfway, run it again to finish) or update from the installer stick")
    disk.guard(guard_ranges(inst))
    orig = {q["name"]: list(q["extents"]) for q in inst.lp.parts}
    # ---- from here on the disk changes. First the state file: a rerun finishes this slot, never the running one.
    WRITING.append(True)
    if inst.resume and inst.slot_of.get(inst.default) is None:
        # a rerun after a stop inside the window: the old boot files are gone already, so the disk's own menu cannot
        # start any version until this run has switched it
        WINDOW.append(sb is not None)
    inst.fat.write(STATE, inst.state_text(rel.ident).encode())
    if sb is not None:
        stick_prepare(sb, t, c, stick_src, copy)
        step(f"stickprep: the installer stick starts slot {c} only, from its own copy")
    for n, s in list(inst.slot_of.items()):
        if s != c:                                   # the old previous version (slot t) or broken entries
            inst.fat.delete("/loader/entries/" + n)
    for x in bf["drop"]:
        inst.fat.delete(x)
    if any(inst.slot_of.get(n) == t for n in inst.fat.ls("/loader/entries")):
        raise Failed("an entry of the disk's menu still starts the slot being written")
    step(f"removed: the boot entries and files of slot {t}")
    lp = inst.lp
    for b, p in rel.dyn.items():
        name = f"{b}_{t}"
        ext = chk["extents"][name]
        ranges = [(inst.super["start"] + a * SECTOR, n * SECTOR) for a, n in ext]
        src = [(a * SECTOR, n * SECTOR) for a, n in p["extents"]]

        def chunks(src=src):
            for off, ln in src:
                for o in range(0, ln, 16 << 20):
                    yield rel.super_rd(off + o, min(16 << 20, ln - o))
        want = write_stream(disk, ranges, chunks())
        disk.sync_drop()
        if readback(disk, ranges, chk["sizes"][name]) != want:
            raise Failed(f"{name} reads back different")
        q = lp.part(name)
        if q is None:
            lp.parts.append(dict(name=name, attrs=p["attrs"], group=chk["group"], extents=ext))
        else:
            q.update(attrs=p["attrs"], group=chk["group"], extents=ext)
    for q in lp.parts:                                   # target-slot partitions the release no longer has
        if q["name"].endswith("_" + t) and q["name"][:-2] not in rel.dyn:
            q["extents"] = []
    step(f"dynamic: partitions of slot {t} written and read back")
    for ph in SLOT_PHYS:
        d = inst.gpt[f"{ph}_{t}"]
        if rel.layout[f"{ph}_a"]["len"] != d["len"]:
            raise Failed(f"{ph}_{t} is not the size of the release's {ph}_a")
        want = write_stream(disk, [(d["start"], d["len"])], rel.read_phys(f"{ph}_a"))
        disk.sync_drop()
        if disk.sha(d["start"], d["len"]) != want:
            raise Failed(f"{ph}_{t} reads back different")
    step(f"physical: slot partitions of slot {t} written and read back")
    blob = lp.serialize()
    LP.parse(blob + bytes(lp.mx - len(blob)))
    disk.guard([g for g in disk.forbidden if not (inst.super["start"] <= g[0] < inst.super["start"] + inst.super["len"] and g[2] != "super")]
               + [(inst.super["start"] + lp.first * SECTOR, inst.super["start"] + inst.super["len"], "super data")])
    for off in lp.slot_offsets():
        disk.pwrite(inst.super["start"] + off, blob)
        disk.sync_drop()
        if disk.pread(inst.super["start"] + off, len(blob)) != blob:
            raise Failed("LP metadata reads back different")
    new = LP.read(lambda o, n: disk.pread(inst.super["start"] + o, n), inst.super["len"])
    if not new.consistent:
        raise Failed("LP metadata copies differ after the write")
    for q in new.parts:
        if not q["name"].endswith("_" + t) and q["extents"] != orig.get(q["name"]):
            raise Failed(f"LP metadata changed {q['name']} of the running slot")
    step("metadata: super metadata written (all copies) and read back")
    new_refs = {x for txt in bf["entries"].values() for en in [entry_parse(txt)] for x in [en["linux"], *en["initrd"]]}
    old_files = [x for x in bf["keep"] if x not in bf["files"] and x not in new_refs]   # e.g. a shared kernel stays
    if not bf["fits"]:
        for n, s in list(inst.slot_of.items()):
            if s == c and n.startswith(PREV):
                inst.fat.delete("/loader/entries/" + n)
        if not bf["new_first"]:
            # the ESP cannot hold the old and the new boot files together: from here until the menu is switched, the
            # disk's own menu cannot start either version (a rerun finishes it; the installer stick still starts one)
            WINDOW.append(sb is not None)
            for x in old_files:
                inst.fat.delete(x)
            say("     the ESP holds one version only: the old boot files are removed before the new ones are written")
            step(f"oldremoved: the old boot files of slot {c} removed")
    for x, data in bf["files"].items():
        inst.fat.write(x, data)
    step(f"bootfiles: boot files of slot {t} written and read back")
    if sb is not None:
        stick_menu(sb, bf, t, c)
        step("stick: the installer stick's menu for the internal disk")
    if bf["fits"]:
        for n, text in bf["prev"].items():
            inst.fat.write("/loader/entries/" + n, text.encode())
    for n in bf["stale"]:
        inst.fat.delete("/loader/entries/" + n)
    for n, text in bf["entries"].items():
        inst.fat.write("/loader/entries/" + n, text.encode())
    inst.fat.write("/loader/loader.conf", bf["loader"].encode())
    WINDOW.clear()                                   # the menu starts the new slot now
    if not bf["fits"] and bf["new_first"]:
        for x in old_files:                          # nothing on the menu uses them any more
            inst.fat.delete(x)
        say("     the ESP holds one version only: the old boot files were removed after the switch")
    inst.fat.write(f"/gbos/{t}/release.txt", f"google build {rel.build}\n{rel.fingerprint}\n{rel.ident}\n".encode())
    inst.fat.delete(STATE)
    disk.sync_drop()                                 # the ESP was written through mtools: flush, read the result from the disk
    step(f"switched: the boot menu starts slot {t}")
    done = Installed(disk, tmp)
    if done.current != t or done.resume:
        raise Failed("the boot menu does not start the new slot")
    if bf["fits"] and bf["prev"] and not any(n.startswith(PREV) and done.slot_of.get(n) == c for n in done.entries):
        raise Failed("the previous version is not on the boot menu")
    if any(done.slot_of.get(n) is None for n in done.entries):
        raise Failed("an entry of the boot menu starts files that are not there")
    say(f"UPDATE OK: slot {t} holds google build {rel.build} and starts by default"
        + (f"; '{rel.entries['lapis-quiet.conf']['title']}{PREV_TITLE}' starts slot {c}" if bf["fits"] and bf["prev"] else ""))


def slot_build(fat, e):
    rd, _ = split_initrd(fat.read(e["initrd"][-1]))
    return build_id(ramdisk_prop(rd, "ro.bootimage.build.fingerprint"))


def rollback(disk, tmp, stick_boot=None, allow_build_change=False):
    inst = Installed(disk, tmp)
    if inst.resume:
        raise Refuse("an update stopped halfway; finish it first (run the update again)")
    prev = {n[len(PREV):]: n for n in inst.entries if n.startswith(PREV)}
    if "lapis-quiet.conf" not in prev or inst.slot_of.get(prev["lapis-quiet.conf"]) != inst.target:
        raise Refuse("there is no previous version on this disk's boot menu")
    pb = slot_build(inst.fat, inst.entries[prev["lapis-quiet.conf"]])
    if pb != inst.build and not allow_build_change:
        raise Refuse(f"the previous version has Google build {pb}, the running one {inst.build}: once the newer build has "
                     "started, Android has upgraded /data and the older one may not start with it. Run again with "
                     "--allow-build-change to switch anyway")
    sb = open_stick(stick_boot, tmp) if stick_boot else None
    sents = stick_entries(sb) if sb is not None else {}
    if sb is not None and (sents.get("gbos-nvme.conf", (None, None))[1] != inst.current
                           or sents.get("gbos-nvme-prev.conf", (None, None))[1] != inst.target):
        raise Refuse("the installer stick's internal-disk entries do not match this disk's menu")
    for sn, dn in (("gbos-nvme.conf", inst.default), ("gbos-nvme-prev.conf", prev["lapis-quiet.conf"])) if sb is not None else ():
        se, de = sents[sn][0], inst.entries[dn]
        if [sb.read(x) for x in [se["linux"], *se["initrd"]]] != [inst.fat.read(x) for x in [de["linux"], *de["initrd"]]]:
            raise Refuse(f"the installer stick's {sn} starts other boot files than this disk's {dn} (the disk was updated "
                         "from another stick since); roll back without --stick-boot")
    swap = {}
    for n, pn in prev.items():
        e, pe = inst.entries.get(n), inst.entries[pn]
        swap[n] = entry_text(pe["title"][:-len(PREV_TITLE)] if pe["title"].endswith(PREV_TITLE) else pe["title"],
                             pe["linux"], pe["initrd"], pe["options"])
        if e is not None:
            swap[pn] = entry_text(e["title"] + PREV_TITLE, e["linux"], e["initrd"], e["options"])
    WRITING.append(True)
    if sb is not None:
        # the stick: 'internal disk' <-> 'previous version'; its serial-debug entry follows the new running slot with
        # the options of this disk's (previous-version) serial-debug entry
        cur, pv = sents["gbos-nvme.conf"][0], sents["gbos-nvme-prev.conf"][0]
        sb.write("/loader/entries/gbos-nvme.conf", entry_text("Googlebook (internal disk)", pv["linux"], pv["initrd"], pv["options"]).encode())
        sb.write("/loader/entries/gbos-nvme-prev.conf", entry_text("Googlebook (internal disk, previous version)",
                                                                   cur["linux"], cur["initrd"], cur["options"]).encode())
        pd = inst.entries.get(prev.get("lapis-debug.conf", ""))
        if pd is not None:
            sb.write("/loader/entries/gbos-nvme-debug.conf", entry_text("Googlebook (internal disk) serial-debug",
                                                                        pv["linux"], pv["initrd"], pd["options"]).encode())
        else:
            sb.delete("/loader/entries/gbos-nvme-debug.conf")
    for n, text in sorted(swap.items(), key=lambda kv: not kv[0].startswith(PREV)):
        inst.fat.write("/loader/entries/" + n, text.encode())
    disk.sync_drop()
    done = Installed(disk, tmp)
    if done.current != inst.target:
        raise Failed("rollback did not switch the boot menu")
    if sb is not None:
        sb.sync_drop()
        after = stick_entries(sb)
        if after.get("gbos-nvme.conf", (None, None))[1] != inst.target or after.get("gbos-nvme-prev.conf", (None, None))[1] != inst.current:
            raise Failed("the installer stick's menu did not follow the rollback")
    say(f"ROLLBACK OK: slot {done.current} starts by default; slot {inst.current} is the previous version")


# ------------------------------------------------------------------ command line

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("cmd", choices=("inspect", "plan", "apply", "rollback"))
    ap.add_argument("disk")
    ap.add_argument("--image")
    ap.add_argument("--no-image-sha", action="store_true", help=argparse.SUPPRESS)  # tests only
    ap.add_argument("--pristine")
    ap.add_argument("--super")
    ap.add_argument("--mode", choices=("stick", "nvme"))
    ap.add_argument("--rdev")
    ap.add_argument("--syspath")
    ap.add_argument("--bytes")
    ap.add_argument("--without-fallback", action="store_true")
    ap.add_argument("--stick-boot")
    ap.add_argument("--allow-build-change", action="store_true")
    a = ap.parse_args(argv)
    tmp = tempfile.mkdtemp(prefix="gbos-update-")
    try:
        if a.cmd == "inspect":
            for l in Installed(Disk(a.disk), tmp).facts():
                say(l)
            return 0
        if a.cmd == "rollback":
            disk = Disk(a.disk, write=True, rdev=a.rdev, syspath=a.syspath, nbytes=a.bytes)
            rollback(disk, tmp, a.stick_boot, a.allow_build_change)
            return 0
        if bool(a.image) == bool(a.pristine) or (a.pristine and not a.super):
            raise Refuse("give --image <release.img>, or --pristine <dir> with --super <device>")
        if a.cmd == "apply" and not a.mode:
            raise Refuse("apply needs --mode stick|nvme")
        if a.no_image_sha and not os.environ.get("GBOS_UPDATE_TEST"):
            raise Refuse("--no-image-sha is for tests")
        rel = image_release(a.image, tmp, not a.no_image_sha) if a.image else pristine_release(a.pristine, a.super, tmp)
        if a.cmd == "plan":
            inst = Installed(Disk(a.disk), tmp)
            chk = check(inst, rel, a.mode or "stick")
            for l in inst.facts() + plan_text(inst, rel, chk, boot_files(inst, rel, a.mode or "stick", tmp)):
                say(l)
            say("PLAN OK (nothing written)")
            return 0
        disk = Disk(a.disk, write=True, rdev=a.rdev, syspath=a.syspath, nbytes=a.bytes)
        inst = Installed(disk, tmp)
        check(inst, rel, a.mode)                 # refuse here, before the first write
        apply(disk, rel, a.mode, a.without_fallback, a.stick_boot, tmp)
        return 0
    except Refuse as e:
        say(f"REFUSED: {e}" + ("" if WRITING else " (nothing was written)"))
        return 1 if WRITING else 2
    except (Failed, RuntimeError, OSError) as e:
        say(f"FAILED: {e}" + ("" if WRITING else " (nothing was written)"))
        if WRITING and WINDOW:
            say("The update stopped after the old boot files were removed and before the boot menu was switched: this"
                " disk's own boot menu cannot start Googlebook now. Run the same update again to finish it"
                + (" (meanwhile the installer stick's 'Googlebook (internal disk)' starts a complete version)." if WINDOW[0]
                   else "."))
            return 1
        if WRITING:
            say("The update stopped halfway. This disk's boot menu still starts the version that ran before by default"
                + (" (and the installer stick's 'Googlebook (internal disk)' starts a complete version)" if STICK else "")
                + "; run the same update again to finish it.")
            return 1
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
