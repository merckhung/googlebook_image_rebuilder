#!/usr/bin/env python3
"""Helper for build-base.sh: rebuilds the KANO base image from the official
lapis recovery zip.  Every subcommand is deterministic; nothing here touches a
block device or needs root.  Run with `python3 -I -B gbos_base.py <cmd> ...`.

Subcommands (see build-base.sh for the order):
  unzip-bin      extract the recovery .bin from the official zip (+ sha256)
  layout         GPT layout -> shell env file
  boot-extract   kernel.efi, vendor cmdline, vendor bootconfig, ramdisk cpios, vbmeta_a
  fetch-zip      fetch members of a remote zip with HTTP Range (ci.android.com)
  check-sha      verify sha256 of files
  loader-confs   write the three generations of systemd-boot config (E, G, M)
  make-initrd    concatenate cpio archives + Android bootconfig trailer
  esp-rewrite    rewrite a FAT32 file in place (same clusters, same timestamps)
  lp-extract     dump LP metadata (json) and every logical partition image
  lp-build       run lpmake with the same metadata, overriding some images
  erofs-meta     walk an EROFS image (inode mode/uid/gid/selinux/caps) -> json
  edit-vendor    apply the vendor tree edits + write fs_config/file_contexts
  edit-system-ext  apply the system_ext tree edits + write configs
  erofs-uuid     print the filesystem UUID of an EROFS image
  avb-orig-size  print the pre-footer (filesystem) size of an AVB-footed image
  footer-props   print the --prop arguments of an AVB footer (via avbtool)
  splice         write a file into an image at an offset, zero-fill the rest
  vbmeta-digest  sha256 of the vbmeta struct (header+auth+aux)
"""

import hashlib
import gzip
import http.client
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

# ---------------------------------------------------------------- utilities


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


# Every file this helper writes is opened through _checked_open: the descriptor is opened without following a symlink
# (O_NOFOLLOW), without blocking on a FIFO and without truncating; fstat on it must say "regular file", and only then is
# it truncated ("w") and handed out. So a block device, a FIFO or any symlink is refused before a single byte is
# written or cut, and nothing can be swapped in between the check and the write.
import builtins
import errno as _errno
import fcntl as _fcntl
import stat as _stat
_builtin_open = builtins.open


def _checked_open(file, mode="r", *args, **kwargs):
    if not any(c in mode for c in "wax+"):
        return _builtin_open(file, mode, *args, **kwargs)
    flags = (os.O_RDWR if "+" in mode else os.O_WRONLY) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if "x" in mode:
        flags |= os.O_CREAT | os.O_EXCL
    elif "w" in mode or "a" in mode:
        flags |= os.O_CREAT
    if "a" in mode:
        flags |= os.O_APPEND
    try:
        fd = os.open(file, flags, 0o666)
    except OSError as e:
        if e.errno in (_errno.ELOOP, _errno.EMLINK, _errno.ENXIO):
            die(f"{file} is not a regular file; refusing to write it")
        raise
    if not _stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        die(f"{file} is not a regular file; refusing to write it")
    _fcntl.fcntl(fd, _fcntl.F_SETFL, _fcntl.fcntl(fd, _fcntl.F_GETFL) & ~os.O_NONBLOCK)
    if "w" in mode:
        os.ftruncate(fd, 0)
    return _builtin_open(fd, mode.replace("x", "w"), *args, **kwargs)


open = _checked_open  # noqa: A001 (module-wide: every open() below is checked)


def _path_write_bytes(self, data):
    with _checked_open(self, "wb") as f:
        return f.write(data)


def _path_write_text(self, data, encoding=None, errors=None, newline=None):
    with _checked_open(self, "w", encoding=encoding, errors=errors, newline=newline) as f:
        return f.write(data)


Path.write_bytes = _path_write_bytes
Path.write_text = _path_write_text


