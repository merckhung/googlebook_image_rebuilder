#!/usr/bin/env python3
"""Stage 2 of the Googlebook build for one device profile (GBOS_PROFILE, builder/lib/devprofile.py): ESP initrd with the
profile's first-stage changes (KANO: pci-stub loaded first), two systemd-boot entries (quiet, serial-debug) made
from the stock vendor_boot options, the profile and the safety rules (profiles/_safety.toml), and two QEMU self-tests
of the finished USB image:
  1. functional: kernel + initrd + the serial-debug options handed to QEMU directly, Android checked on the serial log;
  2. boot chain: firmware -> the image's own ESP -> systemd-boot -> the default entry, checked by disk writes.
The image is built as <name>.partial and renamed to <name> only when every check passes."""
import hashlib, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, time, uuid
from pathlib import Path
if not __debug__:
    sys.exit("builder/image.py checks the safety options with assert: run it without python -O / PYTHONOPTIMIZE")
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import devprofile
import lapis
GBOS_OUT = Path(os.environ["GBOS_OUT"])
SRC = GBOS_OUT / "vendor/stage-vendor.img"
PROF = devprofile.load(os.environ.get("GBOS_PROFILE") or Path(__file__).resolve().parent.parent / "profiles/marasov")
if PROF["model"]["status"] != "verified":
    print(f"WARNING: profile {PROF['model']['id']} is {PROF['model']['status']!r}, not verified on its hardware "
          "(profiles/<model>/profile.toml model.status)", flush=True)
NAME = os.environ.get("GBOS_IMAGE_NAME") or PROF["model"]["image_name"]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.img", NAME):
    sys.exit(f"GBOS_IMAGE_NAME must be a plain file name ending in .img, not {NAME!r}")
OUT = GBOS_OUT / "image"; OUT.mkdir(parents=True, exist_ok=True)
FINAL = OUT / NAME
IMG = OUT / (NAME + ".partial")
PCI_STUB = Path(os.environ["GBOS_PCI_STUB_KO"])
QEMU = os.environ.get("GBOS_QEMU", "/usr/bin/qemu-system-x86_64")
QEMU_CPU = "host"
if PROF["model"]["id"] in ("marasov", "screebo", "lg-15u50u"):
    # These laptops lack AVX-512. Xeon host features can select EVEX crypto
    # paths beyond the 256-bit ISA emulation used on the target laptops.
    QEMU_CPU += ",-avx512f,-avx512dq,-avx512cd,-avx512bw,-avx512vl"
OVMF_CODE = os.environ.get("GBOS_OVMF_CODE", "/usr/share/edk2/x64/OVMF_CODE.4m.fd")
OVMF_VARS = os.environ.get("GBOS_OVMF_VARS", "/usr/share/edk2/x64/OVMF_VARS.4m.fd")
QEMU_LIB = os.environ.get("GBOS_QEMU_LIB", "")
QEMU_SECONDS = int(os.environ.get("GBOS_QEMU_SECONDS", "200"))
DEFAULT_ENTRY = os.environ.get("GBOS_DEFAULT_ENTRY", "lapis-quiet.conf")
EDITOR = os.environ.get("GBOS_LOADER_EDITOR", "no")
ADB = bool(os.environ.get("GBOS_ADB_PUBKEY"))
SKIP_SELFTEST = os.environ.get("GBOS_SKIP_SELFTEST") == "1"
GENERIC = PROF["display"]["stack"] == "gop-swiftshader"   # GOP framebuffer + SwiftShader (profiles/generic-x86)
AMD_HX370 = PROF["model"]["id"] == "amd-hx370"
# Both generic-x86 and amd-hx370's safe default entry use the GOP framebuffer + SwiftShader path in QEMU self-tests.
GOP = GENERIC or AMD_HX370
NATIVE_GOP_FALLBACK = PROF["display"]["fallback"] == "gop-swiftshader"
# Opt-in EC features (default: none, and then every byte below is as without this switch). Each one releases an
# audited set of ChromeOS EC kernel drivers (docs/SAFETY.md, "EC 功能變體"); the EC userspace channels and every EC
# writer stay blocked in every variant. KANO only (its hooks.vendor has the ec() hook).
EC_FEATURES = [f for f in os.environ.get("GBOS_EC", "").split(",") if f]
assert set(EC_FEATURES) <= {"sensors", "typec", "buttons"} and len(set(EC_FEATURES)) == len(EC_FEATURES), EC_FEATURES
assert not EC_FEATURES or PROF["model"]["id"] == "kano", "GBOS_EC is audited for the KANO only"
KMOD_AC = "ac" in PROF["kmods"]   # vendor carries ac.ko (builder/kmod/build-ac.sh; builder/vendor.sh checks the pin)
KMOD_WAKEFILTER = "gbos_wakefilter" in PROF["kmods"]   # vendor carries gbos_wakefilter.ko (build-gbos_wakefilter.sh)
# Developer-only. Empty = no auto-off service (install.sh refuses a non-empty value). When set, QEMU has no
# /data/kano-autooff.nonce, so the serial log must show the rc parsed, the nonce gate staying on, and the
# oneshot exited 0. When empty, the serial log must not mention the service.
TEST_AUTOOFF = os.environ.get("GBOS_TEST_AUTOOFF", "")
if FINAL.exists():
    sys.exit(f"refusing: {FINAL} already exists")
ENTRIES = ("lapis-debug.conf", "lapis-quiet.conf")
assert DEFAULT_ENTRY in ENTRIES, DEFAULT_ENTRY
assert EDITOR in ("yes", "no"), EDITOR

def run(*a):
    print("+", " ".join(map(str, a)), flush=True); subprocess.run(list(map(str, a)), check=True)
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(16 << 20), b""): h.update(b)
    return h.hexdigest()
def gpt(path):
    with open(path, "rb") as f:
        f.seek(512); h = f.read(92)
        if h[:8] != b"EFI PART": sys.exit(f"{path}: no GPT")
        lba, n, esz = struct.unpack_from("<QII", h, 72)
        f.seek(lba * 512); t = f.read(n * esz)
    parts = {}
    for i in range(n):
        e = t[i * esz:(i + 1) * esz]
        if e[:16] == bytes(16): continue
        first, last = struct.unpack_from("<QQ", e, 32)
        parts[e[56:128].decode("utf-16le").rstrip("\0")] = dict(
            type=str(uuid.UUID(bytes_le=e[:16])), guid=str(uuid.UUID(bytes_le=e[16:32])), start=first * 512, end=(last + 1) * 512)
    return parts
