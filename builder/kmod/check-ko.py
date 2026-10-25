#!/usr/bin/env python3
"""Check an out-of-tree module against the lapis GKI kernel before it goes into an image:
  - vermagic: the part the kernel compares (after the release string) equals the GKI's;
  - every undefined symbol is exported by vmlinux (vmlinux.symvers of the GKI build), none comes from a GKI module
    (an unsigned module may not use those: MODULE_SIG_PROTECT), and each recorded CRC equals the exported one;
  - no symbol is exported by the module, and it carries no signature (vendor modules are loaded unsigned).
usage: check-ko.py <module.ko> <vmlinux.symvers> <expected vermagic tail>
The CRC list is either a kbuild vmlinux.symvers (crc, name, owner, ... separated by tabs) or the "<crc> <name>" list of
the vmlinux exports that builder/kmod/prepare.py takes from the image's own kernel (every name there is vmlinux's)."""
import struct, sys

ko, symvers, magic_tail = sys.argv[1:4]
data = open(ko, "rb").read()
if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1 or struct.unpack_from("<H", data, 16)[0] != 1:
    sys.exit(f"{ko}: not a 64-bit little-endian relocatable ELF")
shoff, = struct.unpack_from("<Q", data, 0x28)
shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
secs = []
for i in range(shnum):
    name, typ, _fl, _addr, off, size, link, _info, _al, entsize = struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
    secs.append(dict(name=name, type=typ, off=off, size=size, link=link, entsize=entsize))
strtab = secs[shstrndx]
def cstr(off):
    return data[off:data.index(b"\0", off)].decode()
for s in secs:
    s["name"] = cstr(strtab["off"] + s["name"])
by = {s["name"]: s for s in secs}
def body(n):
    s = by.get(n)
    return data[s["off"]:s["off"] + s["size"]] if s else b""

exported = {}
for line in open(symvers):
    if "\t" in line:
        crc, name, owner = line.split("\t")[:3]
    else:
        crc, name = line.split()
        owner = "vmlinux"
    exported[name] = (int(crc, 16), owner)

problems = []
# vermagic: "<release> SMP preempt mod_unload modversions " -- same_magic() skips the release when CRCs are present
info = dict(x.split("=", 1) for x in body(".modinfo").decode(errors="replace").split("\0") if "=" in x)
vm = info.get("vermagic", "")
if vm.split(" ", 1)[1:] != [magic_tail]:
    problems.append(f"vermagic tail {vm.split(' ', 1)[1:]!r} != {magic_tail!r}")

# undefined symbols
sym = next(s for s in secs if s["type"] == 2)
names = secs[sym["link"]]
undef = set()
for i in range(1, sym["size"] // 24):
    st_name, st_info, _o, st_shndx, _v, _s = struct.unpack_from("<IBBHQQ", data, sym["off"] + i * 24)
    n = cstr(names["off"] + st_name)
    if st_shndx == 0 and n:
        undef.add(n)
for n in sorted(undef):
    if n not in exported:
        problems.append(f"undefined symbol {n} is not exported by the GKI")
    elif exported[n][1] != "vmlinux":
        problems.append(f"undefined symbol {n} comes from GKI module {exported[n][1]} (protected for unsigned modules)")

# recorded CRCs: __versions (struct modversion_info: u64 crc, char name[56]) plus the extended tables
crcs = {}
v = body("__versions")
for i in range(0, len(v), 64):
    crc, = struct.unpack_from("<Q", v, i)
    crcs[v[i + 8:i + 64].split(b"\0")[0].decode()] = crc & 0xFFFFFFFF
ec, en = body("__version_ext_crcs"), body("__version_ext_names")
names_ext = en.split(b"\0")
for i in range(len(ec) // 4):
    crcs[names_ext[i].decode()] = struct.unpack_from("<I", ec, i * 4)[0]
for n in sorted(undef):
    if n not in crcs:
        problems.append(f"no CRC recorded for {n}")
    elif n in exported and crcs[n] != exported[n][0]:
        problems.append(f"CRC of {n}: module {crcs[n]:#010x} != GKI {exported[n][0]:#010x}")
if body("__ksymtab") or body("__ksymtab_gpl"):
    problems.append("the module exports symbols")
if data.endswith(b"~Module signature appended~\n"):
    problems.append("the module is signed")
for p in problems:
    print("FAIL", p)
print(f"{ko}: {len(undef)} imports, {len(crcs)} CRCs, vermagic {vm!r}")
sys.exit(1 if problems else 0)
