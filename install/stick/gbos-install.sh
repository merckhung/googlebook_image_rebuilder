#!/usr/bin/env bash
# Googlebook installer on the KANO maintenance stick: a fresh install of this stick's Googlebook on the internal NVMe.
# Started by gbos-installer.service when the kernel command line has gbos.install=1 (boot entry "Install Googlebook to
# the internal disk"). Order:
#   1. refuse unless kernel lockdown is on, EFI runtime services are off (efi=noruntime), the kernel command line has
#      one module_blacklist with every module of the installer entry (module-blacklist.txt) and blocks the initcalls of
#      the built-in TPM and nvram drivers (builtin-initcalls.txt), none of those modules is loaded and no TPM, nvram,
#      cros_ec, MTD or spidev device exists;
#   2. check the image and this stick before the NVMe driver is loaded: the manifest, the image's partition table
#      (head.img.zst: SHA-256 and GPT CRCs), every pristine file (zstd -t, SHA-256), the image's boot files, and on
#      this stick 512-byte sectors, GPT CRCs, mountable XBOOTLDR/EFI-SYSTEM loader files, and the super partition
#      offset and size. Install mode also hashes this stick's super in this step. Update mode hashes the stick super after
#      the serial matches, before the target is opened for write. Any failure in these step-2 checks stops here and
#      never asks for the serial number; in update mode, a damaged stick super fails later, at that post-serial hash
#      before the target is opened for write;
#   3. load the NVMe driver so the serial can be read. The disk is visible from here. Ask for the last 4 characters
#      of the target's serial number. Do not write, mount, or discard the internal disk until those characters match.
#      Cancel or a wrong serial leaves the disk untouched. The target's sector size is read only after this load;
#   4. write: the image's partition table, super copied from this stick, the pristine partitions, an empty userdata at
#      the tail, new GUIDs, the NVMe ESP boot files, the stick's menu entry for the internal disk; every partition is
#      read back from the disk (direct I/O) and checked against the manifest right after it is written;
#   5. verify the whole result (both GPT copies, every partition against the manifest, sgdisk -v, the boot files read
#      back) and only then print INSTALL OK.
# The internal disk gets the image's own partition table and partitions from /var/lib/gbos-install/pristine (made by
# make-pristine.py from the same image), except super, which is copied from this stick. /data starts empty (Android
# formats it on the first boot); nothing of the stick's own /data is copied. Everything shown is also written to
# /var/log/gbos-install-<time>.log on the stick.
#
# Update mode (kernel command line also has gbos.update=1, boot entry "Update Googlebook on the internal disk (keep
# data)"): the same boot checks and the same installer-data checks, then the NVMe driver is loaded so the serial
# can be read. Update mode hashes the stick super after the serial matches, before the target is opened for write.
# gbos-update.py (installed by setup-stick.sh) then puts this stick's image into the internal disk's other slot.
# /data, metadata and the running version stay; the running version remains on the boot menus as "(previous version)".
# The serial number is confirmed the same way.
set -Eeuo pipefail
shopt -s inherit_errexit
P=/var/lib/gbos-install/pristine
MNT=/run/gbos-install
ENGINE=/usr/local/share/gbos-install/gbos-update.py
MODE=install
case " $(cat /proc/cmdline) " in *" gbos.update=1 "*) MODE=update ;; esac
export MTOOLS_SKIP_CHECK=1
WROTE=0
W=""

log() { printf '%s\n' "$*"; }
cleanup() {
    local m
    for m in "$MNT/xb" "$MNT/esp"; do
        if mountpoint -q "$m" 2>/dev/null; then umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true; fi
        rmdir "$m" 2>/dev/null || true
    done
    if [ -n "$W" ] && [ -d "$W" ]; then rm -rf "$W"; fi
}
finish() {  # finish <exit code> <poweroff|reboot> <message>
    trap - ERR INT
    set +e
    cleanup
    echo; log "$3"; sync
    read -r -p "Press Enter to $2. " _
    systemctl "$2"
    exit "$1"
}
die() {
    trap - ERR
    # inside $(...): report and let the caller fail; the caller's own check or ERR trap stops the installer
    if [ "$BASHPID" != "$$" ]; then echo "ERROR: $*" >&2; exit 1; fi
    log "ERROR: $*"
    if [ "$WROTE" = 1 ]; then
        finish 1 poweroff "Installation stopped. The internal disk is incomplete and will not start; run the installer again."
    else
        finish 1 poweroff "Stopped before anything was written. The internal disk was not changed."
    fi
}
on_err() { die "command failed (exit $1) at line $2: $3"; }
trap 'on_err $? $LINENO "$BASH_COMMAND"' ERR
trap 'die "interrupted"' INT
trap cleanup EXIT

pdev() { case "$1" in *[0-9]) echo "${1}p$2" ;; *) echo "$1$2" ;; esac; }
part_start() { cat "/sys/class/block/$(basename "$(pdev "$1" "$2")")/start" 2>/dev/null || echo -; }
part_size() { cat "/sys/class/block/$(basename "$(pdev "$1" "$2")")/size" 2>/dev/null || echo -; }
field() { awk -v k="$1" '$1 == "part" && $3 == k' "$P/manifest.txt"; }
sector_size() { cat "/sys/block/$(basename "$1")/queue/logical_block_size"; }
# SHA-256 of a whole block device, read from the disk itself (page cache dropped, direct I/O)
devsha() { blockdev --flushbufs "$1"; dd if="$1" bs=4M iflag=direct status=none | sha256sum | cut -c1-64; }
reread() { blockdev --flushbufs "$1"; partx -u "$1" 2>/dev/null || blockdev --rereadpt "$1"; udevadm settle; }
stick_part() { sgdisk -p "$SRC" | awk -v k="$1" '$NF == k {print $1}'; }
# esp_entries <FAT image or partition>: the names in its loader/entries, sorted, space-separated (empty on any error)
esp_entries() { { mdir -b -i "$1" ::/loader/entries || true; } | tr -d '\r' | sed -n 's|^::/loader/entries/\([^/]*\)$|\1|p' |
    LC_ALL=C sort | tr '\n' ' '; }