def sha256_file(path, chunk=16 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def pread(path, off, n):
    with open(path, "rb") as f:
        f.seek(off)
        b = f.read(n)
    if len(b) != n:
        die(f"short read {path} @{off}+{n}")
    return b


def regular_file_only(path):
    """Refuse anything that exists and is not a regular file, or is a symlink (never write a block device)."""
    if os.path.lexists(path) and not _stat.S_ISREG(os.lstat(path).st_mode):
        die(f"{path} is not a regular file; refusing to write it")


def copy_range(src, src_off, dst, dst_off, length):
    """Copy bytes between files; uses copy_file_range (btrfs reflink) when possible."""
    regular_file_only(dst)
    with open(src, "rb") as fi, open(dst, "r+b" if os.path.exists(dst) else "w+b") as fo:
        left = length
        so, do = src_off, dst_off
        try:
            while left:
                n = os.copy_file_range(fi.fileno(), fo.fileno(), min(left, 1 << 30), so, do)
                if n <= 0:
                    raise OSError("copy_file_range returned 0")
                left -= n
                so += n
                do += n
        except OSError:
            fi.seek(so)
            fo.seek(do)
            while left:
                b = fi.read(min(left, 16 << 20))
                if not b:
                    die("short read in copy_range")
                fo.write(b)
                left -= len(b)


# ---------------------------------------------------------------- unzip-bin


def cmd_unzip_bin(zip_path, member, out):
    regular_file_only(out)
    with zipfile.ZipFile(zip_path) as z:
        info = z.getinfo(member)
        h = hashlib.sha256()
        with z.open(info) as src, open(out, "wb") as dst:
            while True:
                b = src.read(16 << 20)
                if not b:
                    break
                h.update(b)
                dst.write(b)
    if os.path.getsize(out) != info.file_size:
        die("extracted size mismatch")
    print(h.hexdigest())


# ---------------------------------------------------------------- GPT


def guid_str(b):
    d1, d2, d3 = struct.unpack_from("<IHH", b, 0)
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{b[8:10].hex()}-{b[10:16].hex()}"


def gpt_parts(path):
    hdr = pread(path, 512, 92)
    if hdr[:8] != b"EFI PART":
        die("no GPT in " + str(path))
    ents, nent, esz = struct.unpack_from("<QII", hdr, 72)
    data = pread(path, ents * 512, nent * esz)
    out = []
    for i in range(nent):
        e = data[i * esz:(i + 1) * esz]
        if e[:16] == b"\0" * 16:
            continue
        first, last, attr = struct.unpack_from("<QQQ", e, 32)
        out.append(dict(index=i + 1, name=e[56:128].decode("utf-16le").rstrip("\0"),
                        type=guid_str(e[0:16]), guid=guid_str(e[16:32]),
                        start=first * 512, size=(last - first + 1) * 512, attr=attr))
    return out


def cmd_layout(img, out_env):
    parts = {p["name"]: p for p in gpt_parts(img)}
    want = {"EFI-SYSTEM": "ESP", "super": "SUPER", "vbmeta_a": "VBMETA",
            "boot_a": "BOOT", "vendor_boot_a": "VENDOR_BOOT", "init_boot_a": "INIT_BOOT"}
    lines = []
    for name, key in want.items():
        p = parts.get(name) or die("GPT partition missing: " + name)
        lines += [f"{key}_OFF={p['start']}", f"{key}_SIZE={p['size']}", f"{key}_GUID={p['guid']}"]
    Path(out_env).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


# ---------------------------------------------------------------- boot images


def align(x, a):
    return (x + a - 1) // a * a


def ramdisk_decompress(blob):
    if blob.startswith((b"070701", b"070702")):
        result = blob
    elif blob.startswith(b"\x1f\x8b"):
        result = gzip.decompress(blob)
    elif blob.startswith((b"\x02\x21\x4c\x18", b"\x04\x22\x4d\x18")):
        r = subprocess.run(["lz4", "-dc"], input=blob, capture_output=True)
        if r.returncode != 0:
            die("lz4 -dc failed: " + r.stderr.decode(errors="replace"))
        result = r.stdout
    else:
        die("unsupported ramdisk compression: " + blob[:8].hex())
    if not result.startswith((b"070701", b"070702")):
        die("ramdisk is not a newc archive")
    # The vendor platform ramdisk starts with a microcode CPIO followed by
    # a gzip-compressed platform CPIO. Keep both, in their original order,
    # and normalize to raw archives for the later initrd editor.
    off = 0
    while off < len(result):
        if result[off:off + 6] not in (b"070701", b"070702"):
            die("invalid concatenated ramdisk archive")
        while True:
            header = result[off:off + 110]
            if len(header) != 110 or header[:6] not in (b"070701", b"070702"):
                die("invalid ramdisk CPIO header")
            size, namesz = int(header[54:62], 16), int(header[94:102], 16)
            name = result[off + 110:off + 110 + namesz].rstrip(b"\0")
            off = align(align(off + 110 + namesz, 4) + size, 4)
            if off > len(result):
                die("truncated ramdisk CPIO")
            if name == b"TRAILER!!!":
                break
        while off < len(result) and result[off] == 0:
            off += 1
        if off < len(result) and result[off:off + 6] not in (b"070701", b"070702"):
            result = result[:off] + ramdisk_decompress(result[off:])
            result += bytes(-len(result) % 512)
    return result


def cmd_boot_extract(img, env_file, outdir):
    env = dict(l.split("=", 1) for l in Path(env_file).read_text().split())
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    # boot_a: Android boot image header v4, page size 4096, kernel only.
    b = int(env["BOOT_OFF"])
    h = pread(img, b, 4096)
    if h[:8] != b"ANDROID!":
        die("boot_a magic")
    ksz, rsz = struct.unpack_from("<II", h, 8)
    if struct.unpack_from("<I", h, 40)[0] != 4:
        die("boot_a header version != 4")
    (out / "kernel.efi").write_bytes(pread(img, b + 4096, ksz))
    # vendor_boot_a: header v4 with a ramdisk table and a bootconfig section.
    v = int(env["VENDOR_BOOT_OFF"])
    h = pread(img, v, 4096)
    if h[:8] != b"VNDRBOOT":
        die("vendor_boot magic")
    hv, page = struct.unpack_from("<II", h, 8)
    vrsz = struct.unpack_from("<I", h, 24)[0]
    cmdline = h[28:28 + 2048].split(b"\0")[0]
    hsz, dtbsz = struct.unpack_from("<II", h, 2096)
    tsz, tnum, tesz, bcsz = struct.unpack_from("<IIII", h, 2112)
    if hv != 4:
        die("vendor_boot header version != 4")
    ram_off = v + align(hsz, page)
    tab_off = ram_off + align(vrsz, page) + align(dtbsz, page)
    bc_off = tab_off + align(tsz, page)
    tab = pread(img, tab_off, tsz)
    names = {1: "vendor_platform", 2: "vendor_recovery", 3: "vendor_dlkm"}
    for i in range(tnum):
        e = tab[i * tesz:(i + 1) * tesz]
        sz, off, typ = struct.unpack_from("<III", e, 0)
        cpio = ramdisk_decompress(pread(img, ram_off + off, sz))
        (out / f"{names.get(typ, 'type%d' % typ)}.cpio").write_bytes(cpio)
    (out / "vendor_cmdline.txt").write_bytes(cmdline)
    (out / "vendor_bootconfig.txt").write_bytes(pread(img, bc_off, bcsz))
    # init_boot_a: header v4, ramdisk only.
    ib = int(env["INIT_BOOT_OFF"])
    h = pread(img, ib, 4096)
    if h[:8] != b"ANDROID!":
        die("init_boot magic")
    ksz, rsz = struct.unpack_from("<II", h, 8)
    (out / "init_boot.cpio").write_bytes(ramdisk_decompress(pread(img, ib + 4096 + align(ksz, 4096), rsz)))
    # vbmeta_a: keep exactly the vbmeta struct (header + auth + aux).
    vb = pread(img, int(env["VBMETA_OFF"]), 256)
    if vb[:4] != b"AVB0":
        die("vbmeta_a magic")
    auth, aux = struct.unpack_from(">QQ", vb, 12)
    (out / "vbmeta_a.orig.img").write_bytes(pread(img, int(env["VBMETA_OFF"]), 256 + auth + aux))
    for f in sorted(out.iterdir()):
        print(f"{sha256_file(f)}  {f.name}  {f.stat().st_size}")


# ---------------------------------------------------------------- remote zip


def _http_get(url, a=None, b=None):
    hdr = {"User-Agent": "gbos-base/1"}
    if a is not None:
        hdr["Range"] = f"bytes={a}-{b - 1}"
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.geturl(), r.headers, r.read()


def _resolve(url):
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "gbos-base/1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        cr = r.headers.get("Content-Range") or ""
        if r.status != 206 or "/" not in cr or not cr.split("/")[1].isdigit():
            raise urllib.error.URLError("server ignored Range for " + url)
        return r.geturl(), int(cr.split("/")[1])


class _Remote:
    """A remote file read with HTTP Range through the URL it redirects to.  ci.android.com redirects to a signed
    storage.googleapis.com URL that expires after 300 s (2026-10-13), and fetching all members takes longer than that on
    an ordinary connection: the signed URL is resolved again every 120 s, and once more when it is refused anyway."""

    REFRESH = int(os.environ.get("GBOS_FETCH_REFRESH", "120"))   # the override exists for tests/builder/fetch-zip-test.py

    def __init__(self, url):
        self.url = url
        self.final, self.size = _resolve(url)
        self.at = time.monotonic()

    def _refresh(self):
        final, size = _resolve(self.url)
        if size != self.size:
            die(f"{self.url} changed size while it was being read ({self.size} -> {size})")
        self.final, self.at = final, time.monotonic()

    def get(self, a, b):
        if time.monotonic() - self.at > self.REFRESH:
            try:
                self._refresh()
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
                # the current signed URL is still good for a while: keep it (a refused request re-resolves below)
                print(f"note: re-resolving {self.url} failed ({e}); keeping the current signed URL", file=sys.stderr)
                self.at = time.monotonic()
        try:
            return _http_get(self.final, a, b)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 401, 403):
                raise
            self._refresh()
            return _http_get(self.final, a, b)


