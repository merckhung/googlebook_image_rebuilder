#!/usr/bin/env bash
# Write a finished googlebook-kano.img to a USB disk (Linux host, as root).
#
# usage: sudo install/write-usb.sh <googlebook-kano.img> /dev/disk/by-id/usb-...
#        By default /data (userdata) is grown to fill the stick (see wusb_py write); --no-grow keeps the image's own
#            userdata size, as before.
#        install/write-usb.sh --check-only <image> <disk>   every check, nothing written. --check-only runs the same
#            re-check that follows the serial prompt in a real run (without the prompt). As a normal user it skips the
#            one probe that needs root (the exclusive open) and says so.
#        sudo install/write-usb.sh --keep-data [--without-fallback] <image> <disk>   update a stick that already holds
#            Googlebook (built with the same AVB key) instead of erasing it: install/gbos-update.py writes the image
#            into the stick's other slot and keeps /data; the running version stays on the boot menu as "(previous
#            version)" (--without-fallback: when the ESP has no room for both, as the update says). Same guards and
#            serial confirmation; the image's checksum is checked by gbos-update.py.
#
# Every guard fails closed: a query that fails or answers oddly is a refusal.
#  - the target is a whole disk (lsblk TYPE disk, listed in /sys/block) on the USB transport, with 512-byte logical
#    sectors (the image's GPT is laid out for them), at least as large as the image, and it reports a serial;
#  - it is not the disk holding / (findmnt -v plus lsblk -s, so LUKS, LVM and btrfs subvolumes count);
#  - nothing on it is mounted (lsblk MOUNTPOINT of the disk and each partition, and the findmnt table) or swap
#    (/proc/swaps), it has no holders (dm-crypt, LVM, md), and it opens with O_EXCL, which also catches ZFS and btrfs
#    members and mounts in other namespaces that lsblk cannot see;
#  - the image matches its .sha256, and its protective MBR and GPT (both copies) are valid;
#  - sgdisk (package gdisk) is installed, for the final read-only GPT check;
#  - a sibling <image>.not-self-tested (the installer stick always writes one, because that build does not
#    boot the image; a skipped QEMU self-test writes one too) is only written after you type "untested".
#    The warning prints that file's first line; it does not assume which build flag created the marker;
#  - you type the disk's serial. Its identity (what the argument resolves to, major:minor, sysfs device, size, sector
#    size, TYPE, TRAN, full SERIAL) is recorded before the prompt; after the answer it is read again together with all
#    the in-use checks, and any difference refuses.
# The write is done by the Python program in wusb_py below: it opens the disk O_WRONLY|O_EXCL without O_CREAT (a
# vanished node can never become a regular file), checks with fstat that it is the confirmed block device, writes, fsyncs,
# drops the page cache and compares a read-back of the whole range with the image. Through the same exclusive
# descriptor it then moves the backup GPT to the end of the disk (what sgdisk -e does) and reads the new GPT back.
# Finally the identity is read again and sgdisk -v (read-only) has to find no problems before PASS.
# Exit: 0 PASS (or CHECKS OK), 1 failed (sha256 mismatch, no confirmation, or the disk is partially written),
#       2 refused (nothing written), 3 image written and verified but the backup GPT step or its check failed.
# Test hook: GBOS_WUSB_TEST_SYSFS=<dir> stands in for /sys. It is honoured only together with --check-only (anything
# else refuses), so it can never steer a write. tests/install/run.sh uses it.
set -euo pipefail
USAGE="usage: sudo write-usb.sh [--no-grow | --keep-data [--without-fallback]] <googlebook-kano.img> /dev/disk/by-id/usb-...   or   write-usb.sh --check-only <image> <disk>"
PHASE=
refuse() { echo "refusing${PHASE:+ ($PHASE)}: $*"; exit 2; }

CHECK_ONLY=0 KEEP=0 NOFALLBACK=0 GROW=grow
while [ $# -gt 2 ]; do
    case "$1" in
        --check-only) CHECK_ONLY=1; shift ;;
        --keep-data) KEEP=1; shift ;;
        --without-fallback) NOFALLBACK=1; shift ;;
        --no-grow) GROW=nogrow; shift ;;
        *) break ;;
    esac