PARTS = gpt(SRC)
if PARTS.get("EFI-SYSTEM", {}).get("type") != "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": sys.exit("no EFI-SYSTEM partition in the base image")
ESP = f"@@{PARTS['EFI-SYSTEM']['start']}"
USERDATA = PARTS["userdata"]
def mtype(img, path):
    return subprocess.run(["mtype", "-i", f"{img}{ESP}", path], check=True, capture_output=True, text=True).stdout.replace("\r", "")

# initrd: drop the LPSS UART drivers from first-stage modules.load, keep the bootconfig as built
IMG.unlink(missing_ok=True)
run("cp", "--reflink=auto", SRC, IMG)
run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/initrd.img", OUT / "src-initrd.img")
src = (OUT / "src-initrd.img").read_bytes()
assert src.endswith(b"#BOOTCONFIG\n")
size, _ = struct.unpack("<II", src[-20:-12]); bc = src[-20 - size:]; body = src[:-20 - size]
digest = (GBOS_OUT / "vendor/logs/vbmeta.digest.txt").read_text().split()[0]
assert re.fullmatch(r"[0-9a-f]{64}", digest) and f"androidboot.vbmeta.digest={digest}\n".encode() in bc, \
    "ESP bootconfig must carry the re-signed vbmeta digest"
DROP = set(PROF["cmdline"]["first_stage_drop"])
STUB = bool(PROF["cmdline"]["pci_stub_ids"])
ISA_EMU_PATH = Path(os.environ.get("GBOS_ISA_EMU_KO") or (Path(__file__).resolve().parent.parent / "work/inputs/kmod/gbos_isa_emu.ko"))
ISA_EMU = ISA_EMU_PATH.is_file()
if "gbos_isa_emu" in PROF["kmods"] and not ISA_EMU:
    sys.exit("profile requires gbos_isa_emu.ko; run install/install.sh to build it")
REQUIRE_ADB_AUTH = ADB and lapis.load().get("EARLY_ADB") == "1"
out = bytearray(); off = 0; changed = 0; added = 0; depchanged = 0; authchanged = 0
while off < len(body):
    if body[off:off+6] != b"070701":
        nxt = body.find(b"070701", off)
        if nxt < 0: out += body[off:]; break
        out += body[off:nxt]; off = nxt
    h = bytearray(body[off:off+110]); ns = int(h[94:102], 16); fs = int(h[54:62], 16)
    nend = (off + 110 + ns + 3) & ~3; name = body[off+110:off+110+ns-1].decode(); data = body[nend:nend+fs]
    nxt = (nend + fs + 3) & ~3
    if name == "system/etc/ramdisk/build.prop" and REQUIRE_ADB_AUTH:
        # Init imports these platform properties before adbd starts. Setting
        # this in /vendor/build.prop is rejected by Android's property policy.
        text = data.decode()
        secure = [line for line in text.splitlines() if line.startswith("ro.adb.secure=")]
        assert not secure or secure == ["ro.adb.secure=1"], "conflicting ADB authentication property"
        if not secure:
            data = (text.rstrip("\n") + "\nro.adb.secure=1\n").encode()
        h[54:62] = b"%08X" % len(data); authchanged += 1
        out += h + body[off+110:nend] + data + b"\0" * ((-len(data)) % 4)
    elif name == "lib/modules/modules.load":
        extra_mods = (["pci-stub.ko"] if STUB else []) + (["gbos_isa_emu.ko"] if ISA_EMU else [])
        data = ("\n".join(extra_mods + [l for l in data.decode().splitlines() if l and l not in DROP]) + "\n").encode()
        h[54:62] = b"%08X" % len(data); changed += 1
        out += h + body[off+110:nend] + data + b"\0" * ((-len(data)) % 4)
        if STUB:
            # new entry: lib/modules/pci-stub.ko (first stage loads it before ueventd can load intel-lpss-pci)
            ko = PCI_STUB.read_bytes()
            nm = b"lib/modules/pci-stub.ko\0"
            fields = [0x7FFF0001, 0o100644, 0, 0, 1, int(h[46:54], 16), len(ko), 0, 0, 0, 0, len(nm), 0]
            hdr = b"070701" + b"".join(b"%08X" % v for v in fields)
            out += hdr + nm + b"\0" * ((-(110 + len(nm))) % 4) + ko + b"\0" * ((-len(ko)) % 4)
            added += 1
        if ISA_EMU:
            ko = ISA_EMU_PATH.read_bytes()
            nm = b"lib/modules/gbos_isa_emu.ko\0"
            fields = [0x7FFF0002, 0o100644, 0, 0, 1, int(h[46:54], 16), len(ko), 0, 0, 0, 0, len(nm), 0]
            hdr = b"070701" + b"".join(b"%08X" % v for v in fields)
            out += hdr + nm + b"\0" * ((-(110 + len(nm))) % 4) + ko + b"\0" * ((-len(ko)) % 4)
            added += 1
    elif name == "lib/modules/gbos_isa_emu.ko" and ISA_EMU:
        # vendor.sh included this file for its firmware gate. The selected
        # payload was emitted above with modules.load; retain only that copy.
        pass
    elif name == "lib/modules/modules.dep" and (STUB or ISA_EMU):
        if STUB:
            data = data + b"/lib/modules/pci-stub.ko:\n"
        if ISA_EMU:
            data = data + b"/lib/modules/gbos_isa_emu.ko:\n"
        h[54:62] = b"%08X" % len(data); depchanged += 1
        out += h + body[off+110:nend] + data + b"\0" * ((-len(data)) % 4)
    else:
        out += body[off:nxt]
    off = nxt
assert changed == 1 and added == ((1 if STUB else 0) + (1 if ISA_EMU else 0)) and depchanged == (1 if (STUB or ISA_EMU) else 0), (changed, added, depchanged)
assert authchanged == int(REQUIRE_ADB_AUTH), "authenticated ADB requires exactly one platform ramdisk property file"
(OUT / "initrd.img").write_bytes(bytes(out) + bc)

# entries: the pinned stock vendor_boot options of the selected Lapis build, the profile's
# additions and the project-wide safety options (builder/lib/devprofile.py make_entries)
VBOOT = PARTS["vendor_boot_a"]
with open(SRC, "rb") as f:
    f.seek(VBOOT["start"]); vh = f.read(4096)