def _zip64_extra(extra, usz, csz, off):
    p = 0
    while p + 4 <= len(extra):
        hid, hl = struct.unpack_from("<HH", extra, p)
        if hid == 1:
            q = p + 4
            if usz == 0xFFFFFFFF:
                usz = struct.unpack_from("<Q", extra, q)[0]
                q += 8
            if csz == 0xFFFFFFFF:
                csz = struct.unpack_from("<Q", extra, q)[0]
                q += 8
            if off == 0xFFFFFFFF:
                off = struct.unpack_from("<Q", extra, q)[0]
        p += 4 + hl
    return usz, csz, off


def cmd_fetch_zip(url, outdir, specs):
    """specs: member=sha256[:outname]"""
    rem = _Remote(url)
    size = rem.size
    print(f"remote size {size} via {rem.final.split('?')[0]}")
    _, _, tail = rem.get(max(0, size - 65558), size)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        die("no EOCD")
    n, cdsize, cdoff = struct.unpack_from("<HII", tail, i + 10)
    if cdoff == 0xFFFFFFFF or n == 0xFFFF:
        j = tail.rfind(b"PK\x06\x07")
        z64 = struct.unpack_from("<Q", tail, j + 8)[0]
        _, _, z = rem.get(z64, z64 + 56)
        n, cdsize, cdoff = struct.unpack_from("<QQQ", z, 32)
    _, _, cd = rem.get(cdoff, cdoff + cdsize)
    index = {}
    p = 0
    while p < len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        meth = struct.unpack_from("<H", cd, p + 10)[0]
        crc, csz, usz = struct.unpack_from("<III", cd, p + 16)
        nl, xl, cl = struct.unpack_from("<HHH", cd, p + 28)
        off = struct.unpack_from("<I", cd, p + 42)[0]
        name = cd[p + 46:p + 46 + nl].decode()
        usz, csz, off = _zip64_extra(cd[p + 46 + nl:p + 46 + nl + xl], usz, csz, off)
        index[name] = (meth, crc, csz, usz, off)
        p += 46 + nl + xl + cl
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        member, rest = spec.split("=", 1)
        want, _, outname = rest.partition(":")
        outname = outname or os.path.basename(member)
        if member not in index:
            die("member not in remote zip: " + member)
        meth, crc, csz, usz, off = index[member]
        _, _, lh = rem.get(off, off + 30)
        if lh[:4] != b"PK\x03\x04":
            die("bad local header for " + member)
        nl, xl = struct.unpack_from("<HH", lh, 26)
        start = off + 30 + nl + xl
        dec = zlib.decompressobj(-15) if meth == 8 else None
        if meth not in (0, 8):
            die(f"unsupported zip method {meth} for {member}")
        h = hashlib.sha256()
        c = 0
        tmp = out / (outname + ".part")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            pos = start
            end = start + csz
            while pos < end:
                nxt = min(end, pos + (32 << 20))
                _, _, blob = rem.get(pos, nxt)
                data = dec.decompress(blob) if dec else blob
                f.write(data)
                h.update(data)
                c = zlib.crc32(data, c)
                pos = nxt
            if dec:
                data = dec.flush()
                f.write(data)
                h.update(data)
                c = zlib.crc32(data, c)
        if c != crc or tmp.stat().st_size != usz:
            die(f"crc/size mismatch for {member}")
        got = h.hexdigest()
        if want and got != want:
            die(f"sha256 mismatch for {member}: {got} != {want}")
        tmp.rename(out / outname)
        print(f"{got}  {member} -> {out / outname}")


def cmd_check_sha(specs):
    bad = 0
    for spec in specs:
        path, want = spec.rsplit("=", 1)
        got = sha256_file(path)
        ok = got == want
        bad += not ok
        print(f"{'OK  ' if ok else 'BAD '} {got}  {path}" + ("" if ok else f"  (want {want})"))
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------- systemd-boot configs

# The three generations of the ESP loader configuration.  E = milestone E
# (REPORT.md line 179ff, QEMU COM1), G = milestone G (line 275ff, KANO LPSS
# UART), M = milestone M (line 444, adds console=ttyS4).  The options always
# start from the vendor_boot cmdline of the official image.
LOADER_SHA = {
    "E/loader.conf": "3d15fead6411f58163dc494e2ca1db088cc7a4ea114a67c353767bcce7cbc31b",
    "E/lapis.conf": "014aa0bda921c1c87efbd0ad79c8b56a4a6bb30046e6ea73c783c7d625a2a444",
    "G/loader.conf": "62ab8a1ebbea77bf2d3f9c255de7178bba980d0656c45bfa1871c9bf89aa9c75",
    "G/lapis.conf": "d3050c6ce1023d6015ee74bc6f22fd6b3e9f2b71c550116906be276ca82107bf",
    "M/lapis.conf": "61d3c16d0ec914edc3e395d66383021a7536250169f9b50a26f41296fc492b27",
}


