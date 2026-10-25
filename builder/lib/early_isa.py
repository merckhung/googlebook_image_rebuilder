"""Wrap Android's first-stage init so ISA emulation loads before its first opcode."""


def newc_entry(name, payload, inode, mode):
    nm = name.encode() + b"\0"
    fields = [inode, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(nm), 0]
    header = b"070701" + b"".join(b"%08X" % v for v in fields)
    return (header + nm + b"\0" * ((-(110 + len(nm))) % 4)
            + payload + b"\0" * ((-len(payload)) % 4))


def wrap_init(archive, loader):
    if not loader.startswith(b"\x7fELF"):
        raise ValueError("ISA stage0 loader is not an ELF executable")
    out = bytearray()
    offset = 0
    renamed = 0
    while offset < len(archive):
        if archive[offset:offset + 6] != b"070701":
            nxt = archive.find(b"070701", offset)
            if nxt < 0:
                nxt = len(archive)
            if any(archive[offset:nxt]):
                raise ValueError("unexpected data in the initrd archive")
            out += archive[offset:nxt]
            offset = nxt
            continue
        h = bytearray(archive[offset:offset + 110])
        ns, size = int(h[94:102], 16), int(h[54:62], 16)
        start = (offset + 110 + ns + 3) & ~3
        end = (start + size + 3) & ~3
        if ns < 1 or end > len(archive):
            raise ValueError("truncated initrd record")
        name = archive[offset + 110:offset + 110 + ns - 1]
        if name == b"init.gbos-real":
            raise ValueError("initrd already contains a stage0 wrapper")
        if name == b"init":
            if archive[start:start + 4] != b"\x7fELF":
                raise ValueError("Android init is not an ELF executable")
            nm = b"init.gbos-real\0"
            h[94:102] = b"%08X" % len(nm)
            out += h + nm + b"\0" * ((-(110 + len(nm))) % 4) + archive[start:end]
            renamed += 1
        else:
            out += archive[offset:end]
        offset = end
    if renamed != 1:
        raise ValueError(f"expected one Android init, found {renamed}")
    out += b"\0" * ((-len(out)) % 4)
    out += newc_entry("init", loader, 0x7FFF0003, 0o100750)
    out += newc_entry("TRAILER!!!", b"", 0, 0)
    return bytes(out)