assert vh[:8] == b"VNDRBOOT" and struct.unpack_from("<I", vh, 8)[0] == 4, "vendor_boot_a: not a v4 vendor boot image"
vcmd = vh[28:28 + 2048].split(b"\0")[0]
assert hashlib.sha256(vcmd).hexdigest() == lapis.load()["EXPECT"]["vendor_cmdline.txt"], \
    "vendor_boot_a command line does not match the selected Lapis build"
E = devprofile.make_entries(PROF, vcmd.decode())
normal, debug, quiet = E["normal"], E["debug"], E["quiet"]
# GBOS_EC: the EC transport/core plus each feature's drivers leave the blacklist; nothing they send writes EC flash,
# CBI, I2C, firmware or reboots the EC (audit: docs/SAFETY.md). cros_ec_dev still adds the cros-ec-chardev, -sysfs,
# -debugfs, -hwmon, -lightbar, -vbc, -charge-control, -usbpd-charger/-logger, -pchg, -led and -gpio/-rtc/-wdt cells; their
# drivers stay blacklisted, so /dev/cros_ec and every EC write interface stay absent. Two drivers outside the released
# set then send EC commands too: cros_kbd_led_backlight (allowed in every entry for the keyboard backlight) binds the
# cros-keyboard-leds cell when the EC reports EC_FEATURE_PWM_KEYB and sends PWM_GET/SET_KEYBOARD_BACKLIGHT (EC RAM only;
# the cell cannot be blocked on its own), and cros_ec_typec's VDM code sends TYPEC_VDM_RESPONSE (a read).
EC_CORE = ["cros_ec", "cros_ec_lpcs", "cros_ec_dev"]
EC_RELEASE = {"sensors": ["cros_ec_sensorhub", "cros_ec_sensors_core", "cros_ec_sensors", "cros_ec_lid_angle"],
              "typec": ["cros_ec_typec", "cros_usbpd_notify"],
              "buttons": ["cros_ec_keyb"]}
# not in this lapis build's vendor_dlkm; blocked in the EC variants so a future vendor_dlkm cannot add an EC writer
EC_EXTRA_BLOCK = ["gpio_cros_ec", "cros_ec_hwmon", "cros_ec_wdt", "rtc_cros_ec", "cros_ec_uart", "cros_ec_rpmsg"]
# must stay blocked in every variant: EC userspace channels, EC writers, other MCUs, TPM, SPI flash, firmware NVRAM
EC_NEVER = ("cros_ec_chardev", "cros_ec_debugfs", "cros_ec_sysfs", "cros_ec_vbc", "cros_ec_lightbar", "i2c_cros_ec_tunnel",
            "cros_charge_control", "leds_cros_ec", "cros_ec_spi", "cros_ec_i2c", "cros_ec_ishtp", "cros_ec_usb",
            "cros_peripheral_charger", "cros_usbpd_charger", "cros_usbpd_logger", "cros_typec_switch", "cros_ec_ucsi",
            "tpm", "tpm_tis", "tpm_tis_core", "tpm_tis_i2c_cr50", "spi_intel_pci", "spi_intel", "thunderbolt", "i2c_dev",
            "gsmi", "nvram", "mtd", "spi_nor", "spidev", "chromeos_acpi")
released = set(EC_CORE + [m for f in EC_FEATURES for m in EC_RELEASE[f]]) if EC_FEATURES else set()
assert released <= set(PROF["blacklist"]["modules"]) and not released & set(EC_NEVER), released
def ec_blacklist(o):
    if not EC_FEATURES:
        return o
    out = []
    for x in o:
        if x.startswith("module_blacklist="):
            ms = x.split("=", 1)[1].split(",")
            x = "module_blacklist=" + ",".join([m for m in ms if m not in released] + [m for m in EC_EXTRA_BLOCK if m not in ms])
        out.append(x)
    return out
normal, debug, quiet = ec_blacklist(normal), ec_blacklist(debug), ec_blacklist(quiet)
def entry(title, o): return f"title {title}\nlinux /kernel.efi\ninitrd /initrd.img\noptions {' '.join(o)}\n"
SERIAL = devprofile.SERIAL
STUB_OPT = "pci-stub.ids=" + ",".join(PROF["cmdline"]["pci_stub_ids"])
assert (not STUB or STUB_OPT in quiet) and not any(o.startswith(SERIAL) for o in quiet)
# quiet differs from the serial-debug entry only by the serial console/log-level parameters and the profile's
# optional quiet_drop / quiet_add (used by amd-hx370 so the default entry is the safe GOP + SwiftShader path).
debug_nobl = [o for o in debug if not o.startswith("module_blacklist=")]
quiet_nobl = [o for o in quiet if not o.startswith("module_blacklist=")]
assert sorted(set(debug_nobl) ^ set(quiet_nobl)) == sorted(
    set(o for o in debug_nobl if o.startswith(SERIAL))
    | {"keep_bootcon", "printk.devkmsg=on", "loglevel=4"}
    | (set(debug_nobl) & {"ignore_loglevel", "loglevel=8"})
    | set(PROF["cmdline"]["quiet_drop"])
), set(debug_nobl) ^ set(quiet_nobl)
debug_bls = [x for x in debug if x.startswith("module_blacklist=")][0].split("=", 1)[1].split(",")
quiet_bls = [x for x in quiet if x.startswith("module_blacklist=")][0].split("=", 1)[1].split(",")
assert quiet_bls == debug_bls + [m for m in PROF["blacklist"]["quiet_add"] if m not in debug_bls and m not in released], (debug_bls, quiet_bls)
if AMD_HX370:
    assert "amdgpu" in quiet_bls and "amdgpu" not in debug_bls, (quiet_bls, debug_bls)
    assert not any(o.startswith("amdgpu.") for o in quiet), quiet
    assert {"amdgpu.modeset=1", "amdgpu.dc=1", "amdgpu.dcdebugmask=0x10"} <= set(debug), debug