def cmd_loader_confs(cmdline_file, outdir, strict):
    cmd = Path(cmdline_file).read_text()
    if "\n" in cmd or "console=null " not in cmd:
        die("unexpected vendor cmdline")
    out = Path(outdir)
    files = {}
    files["E/loader.conf"] = "default lapis.conf\ntimeout 1\neditor no\n"
    files["E/lapis.conf"] = (
        "title Lapis\nlinux /kernel.efi\ninitrd /initrd.img\n"
        f"options {cmd} console=ttyS0,115200 earlycon=uart8250,io,0x3f8,115200 ignore_loglevel\n")
    files["G/loader.conf"] = "default lapis.conf\ntimeout 10\neditor yes\n"
    uart = ("loglevel=8 ignore_loglevel console=ttyS0,115200n8 "
            "console=uart8250,mmio32,0xfe03e000,115200n8 "
            "earlycon=uart8250,mmio32,0xfe03e000,115200n8 xe.force_probe=46a6")
    g_opts = cmd.replace("console=null ", "", 1) + " " + uart
    files["G/lapis.conf"] = f"title KANO lapis UART\nlinux /kernel.efi\ninitrd /initrd.img\noptions {g_opts}\n"
    files["M/lapis.conf"] = files["G/lapis.conf"].replace(
        "console=ttyS0,115200n8 ", "console=ttyS0,115200n8 console=ttyS4,115200n8 ", 1)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "builder/lib"))
    import lapis
    pins = lapis.load()
    bad = 0
    for k, v in files.items():
        p = out / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(v)
        got = sha256_file(p)
        ok = got == pins.get("LOADER_" + k.replace("/", "_").replace(".", "_").upper() + "_SHA256", LOADER_SHA[k])
        bad += not ok
        print(f"{'OK ' if ok else 'BAD'} {got}  {k}")
    if bad and strict:
        die("loader config hashes differ from kano-real.img")


# ---------------------------------------------------------------- initrd / bootconfig


def bootconfig_trailer(text: bytes):
    return text + struct.pack("<II", len(text), sum(text) & 0xFFFFFFFF) + b"#BOOTCONFIG\n"


def split_bootconfig(data: bytes):
    magic = b"#BOOTCONFIG\n"
    if not data.endswith(magic):
        die("no bootconfig trailer")
    end = len(data) - len(magic)
    size, csum = struct.unpack_from("<II", data, end - 8)
    start = end - 8 - size
    bc = data[start:start + size]
    if (sum(bc) & 0xFFFFFFFF) != csum:
        die("bootconfig checksum mismatch")
    return data[:start], bc


def cmd_make_initrd(args):
    """make-initrd OUT BASE_BOOTCONFIG cpio... -- key=value..."""
    out, base = args[0], args[1]
    rest = args[2:]
    sep = rest.index("--")
    cpios, kvs = rest[:sep], rest[sep + 1:]
    text = Path(base).read_bytes()
    if not text.endswith(b"\n"):
        die("base bootconfig must end with newline")
    for kv in kvs:
        if "=" not in kv or "\n" in kv:
            die("bad bootconfig line " + kv)
        text += kv.encode() + b"\n"
    with open(out, "wb") as f:
        for c in cpios:
            f.write(Path(c).read_bytes())
        f.write(bootconfig_trailer(text))
    print(f"{sha256_file(out)}  {out}  bootconfig {len(text)} bytes")


# ---------------------------------------------------------------- FAT32 in-place rewrite


class Fat32:
    def __init__(self, path, base=0):
        self.path, self.base = path, base
        bs = pread(path, base, 512)
        self.bps, self.spc, self.rsv, self.nfats = struct.unpack_from("<HBHB", bs, 11)
        self.fatlen = struct.unpack_from("<I", bs, 36)[0]
        self.root = struct.unpack_from("<I", bs, 44)[0]
        if bs[82:90] != b"FAT32   ":
            die("not FAT32")
        self.csz = self.bps * self.spc
        self.data0 = (self.rsv + self.nfats * self.fatlen) * self.bps
        self.fat = pread(path, base + self.rsv * self.bps, self.fatlen * self.bps)

    def chain(self, c):
        out = []
        while 2 <= c < 0x0FFFFFF8:
            out.append(c)
            c = struct.unpack_from("<I", self.fat, c * 4)[0] & 0x0FFFFFFF
        return out

    def coff(self, c):
        return self.base + self.data0 + (c - 2) * self.csz

    def entries(self, first_cluster):
        """Yield (long_name, short_name, abs_offset_of_entry, entry_bytes)."""
        lfn = {}
        for c in self.chain(first_cluster):
            blob = pread(self.path, self.coff(c), self.csz)
            for i in range(0, self.csz, 32):
                e = blob[i:i + 32]
                if e[0] == 0:
                    return
                if e[0] == 0xE5:
                    lfn = {}
                    continue
                if e[11] == 0x0F:
                    part = (e[1:11] + e[14:26] + e[28:32]).decode("utf-16le", "replace")
                    lfn[e[0] & 0x1F] = part.split("\0")[0]
                    continue
                base, ext = e[0:8].decode().rstrip(), e[8:11].decode().rstrip()
                if e[12] & 0x08:  # NT "lowercase base name" flag (what mtools writes)
                    base = base.lower()
                if e[12] & 0x10:  # NT "lowercase extension" flag
                    ext = ext.lower()
                short = base + ("." + ext if ext else "")
                ln = "".join(lfn[k] for k in sorted(lfn)) if lfn else short
                lfn = {}
                yield ln, short, self.coff(c) + i, e

    def lookup(self, path):
        cl = self.root
        comps = [x for x in path.split("/") if x]
        for n, comp in enumerate(comps):
            for ln, short, off, e in self.entries(cl):
                if comp.lower() in (ln.lower(), short.lower()):
                    hi, lo = struct.unpack_from("<H", e, 20)[0], struct.unpack_from("<H", e, 26)[0]
                    cl = (hi << 16) | lo
                    if n == len(comps) - 1:
                        return off, e, cl
                    break
            else:
                die("not found in FAT: " + path)
        die("bad path " + path)