nvme_entry() {  # nvme_entry <title> <NVMe ESP entry>: the stick's menu entry for the internal disk
    sed -e "s/^title .*/title $1/" -e 's|^linux .*|linux /gbos-nvme/kernel.efi|' -e 's|^initrd .*|initrd /gbos-nvme/initrd.img|' "$2"
}

TTY=$(sed -n 's/.*gbos\.install\.tty=\([A-Za-z0-9]*\).*/\1/p' /proc/cmdline); TTY=${TTY:-tty1}
exec < "/dev/$TTY" > "/dev/$TTY" 2>&1
if [ "$TTY" = tty1 ]; then chvt 1 2>/dev/null || true; setfont LatGrkCyr-12x22 2>/dev/null || true; fi
clear 2>/dev/null || true
LOG=/var/log/gbos-install-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
if [ "$MODE" = update ]; then log "Googlebook internal-disk updater: keeps your data"
else log "Googlebook internal-disk installer"; fi
log "log file on this stick: $LOG ($(date))"

# --- 1. this boot keeps the firmware out of reach
LD=$(cat /sys/kernel/security/lockdown 2>/dev/null || echo missing)
LDMODE=$(sed -n 's/.*\[\([a-z]*\)\].*/\1/p' <<< "$LD")
case "$LDMODE" in
    integrity|confidentiality) ;;
    *) die "kernel lockdown is off ($LD). Start the installer only from the menu entry 'Install Googlebook to the internal disk'." ;;
esac
case " $(cat /proc/cmdline) " in
    *" efi=noruntime "*) ;;
    *) die "EFI runtime services are on (no efi=noruntime). Start the installer only from its menu entry." ;;
esac
log "ok   kernel lockdown: $LDMODE; EFI runtime services off"
for c in python3 zstd sgdisk partx blockdev blkdiscard udevadm lsblk findmnt mcopy mtype dd sha256sum cmp mount umount; do
    command -v "$c" > /dev/null || die "missing tool: $c"
done
if [ "$MODE" = update ]; then
    for c in lz4 mdir mmd mdel minfo; do command -v "$c" > /dev/null || die "missing tool: $c"; done
    [ -f "$ENGINE" ] || die "missing $ENGINE; set up the stick with setup-stick.sh"
fi
W=$(mktemp -d)
cat > "$W/gbos.py" << 'PY'
import hashlib, os, re, struct, subprocess, sys, uuid, zlib
MIB = 1 << 20
MAGIC = b"#BOOTCONFIG\n"
LIFT = b"initcall_blacklist=nvme_init,mei_me_driver_init"

def manifest(path):
    text = open(path, "rb").read().decode("ascii")
    if not text.endswith("\n"): raise ValueError("manifest: no final newline")
    lines = text[:-1].split("\n")
    img, head = lines[0].split(" "), lines[1].split(" ")
    if len(img) != 3 or img[0] != "image" or not img[2].isdigit() or int(img[2]) % 512:
        raise ValueError("manifest: bad image line")
    if len(head) != 4 or head[:3] != ["head", "0", str(MIB)] or not re.fullmatch(r"[0-9a-f]{64}", head[3]):
        raise ValueError("manifest: bad head line")
    parts = {}
    for line in lines[2:]:
        f = line.split(" ")
        if len(f) != 9 or f[0] != "part": raise ValueError(f"manifest: bad line {line!r}")
        _, n, name, start, length, attrs, typ, sha, kind = f
        ok = (re.fullmatch(r"[1-9][0-9]{0,2}", n) and int(n) <= 128 and re.fullmatch(r"[A-Za-z0-9_.-]{1,36}", name)
              and start.isdigit() and length.isdigit() and int(start) % 512 == 0 and int(length) % 512 == 0
              and int(length) > 0 and re.fullmatch(r"[0-9a-f]{16}", attrs)
              and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", typ)
              and ((kind in ("pristine", "stick") and re.fullmatch(r"[0-9a-f]{64}", sha)) or (kind == "new" and sha == "-")))
        if not ok: raise ValueError(f"manifest: bad line {line!r}")
        if int(n) in parts or name in [p["name"] for p in parts.values()]: raise ValueError(f"manifest: {n} {name} twice")
        parts[int(n)] = dict(name=name, start=int(start), len=int(length), attrs=int(attrs, 16), type=typ, sha=sha, kind=kind)
    kinds = {p["name"]: p["kind"] for p in parts.values()}
    if kinds.get("super") != "stick" or kinds.get("userdata") != "new" or kinds.get("EFI-SYSTEM") != "pristine" \
            or kinds.get("boot_a") != "pristine" or sorted(kinds.values()).count("pristine") != len(kinds) - 2:
        raise ValueError("manifest: needs super (stick), userdata (new), EFI-SYSTEM and boot_a, the rest pristine")
    spans = sorted((p["start"], p["start"] + p["len"]) for p in parts.values())
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])): raise ValueError("manifest: partitions overlap")
    return img, head, parts

def header(sector, lba):
    if len(sector) != 512 or sector[:8] != b"EFI PART": raise ValueError(f"no GPT header at LBA {lba}")
    size, crc = struct.unpack_from("<II", sector, 12)
    if not 92 <= size <= 512: raise ValueError(f"GPT header at LBA {lba}: bad size")
    h = bytearray(sector[:size]); h[16:20] = bytes(4)
    if zlib.crc32(h) != crc: raise ValueError(f"GPT header at LBA {lba}: bad CRC")
    my, alt, first, last = struct.unpack_from("<QQQQ", sector, 24)
    elba, n, esz, ecrc = struct.unpack_from("<QIII", sector, 72)
    if my != lba: raise ValueError(f"GPT header at LBA {lba} says it is at LBA {my}")
    if esz < 128 or esz % 8 or not 0 < n * esz <= MIB or first > last: raise ValueError(f"GPT header at LBA {lba}: bad fields")
    return dict(alt=alt, first=first, last=last, disk=sector[56:72], elba=elba, n=n, esz=esz, ecrc=ecrc)