SAFE = PROF["_safety"]
# EC_NEVER is part of the project-wide required blacklist, so every entry of every profile and variant blocks it
assert set(EC_NEVER) <= set(SAFE["blacklist"]["required"]), set(EC_NEVER) - set(SAFE["blacklist"]["required"])
# a profile for other machines must not carry a machine-specific MMIO console (it would write that address)
assert not GOP or not any(o.split("=", 1)[0] in ("console", "earlycon") and "mmio" in o for o in normal + debug + quiet)
for name, o in (("debug", debug), ("quiet", quiet)):
    n = devprofile.boot_cmdline_len(o)
    limit = devprofile.X86_BOOT_CMDLINE_MAX - devprofile.CMDLINE_HEADROOM
    slack = devprofile.X86_BOOT_CMDLINE_MAX - n
    print(f"{name} command line length {n}; kernel max {devprofile.X86_BOOT_CMDLINE_MAX}; "
          f"headroom {devprofile.CMDLINE_HEADROOM}; limit {limit}; slack {slack}")
    assert devprofile.cmdline_within_limit(n), name
    bl = [x for x in o if x.startswith("module_blacklist=")]; assert len(bl) == 1
    bls = bl[0].split("=", 1)[1].split(",")
    for m in SAFE["blacklist"]["required"]:
        assert m in bls or m in released, (name, m)
    assert set(EC_NEVER) <= set(bls), (name, set(EC_NEVER) - set(bls))
    assert not EC_FEATURES or (not released & set(bls) and set(EC_EXTRA_BLOCK) <= set(bls)), name
    for m in PROF["blacklist"]["allow"]:
        assert m not in bls, (name, m)
    if name == "quiet":
        assert set(PROF["blacklist"]["quiet_add"]) <= set(bls), (name, PROF["blacklist"]["quiet_add"])
    assert "efi=noruntime" in o and not any(x.startswith("spi-intel.") for x in o), name
    assert [x for x in o if x.startswith("efi=")] == ["efi=noruntime"], name
(OUT / "lapis-quiet.conf").write_text(entry(PROF["entries"]["quiet"], quiet))
(OUT / "lapis-debug.conf").write_text(entry(PROF["entries"]["debug"], debug))
# editor no (default): the menu cannot edit an entry's options, so the blacklist cannot be removed by a stray key;
# auto-firmware no: no "Reboot Into Firmware" menu item (it sets the OsIndications EFI variable)
(OUT / "loader.conf").write_text(f"default {DEFAULT_ENTRY}\ntimeout 5\neditor {EDITOR}\nauto-firmware no\n")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "initrd.img", "::/initrd.img")
# the base ESP's own lapis.conf (Google's options, without the safety blacklist) is deleted: only the two entries built
# here can be started. The former "plain" entry (no pci-stub) left the KANO panel dark while Android ran (its serial log
# stops when the LPSS UART is taken over; docs/JOURNAL.md, research/kano-compat/PLAN.md 2.5-E), so it is not built any more
run("mdel", "-i", f"{IMG}{ESP}", "::/loader/entries/lapis.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "lapis-debug.conf", "::/loader/entries/lapis-debug.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "lapis-quiet.conf", "::/loader/entries/lapis-quiet.conf")
run("mcopy", "-o", "-i", f"{IMG}{ESP}", OUT / "loader.conf", "::/loader/loader.conf")
for f in ENTRIES:
    assert mtype(IMG, f"::/loader/entries/{f}") == (OUT / f).read_text(), f
listing = subprocess.run(["mdir", "-b", "-i", f"{IMG}{ESP}", "::/loader/entries"], check=True, capture_output=True,
                         text=True).stdout.split("\n")
present = sorted(l.strip().replace("\r", "").rsplit("/", 1)[-1] for l in listing if l.strip() and not l.strip().endswith("/"))
assert present == sorted(ENTRIES), f"ESP loader/entries holds {present}, expected exactly {sorted(ENTRIES)}"
assert mtype(IMG, "::/loader/loader.conf") == (OUT / "loader.conf").read_text()
run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/initrd.img", OUT / "readback-initrd.img")
assert (OUT / "readback-initrd.img").read_bytes() == (OUT / "initrd.img").read_bytes()
def install_checked_kernel(prof, kernel_dir, debug_opts, quiet_opts, read_bzimage, copy_kernel, read_shipped):
    """GBOS_DEV_KERNEL_DIR checks in this file, then the ESP copy of kernel.efi.

    The build calls this. Tests call it with stub readers: dropping the call, skipping
    either SHA-256 check, or turning the length check into a constant fails those tests.
    read_bzimage() is the bzImage after check_kconfig. copy_kernel(path) writes the ESP.
    read_shipped() is the kernel.efi readback. debug_opts is the serial entry.
    """
    checked = devprofile.check_kconfig(prof, kernel_dir)
    dev_bz = Path(kernel_dir) / "bzImage"
    kb = read_bzimage()
    try:
        devprofile.require_same_bzimage(
            kb, checked.bzimage_sha256, f"{dev_bz} changed after check-kconfig read it"
        )
    except devprofile.ProfileError as e:
        sys.exit(str(e))
    assert kb[:2] == b"MZ" and kb[0x202:0x206] == b"HdrS", f"{dev_bz} is not an EFI-stub bzImage"
    # arch/x86/kernel/setup.c strlcat's the builtin CONFIG_CMDLINE in front of this
    # line. The 2000-character check above does not see that prefix.
    builtin_cmd = devprofile.builtin_cmdline_text(checked)
    for name, o in (("serial", debug_opts), ("quiet", quiet_opts)):
        n = len(" ".join(o)) + len("initrd=\\initrd.img ")
        assert devprofile.x86_cmdline_fits(n, builtin_cmd), (
            f"{name} command line ({n}) plus builtin CONFIG_CMDLINE ({len(builtin_cmd)}) "
            f"does not fit in x86 COMMAND_LINE_SIZE ({devprofile.X86_COMMAND_LINE_SIZE})"
        )
    copy_kernel(dev_bz)
    shipped = read_shipped()
    try:
        devprofile.require_same_bzimage(
            shipped, checked.bzimage_sha256, "kernel.efi is not the bzImage check-kconfig verified"
        )
    except devprofile.ProfileError as e:
        sys.exit(str(e))
    return checked