def cmd_esp_rewrite(esp, fatpath, newfile):
    """Overwrite a file's data in its existing cluster chain and update its size.
    FAT, FSInfo and the directory timestamps stay untouched (this is what the
    milestone-M edit did; mcopy -o would allocate new clusters instead)."""
    regular_file_only(esp)
    fs = Fat32(esp)
    off, e, cl = fs.lookup(fatpath)
    ch = fs.chain(cl)
    data = Path(newfile).read_bytes()
    cap = len(ch) * fs.csz
    if len(data) > cap:
        die(f"{fatpath}: {len(data)} bytes do not fit in {len(ch)} clusters")
    padded = data + b"\0" * (cap - len(data))
    with open(esp, "r+b") as f:
        for i, c in enumerate(ch):
            f.seek(fs.coff(c))
            f.write(padded[i * fs.csz:(i + 1) * fs.csz])
        f.seek(off + 28)
        f.write(struct.pack("<I", len(data)))
    print(f"rewrote {fatpath}: {len(data)} bytes in clusters {ch[0]}..{ch[-1]}")


# ---------------------------------------------------------------- LP metadata


def lp_parse(path, base):
    g = pread(path, base + 4096, 4096)
    magic = struct.unpack_from("<I", g, 0)[0]
    if magic != 0x616C4467:
        die("no LP geometry")
    meta_max, slots, lbs = struct.unpack_from("<III", g, 40)
    hoff = base + 3 * 4096
    h = pread(path, hoff, 256)
    if struct.unpack_from("<I", h, 0)[0] != 0x414C5030:
        die("no LP header")
    major, minor, hsz = struct.unpack_from("<HHI", h, 4)
    tables_size = struct.unpack_from("<I", h, 44)[0]
    flags = struct.unpack_from("<I", h, 128)[0] if hsz >= 132 else 0
    t = pread(path, hoff + hsz, tables_size)

    def desc(o):
        return struct.unpack_from("<III", h, o)

    pt, ext, grp, bd = desc(80), desc(92), desc(104), desc(116)
    exts = [struct.unpack_from("<QIQI", t, ext[0] + i * ext[2]) for i in range(ext[1])]
    groups = []
    for i in range(grp[1]):
        o = grp[0] + i * grp[2]
        groups.append(dict(name=t[o:o + 36].split(b"\0")[0].decode(),
                           flags=struct.unpack_from("<I", t, o + 36)[0],
                           max_size=struct.unpack_from("<Q", t, o + 40)[0]))
    bdevs = []
    for i in range(bd[1]):
        o = bd[0] + i * bd[2]
        first, al, aoff, size = struct.unpack_from("<QIIQ", t, o)
        bdevs.append(dict(name=t[o + 24:o + 60].split(b"\0")[0].decode(), first_logical_sector=first,
                          alignment=al, alignment_offset=aoff, size=size,
                          flags=struct.unpack_from("<I", t, o + 60)[0]))
    parts = []
    for i in range(pt[1]):
        o = pt[0] + i * pt[2]
        name = t[o:o + 36].split(b"\0")[0].decode()
        attr, fi, ne, gi = struct.unpack_from("<IIII", t, o + 36)
        e = [dict(sectors=exts[j][0], type=exts[j][1], data=exts[j][2], source=exts[j][3])
             for j in range(fi, fi + ne)]
        parts.append(dict(name=name, attrs=attr, group=groups[gi]["name"],
                          size=sum(x["sectors"] for x in e) * 512, extents=e))
    return dict(geometry=dict(metadata_max_size=meta_max, metadata_slot_count=slots, logical_block_size=lbs),
                header=dict(major=major, minor=minor, header_size=hsz, flags=flags),
                groups=groups, block_devices=bdevs, partitions=parts)


def cmd_lp_extract(img, super_off, outdir):
    base = int(super_off)
    meta = lp_parse(img, base)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    for p in meta["partitions"]:
        if not p["extents"]:
            continue
        dst = out / (p["name"] + ".img")
        if dst.exists():
            dst.unlink()
        dst.touch()
        pos = 0
        for e in p["extents"]:
            if e["type"] != 0:
                die("non-linear LP extent in " + p["name"])
            n = e["sectors"] * 512
            copy_range(img, base + e["data"] * 512, dst, pos, n)
            pos += n
        p["sha256"] = sha256_file(dst)
        print(f"{p['sha256']}  {p['name']}  {p['size']}")
    (out / "lp.json").write_text(json.dumps(meta, indent=1))


def cmd_lp_build(lp_json, imgdir, out, overrides):
    # lpmake opens its --output by name: it writes a new file next to <out>, which replaces <out> only when it is a
    # regular file (a symlink or device at <out> is refused first and never opened)
    regular_file_only(out)
    meta = json.loads(Path(lp_json).read_text())
    ov = dict(o.split("=", 1) for o in overrides)
    g = meta["geometry"]
    if len(meta["block_devices"]) != 1:
        die("expected one super block device")
    bdev = meta["block_devices"][0]
    if bdev["alignment"] != 1048576 or bdev["alignment_offset"] != 0:
        die("unexpected LP alignment")
    args = ["lpmake", "--metadata-size", str(g["metadata_max_size"]), "--super-name", bdev["name"],
            "--metadata-slots", str(g["metadata_slot_count"]), "--device-size", str(bdev["size"])]
    if meta["header"]["flags"] & 1:
        args.append("--virtual-ab")
    for grp in meta["groups"]:
        if grp["name"] != "default":
            args += ["--group", f"{grp['name']}:{grp['max_size']}"]
    for p in meta["partitions"]:
        attrs = "readonly" if p["attrs"] == 1 else ("none" if p["attrs"] == 0 else die("attrs"))
        img = ov.get(p["name"])
        if img is None and p["extents"]:
            img = str(Path(imgdir) / (p["name"] + ".img"))
        size = os.path.getsize(img) if img else 0
        args += ["--partition", f"{p['name']}:{attrs}:{size}:{p['group']}"]
        if img:
            args += ["--image", f"{p['name']}={img}"]
    tmp = f"{out}.lpmake-new"
    regular_file_only(tmp)
    if os.path.lexists(tmp):
        os.unlink(tmp)
    args += ["--output", tmp]
    print("+ " + " ".join(args), flush=True)
    subprocess.check_call(args)
    regular_file_only(tmp)
    regular_file_only(out)
    os.replace(tmp, out)


# ---------------------------------------------------------------- EROFS walker

XATTR_PREFIX = {1: "user.", 2: "system.posix_acl_access", 3: "system.posix_acl_default",
                4: "trusted.", 5: "lustre.", 6: "security.", 7: "system."}


