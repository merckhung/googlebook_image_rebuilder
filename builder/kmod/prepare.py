#!/usr/bin/env python3
"""Prepare what builder/kmod/build-gbos_efifb.sh needs, from the lapis base image itself (no kernel source tree):
  <out>/kh/           the kernel's own headers: kheaders.ko (system_dlkm) embeds them as a .tar.xz
  <out>/symvers.txt   "0x<crc> <name>" for every unprotected vmlinux export: __ksymtab + __kcrctab of the vmlinux in kernel.efi
usage: prepare.py <base image> <otatools dir> <out dir> [kernel.efi from raw recovery]
Needs: mtools (mcopy), lz4, xz, tar. The kernel must be the pinned GKI build (vermagic checked)."""
import hashlib, os, shutil, struct, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import lapis
PINS = lapis.load()
RELEASE = PINS.get("GKI_RELEASE", "6.18.21-android17-5-g3938c8d97142-ab16302624")
VERMAGIC = (RELEASE + " SMP preempt mod_unload modversions ").encode()
base, ota, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
if out.exists() and any(out.iterdir()):
    sys.exit(f"{out} must be new or empty")
out.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="gbos-kmod-"))

def run(*a, **k):
    return subprocess.run(list(map(str, a)), check=True, **k)

def sections(d):
    shoff, = struct.unpack_from("<Q", d, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 0x3a)
    raw = [struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shentsize) for i in range(shnum)]
    stroff = raw[shstrndx][4]
    nm = lambda o: d[stroff + o:d.index(b"\0", stroff + o)].decode()
    return {nm(r[0]): dict(type=r[1], addr=r[3], off=r[4], size=r[5], link=r[6]) for r in raw}, raw

def gpt_part(img, name):
    with open(img, "rb") as f:
        f.seek(512); h = f.read(92)
        lba, n, esz = struct.unpack_from("<QII", h, 72)
        f.seek(lba * 512); t = f.read(n * esz)
    for i in range(n):
        e = t[i * esz:(i + 1) * esz]
        if e[56:128].decode("utf-16le").rstrip("\0") == name:
            return struct.unpack_from("<Q", e, 32)[0] * 512
    sys.exit(f"no {name} partition")

try:
    # 1. vmlinux out of kernel.efi (x86 bzImage, LZ4 legacy payload + 4-byte size)
    if len(sys.argv) == 5:
        shutil.copyfile(sys.argv[4], tmp / "kernel.efi")
    else:
        esp = gpt_part(base, "EFI-SYSTEM")
        run("mcopy", "-o", "-i", f"{base}@@{esp}", "::/kernel.efi", tmp / "kernel.efi", env={**os.environ, "MTOOLS_SKIP_CHECK": "1"})
    k = (tmp / "kernel.efi").read_bytes()
    if hashlib.sha256(k).hexdigest() != PINS["EXPECT"]["kernel.efi"]:
        sys.exit("kernel does not match the selected recovery build")
    setup = (k[0x1f1] or 4) + 1
    poff, plen = struct.unpack_from("<II", k, 0x248)
    payload = k[setup * 512 + poff:setup * 512 + poff + plen]
    if payload[:4] != b"\x02\x21\x4c\x18":
        sys.exit("kernel payload is not LZ4 legacy")
    (tmp / "vmlinux.lz4").write_bytes(payload[:-4])
    run("lz4", "-d", "-q", "-f", tmp / "vmlinux.lz4", tmp / "vmlinux")
    v = (tmp / "vmlinux").read_bytes()
    if v[:4] != b"\x7fELF" or VERMAGIC.split(b" ")[0] not in v:
        sys.exit("vmlinux is not the pinned GKI kernel")
    s, _ = sections(v)
    def cstr(addr):
        for x in s.values():
            if x["addr"] and x["addr"] <= addr < x["addr"] + x["size"]:
                o = x["off"] + addr - x["addr"]
                return v[o:v.index(b"\0", o)].decode()
        raise SystemExit(f"address {addr:#x} outside the sections")
    st, cr = s["__ksymtab"], s["__kcrctab"]
    n = st["size"] // 12
    if cr["size"] != 4 * n:
        sys.exit(f"__kcrctab has {cr['size'] // 4} entries for {n} exports")
    flags = s.get("__kflagstab")
    if flags is None or flags["size"] != n:
        sys.exit("missing or malformed GKI symbol-protection flags")
    lines, protected = [], []
    for i in range(n):
        _val, name_off, _ns = struct.unpack_from("<iii", v, st["off"] + 12 * i)
        name = cstr(st["addr"] + 12 * i + 4 + name_off)
        crc, = struct.unpack_from("<I", v, cr["off"] + 4 * i)
        if v[flags["off"] + i] & 2:  # KSYM_FLAG_PROTECTED
            protected.append(name + "\n")
        else:
            lines.append(f"0x{crc:08x} {name}\n")
    (out / "symvers.txt").write_text("".join(lines))
    (out / "protected-symbols.txt").write_text("".join(protected))
    print("symvers", len(lines), "usable exports;", len(protected), "protected exports excluded")
    # 2. kheaders.ko from system_dlkm -> kernel_headers_data .. kernel_headers_data_end = .tar.xz
    run(sys.executable, HERE.parent / "lib/lp-dump.py", base, tmp, "system_dlkm_a", stdout=subprocess.DEVNULL)
    run(ota / "bin/fsck.erofs", f"--extract={tmp / 'sdlkm'}", "--overwrite", tmp / "system_dlkm_a.img",
        env={**os.environ, "LD_LIBRARY_PATH": str(ota / "lib64")}, stdout=subprocess.DEVNULL)
    kos = list((tmp / "sdlkm").rglob("kheaders.ko"))
    if len(kos) != 1:
        sys.exit(f"expected one kheaders.ko in system_dlkm, found {len(kos)}")
    d = kos[0].read_bytes()
    if VERMAGIC not in d:
        sys.exit("kheaders.ko is for another kernel")
    s, raw = sections(d)
    sym = s[".symtab"]; strs = raw[sym["link"]][4]
    marks = {}
    for j in range(sym["size"] // 24):
        nmo, _info, _other, shndx, val, _sz = struct.unpack_from("<IBBHQQ", d, sym["off"] + 24 * j)
        nm = d[strs + nmo:d.index(b"\0", strs + nmo)].decode()
        if nm in ("kernel_headers_data", "kernel_headers_data_end"):
            marks[nm] = (shndx, val)
    (si, a), (sj, b) = marks["kernel_headers_data"], marks["kernel_headers_data_end"]
    if si != sj:
        sys.exit("kheaders markers in different sections")
    blob = d[raw[si][4] + a:raw[si][4] + b]
    if blob[:6] != b"\xfd7zXZ\0":
        sys.exit("kheaders payload is not xz")
    (tmp / "kheaders.tar.xz").write_bytes(blob)
    (out / "kh").mkdir()
    run("tar", "-xJf", tmp / "kheaders.tar.xz", "-C", out / "kh")
    if not (out / "kh/include/generated/autoconf.h").is_file():
        sys.exit("kheaders lack include/generated/autoconf.h")
    print("kheaders", sum(1 for _ in (out / "kh").rglob("*.h")), "headers")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