def _extract_vendor_gralloc(img, work):
    """Bytes of lib64/libminigbm_gralloc.so inside the image's vendor_a."""
    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    ota = Path(os.environ["GBOS_OTATOOLS"])
    run(sys.executable, str(Path(__file__).resolve().parent / "lib/lp-dump.py"), img, work, "vendor_a")
    raw = (work / "vendor_a.img").read_bytes()
    idx = raw.rfind(b"AVBf")
    if idx < 0:
        sys.exit("vendor_a has no AVB footer")
    orig = struct.unpack_from(">Q", raw, idx + 12)[0]
    erofs = work / "vendor.erofs"
    erofs.write_bytes(raw[:orig])
    tree = work / "tree"
    tree.mkdir()
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(ota / "lib64")
    subprocess.run(
        [str(ota / "bin/fsck.erofs"), f"--extract={tree}", "--overwrite", str(erofs)],
        check=True, env=env)
    lib = tree / "lib64/libminigbm_gralloc.so"
    if not lib.is_file() or lib.is_symlink():
        sys.exit("image vendor libminigbm_gralloc.so missing")
    return lib.read_bytes()

def readback_gralloc(img, prof, work, extract):
    """Refuse when the vendor gralloc inside the image is not the profile pin."""
    import gralloc
    g = prof.get("gralloc")
    if not g:
        return None
    try:
        blob = extract(img, work)
        err = gralloc.readback_mismatch(blob, g["sha256"])
    finally:
        if Path(work).exists():
            shutil.rmtree(work, ignore_errors=True)
    if err:
        sys.exit(err)
    print("gralloc readback", g["sha256"])
    return g["sha256"]

DEV_KERNEL_DIR = os.environ.get("GBOS_DEV_KERNEL_DIR", "")
if DEV_KERNEL_DIR:
    def _read_checked_bz():
        return (Path(DEV_KERNEL_DIR) / "bzImage").read_bytes()

    def _copy_kernel(dev_bz):
        run("mcopy", "-o", "-i", f"{IMG}{ESP}", dev_bz, "::/kernel.efi")

    def _read_shipped_kernel():
        run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/kernel.efi", OUT / "kernel.efi")
        return (OUT / "kernel.efi").read_bytes()

    install_checked_kernel(
        PROF, DEV_KERNEL_DIR, debug, quiet, _read_checked_bz, _copy_kernel, _read_shipped_kernel
    )
else:
    run("mcopy", "-o", "-i", f"{IMG}{ESP}", "::/kernel.efi", OUT / "kernel.efi")
if PROF.get("gralloc"):
    readback_gralloc(IMG, PROF, OUT / "gralloc-readback", _extract_vendor_gralloc)
print("ESP free:", subprocess.run(["mdir", "-i", f"{IMG}{ESP}", "::/"], capture_output=True, text=True).stdout.strip().splitlines()[-1])

# QEMU runs: image on USB (writes go to a throw-away snapshot), blank NVMe attached, until QEMU_SECONDS have passed.
# QEMU still running at the end means the guest neither crashed nor powered off; disk statistics come over QMP.
blank = OUT / "blank-nvme.img"
with open(blank, "wb") as fh: fh.truncate(1 << 30)
def qmp_query(path, *cmds):
    s = socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(str(path)); f = s.makefile("rwb")
    def call(cmd):
        cmd, args = cmd if isinstance(cmd, tuple) else (cmd, None)
        f.write(json.dumps({"execute": cmd, **({"arguments": args} if args else {})}).encode() + b"\n"); f.flush()
        while True:
            r = json.loads(f.readline())
            if "return" in r or "error" in r: return r
    json.loads(f.readline()); call("qmp_capabilities")
    res = [call(c) for c in cmds]; f.close(); s.close(); return res   # QMP takes one client at a time
QMP_DIR = Path(tempfile.mkdtemp(prefix="gbos-qmp-"))   # unix socket paths are limited to about 108 bytes
def ac_ssdt(path):
    """An SSDT with one ACPI AC adapter, \\_SB.AC (ACPI0003, _PSR = 1: on line), for the functional test of ac.ko;
    QEMU's own ACPI tables have no AC device."""
    def pkg(body):   # PkgLength of a body shorter than 4096 bytes, counting the length bytes themselves
        n = len(body) + 1
        if n < 64: return bytes([n]) + body
        n += 1; assert n < 4096
        return bytes([0x40 | (n & 0xF), n >> 4]) + body
    hid = b"\x08_HID\x0dACPI0003\x00"                       # Name (_HID, "ACPI0003")
    psr = b"\x14" + pkg(b"_PSR\x00\xa4\x01")                  # Method (_PSR) { Return (One) }
    sta = b"\x14" + pkg(b"_STA\x00\xa4\x0a\x0f")              # Method (_STA) { Return (0x0F) }
    dev = b"\x5b\x82" + pkg(b"AC__" + hid + psr + sta)       # Device (AC)
    body = b"\x10" + pkg(b"\\_SB_" + dev)                    # Scope (\_SB)
    hdr = bytearray(b"SSDT" + struct.pack("<IBB", 36 + len(body), 2, 0) + b"GBOS  " + b"KANOAC  " + struct.pack("<I", 1)
                    + b"GBOS" + struct.pack("<I", 1))
    t = bytearray(hdr + body); t[9] = (-sum(t)) & 0xFF
    Path(path).write_bytes(bytes(t))
