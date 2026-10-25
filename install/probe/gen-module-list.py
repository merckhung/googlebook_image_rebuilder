#!/usr/bin/env python3
"""Make install/probe/lapis-modules.tsv for install/gbos-probe.sh: the name of every kernel module in the image's
vendor_dlkm and system_dlkm and the device match patterns it declares (the "alias=" strings of its .modinfo section,
from the kernel's MODULE_DEVICE_TABLEs). Only names and patterns are written, no module code.
usage: gen-module-list.py <out.tsv> <modules dir> [<modules dir> ...]
       (the dirs are lib/modules of vendor_dlkm and system_dlkm, e.g. extracted with builder/lib/lp-dump.py + fsck.erofs)"""
import struct, sys
from pathlib import Path

KEEP = ("pci:", "usb:", "acpi*:", "acpi:", "hid:", "i2c:", "sdio:")


def modinfo(path):
    b = path.read_bytes()
    if b[:4] != b"\x7fELF" or b[4] != 2:
        raise SystemExit(f"{path}: not an ELF64 file")
    shoff, = struct.unpack_from("<Q", b, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", b, 0x3A)
    secs = [struct.unpack_from("<IIQQQQIIQQ", b, shoff + i * shentsize) for i in range(shnum)]
    stro = secs[shstrndx][4]
    for name, _t, _f, _a, off, size, *_ in secs:
        n = b[stro + name:b.index(b"\0", stro + name)].decode()
        if n == ".modinfo":
            return [x.decode(errors="replace") for x in b[off:off + size].split(b"\0") if x]
    return []


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    rows = {}
    for d in argv[2:]:
        for ko in sorted(Path(d).glob("*.ko")):
            name = ko.name[:-3].replace("-", "_")
            al = sorted({x.split("=", 1)[1] for x in modinfo(ko) if x.startswith("alias=") and x.split("=", 1)[1].startswith(KEEP)})
            rows.setdefault(name, set()).update(al)
    with open(argv[1], "w") as f:
        f.write("# lapis (Googlebook build 16452207) kernel modules of vendor_dlkm + system_dlkm and their device match\n"
                "# patterns (MODULE_DEVICE_TABLE aliases), made by install/probe/gen-module-list.py; names and patterns only.\n"
                "# <module>\\t<pattern>   ('-' = a module without device patterns)\n")
        for m in sorted(rows):
            for a in sorted(rows[m]) or ["-"]:
                f.write(f"{m}\t{a}\n")
    print(f"{argv[1]}: {len(rows)} modules, {sum(len(v) for v in rows.values())} patterns")


if __name__ == "__main__":
    main(sys.argv)