done
if [ $# -ne 2 ]; then echo "$USAGE"; exit 2; fi
[ "$NOFALLBACK" = 0 ] || [ "$KEEP" = 1 ] || { echo "--without-fallback goes with --keep-data"; exit 2; }
[ "$GROW" = grow ] || [ "$KEEP" = 0 ] || { echo "--no-grow is for a fresh write; --keep-data never changes the GPT"; exit 2; }
case ${BASH_SOURCE[0]} in */*) HERE=${BASH_SOURCE[0]%/*} ;; *) HERE=. ;; esac
ENGINE=$HERE/gbos-update.py
IMG=$1 ARG=$2
SYS=/sys
if [ -n "${GBOS_WUSB_TEST_SYSFS:-}" ]; then
    [ "$CHECK_ONLY" = 1 ] || refuse "GBOS_WUSB_TEST_SYSFS is a test hook and is honoured only with --check-only"
    SYS=$GBOS_WUSB_TEST_SYSFS
fi
IS_ROOT=0
if [ "$(id -u)" = 0 ]; then IS_ROOT=1; fi
[ "$CHECK_ONLY" = 1 ] || [ "$IS_ROOT" = 1 ] || { echo "run as root (it writes a block device)"; exit 2; }

sysread() {  # sysread <file>: the first line of a sysfs attribute; fails when it is missing or empty
    local v=
    { IFS= read -r v || [ -n "$v" ]; } 2>/dev/null < "$1" || return 1
    [ -n "$v" ] || return 1
    printf '%s\n' "$v"
}
lsq() {  # lsq <COLUMN> <disk>: one lsblk column of the disk itself, trimmed; fails when lsblk fails or answers oddly
    local v
    v=$(lsblk -d -n -o "$1" "$2") || return 1
    case $v in *$'\n'*) return 1 ;; esac
    v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    printf '%s\n' "$v"
}
majmin() {  # majmin <node>: decimal major:minor of a device node
    local t
    t=$(stat -L -c '%t %T' -- "$1") || return 1
    printf '%d:%d\n' "0x${t% *}" "0x${t#* }"
}

identity() {  # read what pins the target: sets I_* and IDENT, or WHY and returns 1 when anything cannot be read
    local sysdev sectors
    WHY=
    I_NODE=$(readlink -e -- "$ARG") || { WHY="$ARG does not exist (any more)"; return 1; }
    [ -b "$I_NODE" ] || { WHY="$I_NODE is not a block device"; return 1; }
    I_NAME=${I_NODE##*/}
    [ -d "$SYS/block/$I_NAME" ] || { WHY="$I_NODE is not a whole disk (no $SYS/block/$I_NAME)"; return 1; }
    I_RDEV=$(majmin "$I_NODE") || { WHY="cannot read the major:minor of $I_NODE"; return 1; }
    sysdev=$(sysread "$SYS/block/$I_NAME/dev") || { WHY="cannot read $SYS/block/$I_NAME/dev"; return 1; }
    [ "$sysdev" = "$I_RDEV" ] || { WHY="$SYS/block/$I_NAME/dev says $sysdev but $I_NODE is $I_RDEV"; return 1; }
    I_SYSPATH=$(readlink -e -- "$SYS/block/$I_NAME") || { WHY="cannot resolve $SYS/block/$I_NAME"; return 1; }
    sectors=$(sysread "$SYS/block/$I_NAME/size") || { WHY="cannot read $SYS/block/$I_NAME/size"; return 1; }
    I_LBS=$(sysread "$SYS/block/$I_NAME/queue/logical_block_size") \
        || { WHY="cannot read $SYS/block/$I_NAME/queue/logical_block_size"; return 1; }
    [[ $sectors =~ ^[0-9]+$ && $I_LBS =~ ^[0-9]+$ ]] || { WHY="odd size ($sectors) or sector size ($I_LBS)"; return 1; }
    I_BYTES=$((sectors * 512))   # /sys/block/*/size counts 512-byte units whatever the sector size
    I_TYPE=$(lsq TYPE "$I_NODE") || { WHY="lsblk cannot report TYPE of $I_NODE"; return 1; }
    I_TRAN=$(lsq TRAN "$I_NODE") || { WHY="lsblk cannot report TRAN of $I_NODE"; return 1; }
    I_SERIAL=$(lsq SERIAL "$I_NODE") || { WHY="lsblk cannot report SERIAL of $I_NODE"; return 1; }
    IDENT=$(printf 'argument %s -> %s\nmajor:minor %s\nsysfs %s\nbytes %s\nlogical sector %s\ntype %s\ntransport %s\nserial %s' \
        "$ARG" "$I_NODE" "$I_RDEV" "$I_SYSPATH" "$I_BYTES" "$I_LBS" "$I_TYPE" "$I_TRAN" "$I_SERIAL")
}

not_root_disk() {  # refuse the disk / lives on, also through LUKS, LVM and btrfs subvolumes (-v drops a [/@] suffix)
    local src line deps
    src=$(findmnt -n -v -o SOURCE /) || refuse "findmnt cannot tell what / is mounted from"
    [ -n "$src" ] || refuse "findmnt reports no source for /"
    while IFS= read -r line; do
        if [ -b "$line" ]; then
            deps=$(lsblk -n -r -o MAJ:MIN -s "$line") || refuse "lsblk cannot list the disks under / ($line)"
            if grep -qxF -- "$I_RDEV" <<< "${deps// /}"; then refuse "$I_NODE holds the running system"; fi
        else
            echo "note: / is mounted from '$line', not a disk node; the exclusive-open probe has to catch a disk behind it"
        fi
    done <<< "$src"
}

in_use() {  # refuse when the disk, a partition or anything stacked on them is mounted, swap, held or busy
    local tree mounts swaps swapdevs='' f rest n mm mp h first=1
    tree=$(lsblk -n -r -p -o NAME,MAJ:MIN,MOUNTPOINT "$I_NODE") || refuse "lsblk cannot list $I_NODE and its partitions"
    [ -n "$tree" ] || refuse "lsblk lists nothing for $I_NODE"
    mounts=$(findmnt -n -r -v -o SOURCE) || refuse "findmnt cannot list the mounted filesystems"
    [ -n "$mounts" ] || refuse "findmnt lists no mounted filesystems"
    swaps=$(cat /proc/swaps) || refuse "cannot read /proc/swaps"
    while read -r f rest; do
        if [ "$f" = Filename ] || [ ! -b "$f" ]; then continue; fi   # swap files sit on a mounted filesystem
        mm=$(majmin "$f") || refuse "cannot read the major:minor of swap device $f"
        swapdevs+="$mm"$'\n'
    done <<< "$swaps"
    while read -r n mm mp; do
        [ -n "$n" ] || continue
        if [ "$first" = 1 ]; then
            [ "$n" = "$I_NODE" ] && [ "$mm" = "$I_RDEV" ] || refuse "lsblk describes $n ($mm), not $I_NODE ($I_RDEV)"
            first=0
        fi
        [ -z "$mp" ] || refuse "$n is mounted or used as swap ($mp)"
        if grep -qxF -- "$n" <<< "$mounts"; then refuse "$n is mounted or used as swap (findmnt)"; fi
        if grep -qxF -- "$mm" <<< "$swapdevs"; then refuse "$n is mounted or used as swap (/proc/swaps)"; fi
    done <<< "$tree"
    [ -d "$SYS/block/$I_NAME/holders" ] || refuse "cannot list the holders of $I_NODE ($SYS/block/$I_NAME/holders)"
    for h in "$SYS/block/$I_NAME"/holders/* "$SYS/block/$I_NAME/$I_NAME"*/holders/*; do
        if [ -e "$h" ] || [ -L "$h" ]; then refuse "$I_NODE is in use by ${h##*/} (dm-crypt, LVM or md)"; fi
    done
    if [ "$IS_ROOT" = 1 ]; then
        wusb_py probe "$I_NODE" "$I_RDEV" || refuse "$I_NODE did not pass the exclusive-open probe"
    else
        echo "note: not root, so the exclusive-open probe of $I_NODE was skipped (it needs root); a real run makes it"
    fi
}