def qemu(tag, extra):
    serial, sock, vars_fd = OUT / f"{tag}-serial.log", QMP_DIR / f"{tag}.qmp", OUT / f"{tag}-vars.fd"
    for p in (serial, sock): p.unlink(missing_ok=True)
    shutil.copy(OVMF_VARS, vars_fd)
    cmd = [QEMU, "-machine", "q35,accel=kvm", "-cpu", QEMU_CPU, "-m", "8192",
           "-smp", "4", "-no-reboot", "-vga", "none", "-display", "none",
           # GOP framebuffer (OVMF drives bochs-display; romfile= because no VGA BIOS is needed)
           *(["-device", "bochs-display,id=gfx,romfile=,bus=pcie.0,addr=0x5"] if GOP or NATIVE_GOP_FALLBACK else []),
           "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
           "-drive", f"if=pflash,format=raw,file={vars_fd}",
           "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x3",
           "-drive", f"file={IMG},if=none,id=usbdisk,format=raw,snapshot=on",
           "-device", "usb-storage,bus=xhci.0,drive=usbdisk,bootindex=1",
           "-drive", f"file={blank},if=none,id=nv,format=raw", "-device", "nvme,drive=nv,serial=BLANKNVME,bus=pcie.0,addr=0x2",
           # The desktop audio HAL waits for its internal ALSA card before
           # AudioPolicy and boot completion. Match the emulator's HDA device.
           *(["-audiodev", "none,id=selftest_audio", "-device",
              "ich9-intel-hda,id=selftest_hda,bus=pcie.0,addr=0x1b", "-device",
              "hda-duplex,bus=selftest_hda.0,audiodev=selftest_audio,mixer=off"] if GENERIC else []),
           "-serial", f"file:{serial}", "-monitor", "none", "-qmp", f"unix:{sock},server=on,wait=off"] + extra
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if QEMU_LIB: env["LD_LIBRARY_PATH"] = QEMU_LIB
    print("+", " ".join(map(str, cmd)), flush=True)
    p = subprocess.Popen(list(map(str, cmd)), env=env)
    end = time.time() + QEMU_SECONDS
    while time.time() < end and p.poll() is None: time.sleep(2)
    alive, stats = p.poll() is None, None
    if alive:
        try:
            # GOP framebuffer: capture the screen first, on the same QMP connection
            shot = [("screendump", {"filename": str(OUT / f"{tag}-screen.png"), "format": "png", "device": "gfx"})] if GOP else []
            res = qmp_query(sock, *shot, "query-blockstats")
            if GOP: print(tag, "screendump", res[0])
            r = res[-1]["return"]
            stats = next(d["stats"] for d in r if d.get("device") == "usbdisk" or d.get("qdev", "").endswith("usbdisk"))
        except Exception as e:
            print(tag, "QMP query failed:", e)
        p.terminate()
        try: p.wait(30)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
    print(tag, "QEMU", "ran the full", QEMU_SECONDS, "s" if alive else f"exited early with {p.returncode}", "usbdisk", stats)
    log = serial.read_text(errors="replace") if serial.exists() else ""
    return alive, stats, log

before = sha256(IMG)
checks = {}
if SKIP_SELFTEST:
    print("SKIP QEMU self-tests (GBOS_SKIP_SELFTEST=1): this image is NOT self-tested")
    after = before
    tmp_sum = OUT / (FINAL.name + ".sha256.partial")
    tmp_sum.write_text(f"{after}  {FINAL.name}\n")
    (OUT / (FINAL.name + ".not-self-tested")).write_text("built with GBOS_SKIP_SELFTEST=1: never booted in QEMU before release\n")
    tmp_sum.replace(OUT / (FINAL.name + ".sha256"))
    IMG.replace(FINAL)
    shutil.rmtree(QMP_DIR, ignore_errors=True)
    print(FINAL.name, "sha256", after)
    print("IMAGE OK (NOT SELF-TESTED)", FINAL)
    sys.exit(0)
# 1. functional: for amd-hx370, boot the SAFE default entry (quiet: amdgpu blacklisted, no amdgpu.*, gbos_efifb +
# SwiftShader) with COM1 serial logging enabled; for other profiles, boot the debug entry's options with COM1 console.
# QEMU has no KANO MMIO UART (0xfe03e000), so console= and earlycon= are removed and replaced with COM1
# (console=ttyS0,115200n8 and earlycon=uart8250,io,0x3f8). This checks Android. It does not
# show that the KANO AP UART still prints. On hardware, serial-debug keeps earlycon plus
# keep_bootcon (printk on the bootconsole, not an interactive tty).
if AMD_HX370:
    q = ([("loglevel=8" if o == "loglevel=4" else o) for o in quiet]
         + ["ignore_loglevel", "keep_bootcon", "printk.devkmsg=on",
            "console=ttyS0,115200n8", "earlycon=uart8250,io,0x3f8,115200n8"])
else:
    q = [o for o in debug if not o.startswith(("console=", "earlycon="))] + ["console=ttyS0,115200n8", "earlycon=uart8250,io,0x3f8,115200n8"]
def ec_ssdt():
    """GBOS_EC self-test only: an SSDT with the KANO's EC ACPI devices (\\_SB.CREC = GOOG0004 with GOOG0014, GOOG0007 and
    GOOG0003 below it) and no EC behind them, so ueventd loads the released drivers under this kernel and they must
    give up cleanly. Plain AML, assembled here (no iasl)."""
    def pkg(c):
        if len(c) + 1 <= 0x3F: return bytes([len(c) + 1]) + c
        n = len(c) + 2; assert n < 1 << 12
        return bytes([0x40 | (n & 0xF), n >> 4]) + c
    def hid(h): return b"\x08_HID\x0d" + h.encode() + b"\x00"
    def dev(seg, body): return b"\x5b\x82" + pkg(seg + body)
    crec = dev(b"CREC", hid("GOOG0004") + b"\x08_UID\x01" + dev(b"TYPC", hid("GOOG0014")) + dev(b"CKSC", hid("GOOG0007"))
               + dev(b"USBC", hid("GOOG0003")))
    aml = b"\x10" + pkg(b"\\_SB_" + crec)
    t = bytearray(struct.pack("<4sIBB6s8sI4sI", b"SSDT", 36 + len(aml), 2, 0, b"GBOS  ", b"ECVARNT ", 1, b"GBOS", 1) + aml)
    t[9] = -sum(t) & 0xFF
    return bytes(t)
extra_acpi = []
if KMOD_AC:
    ac_ssdt(OUT / "ac-ssdt.aml"); extra_acpi = ["-acpitable", f"file={OUT / 'ac-ssdt.aml'}"]
if EC_FEATURES:
    (OUT / "ec-ssdt.aml").write_bytes(ec_ssdt()); extra_acpi += ["-acpitable", f"file={OUT / 'ec-ssdt.aml'}"]
