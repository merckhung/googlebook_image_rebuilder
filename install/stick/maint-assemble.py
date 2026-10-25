#!/usr/bin/env python3
"""Assemble a maintenance root and XBOOTLDR tree from pinned tar archives.

No network. pack writes kano-maint.img (ext4, 7936 MiB) and xbootldr.img
(FAT32, 512 MiB) only when this process is root or FAKEROOTKEY is set and
mke2fs -d, mkfs.vfat, and mcopy exist. Otherwise it creates nothing.
When the process is root, including fakeroot, pack records uid 0 on the
assembled root without following a symlink, and mke2fs -d stores that owner.
An Arch package's absolute symlink is stored as a relative link inside the
root, and its metadata members are skipped. A missing boot/vmlinuz-linux is
copied from the single modules vmlinuz. A missing boot/initramfs-linux.img is
built with that root's mkinitcpio, inside a user and mount namespace, so the
host's own hooks and libraries are not what gets packed. That generated image
does not list nvme or nvme_core, and etc/modprobe.d/gbos-nvme.conf blocks both
from autoloading. On this generated initramfs path (pinned modular Arch kernel;
a prebuilt initramfs or a kernel with builtin NVMe is not unpacked or rejected
by this contract and is not covered by the pre-load guarantee), the image and
stick checks run before the NVMe driver is loaded.
Install mode also hashes the stick super before that load.
Update mode hashes the stick super after the serial matches, before the target is opened for write.
gbos-install.sh then runs modprobe --ignore-install, before the serial prompt,
so the serial can be read. The disk is visible from here. Do not write, mount,
or discard it until the last 4 characters match. Cancel or a wrong serial
leaves the disk untouched. The root also gets a fixed /etc/machine-id
so systemd-firstboot does not prompt, and /etc/localtime when zoneinfo UTC
is in the root. The installer cmdline is not how that is done. Package install
scripts are not run.
setup-stick.sh still has to boot the tree before it can see lockdown
(gbos-install.sh checks that).

The tool list is the command -v lists in gbos-install.sh (the install list and
the update list) and setup-stick.sh, plus blkid and systemctl, which those
scripts run without a prior command -v check.
"""
import importlib.util
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(HERE, "builtin-initcalls.txt")
TEMPLATE_PATH = os.path.join(HERE, "gbos-install.entry.template")

# gbos-install.sh install list, then its update list (setup-stick.sh requires
# the same update tools), then the two tools those scripts invoke directly.
REQUIRED_TOOLS = (
    "python3", "zstd", "sgdisk", "partx", "blockdev", "blkdiscard",
    "udevadm", "lsblk", "findmnt", "mcopy", "mtype", "dd", "sha256sum",
    "cmp", "mount", "umount",
    "lz4", "mdir", "mmd", "mdel", "minfo",
    "systemctl", "blkid",
)

MAX_MEMBER = 2 * 1024 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024 * 1024
MAX_HOPS = 8
NAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,200}$")
# Pacman metadata. Not files of the installed system, and .INSTALL is not run.
ARCH_META = {".PKGINFO", ".BUILDINFO", ".MTREE", ".INSTALL"}
ARCH_SUFFIXES = (
    ".pkg.tar.zst", ".pkg.tar.gz", ".pkg.tar.xz", ".pkg.tar.bz2", ".pkg.tar",
)
XBOOTLDR_BYTES = 512 * 1024 * 1024
KANO_MAINT_BYTES = 62 * 128 * 1024 * 1024  # must match maint-layout.KANO_MAINT_SECTORS * 512
EXT_BLOCK = 4096
XBOOT_FILES = ("vmlinuz-linux", "initramfs-linux.img")


def refuse(msg):
    print(f"make-maint: {msg}", file=sys.stderr)
    raise SystemExit(2)


def load_pins():
    path = os.path.join(HERE, "maint-pins.py")
    spec = importlib.util.spec_from_file_location("maint_pins", path)
    if spec is None or spec.loader is None:
        refuse("cannot load the pin checker")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PINS = load_pins()


def take(argv, flag):
    value = None
    out = []
    i = 0
    while i < len(argv):
        if argv[i] == flag:
            if value is not None:
                refuse(f"{flag} given twice")
            if i + 1 >= len(argv) or not argv[i + 1] or argv[i + 1].startswith("--"):
                refuse(f"{flag} needs a path")
            value = argv[i + 1]
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return value, out


def inside(root, path):
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    return path == root or path.startswith(root + os.sep)