class Erofs:
    def __init__(self, path):
        self.f = open(path, "rb")
        sb = self.read(1024, 128)
        if struct.unpack_from("<I", sb, 0)[0] != 0xE0F5E1E2:
            die("not EROFS: " + str(path))
        self.blksz = 1 << sb[12]
        self.root = struct.unpack_from("<H", sb, 14)[0]
        self.meta = struct.unpack_from("<I", sb, 40)[0]
        self.xattr = struct.unpack_from("<I", sb, 44)[0]
        self.uuid = sb[48:64]

    def read(self, off, n):
        self.f.seek(off)
        b = self.f.read(n)
        if len(b) != n:
            die("short EROFS read")
        return b

    def _xentry(self, off):
        nlen, nidx, vsize = struct.unpack("<BBH", self.read(off, 4))
        raw = self.read(off + 4, nlen + vsize)
        name = XATTR_PREFIX.get(nidx, f"idx{nidx}.") + raw[:nlen].decode("latin1")
        return name, raw[nlen:], (4 + nlen + vsize + 3) & ~3

    def inode(self, nid):
        off = self.meta * self.blksz + nid * 32
        b = self.read(off, 64)
        fmt, xcount, mode = struct.unpack_from("<HHH", b, 0)
        ext = fmt & 1
        layout = (fmt >> 1) & 7
        if ext:
            size, = struct.unpack_from("<Q", b, 8)
            iu, = struct.unpack_from("<I", b, 16)
            uid, gid = struct.unpack_from("<II", b, 24)
            nlink, = struct.unpack_from("<I", b, 44)
            isize = 64
        else:
            nlink, size = struct.unpack_from("<HI", b, 6)
            iu, = struct.unpack_from("<I", b, 16)
            uid, gid = struct.unpack_from("<HH", b, 24)
            isize = 32
        xsize = 0
        selinux = caps = None
        if xcount:
            xsize = 12 + 4 * (xcount - 1)
            xoff = off + isize
            hdr = self.read(xoff, 12)
            shared = hdr[4]
            ids = struct.unpack_from(f"<{shared}I", self.read(xoff + 12, 4 * shared)) if shared else ()
            items = []
            ip, end = xoff + 12 + 4 * shared, xoff + xsize
            while ip + 4 <= end:
                name, val, esz = self._xentry(ip)
                items.append((name, val))
                ip += esz
            for sid in ids:
                name, val, _ = self._xentry(self.xattr * self.blksz + sid * 4)
                items.append((name, val))
            for name, val in items:
                if name == "security.selinux":
                    selinux = val.split(b"\0", 1)[0].decode()
                elif name == "security.capability":
                    caps = val.hex()
        return dict(off=off, isize=isize, xsize=xsize, layout=layout, size=size, iu=iu,
                    mode=mode, uid=uid, gid=gid, nlink=nlink, extended=ext, selinux=selinux, caps=caps)

    def dir_entries(self, ino):
        if ino["layout"] not in (0, 2):
            die(f"compressed/chunked directory layout {ino['layout']} not supported")
        size = ino["size"]
        nfull = size // self.blksz if ino["layout"] == 2 else (size + self.blksz - 1) // self.blksz
        chunks = []
        for i in range(nfull):
            n = min(self.blksz, size - i * self.blksz)
            chunks.append(self.read((ino["iu"] + i) * self.blksz, n))
        if ino["layout"] == 2 and size % self.blksz:
            chunks.append(self.read(ino["off"] + ino["isize"] + ino["xsize"], size % self.blksz))
        for c in chunks:
            first = struct.unpack_from("<H", c, 8)[0]
            cnt = first // 12
            ents = [struct.unpack_from("<QHB", c, i * 12) for i in range(cnt)]
            for i, (nid, noff, ftype) in enumerate(ents):
                nend = ents[i + 1][1] if i + 1 < cnt else len(c)
                name = c[noff:nend].split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
                if name in (".", ".."):
                    continue
                yield nid, ftype, name

    def walk(self):
        out = {}

        def rec(nid, rel):
            ino = self.inode(nid)
            out[rel] = ino
            for cnid, ftype, name in self.dir_entries(ino):
                child = name if rel == "" else rel + "/" + name
                if ftype == 2:
                    rec(cnid, child)
                else:
                    out[child] = self.inode(cnid)

        rec(self.root, "")
        return out


def erofs_meta(path):
    m = Erofs(path).walk()
    return {k: dict(mode=v["mode"] & 0o7777, ftype=v["mode"] >> 12, uid=v["uid"], gid=v["gid"],
                    selinux=v["selinux"], caps=v["caps"], nlink=v["nlink"], extended=v["extended"])
            for k, v in m.items()}


def cmd_erofs_meta(img, out):
    m = erofs_meta(img)
    Path(out).write_text(json.dumps(m, indent=0, sort_keys=True))
    print(f"{len(m)} inodes; extended {sum(v['extended'] for v in m.values())}; "
          f"caps {sum(1 for v in m.values() if v['caps'])}; "
          f"hardlinks {sum(1 for v in m.values() if v['nlink'] > 1 and v['ftype'] != 4)}; "
          f"no-selinux {sum(1 for v in m.values() if not v['selinux'])}")


# ---------------------------------------------------------------- fs_config / file_contexts
# Same generator the 2026-10-11 session used (work/src/rebuild.py write_configs):
# one exact-path regex per inode, sorted by (length, path), labels copied from
# the original image's xattrs; uid/gid/mode copied from the original inode.


def esc_regex(path):
    return re.sub(r"([.^$\\*+?()\[\]{}|])", r"\\\1", path)