alive, _stats, log = qemu("functional", ["-kernel", OUT / "kernel.efi", "-initrd", OUT / "initrd.img", "-append", " ".join(q)] + extra_acpi)
has = lambda p: re.search(p, log) is not None
checks.update({
    "QEMU still running at the end (functional)": alive,
    "init second stage": has(r"init second stage started"),
    "no vbmeta mismatch": not has(r"(?i)vbmeta digest (error|mismatch)"),
    "no nvme controller": not has(r"nvme nvme\d"),
    "system_server running": has(r"u:r:system_server:s0"),
    "keymint HAL present": has(r"IKeyMintDevice/default"),
    "fwupd never started": not has(r"starting service 'fwupd"),
    "no rollforward": not has(r"kernel_max_rollforward"),
    "no gsmi": not has(r"gsmi:"),
    "no kernel panic": not has(r"Kernel panic"),
    "first stage did not load the profile's dropped modules (KANO: LPSS UART)": not DROP or not has(r"Loading module /lib/modules/(" + "|".join(re.escape(x[:-3]) for x in sorted(DROP)) + r")\.ko"),
    "split policy compiled (no failure)": has(r"No precompiled sepolicy at /vendor") and not has(r"(?i)(failed to compile|could not load selinux policy|secilc: (?!.*linker: )[^\n]*(error|failed))"),
    "kano-logcat on serial": has(r"kano-logcat: "),
    "data mounted as f2fs": has(r"target=/data,type=f2fs\)=0"),
    "IWeaver not declared": not has(r"Found android.hardware.weaver.IWeaver/default in device VINTF manifest"),
    "kano_bootfix ran": has(r"Service .kano_bootfix. .*exited with status 0"),
    "kano_diag started": has(r"starting service .kano_diag."),
    "Wi-Fi modules of the profile modprobed (KANO: iwlmvm)": all(has(r"modprobe --dirname=/vendor/lib/modules " + re.escape(m)) for m in PROF["wifi"]["modprobe"]),
    "first stage loads pci-stub (if the profile uses it)": not STUB or has(r"Loading module /lib/modules/pci-stub.ko"),
    **({"no EC transport/core/chardev module loaded": not has(r"Loaded kernel module \S*/(cros_ec|cros_ec_lpcs|cros_ec_dev|cros_ec_chardev|cros_ec_sysfs|cros_ec_vbc|cros-ec-typec|cros_typec_switch)\.ko")}
       if not EC_FEATURES else {
       # GBOS_EC: the SSDT above declares the EC's ACPI devices with no EC behind them; the released drivers are loaded
       # by ueventd and must give up cleanly (the boot checks above still pass); the EC channels/writers never load
       "EC variant: no EC channel/writer module loaded": not has(r"Loaded kernel module \S*/(cros_ec_chardev|cros_ec_debugfs|cros_ec_sysfs|cros_ec_vbc|cros_ec_lightbar|i2c-cros-ec-tunnel|cros_charge-control|leds-cros_ec|cros_ec_spi|cros_ec_i2c|cros_ec_ishtp|cros-ec-usb|cros_peripheral_charger|cros_usbpd-charger|cros_usbpd_logger|cros_typec_switch|cros_ec_ucsi)\.ko"),
       "EC variant: cros_ec_lpcs loaded, probed GOOG0004, gave up (no EC in QEMU)": has(r"cros_ec_lpcs GOOG0004:00: EC ID not detected"),
       "EC variant: cros-ec-typec loaded, waits for the EC": has(r"cros-ec-typec GOOG0014:00: couldn't find parent EC device") if "typec" in EC_FEATURES else True,
       "EC variant: no /dev/cros_ec": not has(r"/dev/cros_(ec|fp|ish)\b")}),
    "kano audiotest rc parsed": has(r"init: Parsing file /vendor/etc/init/kano-audiotest.rc"),
    "kano-services rc parsed": has(r"init: Parsing file /vendor/etc/init/kano-services.rc"),
    "no vendor rc rejections": not has(r"(?i)init: .*(kano-services|kano-audiotest|kano-hw|kano-diag|kano-adb|kano-autooff|gbos-offline-setup|amd-hx370-graphics).*(error|invalid|failed to parse|not allowed)"),
})
if os.environ.get("GBOS_SKIP_SETUP") == "1":
    checks["offline setup rc parsed"] = has(r"init: Parsing file /vendor/etc/init/gbos-offline-setup.rc")
if GOP:
    checks.update({
        "generic: GOP framebuffer handed to gbos_efifb": has(r"gbos-efifb: GOP \d+x\d+ depth 32"),
        "generic: gbos_efifb registered as vkms": has(r"gbos_efifb: \d+x\d+ stride \d+ at 0x[0-9a-f]+ \((?:XRGB|XBGR)\), registered as vkms \(scanout\)"),
        "generic: no execmem denial": not has(r"avc: +denied +\{ execmem \}"),
        "generic: graphics allocator stays up": not has(r"Service 'vendor\.graphics\.allocator' \(pid \d+\) exited"),
        "generic: surfaceflinger not restarted": not has(r"Sending SIGKILL to service 'surfaceflinger'|Service 'surfaceflinger' \(pid \d+\) received SIG"),
        "generic: boot completed": has(r"sys\.boot_completed=1"),
        "generic: no DRM master failure": not has(r"Failed to set drm master|DRM/KMS master access required"),
    })
    # a uniform frame (black, or the firmware logo on black) compresses to a few KB; the setup wizard is over 40 KB
    shot = OUT / "functional-screen.png"
    checks["generic: non-blank GOP screendump (PNG over 20 KB)"] = shot.exists() and shot.stat().st_size > 20000
if AMD_HX370:
    checks.update({
        "amd-hx370 safe: graphics helper selected safe entry": has(r"amd-hx370: safe entry: ro\.hardware\.vulkan=pastel hwui=false renderengine=false"),
        "amd-hx370 safe: amdgpu not modprobed": not has(r"modprobe [^\n]*\bamdgpu\b"),
        "amd-hx370 safe: amdgpu not in /proc/modules": not has(r"gbos-dev-modules: [^\n]*\bamdgpu\b"),
        "amd-hx370 radv-probe: vkprop redirected ro.hardware.vulkan": has(r"vkprop: ro\.hardware\.vulkan -> vendor\.gbos\.radv_probe"),
        "amd-hx370 radv-probe: vulkan.radeon.so mapped in process": has(r"radv-probe: mapped [^\n]*/vendor/lib64/hw/vulkan\.radeon\.so"),
        "amd-hx370 radv-probe: vkCreateInstance succeeded": has(r"radv-probe: vkCreateInstance -> 0\b"),
        "amd-hx370 radv-probe: vkEnumeratePhysicalDevices succeeded (0 AMD GPUs in QEMU)": has(r"radv-probe: vkEnumeratePhysicalDevices -> 0 count=0\b"),
    })
