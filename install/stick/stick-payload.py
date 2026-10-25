#!/usr/bin/env python3
"""Rootless, offline half of setup-stick.sh.

Prepares a maintenance root directory and an XBOOTLDR directory for one
installer stick. It does not mount a stick, open a block device, or need
root. The pristine check is gbos-install.sh's own verify-data, loaded from
that script, not copied.

What this does not do yet: write the stick image, the ext4 filesystem, or
uid 0 on the files. Packing the directory into kano-maint (uid 0, sparse)
is a later step. A fresh stick has no previous internal-disk entries to
keep, so the internal-disk entries are written from this image. The ESP
default is the installer entry: there is no kernel at /gbos-nvme yet, so
the default is not gbos-nvme.conf. gbos-install still checks lockdown when
the stick boots. The user stick has no plain Maintenance entry: that item
booted the maintenance kernel with none of the installer's firmware blocks.

Cheap refusals (tools, map, boot entries) run before the super hash and
verify-data, so a bad entry does not read the whole super. Both checks
still run before anything is published.
"""
import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(HERE, "gbos-install.sh")
TEMPLATE = os.path.join(HERE, "gbos-install.entry.template")
INITCALL_MAP = os.path.join(HERE, "builtin-initcalls.txt")
VERIFIER_RE = re.compile(r"cat > \"\$W/gbos\.py\" << 'PY'\n(.*?)\nPY\n", re.S)
PARTUUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PLACEHOLDER_RE = re.compile(r"@[A-Z_]*@")
ESP_ALLOWED = (
    "lapis-debug.conf lapis-quiet.conf ",
    "lapis-debug.conf lapis-quiet.conf lapis.conf ",
)
TOOL_DIRS = ("usr/bin", "usr/sbin", "bin", "sbin")
# gbos-install.sh's tool check, then the update tools setup-stick.sh adds.
REQUIRED_TOOLS = (
    "python3", "zstd", "sgdisk", "partx", "blockdev", "blkdiscard", "udevadm",
    "lsblk", "findmnt", "mcopy", "mtype", "dd", "sha256sum", "cmp", "mount",
    "umount", "lz4", "mdir", "mmd", "mdel", "minfo",
)
BLACKLIST_HEADER = (
    "# module_blacklist of the installer entry (from gbos-install.entry.template, "
    "by setup-stick.sh): gbos-install\n"
    "# refuses to run when the kernel command line does not block every one of these\n"
)
USAGE = (
    "stick-payload: usage: stick-payload.py apply --root DIR --pristine DIR "
    "--xbootldr DIR --esp-dir DIR --image IMAGE --partuuid UUID "
    "[--esp-loader-out FILE]"
)


class PayloadError(Exception):
    pass


def refuse(msg):
    raise PayloadError(msg)


def read_text(path, what):
    try:
        with open(path, "r", encoding="ascii", newline="") as handle:
            return handle.read()
    except OSError as err:
        refuse(f"cannot read {what}: {err.strerror}")
    except UnicodeError:
        refuse(f"{what} is not ASCII")