def lstat_or_none(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def member_parts(name):
    text = name.replace("\\", "/")
    if text.startswith("/"):
        refuse(f"archive member {name} has an absolute path")
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            refuse(f"archive member {name} escapes the root")
        if len(part) > 255:
            refuse(f"archive member {name} has a component that is too long")
        parts.append(part)
    if not parts:
        refuse(f"archive member {name} has an empty path")
    return parts


def arch_package(path):
    name = os.path.basename(path)
    return any(name.endswith(suffix) for suffix in ARCH_SUFFIXES)


def arch_metadata(name):
    """True for a pacman metadata member at the archive root."""
    text = name.replace("\\", "/")
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return False
        parts.append(part)
    return len(parts) == 1 and parts[0] in ARCH_META


def rewrite_abs_symlink(parent_parts, target):
    """Arch records symlink text as a path inside the installed root.

    A clean absolute target (one leading slash, no '..') is stored relative
    to the link so the stored text cannot name the host. Anything else is
    left unchanged, and the absolute-target refusal still applies.
    """
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parts = []
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    common = 0
    limit = min(len(parent_parts), len(parts))
    while common < limit and parent_parts[common] == parts[common]:
        common += 1
    relative = [".."] * (len(parent_parts) - common) + parts[common:]
    if not relative:
        return "."
    return "/".join(relative)


def symlink_text_stays(root_parts_parent, target, where):
    """Lexical check. Absolute targets are host paths and are refused."""
    if target.startswith("/") or target.startswith("\\") or os.path.isabs(target):
        refuse(f"symlink {where} points at {target}, which leaves the root")
    base = list(root_parts_parent)
    for part in target.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not base:
                refuse(f"symlink {where} points at {target}, which leaves the root")
            base.pop()
            continue
        base.append(part)
    return target


def resolve_inside(root, rel):
    """Follow symlinks that stay inside root. Missing path returns None."""
    root = os.path.abspath(root)
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            refuse(f"{rel} escapes the root")
        parts.append(part)
    logical = []
    hops = 0
    idx = 0
    while idx < len(parts):
        name = parts[idx]
        if name in ("", "."):
            idx += 1
            continue
        if name == "..":
            if not logical:
                refuse(f"{rel} escapes the root")
            logical.pop()
            idx += 1
            continue
        cur = root if not logical else os.path.join(root, *logical)
        nxt = os.path.join(cur, name)
        st = lstat_or_none(nxt)
        if st is None:
            return None
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > MAX_HOPS:
                refuse(f"symlink at {rel} exceeded {MAX_HOPS} hops")
            try:
                target = os.readlink(nxt)
            except OSError as e:
                refuse(f"cannot read symlink {rel}: {e.strerror}")
            if target.startswith("/") or target.startswith("\\") or os.path.isabs(target):
                refuse(f"symlink {nxt} points at {target}, which leaves the root")
            tparts = []
            for part in target.split("/"):
                if part not in ("", "."):
                    tparts.append(part)
            parts = parts[:idx] + tparts + parts[idx + 1:]
            continue
        logical.append(name)
        idx += 1
    final = root if not logical else os.path.join(root, *logical)
    if not inside(root, final):
        refuse(f"{rel} escapes the root")
    if lstat_or_none(final) is None:
        return None
    return final


def prepare_parent(root, parts):
    cur = root
    for name in parts[:-1]:
        cur = os.path.join(cur, name)
        st = lstat_or_none(cur)
        if st is None:
            os.mkdir(cur, 0o755)
            continue
        if stat.S_ISLNK(st.st_mode):
            refuse(f"refusing to follow a symlink while extracting {name}")
        if not stat.S_ISDIR(st.st_mode):
            refuse(f"archive parent {name} is not a directory")
    return os.path.join(cur, parts[-1])


def write_member(tar, member, dest, size):
    src = tar.extractfile(member)
    if src is None:
        refuse(f"archive member {member.name} has no contents")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    mode = member.mode & 0o7777
    if mode & 0o777 == 0:
        mode = 0o644
    try:
        fd = os.open(dest, flags, mode & 0o777)
    except FileExistsError:
        refuse(f"archive member {member.name} collides with an existing path")
    except OSError as e:
        refuse(f"cannot write archive member {member.name}: {e.strerror}")
    remaining = size
    try:
        while remaining:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                refuse(f"archive member {member.name} ended early")
            os.write(fd, chunk)
            remaining -= len(chunk)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
        src.close()


def extract_one(tar, member, root, total, arch=False):
    name = member.name
    if arch and arch_metadata(name):
        return total
    if member.isdir():
        parts = member_parts(name)
        dest = prepare_parent(root, parts)
        st = lstat_or_none(dest)
        mode = member.mode & 0o7777
        if mode == 0:
            mode = 0o755
        if st is None:
            os.mkdir(dest, 0o755)
            os.chmod(dest, mode)
        elif stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            refuse(f"archive member {name} collides with an existing path")
        return total
    if member.issym():
        parts = member_parts(name)
        linkname = member.linkname or ""
        if arch:
            rewritten = rewrite_abs_symlink(parts[:-1], linkname)
            if rewritten is not None:
                linkname = rewritten
        symlink_text_stays(parts[:-1], linkname, name)
        dest = prepare_parent(root, parts)
        if lstat_or_none(dest) is not None:
            refuse(f"archive member {name} collides with an existing path")
        os.symlink(linkname, dest)
        return total
    if member.islnk():
        parts = member_parts(name)
        target_parts = member_parts(member.linkname or "")
        target = os.path.join(root, *target_parts)
        st = lstat_or_none(target)
        if st is None or not stat.S_ISREG(st.st_mode):
            refuse(f"hard link {name} does not point at a file already in the archive")
        dest = prepare_parent(root, parts)
        if lstat_or_none(dest) is not None:
            refuse(f"archive member {name} collides with an existing path")
        try:
            os.link(target, dest)
        except OSError as e:
            refuse(f"cannot link archive member {name}: {e.strerror}")
        return total
    if member.isreg():
        size = member.size
        if size < 0 or size > MAX_MEMBER:
            refuse(f"archive member {name} is larger than {MAX_MEMBER} bytes")
        if total + size > MAX_TOTAL:
            refuse(f"archive expands past {MAX_TOTAL} bytes")
        parts = member_parts(name)
        dest = prepare_parent(root, parts)
        if lstat_or_none(dest) is not None:
            refuse(f"archive member {name} collides with an existing path")
        write_member(tar, member, dest, size)
        return total + size
    refuse(f"archive member {name} has unsupported type {member.type!r}")


def open_archive(path, work):
    name = os.path.basename(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as e:
        refuse(f"cannot read {name}: {e.strerror}")
    try:
        if name.endswith(".tar.zst") or name.endswith(".tzst"):
            os.close(fd)
            fd = None
            raw = os.path.join(work, f".{name}.tar")
            decompress_zst(path, raw)
            return tarfile.open(raw, "r:")
        if name.endswith(".tar.gz") or name.endswith(".tgz"):
            return tarfile.open(fileobj=os.fdopen(fd, "rb"), mode="r|gz")
        if name.endswith(".tar"):
            return tarfile.open(fileobj=os.fdopen(fd, "rb"), mode="r|")
    except tarfile.TarError:
        refuse(f"{name} is not a tar archive")
    except SystemExit:
        raise
    except OSError as e:
        refuse(f"cannot read {name}: {e.strerror}")
    if fd is not None:
        os.close(fd)
    refuse(f"{name} is not a tar archive")


def decompress_zst(src, dest):
    zstd = shutil.which("zstd")
    name = os.path.basename(src)
    if not zstd:
        refuse(f"zstd is required to read {name}")
    proc = subprocess.Popen(
        [zstd, "-d", "-c", "--", src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    written = 0
    assert proc.stdout is not None and proc.stderr is not None
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_TOTAL:
                    proc.kill()
                    refuse(f"{name} expands past {MAX_TOTAL} bytes")
                out.write(chunk)
        err = proc.stderr.read()
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if rc != 0:
        refuse(f"zstd could not read {name}")
    if err and written == 0:
        refuse(f"zstd could not read {name}")


def extract_archive(path, root, work, total):
    name = os.path.basename(path)
    arch = arch_package(path)
    tar = open_archive(path, work)
    try:
        for member in tar:
            total = extract_one(tar, member, root, total, arch)
    except tarfile.TarError:
        refuse(f"{name} is not a tar archive")
    finally:
        tar.close()
        if name.endswith(".tar.zst") or name.endswith(".tzst"):
            raw = os.path.join(work, f".{name}.tar")
            try:
                os.unlink(raw)
            except OSError:
                pass
    return total


def module_blacklist(path):
    names = set()
    try:
        text = open(path, encoding="utf-8").read().splitlines()
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    for line in text:
        if not line.startswith("options "):
            continue
        for tok in line.split():
            if tok.startswith("module_blacklist="):
                for mod in tok.split("=", 1)[1].split(","):
                    if mod:
                        names.add(mod.replace("-", "_"))
    if not names:
        refuse(f"no module_blacklist in {path}")
    return names


def load_map(path):
    covered = set()
    initcalls = []
    libraries = []
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) == 2 and re.fullmatch(r"[a-z0-9_]+", fields[1]):
            initcalls.append(fields[1])
            covered.add(fields[0].replace("-", "_"))
            continue
        if (len(fields) == 3 and fields[1] == "-"
                and re.fullmatch(r"[a-z0-9_]+(,[a-z0-9_]+)*", fields[2])):
            covered.add(fields[0].replace("-", "_"))
            libraries.append((fields[0], fields[2]))
            continue
        refuse(f"{path}:{lineno}: bad line")
    if not initcalls:
        refuse(f"no initcalls in {path}")
    have = set(initcalls)
    for mod, need in libraries:
        for one in need.split(","):
            if one not in have:
                refuse(f"{path}: {mod} is accepted only with {one} blocked, which it does not list")
    return covered


def builtin_names(path):
    names = set()
    try:
        lines = open(path, encoding="utf-8", errors="surrogateescape").read().splitlines()
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        base = line.rsplit("/", 1)[-1]
        if base.endswith(".ko"):
            base = base[:-3]
        names.add(base.replace("-", "_"))
    return names


def kernel_version(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as e:
        refuse(f"cannot read kernel {path}: {e.strerror}")
    try:
        data = os.read(fd, 1 << 16)
    finally:
        os.close(fd)
    if len(data) < 0x210 or data[0x202:0x206] != b"HdrS":
        refuse("boot/vmlinuz-linux is not a bzImage (no HdrS header)")
    off = struct.unpack_from("<H", data, 0x20E)[0] + 0x200
    if off < 0 or off >= len(data):
        refuse("boot/vmlinuz-linux version string is outside the header setup-stick.sh reads")
    end = data.find(b"\0", off)
    if end < 0:
        refuse("boot/vmlinuz-linux version string is not terminated")
    token = data[off:end].split(b" ")[0]
    try:
        kver = token.decode("ascii")
    except UnicodeDecodeError:
        refuse("boot/vmlinuz-linux version string is not ASCII")
    if not kver or kver in (".", "..") or "/" in kver or "\\" in kver:
        refuse("boot/vmlinuz-linux has no kernel version")
    return kver


def executable_file(path):
    st = lstat_or_none(path)
    if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    return bool(st.st_mode & 0o111)


def find_tool(root, name):
    if not NAME_RE.fullmatch(name):
        refuse(f"tool name {name} is not a single path component")
    for rel in (f"usr/bin/{name}", f"bin/{name}"):
        found = resolve_inside(root, rel)
        if found is None:
            continue
        if executable_file(found):
            return found
    refuse(f"maintenance root has no executable {name}")


def same_bytes(a, b):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fa = os.open(a, flags)
    try:
        fb = os.open(b, flags)
    except OSError:
        os.close(fa)
        raise
    try:
        while True:
            left = os.read(fa, 1024 * 1024)
            right = os.read(fb, 1024 * 1024)
            if left != right:
                return False
            if not left:
                return True
    finally:
        os.close(fa)
        os.close(fb)


def copy_regular(src, dest):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    inp = os.open(src, flags)
    out_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        out_flags |= os.O_NOFOLLOW
    try:
        out = os.open(dest, out_flags, 0o644)
    except OSError:
        os.close(inp)
        raise
    try:
        while True:
            chunk = os.read(inp, 1024 * 1024)
            if not chunk:
                break
            os.write(out, chunk)
        os.fchmod(out, 0o644)
    finally:
        os.close(inp)
        os.close(out)


def check_contract(root, xbootldr):
    kernel = resolve_inside(root, "boot/vmlinuz-linux")
    if kernel is None or not executable_file(kernel) and not regular_file(kernel):
        refuse("maintenance root has no boot/vmlinuz-linux")
    if not regular_file(kernel):
        refuse("boot/vmlinuz-linux is not a regular file")
    kver = kernel_version(kernel)
    initrd = resolve_inside(root, "boot/initramfs-linux.img")
    if initrd is None or not regular_file(initrd):
        refuse("maintenance root has no boot/initramfs-linux.img")
    if os.path.getsize(initrd) <= 0:
        refuse("boot/initramfs-linux.img is empty")
    moddir = f"usr/lib/modules/{kver}"
    builtin = resolve_inside(root, f"{moddir}/modules.builtin")
    if builtin is None or not regular_file(builtin):
        refuse(f"no usr/lib/modules/{kver}/modules.builtin for the kernel")
    copy = resolve_inside(root, f"{moddir}/vmlinuz")
    if copy is None or not regular_file(copy):
        refuse(f"no usr/lib/modules/{kver}/vmlinuz for the kernel")
    if not same_bytes(kernel, copy):
        refuse(f"usr/lib/modules/{kver}/vmlinuz does not match boot/vmlinuz-linux")
    blacklist = module_blacklist(TEMPLATE_PATH)
    covered = load_map(MAP_PATH)
    missing = sorted(covered - blacklist)
    if missing:
        refuse("builtin-initcalls.txt lists modules missing from the entry's "
               f"module_blacklist: {' '.join(missing)}")
    uncovered = sorted((builtin_names(builtin) & blacklist) - covered)
    if uncovered:
        refuse(f"kernel {kver} has these built in and builtin-initcalls.txt "
               f"does not block them: {' '.join(uncovered)}")
    for name in REQUIRED_TOOLS:
        find_tool(root, name)
    os.mkdir(xbootldr, 0o755) if not os.path.isdir(xbootldr) else None
    copy_regular(kernel, os.path.join(xbootldr, "vmlinuz-linux"))
    copy_regular(initrd, os.path.join(xbootldr, "initramfs-linux.img"))
    return kver


def regular_file(path):
    st = lstat_or_none(path)
    return st is not None and not stat.S_ISLNK(st.st_mode) and stat.S_ISREG(st.st_mode)


def real_dir(path, what):
    st = lstat_or_none(path)
    if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        refuse(what)
    return path


def place_kernel(root):
    """Copy the one modules vmlinuz to boot/vmlinuz-linux when that file is absent.

    The linux package ships the kernel under usr/lib/modules. Its install
    script would copy that file to /boot. This copies the single regular file
    and does not run the install script. It does not build an initramfs.
    """
    dest = os.path.join(root, "boot", "vmlinuz-linux")
    if lstat_or_none(dest) is not None:
        return
    modroot = os.path.join(root, "usr", "lib", "modules")
    st = lstat_or_none(modroot)
    if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return
    found = []
    for name in sorted(os.listdir(modroot)):
        if name in (".", ".."):
            continue
        sub = os.path.join(modroot, name)
        sst = lstat_or_none(sub)
        if sst is None or stat.S_ISLNK(sst.st_mode) or not stat.S_ISDIR(sst.st_mode):
            continue
        cand = os.path.join(sub, "vmlinuz")
        cst = lstat_or_none(cand)
        if cst is not None and stat.S_ISREG(cst.st_mode) and not stat.S_ISLNK(cst.st_mode):
            found.append(cand)
    if len(found) != 1:
        return
    boot = os.path.join(root, "boot")
    bst = lstat_or_none(boot)
    if bst is None:
        os.mkdir(boot, 0o755)
    elif stat.S_ISLNK(bst.st_mode) or not stat.S_ISDIR(bst.st_mode):
        refuse("refusing to follow a symlink while extracting boot")
    copy_regular(found[0], dest)


# Sourced by mkinitcpio. autodetect is not in the hook list, so the image is
# not tied to the build machine. The filesystems hook is not in the list
# either: it adds every filesystem name, and this kernel builds many of them
# in, which mkinitcpio reports as a missing module and a failed image.
# ext4, xhci, and virtio_blk are built in. usb_storage, uas, and vfat are
# modules. nvme and nvme_core are modules too and are intentionally absent:
# v0.2.0 (gbos-install.sh, the comment directly above
# `modprobe --ignore-install nvme_core && modprobe --ignore-install nvme`)
# keeps the maintenance OS from autoloading them on the generated initramfs
# path (pinned modular Arch kernel; a prebuilt boot/initramfs-linux.img is left
# as-is without unpacking, and a kernel with builtin NVMe is not rejected here,
# so prebuilt/builtin inputs are not covered by the pre-load guarantee). On the
# generated path, the installer loads them, with --ignore-install, after
# lockdown, the cmdline, and the device checks, and before the serial prompt.
# Install mode also hashes the stick super before that load.
# Update mode hashes the stick super after the serial matches, before the target is opened for write.
# before the NVMe driver is loaded, those
# checks do not touch the internal disk. The disk is visible from here, once loaded.
# Do not write, mount, or discard until the last 4 characters match. Cancel or
# a wrong serial leaves the disk untouched. module_blacklist cannot express
# the modular autoload block, because it would also block the installer's
# modprobe --ignore-install (refusing a builtin NVMe in assemble would be a
# separate check and is not implemented). The sourced text must not name either
# module. The modconf hook copies /etc/modprobe.d into the initramfs. The rule
# is written once, before build_initramfs, including when that file is already
# present: the builder then leaves the prebuilt image alone without verifying
# its contents, and udev on the booted root still reads this file.
NVME_MODPROBE_NAME = "gbos-nvme.conf"
NVME_MODPROBE = b"install nvme /bin/false\ninstall nvme_core /bin/false\n"
# 32 lowercase hex digits plus a newline. The first four bytes are ASCII "gbos".
# ConditionFirstBoot=yes is true only when /etc/machine-id is missing, empty,
# or the literal "uninitialized". This id keeps systemd-firstboot.service from
# asking for a locale, a keymap, a timezone, or a root password. The reviewed
# installer cmdline does not carry systemd.mask=systemd-firstboot.service.
MACHINE_ID = b"67626f73000000000000000000000001\n"
INITRAMFS_CONFIG = """MODULES=(usb_storage uas vfat)
BINARIES=()
FILES=()
HOOKS=(base systemd modconf block fsck)
COMPRESSION="zstd"
"""

# Runs on the host, inside unshare -U -r -m, then chroots. The mounts stay in
# that namespace. The tool path is inside the root, not a host mkinitcpio.
INITRAMFS_HELPER = """#!/bin/sh
set -eu
if [ "$#" -ne 3 ]; then
    echo "initramfs helper: expected root, kernel version, and tool" >&2
    exit 1
fi
root=$1
kver=$2
tool=$3
case "$root" in
    /*) ;;
    *) echo "initramfs helper: root is not absolute" >&2; exit 1 ;;
esac
case "$tool" in
    /*) ;;
    *) echo "initramfs helper: tool is not absolute" >&2; exit 1 ;;
esac
case "$kver" in
    ""|*[!A-Za-z0-9._+-]*)
        echo "initramfs helper: kernel version is not a single token" >&2
        exit 1
        ;;
esac
/usr/bin/mount --make-rprivate / 2>/dev/null || true
if [ -L "$root/proc" ] || [ -L "$root/sys" ] || [ -L "$root/dev" ] || [ -L "$root/run" ]; then
    echo "refusing to follow a symlink while mounting the initramfs root" >&2
    exit 1
fi
# proc in a user namespace needs the new pid namespace from unshare -p --fork.
/usr/bin/mount -t proc proc "$root/proc"
# A user namespace cannot mount the host sysfs. Device nodes from the host
# also cannot be opened here (the kernel returns EACCES), and mknod is refused.
# Regular stand-ins are enough: this config does not use the autodetect hook,
# so the build must not read the build machine's hardware.
if ! /usr/bin/mount -t sysfs sysfs "$root/sys" 2>/dev/null; then
    /usr/bin/mount -t tmpfs tmpfs "$root/sys"
fi
/usr/bin/mount -t tmpfs tmpfs "$root/dev"
side=${root%/*}
/usr/bin/cp "$side/work/dev-zero" "$root/dev/zero"
/usr/bin/cp "$side/work/dev-urandom" "$root/dev/random"
/usr/bin/cp "$side/work/dev-urandom" "$root/dev/urandom"
chmod 666 "$root/dev/zero" "$root/dev/random" "$root/dev/urandom"
for node in null full tty console ptmx; do
    : > "$root/dev/$node"
    chmod 666 "$root/dev/$node"
done
ln -s /proc/self/fd "$root/dev/fd"
ln -s /proc/self/fd/0 "$root/dev/stdin"
ln -s /proc/self/fd/1 "$root/dev/stdout"
ln -s /proc/self/fd/2 "$root/dev/stderr"
/usr/bin/mount -t tmpfs tmpfs "$root/run" 2>/dev/null || true
if [ -d "$root/tmp" ] && [ ! -L "$root/tmp" ]; then
    chmod 1777 "$root/tmp"
fi
if [ -d "$root/var" ] && [ ! -L "$root/var" ] && [ ! -L "$root/var/tmp" ]; then
    if [ ! -d "$root/var/tmp" ]; then
        mkdir "$root/var/tmp"
    fi
    chmod 1777 "$root/var/tmp"
fi
# The linux package ships modules.builtin and the module files, and does not
# ship modules.dep. Its install script would run depmod. Run that one command
# and nothing else from the install script.
if [ ! -x "$root/usr/bin/depmod" ]; then
    echo "depmod is not in the maintenance root" >&2
    exit 1
fi
chroot_cmd=$(command -v chroot) || {
    echo "chroot is required on the build host" >&2
    exit 1
}
"$chroot_cmd" "$root" /usr/bin/depmod -a "$kver"
exec "$chroot_cmd" "$root" "$tool" -k "$kver" -g /boot/initramfs-linux.img -c /etc/mkinitcpio-gbos.conf
"""


def ensure_real_dir(path, what):
    st = lstat_or_none(path)
    if st is None:
        os.mkdir(path, 0o755)
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        refuse(what)


def write_new_file(path, data, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode)
    except OSError as e:
        refuse(f"cannot write {os.path.basename(path)}: {e.strerror}")
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def mkinitcpio_tool(root):
    """In-chroot path of mkinitcpio, or None when the root has no such file.

    A symlink or a non-executable file is a refusal. Absence is not: the
    contract then reports the missing initramfs.
    """
    for rel in ("usr/bin/mkinitcpio", "bin/mkinitcpio"):
        path = os.path.join(root, *rel.split("/"))
        st = lstat_or_none(path)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or not (st.st_mode & 0o111):
            refuse("mkinitcpio is not an executable file")
        return "/" + rel
    return None


def write_nvme_modprobe(root):
    """Write the v0.2.0 rule that stops nvme and nvme_core from autoloading.

    A symlink at etc, modprobe.d, or the file itself is a refusal. An existing
    file is a refusal too: a package must not be able to replace this rule.
    """
    etc = os.path.join(root, "etc")
    modprobe_d = os.path.join(etc, "modprobe.d")
    dest = os.path.join(modprobe_d, NVME_MODPROBE_NAME)
    ensure_real_dir(etc, "refusing to follow a symlink while writing the NVMe modprobe rule")
    ensure_real_dir(modprobe_d, "refusing to follow a symlink while writing the NVMe modprobe rule")
    write_new_file(dest, NVME_MODPROBE, 0o644)


def write_firstboot(root):
    """Stop the first-boot wizard without a kernel command line change.

    A valid machine-id makes ConditionFirstBoot false. When this root contains
    usr/share/zoneinfo/UTC as a regular file, /etc/localtime is a relative
    symlink to it. Absence of that file is not a failure: the wizard still
    does not run. A symlink at etc, machine-id, or localtime is a refusal, as
    is an existing machine-id or localtime.
    """
    etc = os.path.join(root, "etc")
    ensure_real_dir(etc, "refusing to follow a symlink while writing first-boot files")
    write_new_file(os.path.join(etc, "machine-id"), MACHINE_ID, 0o444)
    zone = os.path.join(root, "usr", "share", "zoneinfo", "UTC")
    zst = lstat_or_none(zone)
    if zst is None:
        return
    if stat.S_ISLNK(zst.st_mode) or not stat.S_ISREG(zst.st_mode):
        refuse("zoneinfo UTC is not a regular file")
    link = os.path.join(etc, "localtime")
    if lstat_or_none(link) is not None:
        refuse("refusing to replace /etc/localtime")
    os.symlink("../usr/share/zoneinfo/UTC", link)


def build_initramfs(root):
    """Build boot/initramfs-linux.img when it is missing and mkinitcpio is there.

    An initramfs that is already present, including an empty file or a symlink,
    is left alone so the contract can refuse it. The generator is the copy
    inside the root. It is started with unshare -U -r -m -p --fork and chroot,
    and -k is
    the version read from boot/vmlinuz-linux. This does not run a package
    install script and does not run the host's mkinitcpio.
    """
    boot = os.path.join(root, "boot")
    dest = os.path.join(boot, "initramfs-linux.img")
    if lstat_or_none(dest) is not None:
        return
    tool = mkinitcpio_tool(root)
    if tool is None:
        return
    kernel = os.path.join(boot, "vmlinuz-linux")
    if not regular_file(kernel):
        return
    kver = kernel_version(kernel)
    ensure_real_dir(boot, "refusing to follow a symlink while writing the initramfs")
    ensure_real_dir(os.path.join(root, "etc"),
                    "refusing to follow a symlink while writing the initramfs config")
    for name in ("proc", "sys", "dev", "run", "tmp"):
        ensure_real_dir(os.path.join(root, name),
                        "refusing to follow a symlink while mounting the initramfs root")
    write_new_file(os.path.join(root, "etc", "mkinitcpio-gbos.conf"),
                   INITRAMFS_CONFIG.encode("ascii"), 0o644)
    # Prepared outside the namespace. Inside it, host device nodes cannot be
    # opened, and a regular file is what /dev/null and /dev/zero have to be.
    work = os.path.join(os.path.dirname(root), "work")
    ensure_real_dir(work, "initramfs work directory is not a directory")
    write_new_file(os.path.join(work, "dev-zero"), b"\0" * (1024 * 1024), 0o644)
    try:
        random_fd = os.open("/dev/urandom", os.O_RDONLY)
    except OSError as e:
        refuse(f"cannot read random bytes: {e.strerror}")
    try:
        random_bytes = os.read(random_fd, 65536)
    finally:
        os.close(random_fd)
    if len(random_bytes) != 65536:
        refuse("cannot read random bytes")
    write_new_file(os.path.join(work, "dev-urandom"), random_bytes, 0o644)
    unshare = shutil.which("unshare")
    if not unshare:
        refuse("unshare is required to build the initramfs")
    helper = os.path.join(os.path.dirname(root), "initramfs-mount.sh")
    write_new_file(helper, INITRAMFS_HELPER.encode("ascii"), 0o755)
    try:
        result = subprocess.run(
            [unshare, "-U", "-r", "-m", "-p", "--fork", helper, root, kver, tool],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            errors="replace",
        )
    except OSError as e:
        refuse(f"cannot run unshare: {e.strerror}")
    if result.returncode != 0:
        lines = (result.stdout or "").strip().splitlines()
        if len(lines) > 1:
            sys.stderr.write("\n".join(lines[-30:]) + "\n")
        tail = lines[-1] if lines else "no output"
        refuse(f"mkinitcpio could not write the initramfs: {tail}")
    bst = lstat_or_none(boot)
    st = lstat_or_none(dest)
    if (bst is None or stat.S_ISLNK(bst.st_mode) or not stat.S_ISDIR(bst.st_mode)
            or st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode)
            or st.st_size <= 0):
        refuse("mkinitcpio did not write boot/initramfs-linux.img")
    print(f"initramfs {st.st_size}", flush=True)


def assemble(cache, out, pins_path):
    out = os.path.abspath(out)
    if os.path.lexists(out):
        refuse(f"output already exists: {out}")
    parent = os.path.dirname(out)
    real_dir(parent, f"output parent is not a directory: {parent}")
    rows = PINS.parse(pins_path)
    if not rows:
        refuse("pin list has no pinned files")
    PINS.verify(rows, cache)
    tmp = tempfile.mkdtemp(prefix=".make-maint-", dir=parent)
    kver = None
    try:
        root = os.path.join(tmp, "root")
        xbootldr = os.path.join(tmp, "xbootldr")
        os.mkdir(root, 0o755)
        os.mkdir(xbootldr, 0o755)
        total = 0
        work = os.path.join(tmp, "work")
        os.mkdir(work, 0o755)
        for row in rows:
            total = extract_archive(os.path.join(cache, row["name"]), root, work, total)
        place_kernel(root)
        write_nvme_modprobe(root)
        write_firstboot(root)
        build_initramfs(root)
        kver = check_contract(root, xbootldr)
        os.rename(tmp, out)
        tmp = None
    finally:
        if tmp is not None and os.path.isdir(tmp):
            shutil.rmtree(tmp)
    print("assemble ok")
    print(f"kver {kver}")
    print(f"tools {len(REQUIRED_TOOLS)}")
    print("lockdown not checked here; gbos-install checks it at boot")


def usable_tool(name):
    """First executable on PATH that is not the Android platform-tools mke2fs.

    That binary is not e2fsprogs. pack must not run it, even as a help check.
    """
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        candidate = os.path.join(raw, name)
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            try:
                if not os.path.isfile(candidate):
                    continue
            except OSError:
                continue
        elif not stat.S_ISREG(st.st_mode):
            continue
        if not os.access(candidate, os.X_OK):
            continue
        try:
            real = os.path.realpath(candidate)
        except OSError:
            continue
        low = real.lower()
        if "platform-tools" in low or f"{os.sep}android{os.sep}" in low:
            continue
        return candidate
    return None


def image_tools():
    """Paths for a root-owned image, or None when one is missing.

    mke2fs must advertise -d. Its help text is enough; a non-zero exit from
    `-h` is normal for e2fsprogs, which treats -h as an unknown option and
    prints the usage that contains `-d root-directory`.
    """
    mke2fs = usable_tool("mke2fs")
    mkfs = usable_tool("mkfs.vfat")
    mcopy = usable_tool("mcopy")
    if not mke2fs or not mkfs or not mcopy:
        return None
    try:
        out = subprocess.run(
            [mke2fs, "-h"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
        )
    except OSError:
        return None
    text = out.stdout or ""
    if "-d" not in text and "root-directory" not in text:
        return None
    return {"mke2fs": mke2fs, "mkfs.vfat": mkfs, "mcopy": mcopy}


def xboot_file(directory, name):
    path = os.path.join(directory, name)
    st = lstat_or_none(path)
    if st is None:
        refuse(f"xbootldr is missing {name}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        refuse(f"xbootldr {name} is not a regular file")
    if st.st_size <= 0:
        refuse(f"xbootldr {name} is empty")
    return path


def create_partial(path, size):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        refuse(f"{os.path.basename(path)} already exists")
    except OSError as e:
        refuse(f"cannot create {os.path.basename(path)}: {e.strerror}")
    try:
        os.ftruncate(fd, size)
    except OSError as e:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        refuse(f"cannot size {os.path.basename(path)}: {e.strerror}")
    os.close(fd)


def run_tool(argv, cwd=None):
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
        )
    except OSError as e:
        refuse(f"cannot run {os.path.basename(argv[0])}: {e.strerror}")
    if result.returncode != 0:
        lines = (result.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else "no output"
        refuse(f"{os.path.basename(argv[0])} exited {result.returncode}: {tail}")


def unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def own_as_root(tree):
    """Give every inode under tree uid 0, without following a symlink.

    Real root changes the inode. Under fakeroot the change is only remembered,
    which is what mke2fs -d then stores. The symlink itself is the inode that
    changes; its target is left alone. A process that is not root does nothing
    here: the caller already refused, or it is the offline stand-in that only
    sets FAKEROOTKEY.
    """
    if os.geteuid() != 0:
        return

    def give(path):
        try:
            os.lchown(path, 0, 0)
        except OSError as e:
            rel = os.path.relpath(path, tree)
            refuse(f"cannot give uid 0 to {rel}: {e.strerror}")

    give(tree)
    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        for name in list(dirnames) + list(filenames):
            give(os.path.join(dirpath, name))


def grant_owner_read(tree):
    """Let mke2fs read a regular file whose mode has no owner-read bit.

    fakeroot does not bypass the kernel check, and the builder is the real
    uid. dbus-daemon-launch-helper is mode 4110. Adding owner-read does not
    widen access once that inode is uid 0. A symlink is not a file to copy.
    """
    if os.geteuid() != 0:
        return
    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError as e:
                refuse(f"cannot stat {name}: {e.strerror}")
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o400:
                continue
            try:
                os.chmod(path, stat.S_IMODE(st.st_mode) | 0o400)
            except OSError as e:
                refuse(f"cannot give owner-read to {name}: {e.strerror}")


def pack(out):
    out = os.path.abspath(out)
    real_dir(out, f"pack output is not a directory: {out}")
    root = os.path.join(out, "root")
    xboot = os.path.join(out, "xbootldr")
    real_dir(root, "pack needs an assembled root")
    real_dir(xboot, "pack needs an assembled xbootldr")
    final_ext = os.path.join(out, "kano-maint.img")
    final_fat = os.path.join(out, "xbootldr.img")
    part_ext = os.path.join(out, "kano-maint.img.partial")
    part_fat = os.path.join(out, "xbootldr.img.partial")
    for path in (final_ext, final_fat, part_ext, part_fat):
        if os.path.lexists(path):
            refuse(f"{os.path.basename(path)} already exists")
    euid = os.geteuid()
    if euid != 0 and not os.environ.get("FAKEROOTKEY"):
        refuse(f"refusing to pack: files would be owned by uid {euid}")
    for name in XBOOT_FILES:
        xboot_file(xboot, name)
    tools = image_tools()
    if tools is None:
        refuse("refusing to pack: mke2fs -d, mkfs.vfat, and mcopy are required "
               "before an image can be written")
    own_as_root(root)
    grant_owner_read(root)
    made = []
    published = []
    try:
        create_partial(part_ext, KANO_MAINT_BYTES)
        made.append(part_ext)
        create_partial(part_fat, XBOOTLDR_BYTES)
        made.append(part_fat)
        blocks = KANO_MAINT_BYTES // EXT_BLOCK
        run_tool([
            tools["mke2fs"], "-F", "-q", "-t", "ext4", "-b", str(EXT_BLOCK),
            "-L", "kano-maint", "-d", root, part_ext, str(blocks),
        ])
        run_tool([
            tools["mkfs.vfat"], "-F", "32", "-n", "XBOOTLDR", part_fat,
        ])
        for name in XBOOT_FILES:
            run_tool([
                tools["mcopy"], "-i", part_fat, "-o", name, "::" + name,
            ], cwd=xboot)
        if os.path.getsize(part_ext) != KANO_MAINT_BYTES:
            refuse("kano-maint.img is the wrong size")
        if os.path.getsize(part_fat) != XBOOTLDR_BYTES:
            refuse("xbootldr.img is the wrong size")
        os.replace(part_ext, final_ext)
        made.remove(part_ext)
        published.append(final_ext)
        os.replace(part_fat, final_fat)
        made.remove(part_fat)
    except BaseException:
        for path in made + published:
            unlink_quiet(path)
        raise
    print("pack ok")
    print(f"kano-maint.img {KANO_MAINT_BYTES}")
    print(f"xbootldr.img {XBOOTLDR_BYTES}")


def main(argv):
    if not argv or argv[0] not in ("assemble", "pack"):
        refuse("usage: maint-assemble.py assemble --cache DIR --out DIR [--file PINS] | pack --out DIR")
    cmd = argv[0]
    pins, rest = take(argv[1:], "--file")
    cache, rest = take(rest, "--cache")
    out, rest = take(rest, "--out")
    if rest:
        refuse(f"unknown argument {rest[0]}")
    if cmd == "pack":
        if pins is not None or cache is not None:
            refuse("pack takes only --out")
        if out is None:
            refuse("--out is required")
        pack(out)
        return
    if cache is None:
        refuse("--cache is required")
    if out is None:
        refuse("--out is required")
    if pins is None:
        pins = os.path.join(HERE, "maint.pins")
    assemble(cache, out, pins)


if __name__ == "__main__":
    main(sys.argv[1:])