def write_configs(mount, root: Path, meta, out_fc: Path, out_fs: Path):
    rows = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        real = []
        for name in dirnames:
            rel = name if rel_dir == "" else rel_dir + "/" + name
            rows[rel] = meta[rel]
            if not os.path.islink(os.path.join(dirpath, name)):
                real.append(name)
        dirnames[:] = real
        for name in filenames:
            rel = name if rel_dir == "" else rel_dir + "/" + name
            rows[rel] = meta[rel]
    rows[""] = meta[""]
    fc, fs, missing = [], [], []
    for rel, info in sorted(rows.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if not info.get("selinux"):
            missing.append(rel)
            continue
        if info.get("caps"):
            die("capability xattr on " + rel + " is not supported by this generator")
        full = mount if rel == "" else mount + "/" + rel
        fc.append(f"{esc_regex(full)} {info['selinux']}")
        mode = f"0{info['mode']:o}"
        if rel == "":
            fs.append(f" {info['uid']} {info['gid']} {mode}")
        else:
            fs.append(f"{mount[1:]}/{rel} {info['uid']} {info['gid']} {mode}")
    if missing:
        die("missing selinux label for: " + ", ".join(missing[:20]))
    out_fc.write_text("\n".join(fc) + "\n")
    out_fs.write_text("\n".join(fs) + "\n")
    return len(fc)


def replace_once(text, old, new, what):
    if text.count(old) != 1:
        die(f"{what}: expected exactly one '{old.strip()[:60]}'")
    return text.replace(old, new, 1)


# Files the 2026-10-11 session added to vendor (REPORT.md milestone L, line 368ff).
GATEKEEPER_RC = (
    "# Disabled so it does not register IGatekeeper/default.\n"
    "# com.android.hardware.gatekeeper.nonsecure owns that instance.\n"
    "service vendor.gatekeeper_default /vendor/bin/hw/android.hardware.gatekeeper-service.trusty \\\n"
    "                                          --dev ${ro.hardware.trusty_ipc_dev.gatekeeper:-/dev/trusty-ipc-dev0}\n"
    "    disabled\n"
    "    user system\n"
    "    group system\n")
MAINT_RC = (
    "# shell can run service check but cannot write /dev/kmsg. It records the\n"
    "# result in a debug property. vendor_init can read that property and can\n"
    "# write kmsg. late_start is after earlyBootEnded.\n"
    "service maint-kmsg /system/bin/sh /vendor/bin/maint-kmsg.sh\n"
    "    class late_start\n"
    "    user shell\n"
    "    group shell log readproc\n"
    "    seclabel u:r:shell:s0\n"
    "    oneshot\n"
    "\n"
    "on property:debug.maint.ready=1\n"
    "    write /dev/kmsg \"<14>maintprobe: ${debug.maint.line}\"\n")
MAINT_SH = (
    "#!/system/bin/sh\n"
    "out=$(/system/bin/service check android.security.maintenance)\n"
    "/system/bin/setprop debug.maint.line \"$out\"\n"
    "/system/bin/setprop debug.maint.ready 1\n")
# sha256 of the edited files as they are inside kano-real.img (vendor_a /
# system_ext_a extracted with fsck.erofs on 2026-10-12).
EDITED_SHA = {
    "etc/init/android.hardware.gatekeeper-service.trusty.rc":
        "8f38e6c52ed40ddf498c769fdb3a4a39404bd95fff91575dc35ea6fcd080d9a1",
    "etc/init/maint-kmsg.rc": "d9a2c218031a57ac1c01e148f52765fda3023ca8d1a5a72d11f03d8fd91618fc",
    "bin/maint-kmsg.sh": "5f2202904a1aea5f1d8125f4f22e81b8c17387cc61f36d8a1d7e0023be492154",
    "build.prop": "d3d938b4ca751f8aaa680b100bf8486e3bf81a2cef1eba51c8a9e6431946b6af",
    "system_ext:etc/vintf/manifest.xml": "4e393ce518613aa273dc2b2cadddbed30b8588bab6692f2dd461ac0545cbc34e",
}


def check_edited(root, rel, key=None):
    got = sha256_file(Path(root) / rel)
    want = EDITED_SHA[key or rel]
    build_fields = {"build.prop": "EDITED_VENDOR_BUILD_PROP_SHA256",
                    "system_ext:etc/vintf/manifest.xml": "EDITED_SYSTEM_EXT_MANIFEST_SHA256"}
    if (key or rel) in build_fields:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "builder/lib"))
        import lapis
        want = lapis.load()[build_fields[key or rel]]
    if got != want:
        die(f"{rel}: sha256 {got} differs from the selected Lapis build ({want})")


def cmd_edit_vendor(tree, meta_json, keymint, gatekeeper, fc_out, fs_out):
    v = Path(tree)
    meta = json.loads(Path(meta_json).read_text())
    # 1. strongbox KeyMint APEX would keep declaring IKeyMintDevice/strongbox
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "builder/lib"))
    import lapis
    strongbox = "apex/com.android.hardware.keymint.strongbox.desktop.apex"
    if lapis.load().get("VENDOR_HAS_STRONGBOX_APEX", "1") == "1":
        (v / strongbox).unlink()
        meta.pop(strongbox)
    elif (v / strongbox).exists() or strongbox in meta:
        die("unexpected strongbox APEX in selected recovery")
    # 2. software KeyMint + Gatekeeper APEXes from Cuttlefish build 16373615
    for src, name in ((keymint, "com.android.hardware.keymint.rust_nonsecure.apex"),
                      (gatekeeper, "com.android.hardware.gatekeeper.nonsecure.apex")):
        with open(src, "rb") as fi, open(v / "apex" / name, "wb") as fo:
            shutil.copyfileobj(fi, fo, 16 << 20)
        os.chmod(v / "apex" / name, 0o644)
        meta["apex/" + name] = dict(meta["apex/com.android.hardware.cas.apex"])
    # 3. Trusty gatekeeper: class hal -> disabled, drop its VINTF fragment
    (v / "etc/init/android.hardware.gatekeeper-service.trusty.rc").write_text(GATEKEEPER_RC)
    (v / "etc/vintf/manifest/android.hardware.gatekeeper-service.trusty.xml").unlink()
    meta.pop("etc/vintf/manifest/android.hardware.gatekeeper-service.trusty.xml")
    # 4. build.prop: no protected VM, default keystore/gatekeeper HALs
    bp = (v / "build.prop").read_text(encoding="latin-1")
    bp = replace_once(bp, "ro.boot.hypervisor.protected_vm.supported=1\n",
                      "ro.boot.hypervisor.protected_vm.supported=0\n", "build.prop")
    bp = replace_once(bp, "ro.hardware.keystore=trusty\n", "ro.hardware.keystore=default\n", "build.prop")
    bp = replace_once(bp, "ro.hardware.gatekeeper=trusty\n", "ro.hardware.gatekeeper=default\n", "build.prop")
    (v / "build.prop").write_text(bp, encoding="latin-1")
    # 5. maint-kmsg probe (writes 'maintprobe: ...' to kmsg, REPORT.md line 388)
    (v / "bin/maint-kmsg.sh").write_text(MAINT_SH)
    os.chmod(v / "bin/maint-kmsg.sh", 0o755)
    (v / "etc/init/maint-kmsg.rc").write_text(MAINT_RC)
    meta["bin/maint-kmsg.sh"] = dict(mode=0o755, ftype=8, uid=0, gid=2000, nlink=1, extended=0,
                                     selinux="u:object_r:vendor_shell_exec:s0", caps=None)
    meta["etc/init/maint-kmsg.rc"] = dict(mode=0o644, ftype=8, uid=0, gid=0, nlink=1, extended=0,
                                          selinux="u:object_r:vendor_configs_file:s0", caps=None)
    for rel in ("etc/init/android.hardware.gatekeeper-service.trusty.rc", "etc/init/maint-kmsg.rc",
                "bin/maint-kmsg.sh", "build.prop"):
        check_edited(v, rel)
    n = write_configs("/vendor", v, meta, Path(fc_out), Path(fs_out))
    print(f"vendor tree edited; {n} config rows")