def entries(blob, h):
    if len(blob) != h["n"] * h["esz"] or zlib.crc32(blob) != h["ecrc"]: raise ValueError("GPT partition entries: bad CRC")
    out = {}
    for i in range(h["n"]):
        e = blob[i * h["esz"]:(i + 1) * h["esz"]]
        if e[:16] == bytes(16): continue
        first, last, attrs = struct.unpack_from("<QQQ", e, 32)
        out[i + 1] = dict(type=str(uuid.UUID(bytes_le=e[:16])), guid=str(uuid.UUID(bytes_le=e[16:32])), first=first,
                          last=last, attrs=attrs, name=e[56:128].decode("utf-16le").rstrip("\0"))
    return out

def protective_mbr(sector):
    if sector[510:512] != b"\x55\xaa" or sector[450] != 0xEE: raise ValueError("no protective MBR")

def same_layout(gpt, parts, ud_start=None, ud_last=None):
    if sorted(gpt) != sorted(parts): raise ValueError(f"partitions {sorted(gpt)} instead of the manifest's {sorted(parts)}")
    for n, m in parts.items():
        e = gpt[n]
        if ud_start is not None and m["name"] == "userdata": first, last = ud_start, ud_last
        else: first, last = m["start"] // 512, (m["start"] + m["len"]) // 512 - 1
        if (e["name"], e["type"], e["first"], e["last"], e["attrs"]) != (m["name"], m["type"], first, last, m["attrs"]):
            raise ValueError(f"partition {n} is {e['name']} {e['type']} {e['first']}-{e['last']} attrs {e['attrs']:016x},"
                             f" not {m['name']} {m['type']} {first}-{last} attrs {m['attrs']:016x}")

def stream_sha(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE); h = hashlib.sha256(); n = 0
    for b in iter(lambda: p.stdout.read(16 << 20), b""): h.update(b); n += len(b)
    if p.wait(): raise ValueError(f"{' '.join(cmd)} failed")
    return h.hexdigest(), n

def verify_data(pdir, headout):
    img, head, parts = manifest(f"{pdir}/manifest.txt")
    raw = subprocess.run(["zstd", "-q", "-dc", f"{pdir}/head.img.zst"], check=True, stdout=subprocess.PIPE).stdout
    if len(raw) != MIB or hashlib.sha256(raw).hexdigest() != head[3]: raise ValueError("head.img.zst does not match the manifest")
    protective_mbr(raw[:512])
    h = header(raw[512:1024], 1)
    if h["elba"] < 2 or h["elba"] * 512 + h["n"] * h["esz"] > MIB: raise ValueError("head.img: entries outside the first MiB")
    if h["alt"] != int(img[2]) // 512 - 1: raise ValueError("head.img: backup GPT is not at the end of the image")
    gpt = entries(raw[h["elba"] * 512:h["elba"] * 512 + h["n"] * h["esz"]], h)
    same_layout(gpt, parts)
    if any(e["first"] < h["first"] or e["last"] > h["last"] for e in gpt.values()): raise ValueError("head.img: partition outside the usable area")
    for n, m in sorted(parts.items()):
        if m["kind"] != "pristine": continue
        f = f"{pdir}/p{n}.img.zst"
        subprocess.run(["zstd", "-q", "-t", f], check=True)
        if stream_sha(["zstd", "-q", "-dc", f]) != (m["sha"], m["len"]): raise ValueError(f"p{n}.img.zst ({m['name']}) does not match the manifest")
    with open(headout, "wb") as o: o.write(raw)
    print(f"ok   installer data: manifest, the image's partition table and {sum(m['kind'] == 'pristine' for m in parts.values())} partition files match")

def read_gpt(dev, backup):
    fd = os.open(dev, os.O_RDONLY)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        def rd(off, n):
            b = os.pread(fd, n, off)
            if len(b) != n: raise ValueError(f"{dev}: short read at {off}")
            return b
        protective_mbr(rd(0, 512))
        p = header(rd(512, 512), 1); pe = rd(p["elba"] * 512, p["n"] * p["esz"]); gpt = entries(pe, p)
        if backup:
            last = size // 512 - 1
            if p["alt"] != last: raise ValueError(f"{dev}: backup GPT is not at the end of the disk")
            b = header(rd(last * 512, 512), last); be = rd(b["elba"] * 512, b["n"] * b["esz"])
            if b["alt"] != 1 or be != pe or b["elba"] <= p["last"] or \
                    (b["first"], b["last"], b["disk"], b["ecrc"]) != (p["first"], p["last"], p["disk"], p["ecrc"]):
                raise ValueError(f"{dev}: backup GPT differs from the primary")
        return p, gpt
    finally:
        os.close(fd)

def check_gpt(dev):
    read_gpt(dev, False)

def verify_target(dev, mpath, src, ud_start, boot_guid):
    _, _, parts = manifest(mpath)
    h, gpt = read_gpt(dev, True)
    same_layout(gpt, parts, int(ud_start), h["last"])
    sh, sgpt = read_gpt(src, False)
    guids = [e["guid"] for e in gpt.values()]
    if len(set(guids)) != len(guids) or set(guids) & {e["guid"] for e in sgpt.values()} or str(uuid.UUID(int=0)) in guids \
            or h["disk"] == sh["disk"]:
        raise ValueError(f"{dev}: partition GUIDs are not new and unique")
    boot_a = [e["guid"] for e in gpt.values() if e["name"] == "boot_a"]
    if boot_a != [boot_guid]: raise ValueError(f"{dev}: boot_a GUID {boot_a} is not {boot_guid}")
    print(f"ok   partition table of {dev}: both GPT copies valid, {len(gpt)} partitions as in the manifest, all GUIDs new")

