#!/usr/bin/env python3
"""Dump logical partitions (slot a) from a GPT disk image's super partition. usage: lp-dump.py <img> <outdir> <name>..."""
import struct, sys
from pathlib import Path
img, out, names = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
out.mkdir(parents=True, exist_ok=True)
with img.open("rb") as f:
    f.seek(512); hdr = f.read(92); assert hdr[:8] == b"EFI PART"
    ents, nent, esz = struct.unpack_from("<QII", hdr, 72)
    f.seek(ents * 512); data = f.read(nent * esz)
    super_off = super_end = None
    for i in range(nent):
        e = data[i*esz:(i+1)*esz]
        if e[56:128].decode("utf-16le").rstrip("\x00") == "super":
            first, last = struct.unpack_from("<QQ", e, 32)
            super_off, super_end = first * 512, (last + 1) * 512
    assert super_off and super_end > super_off
    f.seek(super_off + 4096); assert f.read(4) == b"gDla"
    f.seek(super_off + 12288); h = f.read(256); assert h[:4] == b"0PLA"
    hsize = struct.unpack_from("<I", h, 8)[0]; tsize = struct.unpack_from("<I", h, 44)[0]
    po, pn, ps = struct.unpack_from("<III", h, 80); eo, en, es = struct.unpack_from("<III", h, 92)
    f.seek(super_off + 12288 + hsize); t = f.read(tsize)
    parts = {}
    for i in range(pn):
        p = t[po + i*ps: po + (i+1)*ps]
        name = p[:36].split(b"\0", 1)[0].decode()
        _a, fe, ne, _g = struct.unpack_from("<IIII", p, 36)
        exts = []
        for j in range(fe, fe + ne):
            ns, tt, td, _src = struct.unpack_from("<QIQI", t, eo + j*es)
            exts.append((ns, tt, td))
        parts[name] = exts
    print("partitions:", {k: sum(x[0] for x in v) * 512 for k, v in parts.items() if v})
    f.seek(0, 2); img_size = f.tell()
    for n in names:
        if n not in parts: sys.exit(f"{n}: no such logical partition")
        # every extent must lie inside super (and inside the image) before anything is written
        for ns, tt, td in parts[n]:
            if tt != 0: sys.exit(f"{n}: non-linear extent")
            start, end = super_off + td * 512, super_off + (td + ns) * 512
            if end > super_end: sys.exit(f"{n}: extent {td}+{ns} lies outside super")
            if end > img_size: sys.exit(f"{n}: extent ends past the end of {img}")
        part = out / f"{n}.img"; tmp = out / f"{n}.img.partial"
        with tmp.open("wb") as o:
            for ns, tt, td in parts[n]:
                start = super_off + td * 512; left = ns * 512
                f.seek(start)
                while left:
                    b = f.read(min(left, 64 << 20))
                    if not b: sys.exit(f"{n}: unexpected end of {img}")
                    o.write(b); left -= len(b)
        tmp.replace(part)
        print(n, part.stat().st_size)