SYSEXT_DROP = [
    """    <hal format="aidl" updatable-via-system="true">
        <name>android.hardware.security.keymint</name>
        <version>5</version>
        <fqname>IKeyMintDevice/default</fqname>
    </hal>
""",
    """    <hal format="aidl" updatable-via-system="true">
        <name>android.hardware.security.keymint</name>
        <version>3</version>
        <fqname>IRemotelyProvisionedComponent/default</fqname>
    </hal>
""",
    """    <hal format="aidl" updatable-via-system="true">
        <name>android.hardware.security.secureclock</name>
        <fqname>ISecureClock/default</fqname>
    </hal>
""",
    """    <hal format="aidl" exclusive-to="virtual-machine">
        <name>android.hardware.security.sharedsecret</name>
        <accessor>android.os.IAccessor/sharedsecret/security_vm_shared_secret</accessor>
        <fqname>ISharedSecret/security_vm</fqname>
    </hal>
""",
]


def cmd_edit_system_ext(tree, meta_json, fc_out, fs_out):
    s = Path(tree)
    meta = json.loads(Path(meta_json).read_text())
    man = s / "etc/vintf/manifest.xml"
    text = man.read_text(encoding="latin-1")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "builder/lib"))
    import lapis
    # Some recovery manifests no longer declares the first three HALs.
    blocks = SYSEXT_DROP[-1:] if lapis.load().get("SYSEXT_SECURITY_LAYOUT") == "vm-only" else SYSEXT_DROP
    for block in blocks:
        text = replace_once(text, block, "", "system_ext manifest.xml")
    man.write_text(text, encoding="latin-1")
    check_edited(s, "etc/vintf/manifest.xml", "system_ext:etc/vintf/manifest.xml")
    n = write_configs("/system_ext", s, meta, Path(fc_out), Path(fs_out))
    print(f"system_ext tree edited; {n} config rows")


def cmd_erofs_uuid(img):
    import uuid
    print(uuid.UUID(bytes=Erofs(img).uuid))


def cmd_avb_orig_size(img):
    """Original (filesystem) size from an AVB footer at the end of a partition image."""
    size = os.path.getsize(img)
    foot = pread(img, size - 64, 64)
    if foot[:4] != b"AVBf":
        die("no AVB footer on " + img)
    print(struct.unpack_from(">Q", foot, 12)[0])


def cmd_footer_props(avbtool, img):
    out = subprocess.check_output([avbtool, "info_image", "--image", img], text=True)
    for line in out.splitlines():
        m = re.match(r"\s+Prop: (\S+) -> '(.*)'$", line)
        if m:
            print(f"{m.group(1)}:{m.group(2)}")


# ---------------------------------------------------------------- splice / vbmeta


def cmd_splice(img, off, part_size, src):
    off, part_size = int(off), int(part_size)
    regular_file_only(img)
    n = os.path.getsize(src)
    if n > part_size:
        die(f"{src} ({n}) larger than partition ({part_size})")
    if os.path.getsize(img) < off + part_size:
        die("splice would extend the image")
    copy_range(src, 0, img, off, n)
    if part_size > n:
        with open(img, "r+b") as f:
            f.seek(off + n)
            left = part_size - n
            z = b"\0" * min(left, 16 << 20)
            while left:
                f.write(z[:min(left, len(z))])
                left -= min(left, len(z))
    print(f"spliced {src} ({n} bytes) at {off}, zero-filled to {part_size}")


def vbmeta_struct(b):
    if b[:4] != b"AVB0":
        die("not a vbmeta image")
    auth, aux = struct.unpack_from(">QQ", b, 12)
    return b[:256 + auth + aux]


def cmd_vbmeta_digest(img):
    s = vbmeta_struct(Path(img).read_bytes())
    print(f"{sha256_bytes(s)} {len(s)}")


# ---------------------------------------------------------------- main


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        sys.exit(2)
    c, r = a[0], a[1:]
    table = {
        "unzip-bin": lambda: cmd_unzip_bin(*r),
        "layout": lambda: cmd_layout(*r),
        "boot-extract": lambda: cmd_boot_extract(*r),
        "fetch-zip": lambda: cmd_fetch_zip(r[0], r[1], r[2:]),
        "check-sha": lambda: cmd_check_sha(r),
        "loader-confs": lambda: cmd_loader_confs(r[0], r[1], len(r) > 2 and r[2] == "strict"),
        "make-initrd": lambda: cmd_make_initrd(r),
        "esp-rewrite": lambda: cmd_esp_rewrite(*r),
        "lp-extract": lambda: cmd_lp_extract(*r),
        "lp-build": lambda: cmd_lp_build(r[0], r[1], r[2], r[3:]),
        "erofs-meta": lambda: cmd_erofs_meta(*r),
        "erofs-uuid": lambda: cmd_erofs_uuid(*r),
        "avb-orig-size": lambda: cmd_avb_orig_size(*r),
        "edit-vendor": lambda: cmd_edit_vendor(*r),
        "edit-system-ext": lambda: cmd_edit_system_ext(*r),
        "footer-props": lambda: cmd_footer_props(*r),
        "splice": lambda: cmd_splice(*r),
        "vbmeta-digest": lambda: cmd_vbmeta_digest(*r),
    }
    if c not in table:
        die("unknown subcommand " + c)
    table[c]()


if __name__ == "__main__":
    main()