def real_dir(path, what):
    try:
        info = os.lstat(path)
    except OSError as err:
        refuse(f"{what}: {err.strerror}")
    if stat.S_ISLNK(info.st_mode):
        refuse(f"{what} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        refuse(f"{what} is not a directory")
    return os.path.realpath(path)


def real_file(path, what):
    try:
        info = os.lstat(path)
    except OSError as err:
        refuse(f"{what}: {err.strerror}")
    if stat.S_ISLNK(info.st_mode):
        refuse(f"{what} is a symlink")
    if not stat.S_ISREG(info.st_mode):
        refuse(f"{what} is not a regular file")
    if info.st_size == 0:
        refuse(f"{what} is empty")
    return path


def take(argv, index, flag):
    if index + 1 >= len(argv) or not argv[index + 1] or argv[index + 1].startswith("--"):
        refuse(f"{flag} needs a value")
    return argv[index + 1]


def parse(argv):
    if not argv:
        refuse(USAGE.split(": ", 1)[1])
    got = {}
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag not in (
            "--root", "--pristine", "--xbootldr", "--esp-dir", "--image",
            "--partuuid", "--esp-loader-out",
        ):
            refuse(f"unknown argument {flag}")
        if flag in got:
            refuse(f"{flag} given twice")
        got[flag] = take(argv, index, flag)
        index += 2
    missing = [
        name for name in (
            "--root", "--pristine", "--xbootldr", "--esp-dir", "--image", "--partuuid",
        ) if name not in got
    ]
    if missing:
        refuse(f"{missing[0]} is required")
    if not PARTUUID_RE.fullmatch(got["--partuuid"]):
        refuse(f"partuuid: {got['--partuuid']}")
    return got


def verifier_source():
    try:
        text = open(INSTALLER, "r", encoding="utf-8").read()
    except OSError as err:
        refuse(f"cannot read gbos-install.sh: {err.strerror}")
    match = VERIFIER_RE.search(text)
    if match is None or "def verify_data" not in match.group(1):
        refuse("cannot find the verifier in gbos-install.sh")
    return match.group(1) + "\n"


def write_verifier(work):
    path = os.path.join(work, "gbos.py")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(verifier_source())
    return path


def run_verifier(script, args):
    proc = subprocess.run(
        [sys.executable, "-I", script, *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        refuse(detail or "verifier failed")
    return proc


def module_names(text):
    names = []
    for line in text.splitlines():
        if not line.startswith("options "):
            continue
        for token in line.split(" "):
            if token.startswith("module_blacklist="):
                names.extend(part.replace("-", "_") for part in token.split("=", 1)[1].split(",") if part)
    if not names:
        refuse("no module_blacklist in the installer entry")
    return sorted(set(names))


def parse_map(text):
    rows = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        two = len(fields) == 2 and re.fullmatch(r"[a-z0-9_]+", fields[1])
        three = (
            len(fields) == 3 and fields[1] == "-"
            and re.fullmatch(r"[a-z0-9_]+(,[a-z0-9_]+)*", fields[2])
        )
        if not two and not three:
            refuse(f"builtin-initcalls.txt has a bad line: {raw.strip()}")
        rows.append(fields)
    initcalls = [fields[1] for fields in rows if fields[1] != "-"]
    if not initcalls:
        refuse("no initcalls in builtin-initcalls.txt")
    joined = ",".join(initcalls)
    have = set(initcalls)
    for fields in rows:
        if fields[1] != "-":
            continue
        for name in fields[2].split(","):
            if name not in have:
                refuse(
                    f"{fields[0]} is accepted only with {name} blocked, "
                    "which builtin-initcalls.txt does not list"
                )
    covered = sorted({fields[0].replace("-", "_") for fields in rows})
    return joined, covered


def kernel_version_from_image(data):
    if len(data) < 0x210 or data[0x202:0x206] != b"HdrS":
        return ""
    offset = struct.unpack_from("<H", data, 0x20E)[0] + 0x200
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\0", offset)
    if end < 0:
        return ""
    token = data[offset:end].split(b" ")[0]
    try:
        return token.decode("ascii")
    except UnicodeError:
        return ""


def read_file_bytes(path, what):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as err:
        refuse(f"cannot read {what}: {err.strerror}")


def find_kver(root, kernel):
    modules = os.path.join(root, "usr", "lib", "modules")
    found = ""
    if os.path.isdir(modules) and not os.path.islink(modules):
        for name in sorted(os.listdir(modules)):
            candidate = os.path.join(modules, name, "vmlinuz")
            try:
                info = os.lstat(candidate)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if read_file_bytes(candidate, "modules vmlinuz") == kernel:
                found = name
    if found:
        return found
    return kernel_version_from_image(kernel[:1 << 16])


def builtin_modules(path):
    names = set()
    for line in read_text(path, "modules.builtin").splitlines():
        base = line.strip().rsplit("/", 1)[-1]
        if base.endswith(".ko"):
            base = base[:-3]
        if base:
            names.add(base.replace("-", "_"))
    return names


def has_tool(root, name):
    for directory in TOOL_DIRS:
        path = os.path.join(root, directory, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return True
    return False


def check_tools(root):
    missing = [name for name in REQUIRED_TOOLS if not has_tool(root, name)]
    if missing:
        refuse("the maintenance root needs " + ", ".join(missing))


def esp_entries(esp_dir):
    directory = os.path.join(esp_dir, "loader", "entries")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        refuse("the image's ESP has unexpected boot entries: none readable")
    files = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
        except OSError as err:
            refuse(f"cannot read the image entry {name}: {err.strerror}")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            refuse(f"the image entry {name} is not a regular file")
        files.append((name, read_text(path, f"the image entry {name}")))
    joined = " ".join(name for name, _text in files) + (" " if files else "")
    if joined not in ESP_ALLOWED:
        shown = joined.strip() or "none readable"
        refuse(f"the image's ESP has unexpected boot entries: {shown}")
    return files


def check_entry_blacklists(files, allowed):
    allowed_set = set(allowed)
    for name, text in files:
        try:
            names = module_names(text)
        except PayloadError:
            refuse(f"the image's entry {name} has no module_blacklist")
        missing = sorted(set(names) - allowed_set)
        if missing:
            refuse(
                f"the image's {name} blocks modules that the installer entry does not: "
                + " ".join(missing)
            )


def check_builtin_coverage(root, kver, covered, blocked):
    path = os.path.join(root, "usr", "lib", "modules", kver, "modules.builtin")
    if not os.path.isfile(path) or os.path.islink(path):
        refuse(f"no modules.builtin for the xbootldr kernel ({kver or 'version unknown'})")
    built = builtin_modules(path) & set(blocked)
    uncovered = sorted(built - set(covered))
    if uncovered:
        refuse(
            f"kernel {kver} has these built in and builtin-initcalls.txt does not "
            "block them: " + " ".join(uncovered)
        )
    return path


def render_pair(template, partuuid, initcalls):
    install = template.replace("@ROOT_PARTUUID@", partuuid).replace(
        "@INITCALL_BLACKLIST@", initcalls
    )
    if PLACEHOLDER_RE.search(install):
        refuse("template not rendered")
    update_lines = []
    for line in install.splitlines(keepends=True):
        if line.startswith("title "):
            newline = "\n" if line.endswith("\n") else ""
            update_lines.append(
                "title Update Googlebook on the internal disk (keep data)" + newline
            )
        elif line.startswith("options ") and line.rstrip("\n").endswith(" gbos.install=1"):
            newline = "\n" if line.endswith("\n") else ""
            update_lines.append(line.rstrip("\n") + " gbos.update=1" + newline)
        else:
            update_lines.append(line)
    update = "".join(update_lines)
    marker = " gbos.install=1 gbos.update=1"
    hits = [
        line for line in update.splitlines()
        if line.startswith("options ") and line.endswith(marker)
    ]
    if len(hits) != 1:
        refuse("cannot derive the update entry")
    for text in (install, update):
        options = [line for line in text.splitlines() if line.startswith("options ")]
        if len(options) != 1 or len(options[0]) + 1 >= 1900:
            refuse("kernel command line too long")
    return install if install.endswith("\n") else install + "\n", (
        update if update.endswith("\n") else update + "\n"
    )


# User stick menu. A plain "Maintenance" shell is not one of these: it would
# boot the same kernel with no lockdown, module_blacklist, initcall_blacklist,
# or efi=noruntime, and it would not run gbos-install's serial check.
ALLOWED_XBOOT_ENTRIES = (
    "gbos-install.conf",
    "gbos-update.conf",
    "gbos-nvme.conf",
    "gbos-nvme-debug.conf",
)
PROTECTION_TOKENS = (
    "lockdown=integrity",
    "efi=noruntime",
    "module_blacklist=",
    "initcall_blacklist=",
)


def assert_maint_kernel_protected(name, text):
    lines = text.splitlines()
    if "linux /vmlinuz-linux" not in lines:
        return
    if name == "maintenance.conf" or "title Maintenance" in lines:
        refuse("plain Maintenance entry is not on the user stick")
    options = [line for line in lines if line.startswith("options ")]
    if len(options) != 1:
        refuse(f"{name} needs exactly one options line")
    missing = [token for token in PROTECTION_TOKENS if token not in options[0]]
    if missing:
        refuse(f"{name} boots the maintenance kernel without " + " ".join(missing))


def publish_xboot_entries(entry_dir, entries):
    names = [name for name, _text in entries]
    if names != list(ALLOWED_XBOOT_ENTRIES):
        refuse("installer stick boot entries changed")
    for name, text in entries:
        assert_maint_kernel_protected(name, text)
    ensure_dir(entry_dir)
    allowed = set(ALLOWED_XBOOT_ENTRIES)
    for name in os.listdir(entry_dir):
        if name in allowed:
            continue
        path = os.path.join(entry_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            refuse(f"xbootldr entries contains a directory: {name}")
        os.unlink(path)
    for name, text in entries:
        write_file(os.path.join(entry_dir, name), text.encode("ascii"), 0o644)
    if sorted(os.listdir(entry_dir)) != sorted(ALLOWED_XBOOT_ENTRIES):
        refuse("plain Maintenance entry is still present")


def esp_loader_text():
    # Fresh stick: /gbos-nvme does not exist until the installer runs, so the
    # default is the installer entry rather than gbos-nvme.conf.
    return (
        "default gbos-install.conf\n"
        "timeout 5\n"
        "editor no\n"
        "auto-firmware no\n"
    )


def rewrite_internal(text, title):
    lines = []
    for line in text.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        body = line[:-1] if line.endswith("\n") else line
        if body.startswith("title "):
            lines.append(f"title {title}{newline}")
        elif body.startswith("linux "):
            lines.append(f"linux /gbos-nvme/kernel.efi{newline}")
        elif body.startswith("initrd "):
            lines.append(f"initrd /gbos-nvme/initrd.img{newline}")
        else:
            lines.append(line)
    rendered = "".join(lines)
    return rendered if rendered.endswith("\n") else rendered + "\n"


def lift_internal(files, script, work):
    by_name = dict(files)
    made = []
    for source, title, dest in (
        ("lapis-quiet.conf", "Googlebook (internal disk)", "gbos-nvme.conf"),
        ("lapis-debug.conf", "Googlebook (internal disk) serial-debug", "gbos-nvme-debug.conf"),
    ):
        src = os.path.join(work, source)
        out = os.path.join(work, dest)
        with open(src, "w", encoding="ascii", newline="") as handle:
            handle.write(by_name[source])
        try:
            run_verifier(script, ["lift-entry", src, out])
        except PayloadError as err:
            refuse(f"boot entry {source} of the image cannot be changed safely: {err}")
        made.append((dest, rewrite_internal(read_text(out, dest), title)))
    return made


def manifest_super(pristine):
    path = os.path.join(pristine, "manifest.txt")
    text = read_text(path, "manifest.txt")
    if not text.endswith("\n"):
        refuse("manifest: no final newline")
    found = None
    for line in text[:-1].split("\n"):
        fields = line.split(" ")
        if len(fields) >= 8 and fields[0] == "part" and fields[2] == "super":
            found = fields
    if found is None or found[-1] != "stick" or not re.fullmatch(r"[0-9a-f]{64}", found[7]):
        refuse("manifest: needs a super partition")
    try:
        start, length = int(found[3]), int(found[4])
    except ValueError:
        refuse("manifest: super offsets are not numbers")
    if start < 0 or length <= 0:
        refuse("manifest: super offsets are not numbers")
    return start, length, found[7]


def sha256_range(path, start, length):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        left = length
        offset = start
        while left:
            chunk = os.pread(fd, min(left, 8 << 20), offset)
            if len(chunk) != min(left, 8 << 20):
                refuse("the image is shorter than the manifest's super")
            digest.update(chunk)
            offset += len(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def check_super(image, pristine):
    start, length, expect = manifest_super(pristine)
    if sha256_range(image, start, length) != expect:
        refuse("super in the image is not the manifest's super")


def engine_path():
    for path in (os.path.join(HERE, "gbos-update.py"), os.path.join(HERE, "..", "gbos-update.py")):
        if os.path.isfile(path) and not os.path.islink(path):
            return os.path.realpath(path)
    refuse("need gbos-update.py (install/gbos-update.py)")


def ensure_dir(path):
    if os.path.isdir(path) and not os.path.islink(path):
        return
    if os.path.lexists(path):
        refuse(f"{path} is not a directory")
    os.makedirs(path, 0o755)


def write_file(path, data, mode):
    ensure_dir(os.path.dirname(path))
    tmp = path + ".gbos-new"
    if os.path.lexists(tmp):
        refuse(f"temporary file already exists: {os.path.basename(tmp)}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        view = memoryview(data)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                refuse(f"short write: {os.path.basename(path)}")
            view = view[wrote:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    with open(path, "rb") as handle:
        if handle.read() != data:
            refuse(f"read-back differs: {os.path.basename(path)}")


def write_link(path, target):
    ensure_dir(os.path.dirname(path))
    tmp = path + ".gbos-new"
    if os.path.lexists(tmp):
        refuse(f"temporary file already exists: {os.path.basename(tmp)}")
    os.symlink(target, tmp)
    os.replace(tmp, path)
    if os.readlink(path) != target:
        refuse("read-back of the installer service link differs")


def copy_pristine(src, dst):
    os.mkdir(dst, 0o755)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel = os.path.relpath(dirpath, src)
        for name in list(dirnames):
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                refuse("pristine data contains a symlink")
            os.mkdir(os.path.join(dst, rel, name) if rel != "." else os.path.join(dst, name), 0o755)
        for name in filenames:
            child = os.path.join(dirpath, name)
            info = os.lstat(child)
            if not stat.S_ISREG(info.st_mode):
                refuse(f"pristine data contains a non-regular file: {name}")
            target = os.path.join(dst, rel, name) if rel != "." else os.path.join(dst, name)
            write_file(target, read_file_bytes(child, name), stat.S_IMODE(info.st_mode) or 0o644)


def publish_pristine(root, staging):
    final = os.path.join(root, "var", "lib", "gbos-install", "pristine")
    if os.path.islink(final):
        refuse("pristine destination is a symlink")
    backup = final + ".old"
    if os.path.lexists(backup):
        refuse("temporary pristine copy already exists")
    if os.path.lexists(final):
        os.rename(final, backup)
    try:
        os.replace(staging, final)
    except BaseException:
        if os.path.lexists(backup) and not os.path.lexists(final):
            os.rename(backup, final)
        raise
    if os.path.lexists(backup):
        shutil.rmtree(backup)


def apply(args):
    root = real_dir(args["--root"], "--root")
    pristine = real_dir(args["--pristine"], "--pristine")
    xboot = real_dir(args["--xbootldr"], "--xbootldr")
    esp = real_dir(args["--esp-dir"], "--esp-dir")
    image = real_file(args["--image"], "--image")
    partuuid = args["--partuuid"]
    kernel_path = os.path.join(xboot, "vmlinuz-linux")
    initrd_path = os.path.join(xboot, "initramfs-linux.img")
    if not os.path.isfile(kernel_path) or os.path.islink(kernel_path):
        refuse("no maintenance kernel in xbootldr")
    if not os.path.isfile(initrd_path) or os.path.islink(initrd_path):
        refuse("no maintenance kernel in xbootldr")
    kernel = read_file_bytes(kernel_path, "vmlinuz-linux")
    if not kernel or not read_file_bytes(initrd_path, "initramfs-linux.img"):
        refuse("no maintenance kernel in xbootldr")
    check_tools(root)
    template = read_text(TEMPLATE, "gbos-install.entry.template")
    blocked = module_names(template)
    initcalls, covered = parse_map(read_text(INITCALL_MAP, "builtin-initcalls.txt"))
    missing = sorted(set(covered) - set(blocked))
    if missing:
        refuse(
            "builtin-initcalls.txt lists modules missing from the entry's "
            "module_blacklist: " + " ".join(missing)
        )
    kver = find_kver(root, kernel)
    if not kver:
        refuse("no modules.builtin for the xbootldr kernel (version unknown)")
    check_builtin_coverage(root, kver, covered, blocked)
    files = esp_entries(esp)
    check_entry_blacklists(files, blocked)
    install, update = render_pair(template, partuuid, initcalls)
    work = tempfile.mkdtemp(prefix="gbos-payload-")
    staging = os.path.join(root, "var", "lib", "gbos-install", "pristine.new")
    try:
        script = write_verifier(work)
        internal = lift_internal(files, script, work)
        if os.path.lexists(staging):
            refuse("temporary pristine copy already exists")
        check_super(image, pristine)
        head = os.path.join(work, "head.img")
        try:
            run_verifier(script, ["verify-data", pristine, head])
        except PayloadError as err:
            refuse(f"the pristine data is damaged: {err}")
        ensure_dir(os.path.dirname(staging))
        copy_pristine(pristine, staging)
        try:
            run_verifier(script, ["verify-data", staging, os.path.join(work, "head2.img")])
        except PayloadError as err:
            refuse(f"the copied pristine data is damaged: {err}")
        engine = engine_path()
        blacklist = BLACKLIST_HEADER + "".join(f"{name}\n" for name in blocked)
        entries = [
            ("gbos-install.conf", install),
            ("gbos-update.conf", update),
            *internal,
        ]
        write_file(
            os.path.join(root, "usr", "local", "sbin", "gbos-install"),
            read_file_bytes(INSTALLER, "gbos-install.sh"), 0o755,
        )
        write_file(
            os.path.join(root, "etc", "systemd", "system", "gbos-installer.service"),
            read_file_bytes(os.path.join(HERE, "gbos-installer.service"), "gbos-installer.service"),
            0o644,
        )
        write_link(
            os.path.join(
                root, "etc", "systemd", "system", "multi-user.target.wants",
                "gbos-installer.service",
            ),
            "/etc/systemd/system/gbos-installer.service",
        )
        share = os.path.join(root, "usr", "local", "share", "gbos-install")
        write_file(os.path.join(share, "builtin-initcalls.txt"),
                   read_file_bytes(INITCALL_MAP, "builtin-initcalls.txt"), 0o644)
        write_file(os.path.join(share, "module-blacklist.txt"), blacklist.encode("ascii"), 0o644)
        write_file(os.path.join(share, "gbos-update.py"), read_file_bytes(engine, "gbos-update.py"), 0o644)
        publish_xboot_entries(os.path.join(xboot, "loader", "entries"), entries)
        loader = esp_loader_text()
        if "--esp-loader-out" in args:
            write_file(args["--esp-loader-out"], loader.encode("ascii"), 0o644)
        publish_pristine(root, staging)
        staging = ""
    except BaseException:
        if staging and os.path.isdir(staging) and not os.path.islink(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    names = " ".join(name for name, _text in entries)
    return [
        "stick-payload ok",
        f"kernel {kver}",
        f"initcalls {initcalls}",
        f"entries {names}",
        "esp_loader default gbos-install.conf",
        "super ok",
        "note: lockdown support is checked by gbos-install at boot",
        "note: file owners stay the builder's uid; the ext4 pack records uid 0",
    ]


def main(argv):
    if not argv or argv[0] != "apply":
        print(USAGE, file=sys.stderr)
        return 2
    try:
        lines = apply(parse(argv[1:]))
    except PayloadError as err:
        print(f"stick-payload: {err}", file=sys.stderr)
        return 2
    for line in lines:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