def bootconfig(d):
    if not d.endswith(MAGIC): raise ValueError("initrd has no bootconfig")
    end = len(d) - len(MAGIC); size, csum = struct.unpack_from("<II", d, end - 8); start = end - 8 - size
    bc = d[start:start + size]
    if start < 0 or (sum(bc) & 0xFFFFFFFF) != csum: raise ValueError("bootconfig checksum")
    lines = bc.split(b"\n"); hits = [i for i, l in enumerate(lines) if l.startswith(b"androidboot.boot_part_uuid=")]
    if len(hits) != 1 or not re.fullmatch(rb"androidboot\.boot_part_uuid=[0-9a-f-]{36}", lines[hits[0]]):
        raise ValueError("bootconfig: needs exactly one androidboot.boot_part_uuid=<GUID> line")
    return start, lines, hits[0]

def bootconfig_set(path, guid):
    d = open(path, "rb").read(); start, lines, i = bootconfig(d)
    lines[i] = b"androidboot.boot_part_uuid=" + guid.encode()
    raw = b"\n".join(lines)
    out = d[:start] + raw + struct.pack("<II", len(raw), sum(raw) & 0xFFFFFFFF) + MAGIC
    if len(out) != len(d): raise ValueError("bootconfig size changed")
    with open(path, "wb") as o: o.write(out)

def bootconfig_check(path, guid):
    _, lines, i = bootconfig(open(path, "rb").read())
    if guid != "-" and lines[i] != b"androidboot.boot_part_uuid=" + guid.encode():
        raise ValueError(f"initrd points at {lines[i].decode()}, not boot_a {guid}")

def lift_entry(src, dst):
    # the image's entry with only the NVMe initcall block lifted; module_blacklist and efi=noruntime must stay
    t = open(src, "rb").read()
    opts = [l for l in t.split(b"\n") if l.startswith(b"options ")]
    if len(opts) != 1 or t.count(LIFT) != 1 or opts[0].split().count(LIFT) != 1: raise ValueError(f"{src}: no NVMe block to lift")
    toks = opts[0].split()
    bl = [x for x in toks if x.startswith(b"module_blacklist=")]
    if b"efi=noruntime" not in toks or len(bl) != 1 or {b"nvme", b"nvme_core"} & set(bl[0][17:].split(b",")):
        raise ValueError(f"{src}: needs efi=noruntime and one module_blacklist without nvme")
    new = t.replace(LIFT, b"initcall_blacklist=mei_me_driver_init")
    if b"nvme_init" in new: raise ValueError(f"{src}: nvme_init still blocked")
    with open(dst, "wb") as o: o.write(new)

def names(path, fields):
    # non-comment lines of module-blacklist.txt (one module) or builtin-initcalls.txt (module, initcall or "-", ...)
    out = []
    for line in open(path, "rb").read().decode("ascii").split("\n"):
        f = line.split("#", 1)[0].split()
        if not f: continue
        if len(f) not in fields or not all(re.fullmatch(r"[a-z0-9_,-]+", x) for x in f): raise ValueError(f"{path}: bad line {line!r}")
        out.append(f)
    if not out: raise ValueError(f"{path}: empty")
    return out

def check_cmdline(cmdline, modules, initcall_map, loaded):
    # the kernel keeps only the last module_blacklist= and compares names exactly; initcall_blacklist= adds up
    toks = open(cmdline, "rb").read().decode("ascii", "replace").split()
    need = [f[0] for f in names(modules, (1,))]
    icmap = names(initcall_map, (2, 3))
    ics = [f[1] for f in icmap if f[1] != "-"]
    errs = []
    if [f[0] for f in icmap if f[0] not in need]: errs.append(f"{modules} lacks modules of {initcall_map}")
    bl = [t.split("=", 1)[1] for t in toks if t.startswith("module_blacklist=")]
    if len(bl) != 1: errs.append(f"{len(bl)} module_blacklist= on the kernel command line instead of 1")
    else:
        miss = [m for m in need if m not in bl[0].split(",")]
        if miss: errs.append("module_blacklist lacks " + ",".join(miss))
    have = ",".join(t.split("=", 1)[1] for t in toks if t.startswith("initcall_blacklist=")).split(",")
    miss = [i for i in ics if i not in have]
    if miss: errs.append("built-in drivers are not blocked: initcall_blacklist lacks " + ",".join(miss))
    if "efi=noruntime" not in toks: errs.append("no efi=noruntime")
    on = sorted({l.split()[0] for l in open(loaded).read().splitlines() if l.strip()} & set(need))
    if on: errs.append("blocked modules are loaded: " + ",".join(on))
    if errs: raise ValueError("; ".join(errs))
    print(f"ok   kernel command line: module_blacklist has all {len(need)} modules of the installer entry, none loaded;"
          f" initcall_blacklist has {','.join(ics)}")

try:
    cmd, args = sys.argv[1], sys.argv[2:]
    {"verify-data": verify_data, "verify-target": verify_target, "bootconfig-set": bootconfig_set,
     "bootconfig-check": bootconfig_check, "lift-entry": lift_entry, "check-gpt": check_gpt,
     "check-cmdline": check_cmdline}[cmd](*args)
except Exception as e:  # one plain line for the screen and the log
    sys.exit(f"ERROR: {e}")
PY
# the whole module_blacklist of the installer entry (module-blacklist.txt, written by setup-stick.sh) and the initcalls
# of the drivers built into the maintenance kernel (builtin-initcalls.txt; module_blacklist cannot stop those)
MAPF=/usr/local/share/gbos-install/builtin-initcalls.txt
MODF=/usr/local/share/gbos-install/module-blacklist.txt
[ -f "$MAPF" ] && [ -f "$MODF" ] || die "missing $MAPF or $MODF; set up the stick with setup-stick.sh"
python3 "$W/gbos.py" check-cmdline /proc/cmdline "$MODF" "$MAPF" /proc/modules \
    || die "this boot does not block what the installer entry blocks. Start the installer only from its menu entry."
PRESENT=""
for f in /dev/tpm0 /dev/tpmrm0 /dev/nvram /dev/cros_ec /dev/mtd* /dev/spidev*; do if [ -e "$f" ]; then PRESENT="$PRESENT $f"; fi; done
if [ -d /sys/class/tpm ] && [ -n "$(ls -A /sys/class/tpm)" ]; then PRESENT="$PRESENT /sys/class/tpm/*"; fi
[ -z "$PRESENT" ] || die "built-in drivers are not blocked:$PRESENT exists"
log "ok   built-in drivers blocked: no TPM, nvram, cros_ec, MTD or spidev device"

