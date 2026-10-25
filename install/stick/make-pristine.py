#!/usr/bin/env python3
"""Prepare what the stick installer needs from a finished googlebook-kano.img: the image's first MiB (protective MBR and
primary GPT) and every partition except super and userdata, zstd-compressed, plus a manifest with each partition's
place, attributes, type and SHA-256 (and the SHA-256 of the first MiB). The installer copies super from the stick itself
and checks it, the first MiB and every partition file against this manifest before it writes anything.
Manifest lines: "image <file name> <size>", "head 0 1048576 <sha256>",
                "part <n> <name> <start> <length> <attrs hex16> <type guid> <sha256 or -> <pristine|stick|new>"
usage: make-pristine.py <googlebook-kano.img> <out dir>"""
import hashlib, re, struct, subprocess, sys, uuid, zlib
from pathlib import Path
img, out = Path(sys.argv[1]), Path(sys.argv[2])
if not re.fullmatch(r"[A-Za-z0-9._-]+", img.name): sys.exit(f"image file name must not contain spaces: {img.name!r}")
size = img.stat().st_size
if size % 512: sys.exit("image size is not a multiple of 512")
f = img.open("rb")
def read(start, length):
    f.seek(start); b = f.read(length)
    if len(b) != length: sys.exit("image ends early")
    return b
mbr, hd = read(0, 512), read(512, 512)
if mbr[510:512] != b"\x55\xaa" or mbr[450] != 0xEE: sys.exit("no protective MBR")
if hd[:8] != b"EFI PART": sys.exit("no GPT")
hsize, hcrc = struct.unpack_from("<II", hd, 12)
if not 92 <= hsize <= 512 or zlib.crc32(hd[:16] + bytes(4) + hd[20:hsize]) != hcrc: sys.exit("GPT header CRC is wrong")
my, alt = struct.unpack_from("<QQ", hd, 24)
lba, n, esz, ecrc = struct.unpack_from("<QIII", hd, 72)
if my != 1 or alt != size // 512 - 1: sys.exit("GPT header is not at LBA 1 or its backup is not at the end of the image")
if lba < 2 or lba * 512 + n * esz > 1 << 20: sys.exit("GPT entries are not inside the first MiB")
t = read(lba * 512, n * esz)
if zlib.crc32(t) != ecrc: sys.exit("GPT entries CRC is wrong")
def chunks(start, length):
    f.seek(start); left = length
    while left:
        b = f.read(min(left, 16 << 20))
        if not b: sys.exit("image ends early")
        yield b; left -= len(b)
def save(name, start, length):
    hh = hashlib.sha256()
    z = subprocess.Popen(["zstd", "-q", "-19", "-T0", "-o", str(out / name)], stdin=subprocess.PIPE)
    for b in chunks(start, length): hh.update(b); z.stdin.write(b)
    z.stdin.close()
    if z.wait(): sys.exit(f"zstd failed for {name}")
    if subprocess.run(["zstd", "-q", "-t", str(out / name)]).returncode: sys.exit(f"zstd -t failed for {name}")
    return hh.hexdigest()
out.mkdir(parents=True, exist_ok=False)
lines = [f"image {img.name} {size}", f"head 0 {1 << 20} {save('head.img.zst', 0, 1 << 20)}"]
names = set()
for i in range(n):
    e = t[i * esz:(i + 1) * esz]
    if e[:16] == bytes(16): continue
    first, last, attrs = struct.unpack_from("<QQQ", e, 32)
    name = e[56:128].decode("utf-16le").rstrip("\0")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,36}", name) or name in names: sys.exit(f"partition {i + 1}: name {name!r} is empty, repeated or has spaces")
    names.add(name)
    start, length = first * 512, (last + 1 - first) * 512
    typ = str(uuid.UUID(bytes_le=e[:16]))
    if name == "super":
        hh = hashlib.sha256()
        for b in chunks(start, length): hh.update(b)
        lines.append(f"part {i + 1} {name} {start} {length} {attrs:016x} {typ} {hh.hexdigest()} stick")
    elif name == "userdata":
        lines.append(f"part {i + 1} {name} {start} {length} {attrs:016x} {typ} - new")
    else:
        lines.append(f"part {i + 1} {name} {start} {length} {attrs:016x} {typ} {save(f'p{i + 1}.img.zst', start, length)} pristine")
    print(lines[-1], flush=True)
for need in ("super", "userdata", "EFI-SYSTEM", "boot_a"):
    if need not in names: sys.exit(f"the image has no {need} partition")
(out / "manifest.txt.partial").write_text("\n".join(lines) + "\n")
(out / "manifest.txt.partial").rename(out / "manifest.txt")