wusb_py() {  # wusb_py probe|write ...: the exclusive-open probe and the writer, see the docstring
    python3 -I - "$@" <<'PYEOF'   # -I: no cwd or PYTHON* env on the module path (this runs as root)
# >>> write-usb.py (embedded in install/write-usb.sh; tests/install/run.sh extracts the lines between these markers)
"""Exclusive-open probe and writer for install/write-usb.sh.

probe <device> <major:minor>
    Open the device read-only with O_EXCL and close it. EBUSY means something holds it: a mounted or swap partition,
    dm-crypt/LVM/md, a ZFS pool or btrfs member, a mount in another namespace, or another exclusive opener.
gptcheck <image> <device bytes>
    Only reads the image: its protective MBR and both GPTs must be valid and fit a disk of that size.
write <image> <sha256> <device> <major:minor> <device bytes> <sysfs device path>
    Check the image's protective MBR and GPT (primary and backup, CRCs, layout) and work out the GPT for the larger disk
    before anything is written. Open the device O_WRONLY|O_EXCL, check with fstat that it is the confirmed block device
    and that its sysfs device, size and sector size are the confirmed ones, write the image in large chunks (fsync every
    256 MiB), fsync, drop the device's page cache (BLKFLSBUF, POSIX_FADV_DONTNEED), read the range back through a second
    descriptor and compare it with the image and the sha256. Then, still through the exclusive descriptor, move the
    backup GPT to the end of the disk exactly as `sgdisk -e` does (backup entries and header at the end, primary header
    and protective MBR updated, the old backup left where it was), drop the cache again and read all of it back. The
    exclusive descriptor stays open until that last comparison is done. With grow (what write-usb.sh passes unless
    --no-grow), the same step also gives userdata the free space at the end of the disk: if userdata is the last
    partition it is extended in place, otherwise its entry moves to the first 1 MiB boundary after the last partition
    and runs to the last usable sector (as the installer stick does on the internal disk). Its type, unique GUID,
    attributes and name stay. The first 16 MiB of the new range are zeroed first, so Android finds no filesystem and
    formats /data (f2fs) at the new size on the first boot. Skipped when it would not make userdata larger.
Nothing here opens with O_CREAT, so a device node that disappeared can never turn into a regular file.
Exit 0 done, 2 refused before the first byte was written, 1 failed after writing started, 3 the image is written and
read back correctly but the GPT step failed.
"""
import errno
import fcntl
import hashlib
import os
import stat
import struct
import sys
import time
import zlib

BLKFLSBUF = 0x1261  # _IO(0x12, 97): write back and drop the block device's page cache
BLKSSZGET = 0x1268  # _IO(0x12, 104): logical sector size
CHUNK = 16 << 20
SYNC_EVERY = 256 << 20
MIB = 1 << 20
started = False  # set right before the first byte goes to the device
gpt_phase = False  # set once the image is written and read back, right before the GPT is touched


class Stop(Exception):
    """Refusal before the first write (exit 2) or failure after it (exit 1)."""


def parse_rdev(text):
    try:
        major, minor = (int(x, 10) for x in text.split(":"))
    except ValueError:
        raise Stop("bad major:minor %r" % text)
    return os.makedev(major, minor)


def rdev_str(rdev):
    return "%d:%d" % (os.major(rdev), os.minor(rdev))


def check_node(path, rdev):
    """Before anything is opened: the path must already be the confirmed block device node."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise Stop("%s does not exist (was the disk unplugged?)" % path)
    if not stat.S_ISBLK(st.st_mode):
        raise Stop("%s is not a block device" % path)
    if st.st_rdev != rdev:
        raise Stop("%s is now device %s, not the confirmed %s" % (path, rdev_str(st.st_rdev), rdev_str(rdev)))


def open_dev(path, flags, rdev):
    try:
        fd = os.open(path, flags | os.O_CLOEXEC | os.O_NOFOLLOW)  # never O_CREAT
    except OSError as e:
        if e.errno == errno.EBUSY:
            raise Stop("%s is in use: a partition is mounted or used as swap, it is a dm-crypt/LVM/md, ZFS or btrfs "
                       "member, or another program holds it exclusively" % path)
        raise Stop("cannot open %s: %s" % (path, e.strerror))
    st = os.fstat(fd)
    if not stat.S_ISBLK(st.st_mode):
        os.close(fd)
        raise Stop("%s is not a block device" % path)
    if st.st_rdev != rdev:
        os.close(fd)
        raise Stop("%s opened as device %s, not the confirmed %s" % (path, rdev_str(st.st_rdev), rdev_str(rdev)))
    return fd


def read_full(fd, n, what, offset):
    parts = []
    while n:
        try:
            b = os.read(fd, n)
        except OSError as e:
            raise Stop("reading %s failed near byte %d: %s" % (what, offset, e.strerror))
        if not b:
            raise Stop("%s ended early, at byte %d" % (what, offset))
        parts.append(b)
        n -= len(b)
        offset += len(b)
    return b"".join(parts)


class Progress:
    def __init__(self, verb, total):
        self.verb, self.total = verb, total
        self.t0 = self.last = time.monotonic()
        self.tty = sys.stderr.isatty()

    def show(self, done, final=False):
        now = time.monotonic()
        if not final and now - self.last < (1 if self.tty else 15):
            return
        self.last = now
        rate = done / max(now - self.t0, 1e-3) / 1e6
        line = "  %s %d / %d MiB (%d%%), %.1f MB/s" % (
            self.verb, done // MIB, self.total // MIB, done * 100 // self.total, rate)
        sys.stderr.write(("\r" + line + ("\n" if final else "")) if self.tty else line + "\n")
        sys.stderr.flush()


def copy(ifd, wfd, size, sha, dev):
    global started
    h = hashlib.sha256()
    prog = Progress("written", size)
    done = synced = 0
    while done < size:
        buf = read_full(ifd, min(CHUNK, size - done), "the image", done)
        h.update(buf)
        view = memoryview(buf)
        started = True
        while len(view):
            try:
                n = os.write(wfd, view)
            except OSError as e:
                raise Stop("writing %s failed at byte %d: %s" % (dev, done, e.strerror))
            if n <= 0:
                raise Stop("writing %s stopped at byte %d" % (dev, done))
            view = view[n:]
            done += n
        if done - synced >= SYNC_EVERY or done == size:
            try:
                os.fsync(wfd)
            except OSError as e:
                raise Stop("flushing %s failed below byte %d: %s" % (dev, done, e.strerror))
            synced = done
        prog.show(done)
    prog.show(done, final=True)
    if h.hexdigest() != sha:
        raise Stop("the image changed while it was being written (its sha256 is now %s)" % h.hexdigest())


def open_uncached(wfd, dev, rdev):
    """A read descriptor for the device after its page cache was dropped, so reads come from the disk."""
    dropped = False
    try:
        fcntl.ioctl(wfd, BLKFLSBUF)  # needs root; writes back and invalidates the device's cached pages
        dropped = True
    except OSError:
        pass
    rfd = open_dev(dev, os.O_RDONLY, rdev)
    try:
        os.posix_fadvise(rfd, 0, 0, os.POSIX_FADV_DONTNEED)
        dropped = True
    except (AttributeError, OSError):
        pass
    if not dropped:
        os.close(rfd)
        raise Stop("could not drop the page cache of %s, so a read-back could not prove what the disk holds" % dev)
    return rfd


def pread_full(fd, n, offset, what):
    parts = []
    while n:
        try:
            b = os.pread(fd, n, offset)
        except OSError as e:
            raise Stop("reading %s failed at byte %d: %s" % (what, offset, e.strerror))
        if not b:
            raise Stop("%s ended early, at byte %d" % (what, offset))
        parts.append(b)
        n -= len(b)
        offset += len(b)
    return b"".join(parts)


def pwrite_full(fd, data, offset, what):
    view = memoryview(data)
    while len(view):
        try:
            n = os.pwrite(fd, view, offset)
        except OSError as e:
            raise Stop("writing %s failed at byte %d: %s" % (what, offset, e.strerror))
        if n <= 0:
            raise Stop("writing %s stopped at byte %d" % (what, offset))
        view = view[n:]
        offset += n


def gpt_header(b, lba, what):
    """Parse one GPT header sector and check its signature, size, CRC and own LBA."""
    if b[:8] != b"EFI PART":
        raise Stop("%s has no GPT header at LBA %d" % (what, lba))
    hsize, hcrc = struct.unpack_from("<II", b, 12)
    if not 92 <= hsize <= 512 or zlib.crc32(b[:16] + bytes(4) + b[20:hsize]) != hcrc:
        raise Stop("%s: bad GPT header size or CRC at LBA %d" % (what, lba))
    my, alt, first, last = struct.unpack_from("<QQQQ", b, 24)
    elba, n, esz, ecrc = struct.unpack_from("<QIII", b, 72)
    if my != lba:
        raise Stop("%s: the GPT header at LBA %d says it is at LBA %d" % (what, lba, my))
    return dict(hsize=hsize, my=my, alt=alt, first=first, last=last, disk=b[56:72], elba=elba, n=n, esz=esz, ecrc=ecrc)


def gpt_with(b, hsize, **fields):
    """A copy of the header sector b with some fields changed and its CRC recomputed."""
    h = bytearray(b)
    for name, value in fields.items():
        if name == "ecrc":
            struct.pack_into("<I", h, 88, value)
            continue
        struct.pack_into("<Q", h, {"my": 24, "alt": 32, "last": 48, "elba": 72}[name], value)
    struct.pack_into("<I", h, 16, 0)
    struct.pack_into("<I", h, 16, zlib.crc32(bytes(h[:hsize])))
    return bytes(h)


GROW_ALIGN = 2048        # new userdata starts on a 1 MiB boundary, as the installer stick's gbos-install.sh does
GROW_ZERO = 16 << 20     # bytes zeroed at the new userdata start, so Android sees an unformatted /data and formats it


def userdata_grow(table, n_ent, esz, last_usable, img_n):
    """The table with the userdata entry moved to [after the image's last partition, aligned] .. last usable LBA, or
    None when that would not make userdata larger. Only the start and end LBA of the one entry named userdata change:
    type GUID, unique GUID, attributes and name stay. The old userdata area is left in place, unused."""
    ud, top = None, 0
    for i in range(n_ent):
        e = table[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16):
            continue
        name = e[56:128].decode("utf-16le", "replace").split("\0")[0]
        a, z = struct.unpack_from("<QQ", e, 32)
        if name == "userdata":
            if ud is not None:
                raise Stop("the image has more than one partition named userdata")
            ud = (i, a, z)
        else:
            top = max(top, z)
    if ud is None:
        return None   # nothing to grow (not a Googlebook image): written as it is
    i, a, z = ud
    if z >= top:   # userdata already is the last partition: extend it in place
        start = a
    else:
        start = (top + 1 + GROW_ALIGN - 1) // GROW_ALIGN * GROW_ALIGN
        if start < img_n:
            start = max(start, (img_n + GROW_ALIGN - 1) // GROW_ALIGN * GROW_ALIGN)
    if last_usable < start or last_usable - start + 1 <= z - a + 1 or (last_usable - start + 1) * 512 < 2 * GROW_ZERO:
        return None
    t = bytearray(table)
    struct.pack_into("<QQ", t, i * esz + 32, start, last_usable)
    return dict(table=bytes(t), index=i, old=(a, z), new=(start, last_usable), moved=start != a)


def gpt_plan(ifd, size, devbytes, what="the image", grow=False):
    """Check the image's protective MBR and both GPTs, and return the sectors that make the disk's GPT: what
    `sgdisk -e` writes on a disk larger than the image. With grow, userdata also takes the free space at the end of
    the disk (userdata_grow). Nothing is written here."""
    img_n, n = size // 512, devbytes // 512
    if n < img_n:
        raise Stop("the disk (%d sectors) is smaller than the image (%d sectors)" % (n, img_n))
    mbr = pread_full(ifd, 512, 0, what)
    ents = [struct.unpack_from("<B3sB3sII", mbr, 446 + 16 * i) for i in range(4)]
    if mbr[510:512] != b"\x55\xaa" or ents[0][2] != 0xEE or ents[0][4] != 1 \
            or ents[0][5] != min(img_n - 1, 0xFFFFFFFF) or any(mbr[462:510]):
        raise Stop("%s does not start with a standard protective MBR (one 0xEE entry covering the image)" % what)
    hb = pread_full(ifd, 512, 512, what)
    h = gpt_header(hb, 1, what)
    tbytes = h["n"] * h["esz"]
    if h["esz"] < 128 or h["esz"] % 8 or not 0 < tbytes <= 1 << 20 or tbytes % 512:
        raise Stop("%s: unexpected GPT entry table (%d entries of %d bytes)" % (what, h["n"], h["esz"]))
    t = tbytes // 512
    if h["alt"] != img_n - 1 or h["elba"] != 2 or h["first"] < 2 + t or not h["first"] <= h["last"] <= img_n - 2 - t:
        raise Stop("%s: unexpected GPT layout (backup LBA %d, entries LBA %d, usable %d-%d)"
                   % (what, h["alt"], h["elba"], h["first"], h["last"]))
    table = pread_full(ifd, tbytes, 2 * 512, what)
    if zlib.crc32(table) != h["ecrc"]:
        raise Stop("%s: bad GPT entry table CRC" % what)
    bb = pread_full(ifd, 512, (img_n - 1) * 512, what)
    b = gpt_header(bb, img_n - 1, what + " (backup GPT)")
    if b["alt"] != 1 or b["elba"] != img_n - 1 - t or pread_full(ifd, tbytes, b["elba"] * 512, what) != table \
            or [b[k] for k in ("hsize", "first", "last", "disk", "n", "esz", "ecrc")] \
            != [h[k] for k in ("hsize", "first", "last", "disk", "n", "esz", "ecrc")]:
        raise Stop("%s: the backup GPT does not match the primary" % what)
    for i in range(h["n"]):
        e = table[i * h["esz"]:(i + 1) * h["esz"]]
        if e[:16] == bytes(16):
            continue
        a, z = struct.unpack_from("<QQ", e, 32)
        if not h["first"] <= a <= z <= h["last"]:
            raise Stop("%s: partition %d (%d-%d) lies outside the usable area" % (what, i + 1, a, z))
    g = userdata_grow(table, h["n"], h["esz"], n - 2 - t, img_n) if grow and n > img_n else None
    if n == img_n:  # the backup is already at the end of the disk: nothing changes
        return dict(img_n=img_n, n=n, t=t, table=table, mbr=mbr, primary=hb, backup=bb, grow=None)
    newmbr = bytearray(mbr)
    struct.pack_into("<I", newmbr, 446 + 12, min(n - 1, 0xFFFFFFFF))
    nt = g["table"] if g else table
    crc = zlib.crc32(nt)
    return dict(img_n=img_n, n=n, t=t, table=nt, mbr=bytes(newmbr), grow=g,
                primary=gpt_with(hb, h["hsize"], alt=n - 1, last=n - 2 - t, ecrc=crc),
                backup=gpt_with(hb, h["hsize"], my=n - 1, alt=1, last=n - 2 - t, elba=n - 1 - t, ecrc=crc))


def gpt_write(wfd, plan, what):
    """Move the backup GPT to the end of the disk: backup entries and header first, then the primary header, then the
    protective MBR, each step flushed. An interruption leaves the image's own GPT valid. The old backup stays where it
    was (outside every partition), as with sgdisk -e."""
    if plan["n"] == plan["img_n"]:
        return  # the image already ends at the end of the disk
    n, t = plan["n"], plan["t"]
    steps = [(plan["table"], n - 1 - t), (plan["backup"], n - 1)]
    if plan.get("grow"):
        # the new userdata start is zeroed before any table points at it, then the primary entries are rewritten
        steps = [(bytes(GROW_ZERO), plan["grow"]["new"][0])] + steps + [(plan["table"], 2)]
    for data, lba in steps + [(plan["primary"], 1), (plan["mbr"], 0)]:
        pwrite_full(wfd, data, lba * 512, what)
        try:
            os.fsync(wfd)
        except OSError as e:
            raise Stop("flushing %s failed after writing LBA %d: %s" % (what, lba, e.strerror))


def gpt_check(rfd, plan, what):
    """Read the protective MBR and both GPTs back and compare them with the plan, byte for byte."""
    n, t = plan["n"], plan["t"]
    tbytes = len(plan["table"])
    for name, lba, want in (("protective MBR", 0, plan["mbr"]), ("primary GPT header", 1, plan["primary"]),
                            ("primary GPT entries", 2, plan["table"]), ("backup GPT entries", n - 1 - t, plan["table"]),
                            ("backup GPT header", n - 1, plan["backup"])):
        if pread_full(rfd, len(want), lba * 512, what) != want:
            raise Stop("%s: the %s at LBA %d did not read back as written" % (what, name, lba))
    h = gpt_header(plan["primary"], 1, what)
    b = gpt_header(plan["backup"], n - 1, what)
    if h["alt"] != n - 1 or b["alt"] != 1 or b["elba"] != n - 1 - t or zlib.crc32(plan["table"][:tbytes]) != h["ecrc"] \
            or b["ecrc"] != h["ecrc"]:
        raise Stop("%s: the new GPT is inconsistent" % what)
    g = plan.get("grow")
    if g and pread_full(rfd, GROW_ZERO, g["new"][0] * 512, what) != bytes(GROW_ZERO):
        raise Stop("%s: the start of the new userdata did not read back as zeros" % what)


def verify(ifd, wfd, dev, rdev, size, sha):
    rfd = open_uncached(wfd, dev, rdev)
    h = hashlib.sha256()
    prog = Progress("read back and compared", size)
    os.lseek(ifd, 0, os.SEEK_SET)
    done = 0
    while done < size:
        n = min(CHUNK, size - done)
        want = read_full(ifd, n, "the image", done)
        got = read_full(rfd, n, dev, done)
        if got != want:
            first = next(i for i in range(n) if got[i] != want[i])
            raise Stop("the read-back of %s differs from the image at byte %d" % (dev, done + first))
        h.update(got)
        done += n
        prog.show(done)
    prog.show(done, final=True)
    os.close(rfd)
    if h.hexdigest() != sha:
        raise Stop("the read-back sha256 %s is not the image's %s" % (h.hexdigest(), sha))


def write(img, sha, dev, rdev_text, devbytes_text, syspath, grow="nogrow"):
    rdev = parse_rdev(rdev_text)
    if len(sha) != 64 or sha.strip("0123456789abcdef"):
        raise Stop("bad sha256 %r" % sha)
    try:
        devbytes = int(devbytes_text, 10)
    except ValueError:
        raise Stop("bad device size %r" % devbytes_text)
    ifd = os.open(img, os.O_RDONLY | os.O_CLOEXEC)
    ist = os.fstat(ifd)
    size = ist.st_size
    if not stat.S_ISREG(ist.st_mode) or size == 0 or size % 512:
        raise Stop("%s must be a non-empty regular file of whole 512-byte sectors" % img)
    if size > devbytes:
        raise Stop("the image (%d bytes) is larger than the disk (%d bytes)" % (size, devbytes))
    plan = gpt_plan(ifd, size, devbytes, grow=grow_flag(grow))  # a malformed image GPT is refused before anything is written
    check_node(dev, rdev)
    wfd = open_dev(dev, os.O_WRONLY | os.O_EXCL, rdev)
    # the number still belongs to the disk that was confirmed: same sysfs device, size and sector size
    real = os.path.realpath("/sys/dev/block/" + rdev_str(rdev))
    if real != syspath:
        raise Stop("device %s is now %s, not the confirmed %s" % (rdev_str(rdev), real, syspath))
    end = os.lseek(wfd, 0, os.SEEK_END)
    os.lseek(wfd, 0, os.SEEK_SET)
    if end != devbytes:
        raise Stop("%s now has %d bytes, not the confirmed %d" % (dev, end, devbytes))
    lbs = struct.unpack("i", fcntl.ioctl(wfd, BLKSSZGET, b"\0\0\0\0"))[0]
    if lbs != 512:
        raise Stop("%s reports %d-byte logical sectors; the image needs 512" % (dev, lbs))
    copy(ifd, wfd, size, sha, dev)
    verify(ifd, wfd, dev, rdev, size, sha)
    global gpt_phase
    gpt_phase = True
    print("== %s reads back identical to the image; moving the backup GPT to the end of the disk (%d -> %d sectors)"
          % (dev, plan["img_n"], plan["n"]), file=sys.stderr)
    gpt_write(wfd, plan, dev)
    rfd = open_uncached(wfd, dev, rdev)
    gpt_check(rfd, plan, dev)
    os.close(rfd)
    print("== GPT read back: protective MBR, primary and backup GPT as planned (backup at LBA %d)" % (plan["n"] - 1),
          file=sys.stderr)
    describe_grow(plan, sys.stderr)
    os.close(wfd)  # gives up the exclusive claim only now
    os.close(ifd)


def grow_flag(text):
    if text not in ("grow", "nogrow"):
        raise Stop("bad grow mode %r" % text)
    return text == "grow"


def describe_grow(plan, out):
    g = plan.get("grow")
    if g:
        (a, z), (s, e) = g["old"], g["new"]
        print("userdata: %d MiB at LBA %d -> %d MiB at LBA %d-%d%s; first %d MiB zeroed, Android formats /data on the "
              "first boot" % ((z - a + 1) // 2048, a, (e - s + 1) // 2048, s, e,
                              " (moved past the last partition)" if g["moved"] else "", GROW_ZERO >> 20), file=out)
    else:
        print("userdata: left at the image's size", file=out)


def gptcheck(img, devbytes_text, grow="nogrow"):
    try:
        devbytes = int(devbytes_text, 10)
    except ValueError:
        raise Stop("bad device size %r" % devbytes_text)
    ifd = os.open(img, os.O_RDONLY | os.O_CLOEXEC)
    ist = os.fstat(ifd)
    if not stat.S_ISREG(ist.st_mode) or ist.st_size == 0 or ist.st_size % 512:
        raise Stop("%s must be a non-empty regular file of whole 512-byte sectors" % img)
    plan = gpt_plan(ifd, ist.st_size, devbytes, grow=grow_flag(grow))
    os.close(ifd)
    print("image GPT OK: backup GPT at LBA %d of the image, LBA %d of the disk" % (plan["img_n"] - 1, plan["n"] - 1))
    describe_grow(plan, sys.stdout)


def status():
    return 3 if gpt_phase else 1 if started else 2


def main(argv):
    try:
        if len(argv) == 3 and argv[0] == "probe":
            rdev = parse_rdev(argv[2])
            check_node(argv[1], rdev)
            os.close(open_dev(argv[1], os.O_RDONLY | os.O_EXCL, rdev))
        elif len(argv) in (3, 4) and argv[0] == "gptcheck":
            gptcheck(*argv[1:])
        elif len(argv) in (7, 8) and argv[0] == "write":
            write(*argv[1:])
        else:
            raise Stop("usage: probe <device> <major:minor> | gptcheck <image> <device bytes> [grow|nogrow] | "
                       "write <image> <sha256> <device> <major:minor> <device bytes> <sysfs device path> [grow|nogrow]")
    except (Stop, OSError) as e:
        print("%s: %s" % ("FAIL" if started else "refusing", e), file=sys.stderr)
        return status()
    except KeyboardInterrupt:
        print("FAIL: interrupted" if started else "refusing: interrupted", file=sys.stderr)
        return status()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
# <<< write-usb.py
PYEOF
}

command -v python3 >/dev/null 2>&1 || refuse "python3 is needed (the exclusive open, the write and the read-back)"
command -v sgdisk >/dev/null 2>&1 || refuse "sgdisk is needed for the final GPT check (package gdisk); nothing written"
[ -f "$IMG" ] && [ -f "$IMG.sha256" ] || refuse "image or $IMG.sha256 missing"
SIZE=$(stat -c %s -- "$IMG") || refuse "cannot stat $IMG"
[ "$SIZE" -gt 0 ] && [ $((SIZE % 512)) -eq 0 ] || refuse "$IMG is empty or not whole 512-byte sectors"
SHA=$(head -n 1 -- "$IMG.sha256" | cut -d' ' -f1) || refuse "cannot read $IMG.sha256"
[[ $SHA =~ ^[0-9a-f]{64}$ ]] || refuse "$IMG.sha256 does not start with a sha256"
RES=$(readlink -e -- "$ARG") || refuse "$ARG does not exist"
[ -b "$RES" ] || refuse "$RES is not a block device"
identity || refuse "$WHY"
[ "$I_TYPE" = disk ] || refuse "$I_NODE is not a whole disk (lsblk TYPE '$I_TYPE')"
[ "$I_TRAN" = usb ] || refuse "$I_NODE is not on the USB transport (lsblk TRAN '$I_TRAN')"
not_root_disk
[ "$I_LBS" = 512 ] || refuse "$I_NODE has $I_LBS-byte logical sectors; the image's GPT is laid out for 512-byte sectors"
[ "$I_BYTES" -ge "$SIZE" ] || refuse "$I_NODE ($I_BYTES bytes) is smaller than the image ($SIZE bytes)"
[ -n "$I_SERIAL" ] || refuse "$I_NODE reports no serial number to confirm against"
in_use
RM=$(sysread "$SYS/block/$I_NAME/removable") || RM=unknown
[ "$RM" = 1 ] || echo "note: $I_NODE does not report removable media (USB SSDs often don't)"
echo "== checking image sha256"
[ "$(sha256sum < "$IMG" | cut -d' ' -f1)" = "$SHA" ] || { echo "image sha256 mismatch"; exit 1; }
echo "== checking the image's protective MBR and GPT"
wusb_py gptcheck "$IMG" "$I_BYTES" "$GROW" || refuse "the image's GPT cannot be moved to the end of $I_NODE (above)"
lsblk -o NAME,SIZE,TRAN,MODEL,SERIAL "$I_NODE" || refuse "lsblk cannot describe $I_NODE"
printf '%s\n' "$IDENT" | sed 's/^/  /'
CONFIRMED=$IDENT
UNTESTED=0
if [ -e "$IMG.not-self-tested" ]; then
    UNTESTED=1
    MARKER="$IMG.not-self-tested"
    echo "WARNING: $MARKER is present: this image was not booted in QEMU before this write."
    if [ -f "$MARKER" ] && [ ! -L "$MARKER" ]; then
        REASON=$(head -n 1 -- "$MARKER" || true)
        if [ -n "$REASON" ]; then
            printf '%s\n' "         marker: $REASON"
        fi
    else
        echo "         the marker is not a regular file; type untested anyway."
    fi
    echo "         Prefer an image that has been booted on a Linux host with KVM."
fi
if [ "$KEEP" = 1 ]; then
    [ -f "$ENGINE" ] || refuse "--keep-data needs $ENGINE"
    for c in lz4 mdir mmd mdel minfo mtype mcopy; do command -v "$c" >/dev/null 2>&1 || refuse "--keep-data needs $c (lz4, mtools)"; done
    echo "== what is on $I_NODE now"
    if [ "$IS_ROOT" = 1 ]; then
        python3 -I "$ENGINE" inspect "$I_NODE" || refuse "$I_NODE holds no Googlebook that can be updated (above); write it without --keep-data instead"
    else
        echo "note: not root, so $I_NODE was not read (gbos-update.py inspect needs root)"
    fi
fi
if [ "$CHECK_ONLY" = 1 ]; then
    echo "== check only: no prompt"
elif [ "$KEEP" = 1 ]; then
    if [ "$UNTESTED" = 1 ]; then
        read -r -p "Type untested to install this image anyway: " ANSWER || { echo; echo "no answer, nothing written"; exit 1; }
        [ "$ANSWER" = untested ] || { echo "not acknowledged, nothing written"; exit 1; }
    fi
    read -r -p "Googlebook on $I_NODE gets this image; /data stays. Type its serial ($I_SERIAL) to continue: " ANSWER \
        || { echo; echo "no answer, nothing written"; exit 1; }
    [ "$ANSWER" = "$I_SERIAL" ] || { echo "serial not confirmed, nothing written"; exit 1; }
else
    if [ "$UNTESTED" = 1 ]; then
        read -r -p "Type untested to write this image anyway: " ANSWER || { echo; echo "no answer, nothing written"; exit 1; }
        [ "$ANSWER" = untested ] || { echo "not acknowledged, nothing written"; exit 1; }
    fi
    read -r -p "Everything on $I_NODE will be erased. Type its serial ($I_SERIAL) to continue: " ANSWER \
        || { echo; echo "no answer, nothing written"; exit 1; }
    [ "$ANSWER" = "$I_SERIAL" ] || { echo "serial not confirmed, nothing written"; exit 1; }
fi

# between the prompt and the write the disk may have been unplugged, replaced or mounted: read everything again
PHASE=re-check
echo "== re-checking $ARG"
identity || refuse "$WHY"
if [ "$IDENT" != "$CONFIRMED" ]; then
    echo "confirmed:"; printf '%s\n' "$CONFIRMED" | sed 's/^/  /'
    echo "now:"; printf '%s\n' "$IDENT" | sed 's/^/  /'
    refuse "$ARG changed since it was confirmed; nothing written"
fi
not_root_disk
in_use
PHASE=
if [ "$CHECK_ONLY" = 1 ]; then echo "CHECKS OK: $I_NODE (serial $I_SERIAL) could take the image; nothing written"; exit 0; fi

if [ "$KEEP" = 1 ]; then
    echo "== updating $I_NODE (install/gbos-update.py)"
    NF=()
    if [ "$NOFALLBACK" = 1 ]; then NF=(--without-fallback); fi
    if python3 -I "$ENGINE" apply "$I_NODE" --image "$IMG" --mode stick --rdev "$I_RDEV" --syspath "$I_SYSPATH" --bytes "$I_BYTES" \
            ${NF[@]+"${NF[@]}"}; then :; else
        rc=$?
        if [ "$rc" = 2 ]; then echo "nothing was written to $I_NODE"; exit 2; fi
        echo "FAIL: the update of $I_NODE stopped halfway (above); run the same command again to finish it"
        exit 1
    fi
    identity || gpt_fail_keep=1
    if [ -n "${gpt_fail_keep:-}" ] || [ "$IDENT" != "$CONFIRMED" ]; then echo "FAIL: $ARG is no longer the disk that was updated"; exit 3; fi
    if ! OUT=$(sgdisk -v "$I_NODE" 2>&1) || ! grep -q 'No problems found' <<< "$OUT"; then
        printf '%s\n' "$OUT"; echo "FAIL: sgdisk -v reports problems on $I_NODE (the update does not change the GPT)"; exit 3
    fi
    echo "PASS: $I_NODE updated (read back and compared); /data kept; GPT unchanged and verified"
    exit 0
fi
echo "== writing $SIZE bytes to $I_NODE"
if wusb_py write "$IMG" "$SHA" "$I_NODE" "$I_RDEV" "$I_BYTES" "$I_SYSPATH" "$GROW"; then :; else
    rc=$?
    if [ "$rc" = 2 ]; then echo "nothing was written to $I_NODE"; exit 2; fi
    if [ "$rc" = 3 ]; then
        echo "FAIL: the image content on $I_NODE is written and verified, but moving the backup GPT to the end of the" \
            "disk failed (above). Fix: sgdisk -e $I_NODE && sgdisk -v $I_NODE"
        exit 3
    fi
    echo "FAIL: $I_NODE is partially written or did not read back as the image; it will not boot"
    exit 1
fi
echo "== $I_NODE reads back identical to the image (sha256 $SHA), backup GPT at the end of the disk"

# an independent, read-only look at the GPT the writer made (sgdisk opens the disk read-only for -v)
gpt_fail() {
    echo "FAIL: the image content on $I_NODE is written and verified, but $*;" \
        "the GPT was NOT confirmed by sgdisk. Check: sgdisk -v $I_NODE"
    exit 3
}
identity || gpt_fail "the disk's identity cannot be read again ($WHY)"
[ "$IDENT" = "$CONFIRMED" ] || gpt_fail "$ARG is no longer the disk that was written"
echo "== verifying the GPT (sgdisk -v, read-only)"
if ! OUT=$(sgdisk -v "$I_NODE" 2>&1); then printf '%s\n' "$OUT"; gpt_fail "sgdisk -v failed"; fi
printf '%s\n' "$OUT"
grep -q 'No problems found' <<< "$OUT" || gpt_fail "sgdisk -v reports problems (above)"
echo "PASS: $I_NODE holds the image (written, read back and compared) and its GPT verifies, backup at the end of the disk"