if DEV_KERNEL_DIR:
    checks["dev-kernel: self-built Linux version"] = has(r"Linux version 6\.18\.21-android17-5\S* \(gbos@gbos\)")
    checks["dev-kernel: rust_binder loaded"] = has(r"rust_binder: Loaded Rust Binder\.")
    checks["dev-kernel: no module symbol/CRC/protection errors"] = not has(r"Cannot use protected symbol|disagrees about version of symbol|Unknown symbol in module")
    if AMD_HX370:
        checks["dev-kernel: gbos_efifb live in /proc/modules on safe entry"] = has(r"gbos-dev-modules: .*\bgbos_efifb\b")
        checks["dev-kernel: simpledrm not modprobed or live in /proc/modules on amd-hx370"] = (
            not has(r"modprobe [^\n]*\bsimpledrm\b") and not has(r"gbos-dev-modules: [^\n]*\bsimpledrm\b")
        )
    elif (Path(DEV_KERNEL_DIR) / "modules/amdgpu.ko").is_file():
        checks["dev-kernel: amdgpu.ko modprobe ran"] = has(r"Service 'exec \d+ \(/vendor/bin/modprobe --dirname=/vendor/lib/dev-modules amdgpu\)' \(pid \d+\) exited with status 0")
        checks["dev-kernel: amdgpu + audio + simpledrm live in /proc/modules"] = (
            has(r"gbos-dev-modules: .*\bamdgpu\b")
            and has(r"gbos-dev-modules: .*\bsnd_sof_amd_acp70\b")
            and has(r"gbos-dev-modules: .*\bsimpledrm\b")
        )
if KMOD_AC:
    checks.update({
        "ac.ko modprobe ran (profile kmods: ac)": has(r"Service 'exec \d+ \(/vendor/bin/modprobe --dirname=/vendor/lib/kano ac\)' \(pid \d+\) exited with status 0"),
        "AC adapter bound, on line": has(r"AC Adapter \[\w+\] \(on-line\)"),
        "healthd sees the charger (chg=a)": has(r"healthd: battery .*chg=a"),
        "no SELinux denial on the AC power supply": not has(r'avc:  denied .*(power_supply|ACPI0003)'),
    })
else:
    checks["no ac.ko (profile has no kmods: ac)"] = not has(r"modprobe --dirname=/vendor/lib/kano ac\)")
if KMOD_WAKEFILTER:
    checks.update({
        "gbos_wakefilter modprobe ran (profile kmods: gbos_wakefilter)": has(r"Service 'exec \d+ \(/vendor/bin/modprobe --dirname=/vendor/lib/kano gbos_wakefilter\)' \(pid \d+\) exited with status 0"),
        "gbos_wakefilter watches the ACPI Power Button": has(r"gbos_wakefilter: watching Power Button \(LNXPWRBN/button/input0\)"),
    })
else:
    checks["no gbos_wakefilter (profile has no kmods: gbos_wakefilter)"] = not has(r"gbos_wakefilter")
if ADB:
    checks["kano-adb rc parsed (GBOS_ADB_PUBKEY set)"] = has(r"init: Parsing file /vendor/etc/init/kano-adb.rc")
else:
    checks["no kano-adb (GBOS_ADB_PUBKEY not set)"] = not has(r"kano-adb|kano_adb")
def autooff_log_checks(log, enabled):
    """QEMU serial checks for GBOS_TEST_AUTOOFF. enabled is the env value; empty means the service is absent."""
    found = lambda p: re.search(p, log) is not None
    if enabled:
        return {
            "kano-autooff rc parsed (GBOS_TEST_AUTOOFF)": found(r"init: Parsing file /vendor/etc/init/kano-autooff\.rc"),
            "kano-autooff refuses QEMU (no /data nonce, staying on)": found(r"KANO-AUTOOFF: nonce gate closed, staying on"),
            "kano-autooff did not power off (no /data nonce)": not found(r"KANO-AUTOOFF: powering off"),
            "kano_autooff exited 0": found(r"Service .kano_autooff. .*exited with status 0"),
        }
    return {"no kano-autooff (GBOS_TEST_AUTOOFF not set)": not found(r"kano-autooff|kano_autooff|KANO-AUTOOFF")}
checks.update(autooff_log_checks(log, TEST_AUTOOFF))

# 2. boot chain: no -kernel, so OVMF boots the image's own ESP and systemd-boot starts the default
# entry with the options stored on that ESP (nothing is rewritten to COM1). Ran only when the
# default is quiet: quiet has no earlycon, and disk writes show Android reached /data. The debug
# entry's earlycon is the KANO MMIO UART, which QEMU does not have, so this test is not a hardware
# UART log of the debug entry.
if DEFAULT_ENTRY == "lapis-quiet.conf":
    alive, stats, clog = qemu("chain", [])
    wrote = (stats or {}).get("wr_bytes", 0); top = (stats or {}).get("wr_highest_offset", 0)
    checks["QEMU still running at the end (chain)"] = alive
    checks["chain: default entry keeps the serial console silent"] = "Linux version" not in clog and "Boot in" in clog
    if GOP:
        cshot = OUT / "chain-screen.png"
        checks["chain (generic): non-blank GOP screendump (PNG over 20 KB)"] = cshot.exists() and cshot.stat().st_size > 20000
    checks["chain: Android wrote /data (>= 16 MiB, into userdata)"] = wrote >= (16 << 20) and USERDATA["start"] <= top <= USERDATA["end"]
    print("chain: usbdisk wr_bytes", wrote, "wr_highest_offset", top, "userdata", USERDATA["start"], "-", USERDATA["end"])
    template = Path(OVMF_VARS).read_bytes(); after_vars = (OUT / "chain-vars.fd").read_bytes()
    print("chain: OVMF variable store bytes changed in QEMU (firmware boot entries; info only):",
          sum(a != b for a, b in zip(template, after_vars)))
else:
    print(f"SKIP boot chain test: default entry {DEFAULT_ENTRY} sends earlycon to the KANO's LPSS UART, which QEMU lacks")

after = sha256(IMG)
checks["image unchanged by the QEMU runs"] = before == after
for k, v in checks.items(): print("PASS" if v else "FAIL", k)
if not all(checks.values()):
    print(f"image left at {IMG} (not renamed: a check failed)")
    shutil.rmtree(QMP_DIR, ignore_errors=True)
    sys.exit(1)
# checksum first, then the image: a name that looks finished always comes with its checksum
tmp_sum = OUT / (FINAL.name + ".sha256.partial")
tmp_sum.write_text(f"{after}  {FINAL.name}\n")
tmp_sum.replace(OUT / (FINAL.name + ".sha256"))
IMG.replace(FINAL)
shutil.rmtree(QMP_DIR, ignore_errors=True)
print(FINAL.name, "sha256", after)
print("IMAGE OK", FINAL)