# --- 2. check the image and this stick before the NVMe driver is loaded.
# The load below only reads the serial. Do not write, mount, or discard until the last 4 characters match.
log "Checking the installer data on this stick (about a minute)..."
[ -f "$P/manifest.txt" ] || die "no installer data in $P; set up the stick with setup-stick.sh"
python3 "$W/gbos.py" verify-data "$P" "$W/head.img" || die "the installer data in $P is damaged; set up the stick again"
read -r _ SN _ SSTART SLEN _ _ SSHA _ <<< "$(field super)"
read -r _ UN _ _ _ UATTR UTYPE _ _ <<< "$(field userdata)"
read -r _ EN _ _ _ _ _ _ _ <<< "$(field EFI-SYSTEM)"
read -r _ BN _ _ _ _ _ _ _ <<< "$(field boot_a)"
# the image's last partition (userdata excluded) decides where the new userdata starts
LAST=$(awk '$1 == "part" && $3 != "userdata" {e = ($4 + $5) / 512; if (e > m) m = e} END {printf "%d", m}' "$P/manifest.txt")
UD_START=$(( (LAST + 2047) / 2048 * 2048 ))

# boot files of the image's ESP: the NVMe ESP gets these with only the NVMe block lifted and the new boot_a GUID
zstd -q -dc "$P/p$EN.img.zst" > "$W/esp.img"
mcopy -n -i "$W/esp.img" ::/initrd.img "$W/initrd.orig"
mcopy -n -i "$W/esp.img" ::/kernel.efi "$W/kernel.efi"
python3 "$W/gbos.py" bootconfig-check "$W/initrd.orig" - || die "the image's initrd has no usable bootconfig"
# quiet and serial-debug; images built before the plain entry was dropped also carry it (lapis.conf). Any other set
# (a missing entry, an unknown one such as an unmodified original) stops here.
ENTRIES=$(esp_entries "$W/esp.img")
case "$ENTRIES" in
    "lapis-debug.conf lapis-quiet.conf " | "lapis-debug.conf lapis-quiet.conf lapis.conf ") ;;
    *) die "the image's ESP has unexpected boot entries: ${ENTRIES:-none readable}" ;;
esac
for f in $ENTRIES; do
    mtype -i "$W/esp.img" "::/loader/entries/$f" > "$W/$f.orig"
    python3 "$W/gbos.py" lift-entry "$W/$f.orig" "$W/$f" || die "boot entry $f of the image cannot be changed safely"
done
rm -f "$W/esp.img"
printf 'default lapis-quiet.conf\ntimeout 5\neditor no\nauto-firmware no\n' > "$W/loader.conf"
printf 'default gbos-nvme.conf\ntimeout 5\neditor no\nauto-firmware no\n' > "$W/stick-loader.conf"
nvme_entry "Googlebook (internal disk)" "$W/lapis-quiet.conf" > "$W/gbos-nvme.conf"
nvme_entry "Googlebook (internal disk) serial-debug" "$W/lapis-debug.conf" > "$W/gbos-nvme-debug.conf"
log "ok   boot files of the image"

SRC=/dev/$(lsblk -no PKNAME "$(findmnt -nvo SOURCE /)")
[ -b "$SRC" ] || die "cannot find the USB stick this system runs from"
[ "$(lsblk -dno TRAN "$SRC" | tr -d ' ')" = usb ] || die "this system is not running from a USB stick ($SRC)"
[ "$(sector_size "$SRC")" = 512 ] || die "the stick $SRC does not use 512-byte sectors"
XN=$(stick_part XBOOTLDR); SEN=$(stick_part EFI-SYSTEM)
[[ "$XN" =~ ^[0-9]+$ ]] && [[ "$SEN" =~ ^[0-9]+$ ]] || die "the stick has no single XBOOTLDR and EFI-SYSTEM partition"
python3 "$W/gbos.py" check-gpt "$SRC" || die "the stick's partition table is damaged"
mkdir -p "$MNT/xb" "$MNT/esp"    # both are written after the install: make sure they mount (vfat under lockdown)
mount -o ro "$(pdev "$SRC" "$XN")" "$MNT/xb"; mount -o ro "$(pdev "$SRC" "$SEN")" "$MNT/esp"
[ -d "$MNT/xb/loader/entries" ] && [ -f "$MNT/esp/loader/loader.conf" ] || die "the stick's boot partitions lack loader files"
umount "$MNT/xb" "$MNT/esp"
[ "$(part_start "$SRC" "$SN")" = $((SSTART / 512)) ] && [ "$(part_size "$SRC" "$SN")" = $((SLEN / 512)) ] \
    || die "the stick's super partition is not where the installer data puts it"
if [ "$MODE" = install ]; then   # (update mode: gbos-update.py checks super itself before it writes)
    log "Checking the Googlebook copy on this stick ($((SLEN / 1000000000)) GB). This takes several minutes on a USB stick..."
    GOT=$(dd if="$(pdev "$SRC" "$SN")" bs=4M status=progress 2> "/dev/$TTY" | sha256sum | cut -c1-64)
    [ "$GOT" = "$SSHA" ] || die "super on this stick does not match the installer data (damaged stick, or a new image without setup-stick.sh)"
    log "ok   super on this stick matches the installer data"
fi

# On the generated initramfs path (pinned Arch modular kernel + assemble's mkinitcpio config), the maintenance OS does
# not autoload the NVMe driver (install nvme /bin/false in modprobe.d), so every check above ran before the NVMe driver
# is loaded and the installer loads it here with --ignore-install so the serial can be read (a prebuilt initramfs or a
# kernel with builtin NVMe is not covered by that pre-load guarantee). The disk is visible from here. Do not write,
# mount, or discard it until the last 4 characters match. Cancel or a wrong serial leaves the disk untouched.
if ! ls /dev/nvme*n1 > /dev/null 2>&1; then
    modprobe --ignore-install ufshcd_core 2>/dev/null || true
    modprobe --ignore-install ufshcd_pci 2>/dev/null || true
    modprobe --ignore-install sd_mod 2>/dev/null || true
    modprobe --ignore-install nvme_core 2>/dev/null || true
    modprobe --ignore-install nvme 2>/dev/null || true
    udevadm settle
    for _ in $(seq 1 40); do
        if ls /dev/nvme*n1 > /dev/null 2>&1 || lsblk -dnpo NAME,TRAN,TYPE | awk '$2 != "usb" && $3 == "disk" {found=1} END {exit !found}'; then
            break
        fi
        sleep 0.5
    done
    udevadm settle
fi
mapfile -t C < <(lsblk -dnpo NAME,TRAN,TYPE | awk '($2 == "nvme" || $2 == "ufs" || $2 == "scsi" || $2 == "sata") && $2 != "usb" && $3 == "disk" {print $1}')
if [ "${#C[@]}" -eq 0 ]; then
    mapfile -t C < <(lsblk -dnpo NAME,TRAN,TYPE | awk '$2 != "usb" && $3 == "disk" && $1 !~ /loop|ram|zram/ {print $1}')
fi
[ "${#C[@]}" -ge 1 ] || die "no internal UFS or NVMe disk found"
T=${C[0]}
if [ "${#C[@]}" -gt 1 ]; then
    for i in "${!C[@]}"; do echo "  $((i + 1)). ${C[$i]} $(lsblk -dno MODEL,SIZE,SERIAL "${C[$i]}")"; done
    read -r -p "Which disk (number)? " k || k=""
    [[ "$k" =~ ^[0-9]+$ ]] && [ "$k" -ge 1 ] && [ "$k" -le "${#C[@]}" ] || finish 0 poweroff "Cancelled. Nothing was written."
    T=${C[$((k - 1))]}
fi
[ "$T" != "$SRC" ] || die "target and source are the same disk"
# The pristine GPT is encoded in 512-byte LBAs and is copied verbatim below.
# A 4Kn target needs a converted GPT and sector-aware layout before any erase.
[ "$(sector_size "$T")" = 512 ] || die "$T uses $(sector_size "$T")-byte logical sectors; this installer requires 512-byte sectors (4Kn conversion is not implemented)"
MODEL=$(lsblk -dno MODEL "$T" | sed 's/ *$//'); SERIAL=$(lsblk -dno SERIAL "$T" | tr -d ' '); SIZE=$(lsblk -dbno SIZE "$T")
[ -n "$SERIAL" ] && [ "${#SERIAL}" -ge 4 ] || die "$T reports no serial number to confirm against"
DEVNO=$(cat "/sys/block/$(basename "$T")/dev")
in_use() {  # prints why $T is in use (mounted, swap, held by LVM, RAID or dm-crypt) or cannot be checked, or nothing
    local mp h rc=0 b
    b=$(basename "$T")
    [ -d "/sys/block/$b/holders" ] && [ "$(cat "/sys/block/$b/dev" 2>/dev/null)" = "$DEVNO" ] || { echo "cannot check $T: /sys/block/$b is not the disk it was"; return; }
    mp=$(lsblk -nro MOUNTPOINTS "$T") || { echo "cannot read the mount points of $T"; return; }
    if [ -n "${mp//[[:space:]]/}" ]; then echo "$T has mounted partitions"; return; fi
    awk -v d="$T" '$1 ~ ("^" d "(p[0-9]+)?$") {f = 1} END {exit !f}' /proc/mounts /proc/swaps || rc=$?
    case $rc in
        0) echo "$T is mounted or used as swap"; return ;;
        1) ;;
        *) echo "cannot read /proc/mounts or /proc/swaps"; return ;;
    esac
    for h in /sys/block/"$b"/holders/* /sys/block/"$b"/"$b"*/holders/*; do
        if [ -e "$h" ]; then echo "$T is in use by $(basename "$h")"; return; fi
    done
}
U=$(in_use); [ -z "$U" ] || die "$U"
[ "$SIZE" -ge $(( (UD_START + 16 * 1024 * 1024 * 2) * 512 )) ] || die "$T is too small (needs at least $(( UD_START / 2 / 1024 / 1024 + 16 )) GiB)"

if [ "$MODE" = update ]; then
    echo
    python3 -I "$ENGINE" inspect "$T" || die "$T holds no Googlebook this updater can update (above)"
    echo
    echo "  Target: $T  $MODEL  $((SIZE / 1000000000)) GB  serial $SERIAL"
    echo "  Update to: this stick's $(awk '$1 == "image" {print $2}' "$P/manifest.txt") (checked again before anything is written)"
    echo "  /data stays: apps, files and your Google account. The version that runs now stays on this stick's menu as"
    echo "  'Googlebook (internal disk, previous version)', and on the internal disk's own menu as '(previous version)'"
    echo "  when its boot partition has room for both. When it has not (a Google build with a new kernel), the internal"
    echo "  disk's own menu starts nothing while the update runs: this stick's 'Googlebook (internal disk)' still does."
    echo
    read -r -p "  Type the last 4 characters of the serial ($SERIAL) to update, or just Enter to cancel: " A || A=""
    LAST4=${SERIAL: -4}
    [ -n "$A" ] && [ "${A^^}" = "${LAST4^^}" ] || finish 0 poweroff "Cancelled. Nothing was written."
    [ "$(lsblk -dno SERIAL "$T" | tr -d ' ')" = "$SERIAL" ] && [ "$(lsblk -dbno SIZE "$T")" = "$SIZE" ] \
        && [ "$(cat "/sys/block/$(basename "$T")/dev")" = "$DEVNO" ] || die "$T changed while waiting"
    U=$(in_use); [ -z "$U" ] || die "$U"
    log "confirmed: updating $T ($MODEL, serial $SERIAL)"
    if python3 -I "$ENGINE" apply "$T" --pristine "$P" --super "$(pdev "$SRC" "$SN")" --mode nvme --rdev "$DEVNO" \
            --syspath "$(readlink -e "/sys/dev/block/$DEVNO")" --bytes "$SIZE" \
            --stick-boot "$(pdev "$SRC" "$XN")"; then rc=0; else rc=$?; fi
    case $rc in
        0)  # the stick's menu starts the internal disk by default (a stick set up before the first install may not yet)
            S=$(pdev "$SRC" "$SEN")
            if mount "$S" "$MNT/esp" && cp "$W/stick-loader.conf" "$MNT/esp/loader/loader.conf" && sync && umount "$MNT/esp" \
                    && blockdev --flushbufs "$S" && mtype -i "$S" ::/loader/loader.conf > "$W/rb.stick-loader.conf" \
                    && cmp -s "$W/stick-loader.conf" "$W/rb.stick-loader.conf"; then
                finish 0 reboot "Update done. Remove the USB stick, or keep it inserted: its menu starts the internal disk by default."
            fi
            umount "$MNT/esp" 2> /dev/null || true
            finish 0 reboot "Update done. This stick's menu could not be set to start the internal disk by default: choose 'Googlebook (internal disk)' on it, or remove the stick." ;;
        2) finish 1 poweroff "Stopped before anything was written. The internal disk was not changed." ;;
        *) finish 1 poweroff "The update stopped halfway (the reason is above). This stick's 'Googlebook (internal disk)' still starts a complete version of Googlebook on the internal disk, the one that ran before unless only the last step was left; start 'Update Googlebook on the internal disk' again to finish." ;;
    esac
fi
echo
echo "  Target: $T  $MODEL  $((SIZE / 1000000000)) GB  serial $SERIAL"
echo "  Source: this USB stick, $(awk '$1 == "image" {print $2}' "$P/manifest.txt") (checked)"
echo
echo "  EVERYTHING on $T will be erased: every partition and every system on it."
echo "  Googlebook starts fresh at the welcome screen; you sign in to your Google account again."
echo
read -r -p "  Type the last 4 characters of the serial ($SERIAL) to install, or just Enter to cancel: " A || A=""
LAST4=${SERIAL: -4}
[ -n "$A" ] && [ "${A^^}" = "${LAST4^^}" ] || finish 0 poweroff "Cancelled. Nothing was written."
[ "$(lsblk -dno SERIAL "$T" | tr -d ' ')" = "$SERIAL" ] && [ "$(lsblk -dbno SIZE "$T")" = "$SIZE" ] \
    && [ "$(cat "/sys/block/$(basename "$T")/dev")" = "$DEVNO" ] || die "$T changed while waiting"
U=$(in_use); [ -z "$U" ] || die "$U"
log "confirmed: installing on $T ($MODEL, serial $SERIAL)"

# --- 3. write; each step is read back from the disk
B=/var/lib/gbos-install/backup/${SERIAL//[^A-Za-z0-9._-]/_}-$(date +%Y%m%d-%H%M%S); mkdir -p "$B"
dd if="$T" of="$B/head.bin" bs=1M count=1 status=none
dd if="$T" of="$B/tail.bin" bs=1M iflag=skip_bytes skip=$((SIZE - 1048576)) count=1 status=none
( cd "$B" && sha256sum head.bin tail.bin > SHA256SUMS ); sync
log "ok   saved the old partition table of $T to $B (only the table, not your data)"
WROTE=1
if blkdiscard -f "$T" 2>/dev/null; then log "ok   discarded the old contents"; else log "     (discard not supported, continuing)"; fi
dd if="$W/head.img" of="$T" bs=1M oflag=direct conv=fsync status=none
sgdisk -e "$T" > /dev/null
reread "$T"
while read -r _ n name start len _ _ _ _; do
    [ "$(part_start "$T" "$n")" = $((start / 512)) ] && [ "$(part_size "$T" "$n")" = $((len / 512)) ] || die "partition $n ($name) not created as in the image"
done < <(awk '$1 == "part"' "$P/manifest.txt")
log "ok   partition table of the image"
log "     copying super ($((SLEN / 1000000000)) GB) from the stick; this takes a few minutes"
dd if="$(pdev "$SRC" "$SN")" of="$(pdev "$T" "$SN")" bs=4M iflag=fullblock oflag=direct conv=fsync status=progress 2> "/dev/$TTY"
[ "$(devsha "$(pdev "$T" "$SN")")" = "$SSHA" ] || die "super on $T does not match the image (read back from the disk)"
log "ok   super (read back from $T, SHA-256 matches the image)"
while read -r _ n name _ _ _ _ sha kind; do
    [ "$kind" = pristine ] || continue
    zstd -q -dc "$P/p$n.img.zst" | dd of="$(pdev "$T" "$n")" bs=4M iflag=fullblock oflag=direct conv=fsync status=none
    [ "$(devsha "$(pdev "$T" "$n")")" = "$sha" ] || die "$name on $T does not match the image (read back from the disk)"
done < <(awk '$1 == "part"' "$P/manifest.txt")
log "ok   $(awk '$1 == "part" && $NF == "pristine"' "$P/manifest.txt" | wc -l) more partitions (each read back, SHA-256 matches the image)"
sgdisk -d "$UN" -n "$UN:$UD_START:0" -c "$UN:userdata" -t "$UN:$UTYPE" "$T" > /dev/null
for b in $(seq 0 63); do
    if [ $(( (16#$UATTR >> b) & 1 )) = 1 ]; then sgdisk -A "$UN:set:$b" "$T" > /dev/null; fi
done
sgdisk -G "$T" > /dev/null    # new unique GUIDs: the stick and the internal disk never share a partition GUID
reread "$T"
[ "$(part_start "$T" "$UN")" = "$UD_START" ] || die "userdata not recreated"
dd if=/dev/zero of="$(pdev "$T" "$UN")" bs=1M count=16 oflag=direct conv=fsync status=none
log "ok   empty /data partition, $(( $(part_size "$T" "$UN") * 512 / 1000000000 )) GB (formatted on the first boot)"

E=$(pdev "$T" "$EN")
BG=$(sgdisk -i "$BN" "$T" | sed -n 's/^Partition unique GUID: //p' | tr 'A-F' 'a-f')
[[ "$BG" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || die "cannot read the GUID of boot_a"
cp "$W/initrd.orig" "$W/initrd.img"
python3 "$W/gbos.py" bootconfig-set "$W/initrd.img" "$BG" || die "cannot set boot_part_uuid in the initrd"
mcopy -o -i "$E" "$W/initrd.img" ::/initrd.img
for f in $ENTRIES; do mcopy -o -i "$E" "$W/$f" "::/loader/entries/$f"; done
mcopy -o -i "$E" "$W/loader.conf" ::/loader/loader.conf
log "ok   boot files: Android finds this disk by boot_a $BG, NVMe allowed, quiet entry by default"

# the stick's menu: an entry for the internal disk (XBOOTLDR), started by default. What updates of an earlier
# installation left there (its previous version and the per-slot copies) would start a disk that is gone: removed.
mount "$(pdev "$SRC" "$XN")" "$MNT/xb"
rm -f "$MNT/xb/loader/entries/gbos-nvme-prev.conf"; rm -rf "$MNT/xb/gbos-nvme/a" "$MNT/xb/gbos-nvme/b"
mkdir -p "$MNT/xb/gbos-nvme" "$MNT/xb/loader/entries"
cp "$W/kernel.efi" "$MNT/xb/gbos-nvme/kernel.efi"; cp "$W/initrd.img" "$MNT/xb/gbos-nvme/initrd.img"
cp "$W/gbos-nvme.conf" "$W/gbos-nvme-debug.conf" "$MNT/xb/loader/entries/"
sync; umount "$MNT/xb"
mount "$(pdev "$SRC" "$SEN")" "$MNT/esp"
cp "$W/stick-loader.conf" "$MNT/esp/loader/loader.conf"
sync; umount "$MNT/esp"
log "ok   the stick's menu starts the new installation by default"

# --- 4. verify the result, read back from the disks
log "Checking the result..."
blockdev --flushbufs "$T"
python3 "$W/gbos.py" verify-target "$T" "$P/manifest.txt" "$SRC" "$UD_START" "$BG" || die "the partition table on $T is not as expected"
SV=$(sgdisk -v "$T" 2>&1) || die "sgdisk -v failed on $T: $SV"
log "$SV"
grep -q '^No problems found' <<< "$SV" || die "sgdisk reports problems on $T"
cmp -s -n 16777216 <(dd if="$(pdev "$T" "$UN")" bs=1M count=16 iflag=direct status=none) /dev/zero || die "the start of /data is not empty"
blockdev --flushbufs "$E"
mcopy -n -i "$E" ::/initrd.img "$W/rb.initrd.img"
mcopy -n -i "$E" ::/kernel.efi "$W/rb.kernel.efi"
cmp -s "$W/initrd.img" "$W/rb.initrd.img" || die "ESP initrd on $T differs from what was written"
python3 "$W/gbos.py" bootconfig-check "$W/rb.initrd.img" "$BG" || die "ESP initrd on $T does not point at boot_a"
cmp -s "$W/kernel.efi" "$W/rb.kernel.efi" || die "ESP kernel on $T differs from the image"
for f in $ENTRIES; do
    mtype -i "$E" "::/loader/entries/$f" > "$W/rb.$f"
    cmp -s "$W/$f" "$W/rb.$f" || die "ESP entry $f on $T differs from what was written"
done
[ "$(esp_entries "$E")" = "$ENTRIES" ] || die "the ESP on $T does not hold exactly the entries $ENTRIES"
mtype -i "$E" ::/loader/loader.conf > "$W/rb.loader.conf"
cmp -s "$W/loader.conf" "$W/rb.loader.conf" || die "ESP loader.conf on $T differs from what was written"
X=$(pdev "$SRC" "$XN"); blockdev --flushbufs "$X"
mcopy -n -i "$X" ::/gbos-nvme/kernel.efi "$W/rb.x.kernel.efi"
mcopy -n -i "$X" ::/gbos-nvme/initrd.img "$W/rb.x.initrd.img"
cmp -s "$W/rb.kernel.efi" "$W/rb.x.kernel.efi" && cmp -s "$W/rb.initrd.img" "$W/rb.x.initrd.img" \
    || die "the stick's copy of the internal disk's kernel or initrd differs"
[ "$( { mdir -b -i "$X" ::/loader/entries || true; } | tr -d '\r' | sed -n 's|^::/loader/entries/\(gbos-nvme[^/]*\.conf\)$|\1|p' | LC_ALL=C sort | tr '\n' ' ')" \
    = "gbos-nvme-debug.conf gbos-nvme.conf " ] || die "the stick's menu holds other internal-disk entries than gbos-nvme.conf and gbos-nvme-debug.conf"
! mdir -b -i "$X" ::/gbos-nvme/a > /dev/null 2>&1 && ! mdir -b -i "$X" ::/gbos-nvme/b > /dev/null 2>&1 \
    || die "the stick still holds copies of an earlier installation (/gbos-nvme/a or /gbos-nvme/b)"
for f in gbos-nvme.conf gbos-nvme-debug.conf; do
    mtype -i "$X" "::/loader/entries/$f" > "$W/rb.$f"
    cmp -s "$W/$f" "$W/rb.$f" && grep -qx 'linux /gbos-nvme/kernel.efi' "$W/rb.$f" && grep -qx 'initrd /gbos-nvme/initrd.img' "$W/rb.$f" \
        || die "the stick's entry $f is not as written"
done
S=$(pdev "$SRC" "$SEN"); blockdev --flushbufs "$S"
mtype -i "$S" ::/loader/loader.conf > "$W/rb.stick-loader.conf"
cmp -s "$W/stick-loader.conf" "$W/rb.stick-loader.conf" || die "the stick's loader.conf is not as written"
log "ok   boot files read back: ESP initrd, kernel, entries, loader.conf; the stick's entry and kernel"
log "INSTALL OK: $T holds Googlebook. The first boot sets up /data and takes a few minutes. ($(date))"
finish 0 reboot "Remove the USB stick, or keep it inserted: its menu starts the internal disk by default."
