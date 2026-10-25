#!/bin/bash
# Stage 1 of the Googlebook build: rebuild the vendor partition of a lapis base image with the project's changes and the
# device profile's (GBOS_PROFILE: firmware list and profile.toml values, model-specific edits in its hooks.vendor file;
# see docs/HOW-IT-WORKS.md and docs/NEW-DEVICE.md), re-sign vbmeta with your AVB key and refresh the ESP initrd bootconfig.
# History of the individual changes (safe2 ... safe25, all verified on the real KANO):
# Rebuild kano-safe25.img: safe24 + ACPI Power Button no longer excluded (KANO has no cros_ec_buttons); safe24 = safe23 + light on-disk diag (adb is the live channel); safe23 = safe22 + adbd on tcp:5555 (system pins localhost), udsattestation stopped; safe22 = safe21 + key-only adb over TCP 5555 for the harness (the builder's key), fsync'd heavy diag files; safe21 = safe20 + stop timberslide/gscd (EC/TPM crash loops), 5-min system_server watchdog setting, audio self-test only on request, battery/bluetooth/camera/sensors/power in the diag; safe20 = safe19 + one-shot audio self-test (dras_tool tone + mic), diag without global sync (small files via dd conv=fsync), audio dumpsys pipes; safe19 = safe18 + DRAS audio config for the KANO card (sof-nau8825, from ChromeOS kano UCM), keyboard backlight LED path for the KANO (idc, ueventd, genfscon), serial logcat only on the serial-debug entry, lighter diag (60 s, io-top, audio/input dumps, reader timeouts); safe18 = safe17 + cmd/dumpsys fd/pipe access for the kano debug domain, timeouts and display/backlight reads in kano-diag, Wi-Fi join via pipes; safe16 without the firmware updaters/flash tools (fwupd + cabs, flashrom, futility, ectool, crossystem, ap-ec-fw.zip); safe15 + KANO firmware (AX211 Wi-Fi, AX211 Bluetooth, SOF IPC3 max98373+nau8825), iwlmvm load, Wi-Fi autoconnect from /data/kano-wifi.conf; safe13 + kano_diag (dmesg and logcat snapshots in /data/kano-diag every 20 s, for boots where the serial console dies); safe12 + kano_bootfix (sets persist.vold.dont_commit_checkpoint=1 every boot, so a fresh /data does not fall back into the 'Checkpoint commit failed' loop); safe11 without the IWeaver/ISecretkeeper VINTF declarations (their HALs need the protected VM, so LockSettings waited forever and the watchdog killed system_server); safe10 with /data on f2fs (4K fsync on the Kingston: f2fs median 4 ms vs ext4 p90 18 s) and without the rejected cross-treble overrides; safe9 + stop the GSC/EC/Trusty crash loops (each crash writes a tombstone to the slow stick and system_server fsyncs stall past the watchdog) + quieter serial logcat; safe8 + no cppreopts (system_server watchdog loop on the slow stick) + serial logcat (permissive kano_kmsg_logcat domain, on-device policy compile); safe7 + userdata without checkpoint=block (no dm-bow rollback after power cuts); experiment A (colour pipeline off) + drm_hwcomposer client backend (no tiled buffer on a plane; xe faults on DPT scanout) + unencrypted userdata, no USB voldmanaged, ro.debuggable=1 (serial console).
# Vendor drops fwupd's on-boot starts and desktop_kernel_max_rollforward_boot.rc.
# Other logical partitions stay byte-identical. vbmeta is re-signed.
# ESP initrd keeps the current archive and takes the standalone bootconfig,
# with vbmeta digest, size, and boot_a UUID filled in.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${GBOS_BASE:?GBOS_BASE = base image (output of base/build-base.sh)}"
: "${GBOS_INITRD:?GBOS_INITRD = standalone initrd with bootconfig (output of base/build-base.sh)}"
: "${GBOS_OUT:?GBOS_OUT = work/output directory}"
: "${GBOS_OTATOOLS:?GBOS_OTATOOLS = AOSP otatools directory (bin/avbtool, bin/mkfs.erofs, bin/fsck.erofs, lib64)}"
: "${GBOS_AVB_KEY:?GBOS_AVB_KEY = your RSA-4096 AVB private key (PEM)}"
: "${GBOS_PROFILE:?GBOS_PROFILE = device profile directory, e.g. profiles/kano}"
REAL="$GBOS_BASE"
STANDALONE="$GBOS_INITRD"
WORK="$GBOS_OUT/vendor"
IMG="$WORK/stage-vendor.img"
TMP="$WORK/tmp"
OT="$GBOS_OTATOOLS"
KEY="$GBOS_AVB_KEY"
FW="${GBOS_FIRMWARE:?GBOS_FIRMWARE = firmware directory (builder/fetch-inputs.sh <dir> fills <dir>/firmware)}"
export GBOS_AUDIO_SRC="${GBOS_AUDIO_SRC:?GBOS_AUDIO_SRC = audio sources of the profile (builder/fetch-inputs.sh <dir> fills <dir>/audio-src)}"
export GBOS_PROFILE GBOS_ADB_PUBKEY="${GBOS_ADB_PUBKEY:-}"
# GBOS_EC (opt-in EC features, see builder/image.py): a comma list of sensors, typec, buttons; empty = none
export GBOS_EC="${GBOS_EC:-}"
read -r VENDOR_UUID VENDOR_FINGERPRINT VENDOR_SECURITY_PATCH VBMETA_ROLLBACK_INDEX < <(python3 "$HERE/lib/lapis.py")
export VENDOR_UUID
export GBOS_SKIP_SETUP="${GBOS_SKIP_SETUP:-0}"
[[ "$GBOS_SKIP_SETUP" = 0 || "$GBOS_SKIP_SETUP" = 1 ]] \
    || { echo "GBOS_SKIP_SETUP must be 0 or 1"; exit 2; }
[[ "$GBOS_EC" =~ ^((sensors|typec|buttons)(,(sensors|typec|buttons))*)?$ ]] \
    || { echo "GBOS_EC must be empty or a comma list of sensors, typec, buttons, not $GBOS_EC"; exit 2; }
# GBOS_TEST_AUTOOFF (developer-only; install/install.sh refuses it): empty = off; else seconds 60..7200.
# Canonical decimal, no leading zeros. Check the text before any arithmetic: bash wraps 2^64+60 to 60.
if [ -n "${GBOS_TEST_AUTOOFF:-}" ]; then
    [[ "$GBOS_TEST_AUTOOFF" =~ ^[1-9][0-9]{1,3}$ ]] \
        || { echo "GBOS_TEST_AUTOOFF must be empty or an integer 60..7200, not $GBOS_TEST_AUTOOFF"; exit 2; }
    [ "$GBOS_TEST_AUTOOFF" -ge 60 ] && [ "$GBOS_TEST_AUTOOFF" -le 7200 ] \
        || { echo "GBOS_TEST_AUTOOFF must be empty or an integer 60..7200, not $GBOS_TEST_AUTOOFF"; exit 2; }
    export GBOS_TEST_AUTOOFF
fi
export GBOS_DEV_KERNEL_DIR="${GBOS_DEV_KERNEL_DIR:-}"
if [ -n "$GBOS_DEV_KERNEL_DIR" ]; then
    [ -f "$GBOS_DEV_KERNEL_DIR/bzImage" ] || { echo "GBOS_DEV_KERNEL_DIR must have bzImage, not $GBOS_DEV_KERNEL_DIR"; exit 2; }
    [ -d "$GBOS_DEV_KERNEL_DIR/modules" ] && [ -f "$GBOS_DEV_KERNEL_DIR/modules/modules.dep" ] \
        && compgen -G "$GBOS_DEV_KERNEL_DIR/modules/*.ko" > /dev/null \
        || { echo "GBOS_DEV_KERNEL_DIR must have modules/*.ko and modules/modules.dep, not $GBOS_DEV_KERNEL_DIR"; exit 2; }
fi
if [ -n "$GBOS_ADB_PUBKEY" ]; then
    # an adb public key is exactly one line "<base64 of the 524-byte RSAPublicKey> [user@host]"; anything else (a private
    # key, a second line, control characters) never goes into the image
    python3 - "$GBOS_ADB_PUBKEY" << 'PY' || { echo "GBOS_ADB_PUBKEY must be an adb public key file (~/.android/adbkey.pub), not $GBOS_ADB_PUBKEY"; exit 2; }
import base64, binascii, re, struct, sys
try:
    raw = open(sys.argv[1], "rb").read()
    text = raw.decode("ascii")
except (OSError, UnicodeDecodeError):
    sys.exit(1)
if len(raw) > 2048 or not re.fullmatch(r"[A-Za-z0-9+/=]+( [\x21-\x7e]+)?\n?", text):
    sys.exit(1)
try:
    key = base64.b64decode(text.split()[0], validate=True)
except (binascii.Error, ValueError):
    sys.exit(1)
# RSAPublicKey: u32 words (64), u32 n0inv, u32 n[64], u32 rr[64], u32 exponent
if len(key) != 524 or struct.unpack_from("<I", key, 0)[0] != 64 or struct.unpack_from("<I", key, 520)[0] not in (3, 65537):
    sys.exit(1)
PY
fi
python3 "$HERE/lib/devprofile.py" check "$GBOS_PROFILE" || exit 2
if [ -n "$GBOS_DEV_KERNEL_DIR" ]; then
    python3 "$HERE/lib/devprofile.py" check-kconfig "$GBOS_PROFILE" "$GBOS_DEV_KERNEL_DIR" || exit 2
fi
AUDIO_LIST=$(python3 "$HERE/lib/devprofile.py" get "$GBOS_PROFILE" audio.sources)
if [ -n "$AUDIO_LIST" ]; then
    grep -v '^#' "$GBOS_PROFILE/$AUDIO_LIST" | awk 'NF == 3 {print $3 "  " $1}' | ( cd "$GBOS_AUDIO_SRC" && sha256sum -c ) >/dev/null \
        || { echo "audio sources in $GBOS_AUDIO_SRC do not match $GBOS_PROFILE/$AUDIO_LIST"; exit 1; }
fi
SALT=b14f17c0007cfc878a5cdac13f0514ac3b7d22996d4db3d254082387c6705124

# otatools binaries get their own lib64 (avbtool runs fec from PATH and passes it on); python3, mtools, zstd etc. do not
ot() { local b=$1; shift; LD_LIBRARY_PATH="$OT/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$OT/bin/$b" "$@"; }
export PATH="$OT/bin:$PATH"
export MTOOLS_SKIP_CHECK=1

rm -rf "$TMP"
mkdir -p "$TMP" "$WORK/logs"

echo "copy base image"
cp --reflink=auto "$REAL" "$IMG"

python3 - "$IMG" "$TMP" "$HERE/lib" << 'PY'
import os, pickle, struct, sys, uuid
from pathlib import Path

img = Path(sys.argv[1])
tmp = Path(sys.argv[2])
sys.path.insert(0, sys.argv[3])
import rebuild

def gpt(path):
    with path.open("rb") as f:
        f.seek(512)
        hdr = f.read(92)
        if hdr[:8] != b"EFI PART":
            raise SystemExit("not gpt")
        ents, nent, esz = struct.unpack_from("<QII", hdr, 72)
        f.seek(ents * 512)
        data = f.read(nent * esz)

    def guid(b):
        d1, d2, d3 = struct.unpack_from("<IHH", b, 0)
        rest = b[8:16]
        return f"{d1:08x}-{d2:04x}-{d3:04x}-{rest[:2].hex()}-{rest[2:].hex()}"

    out = []
    for i in range(nent):
        e = data[i * esz:(i + 1) * esz]
        if e[:16] == b"\x00" * 16:
            continue
        name = e[56:128].decode("utf-16le").rstrip("\x00")
        first, last = struct.unpack_from("<QQ", e, 32)
        out.append({
            "name": name,
            "guid": guid(e[16:32]),
            "start": first * 512,
            "size": (last - first + 1) * 512,
        })
    return out

def vendor_extent(path, super_off):
    with path.open("rb") as f:
        f.seek(super_off + 4096)
        geo = f.read(52)
        if geo[:4] != b"gDla":
            raise SystemExit("no LP geometry")
        f.seek(super_off + 12288)
        hdr = f.read(256)
        if hdr[:4] != b"0PLA":
            raise SystemExit("no LP header")
        # LpMetadataHeader: header size at 8, tables size at 44, partitions and extents table descriptors at 80 and 92
        hsize = struct.unpack_from("<I", hdr, 8)[0]
        tables_size = struct.unpack_from("<I", hdr, 44)[0]
        p_off, p_num, p_size = struct.unpack_from("<III", hdr, 80)
        e_off, e_num, e_size = struct.unpack_from("<III", hdr, 92)
        f.seek(super_off + 12288 + hsize)
        tables = f.read(tables_size)
    for i in range(p_num):
        e = tables[p_off + i * p_size:p_off + (i + 1) * p_size]
        name = e[:36].split(b"\x00", 1)[0].decode()
        _attr, first, num, _group = struct.unpack_from("<IIII", e, 36)
        if name != "vendor_a":
            continue
        if num != 1 or first >= e_num:
            raise SystemExit(f"vendor extents {num}")
        nsec, ttype, tdata, _tsource = struct.unpack_from("<QIQI", tables, e_off + first * e_size)
        if ttype != 0:
            raise SystemExit("vendor extent is not linear")
        return tdata * 512, nsec * 512
    raise SystemExit("vendor_a missing")

parts = {p["name"]: p for p in gpt(img)}
super_off = parts["super"]["start"]
rel_off, vendor_size = vendor_extent(img, super_off)
if rel_off + vendor_size > parts["super"]["size"]:
    raise SystemExit("vendor extent runs past the super partition")
vendor_off = super_off + rel_off
with img.open("rb") as f:
    f.seek(vendor_off)
    blob = f.read(vendor_size)
idx = blob.rfind(b"AVBf")
if idx < 0:
    raise SystemExit("vendor has no AVB footer")
orig = struct.unpack_from(">Q", blob, idx + 12)[0]
erofs = blob[:orig]
(tmp / "vendor.erofs.src").write_bytes(erofs)
u = str(uuid.UUID(bytes=erofs[1024 + 48:1024 + 64]))
if u != os.environ["VENDOR_UUID"]:
    raise SystemExit("unexpected vendor uuid " + u + ": does not match the selected Lapis build")

data, blksz, meta_off, xattr, root = rebuild.load_image(tmp / "vendor.erofs.src")
vmap, _hard = rebuild.walk_image(str(tmp / "vendor.erofs.src"), root, data, blksz, meta_off, xattr)
(tmp / "vendor.meta.pickle").write_bytes(pickle.dumps(vmap))
(tmp / "offsets.env").write_text(
    f"ESP_OFF={parts['EFI-SYSTEM']['start']}\n"
    f"VENDOR_OFF={vendor_off}\n"
    f"VENDOR_SIZE={vendor_size}\n"
    f"VBMETA_OFF={parts['vbmeta_a']['start']}\n"
    f"BOOT_UUID={parts['boot_a']['guid']}\n"
    f"VENDOR_UUID={u}\n"
)
print((tmp / "offsets.env").read_text())
PY

# shellcheck disable=SC1091
source "$TMP/offsets.env"

FWLIST=$(python3 "$HERE/lib/devprofile.py" firmware "$GBOS_PROFILE") || exit 1
echo "device firmware of the profile (RAM-loaded by the drivers, nothing is flashed): $(printf '%s\n' "$FWLIST" | grep -c .) files"
# GBOS_FIRMWARE is a linux-firmware layout (plain or .zst files, symlinks allowed), e.g. /usr/lib/firmware on Arch or
# the <dir>/firmware that builder/fetch-inputs.sh fills from the upstream releases. Contents are pinned in the profile;
# other versions were never tested on the device, so they are refused unless GBOS_ALLOW_FIRMWARE_DRIFT=1.
fw() {
    local p="$FW/$1"
    mkdir -p "$(dirname "$2")"
    if [ -e "$p" ]; then cp "$(readlink -f "$p")" "$2"
    elif [ -e "$p.zst" ]; then zstd -q -d -f "$(readlink -f "$p.zst")" -o "$2"
    else echo "firmware missing: $p (or $p.zst); run builder/fetch-inputs.sh or set GBOS_FIRMWARE"; exit 1; fi
}
while read -r path _from _src _sha; do
    [ -n "$path" ] || continue
    fw "$path" "$TMP/fw/$path"
done <<< "$FWLIST"
mkdir -p "$TMP/fw"   # a profile without device firmware (profiles/generic-x86) has an empty list
if [ -n "$FWLIST" ] && ! ( cd "$TMP" && printf '%s\n' "$FWLIST" | awk 'NF == 4 {print $4 "  fw/" $1}' | sha256sum -c )
then
    [ "${GBOS_ALLOW_FIRMWARE_DRIFT:-}" = 1 ] || { echo "firmware differs from the tested versions (see above); set GBOS_ALLOW_FIRMWARE_DRIFT=1 to use it anyway"; exit 1; }
    echo "WARNING: building with untested firmware versions (GBOS_ALLOW_FIRMWARE_DRIFT=1)"
fi
( cd "$TMP" && find fw -type f -exec sha256sum {} + )
# Extra kernel modules the profile ships (kmods; builder/kmod/build-<name>.sh builds them from pinned sources).
# ac: the ACPI AC adapter driver (GBOS_KMOD_AC = the ac.ko that builder/kmod/build-ac.sh made), so Android sees the
# charger. It reads the ACPI _PSR method only and sends no EC host command.
KMODS=$(python3 "$HERE/lib/devprofile.py" kmods "$GBOS_PROFILE") || exit 1
export GBOS_KMOD_AC="${GBOS_KMOD_AC:-}"
if printf '%s\n' "$KMODS" | grep -qx ac; then
    want=$(python3 "$HERE/lib/lapis.py" --get AC_KO_SHA)
    [ -n "$want" ] || want=$(sed -n 's/^AC_KO_SHA=\([0-9a-f]\{64\}\)$/\1/p' "$HERE/kmod/build-ac.sh")
    [ -n "$want" ] && [ -f "$GBOS_KMOD_AC" ] && [ "$(sha256sum < "$GBOS_KMOD_AC" | cut -c1-64)" = "$want" ] \
        || { echo "the profile ships ac.ko: GBOS_KMOD_AC (${GBOS_KMOD_AC:-unset}) must be the ac.ko pinned in builder/kmod/build-ac.sh"; exit 1; }
    mkdir -p "$TMP/kmod"; cp "$GBOS_KMOD_AC" "$TMP/kmod/ac.ko"
    echo "ACPI AC adapter driver: ac.ko $want"
elif [ -n "$GBOS_KMOD_AC" ]; then
    echo "GBOS_KMOD_AC is set but the profile does not list kmods = [\"ac\"]"; exit 1
fi
# gbos_wakefilter: drops the ACPI power button's resume KEY_WAKEUP when the button did not wake the system
# (GBOS_KMOD_WAKEFILTER = the gbos_wakefilter.ko that builder/kmod/build-gbos_wakefilter.sh made).
export GBOS_KMOD_WAKEFILTER="${GBOS_KMOD_WAKEFILTER:-}"
if printf '%s\n' "$KMODS" | grep -qx gbos_wakefilter; then
    want=$(python3 "$HERE/lib/lapis.py" --get WAKEFILTER_KO_SHA)
    [ -n "$want" ] || want=$(sed -n 's/^WAKEFILTER_KO_SHA=\([0-9a-f]\{64\}\)$/\1/p' "$HERE/kmod/build-gbos_wakefilter.sh")
    [ -n "$want" ] && [ -f "$GBOS_KMOD_WAKEFILTER" ] && [ "$(sha256sum < "$GBOS_KMOD_WAKEFILTER" | cut -c1-64)" = "$want" ] \
        || { echo "the profile ships gbos_wakefilter.ko: GBOS_KMOD_WAKEFILTER (${GBOS_KMOD_WAKEFILTER:-unset}) must be the one pinned in builder/kmod/build-gbos_wakefilter.sh"; exit 1; }
    mkdir -p "$TMP/kmod"; cp "$GBOS_KMOD_WAKEFILTER" "$TMP/kmod/gbos_wakefilter.ko"
    echo "power-button wake filter: gbos_wakefilter.ko $want"
elif [ -n "$GBOS_KMOD_WAKEFILTER" ]; then
    echo "GBOS_KMOD_WAKEFILTER is set but the profile does not list kmods \"gbos_wakefilter\""; exit 1
fi

echo "extract vendor tree"
mkdir -p "$TMP/vendor-tree"
ot fsck.erofs --extract="$TMP/vendor-tree" --overwrite --preserve-perms "$TMP/vendor.erofs.src"

python3 - "$TMP" "$HERE/lib" << 'PY'
import os, pickle, sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
import rebuild
import devprofile
import lapis
LAPIS = lapis.load()
GKI_RELEASE = LAPIS.get("GKI_RELEASE", "6.18.21-android17-5-g3938c8d97142-ab16302624")
import kano_autooff
import importlib.util
import types

tmp = Path(sys.argv[1])
tree = tmp / "vendor-tree"
vmap = pickle.loads((tmp / "vendor.meta.pickle").read_bytes())
prof = devprofile.load(os.environ["GBOS_PROFILE"])
hooks = None
if prof["hooks"]["vendor"]:
    spec = importlib.util.spec_from_file_location("profile_vendor_hooks", Path(prof["_dir"]) / prof["hooks"]["vendor"])
    hooks = importlib.util.module_from_spec(spec); spec.loader.exec_module(hooks)
ctx = types.SimpleNamespace(tree=tree, vmap=vmap, prof=prof, audio_src=Path(os.environ["GBOS_AUDIO_SRC"]))
fw = tree / "etc/init/fwupd.rc"
text = fw.read_text()
block = (
    "on boot\n"
    "    start fwupd\n"
    "    # Start the daemon in the background first\n"
    "    start fwupd_script\n"
    "\n"
)
if block not in text:
    raise SystemExit("fwupd on boot block not found")
fw.write_text(text.replace(block, "", 1))
roll = tree / "etc/init/desktop_kernel_max_rollforward_boot.rc"
if not roll.is_file():
    raise SystemExit("rollforward rc missing")
roll.unlink()
import shutil
# Device firmware of the profile. New directories copy the firmware/i915 directory metadata, files that of a stock blob.
for fwe in prof["firmware"]:
    rel = fwe["path"]
    parts = rel.split("/")
    for i in range(1, len(parts)):
        d = "firmware/" + "/".join(parts[:i])
        if not (tree / d).exists():
            (tree / d).mkdir()
            (tree / d).chmod(0o755)
            vmap[d] = dict(vmap["firmware/i915"])
    dst = tree / "firmware" / rel
    if dst.exists():
        raise SystemExit("already present: " + rel)
    shutil.copyfile(tmp / "fw" / rel, dst)
    dst.chmod(0o644)
    vmap["firmware/" + rel] = dict(vmap["firmware/i915/xe2lpd_dmc.bin"])
import re as _re
fst = tree / "etc/fstab.android-desktop"
ft = fst.read_text()
ud = [l for l in ft.splitlines() if l.startswith("/dev/block/by-name/userdata")]
if len(ud) != 1 or "fileencryption=" not in ud[0]:
    raise SystemExit("userdata line not as expected")
new_ud = _re.sub(r"(fileencryption|keydirectory|metadata_encryption)=[^,\s]*,?", "", ud[0]).replace(",,", ",").rstrip(",")
assert new_ud.count(",checkpoint=block") == 1, "checkpoint flag"
new_ud = new_ud.replace(",checkpoint=block", "")
ud_fields = new_ud.split()
assert len(ud_fields) == 5 and ud_fields[1:3] == ["/data", "ext4"], "userdata ext4 record"
assert set(ud_fields[3].split(",")) in (
    {"rw", "nodev", "noatime", "nosuid", "errors=panic"},
    {"rw", "nodev", "noatime", "nosuid", "errors=panic", "inlinecrypt"},
), "userdata ext4 options"
old_fs = " /data ext4 " + ud_fields[3] + " "
assert new_ud.count(old_fs) == 1, "userdata ext4 spacing"
new_ud = new_ud.replace(old_fs, " /data f2fs nodev,noatime,nosuid,discard ")
ft = ft.replace(ud[0], new_ud)
ft = "\n".join(l for l in ft.splitlines() if "voldmanaged=usb" not in l) + "\n"
fst.write_text(ft)
bp = tree / "build.prop"
bp.write_text(bp.read_text().rstrip("\n") + "\n# KANO debug: serial console shell\nro.debuggable=1\n")
# Model display settings (KANO: drm_hwcomposer colour pipeline off and client composition, see the profile).
if hooks and hasattr(hooks, "display"):
    hooks.display(ctx)
elif prof["display"]["build_prop_set"] or prof["display"]["hwc_backend"]:
    raise SystemExit("profile display settings need hooks.vendor with display()")
# No cppreopts: copying system_other odex to /data took 540 s on the stick and pushed PMS past the watchdog.
bt = bp.read_text()
if bt.splitlines().count("ro.cp_system_other_odex=1") != 1:
    raise SystemExit("cp_system_other_odex not exactly once")
bt = bt.replace("\nro.cp_system_other_odex=1\n", "\nro.cp_system_other_odex=0\n", 1)
bp.write_text(bt)
# Serial logcat. init compiles the split policy when precompiled_sepolicy is missing ("No precompiled sepolicy").
pre = tree / "etc/selinux/precompiled_sepolicy"
if not pre.is_file():
    raise SystemExit("precompiled_sepolicy missing")
pre.unlink()
cil = tree / "etc/selinux/vendor_sepolicy.cil"
cil.write_text(cil.read_text().rstrip("\n") + "\n" + """;; KANO serial logcat: a permissive vendor domain for /vendor/etc/kano-logcat.sh (run by /system/bin/sh).
;; Only init (enforcing) needs rules; what kano_kmsg_logcat itself does is allowed by typepermissive,
;; so the Treble exec/entrypoint neverallows stay clean (checked with secilc without -N).
(type kano_kmsg_logcat)
(roletype object_r kano_kmsg_logcat)
(typeattributeset domain (kano_kmsg_logcat))
(typepermissive kano_kmsg_logcat)
(allow init_202604 kano_kmsg_logcat (process (transition siginh rlimitinh)))
(dontaudit init_202604 kano_kmsg_logcat (process (noatsecure)))
;; KANO boot fix: one setprop at post-fs-data. vold would otherwise call the boot-control HAL's markBootSuccessful,
;; which runs crossystem against firmware NVRAM (blocked here on purpose) and reboots with 'Checkpoint commit failed'.
(type kano_bootfix)
(roletype object_r kano_bootfix)
(typeattributeset domain (kano_bootfix))
(typepermissive kano_bootfix)
(allow init_202604 kano_bootfix (process (transition siginh rlimitinh)))
(dontaudit init_202604 kano_bootfix (process (noatsecure)))
;; cmd/dumpsys hand their stdio fds to the service: let system_server and surfaceflinger use the kano debug domain's fds and pipes
(allow system_server_202604 kano_kmsg_logcat (fd (use)))
(allow system_server_202604 kano_kmsg_logcat (fifo_file (read write getattr ioctl append)))
(allow surfaceflinger_202604 kano_kmsg_logcat (fd (use)))
(allow surfaceflinger_202604 kano_kmsg_logcat (fifo_file (read write getattr ioctl append)))
""")
(tree / "etc/kano-logcat.sh").write_text("""#!/system/bin/sh
# Copy a filtered logcat to the kernel log (KANO serial console, 115200 baud): one kmsg record per line, at most ~30 lines/s.
exec 3>/dev/kmsg
while true; do
  /system/bin/logcat -b main,system,crash -v time '*:E' SystemServer:I SystemServerTiming:I ActivityManager:I PackageManager:W Watchdog:I AndroidRuntime:I SystemServiceManager:I BootReceiver:I DEBUG:S cutils-trace:S prefetch:S timberslide:S DesktopChargeControlHal:S gscd:S secretkeeper-hal-trusty:S SystemServiceRegistry:S 2>&1 |
  while IFS= read -r line; do
    printf '<14>kano-logcat: %s\\n' "$line" >&3
    sleep 0.03
  done
  sleep 1
done
""")
(tree / "etc/init/kano-logcat.rc").write_text("""# KANO: filtered logcat on the serial console (see /vendor/etc/kano-logcat.sh)
service kano_logcat /system/bin/sh /vendor/etc/kano-logcat.sh
    class core
    user root
    group log
    seclabel u:r:kano_kmsg_logcat:s0
""")
for f in ("etc/kano-logcat.sh", "etc/init/kano-logcat.rc"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# Daemons that need the Cr50 GSC, the ChromeOS EC or the Trusty secretkeeper port crash every few seconds here.
sk = tree / "etc/init/android.hardware.security.secretkeeper.trusty.desktop.rc"
st = sk.read_text()
iface = "    interface aidl android.hardware.security.secretkeeper.ISecretkeeper/default\n"
if st.count(iface) != 1 or "    class hal\n" not in st:
    raise SystemExit("secretkeeper rc not as expected")
st = st.replace(iface, "    # KANO: no Trusty secretkeeper port without the protected VM; not started, no lazy interface start\n", 1)
st = st.replace("    class hal\n", "    class hal\n    disabled\n", 1)
sk.write_text(st)
print("secretkeeper rc:", st)
cc = tree / "etc/init/hal_desktop_charge_control_default.rc"
ct = cc.read_text()
if ct.count("    class hal\n") != 1 or "disabled" in ct:
    raise SystemExit("charge control rc not as expected")
cc.write_text(ct.replace("    class hal\n", "    class hal\n    # KANO: needs the ChromeOS EC (cros_ec is blacklisted)\n    disabled\n", 1))
print("charge control rc:", cc.read_text())
# Weaver and Secretkeeper only start with a protected VM, which the KANO lacks; undeclare them so waitForDeclaredService returns null.
for f in ("etc/vintf/manifest/android.hardware.weaver-service.android-desktop.xml",
          "etc/vintf/manifest/android.hardware.security.secretkeeper.trusty.xml"):
    x = tree / f
    if not x.is_file():
        raise SystemExit("missing " + f)
    x.unlink()
(tree / "etc/init/kano-bootfix.rc").write_text("""# KANO: vold treats every boot as a checkpoint boot and its commit would call the boot-control HAL's
# markBootSuccessful, which runs crossystem (firmware NVRAM, deliberately unreachable here) and fails, rebooting
# the device. Tell vold not to commit; nothing on this image uses checkpoints (/data is f2fs without checkpoint).
service kano_bootfix /system/bin/setprop persist.vold.dont_commit_checkpoint 1
    oneshot
    disabled
    user root
    seclabel u:r:kano_bootfix:s0

on post-fs-data
    start kano_bootfix
""")
(tree / "etc/init/kano-bootfix.rc").chmod(0o644)
vmap["etc/init/kano-bootfix.rc"] = dict(vmap["etc/init/fwupd.rc"])
(tree / "etc/kano-diag.sh").write_text("""#!/system/bin/sh
# KANO debug: dmesg/logcat/display/input/audio/Wi-Fi snapshots on /data every 20 s (for boots whose serial console dies).
D=/data/kano-diag
mkdir -p $D
n=0
snap() { f=$1; shift; timeout 20 "$@" < /dev/null 2>&1 | cat > $D/$f.tmp; mv $D/$f.tmp $D/$f; }
while true; do
  snap dmesg.txt /system/bin/dmesg
  snap logcat.txt /system/bin/logcat -d -b main,system,crash -v time '*:W'
  snap sf-displays.txt /system/bin/dumpsys SurfaceFlinger --display-id
  snap display.txt /system/bin/dumpsys display
  snap wifi-status.txt /system/bin/cmd wifi status
  { ls -l /dev/dri /sys/class/drm; for c in /sys/class/drm/card*-*; do echo "$c status=$(cat $c/status) enabled=$(cat $c/enabled) dpms=$(cat $c/dpms)"; head -n 3 $c/modes; done
    for b in /sys/class/backlight/*; do echo "$b brightness=$(cat $b/brightness) actual=$(cat $b/actual_brightness) max=$(cat $b/max_brightness) bl_power=$(cat $b/bl_power)"; done; } > $D/drm.txt 2>&1
  cat /proc/bus/input/devices > $D/input.txt 2>&1
  cat /proc/asound/cards > $D/asound.txt 2>&1
  ip addr > $D/ip.txt 2>&1
  for d in /sys/bus/pci/devices/0000:00:1e.0 /sys/bus/pci/devices/0000:00:15.*  /sys/bus/pci/devices/0000:00:19.*; do echo "$d -> $(readlink $d/driver)"; done > $D/lpss.txt 2>&1
  n=$((n + 1)); echo "$n $(cat /proc/uptime)" > $D/heartbeat.txt
  sync
  sleep 20
done
""")
(tree / "etc/init/kano-diag.rc").write_text("""# KANO debug: dmesg/logcat snapshots in /data/kano-diag (see /vendor/etc/kano-diag.sh)
service kano_diag /system/bin/sh /vendor/etc/kano-diag.sh
    class late_start
    user root
    group log
    seclabel u:r:kano_kmsg_logcat:s0
""")
for f in ("etc/kano-diag.sh", "etc/init/kano-diag.rc"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
if prof["wifi"]["modprobe"] and not (hooks and hasattr(hooks, "wifi_rc")):
    raise SystemExit("profile wifi.modprobe needs hooks.vendor with wifi_rc()")
hw_head = hooks.wifi_rc(ctx) if hooks and hasattr(hooks, "wifi_rc") else ""
KANO_KMODS = [k for k in ("ac", "gbos_wakefilter") if k in prof["kmods"]]
KMOD_LOAD = {
    "ac": "    # ACPI AC adapter (ACPI0003): Android sees the charger as a Mains power supply; no EC host command\n",
    "gbos_wakefilter": "    # drop the ACPI power button's resume KEY_WAKEUP when the button did not wake the system (dark resumes stay dark)\n",
}
if KANO_KMODS:
    # /vendor/lib/kano/<module>.ko with their own modules.dep: vendor_modprobe may module_load vendor_file (vendor
    # sepolicy); metadata of the directory from /vendor/lib, of the files from a plain vendor_file library
    ref = next(k for k in sorted(vmap) if k.startswith("lib64/") and vmap[k]["selinux"] == "u:object_r:vendor_file:s0"
               and vmap[k]["mode"] == 0o644 and (tree / k).is_file() and not (tree / k).is_symlink())
    (tree / "lib/kano").mkdir()
    (tree / "lib/kano").chmod(0o755)
    vmap["lib/kano"] = dict(vmap["lib"])
    for k in KANO_KMODS:
        shutil.copyfile(tmp / f"kmod/{k}.ko", tree / f"lib/kano/{k}.ko")
    (tree / "lib/kano/modules.dep").write_text("".join(f"{k}.ko:\n" for k in KANO_KMODS))
    for f in [f"lib/kano/{k}.ko" for k in KANO_KMODS] + ["lib/kano/modules.dep"]:
        (tree / f).chmod(0o644)
        vmap[f] = dict(vmap[ref])
    kmod_load = "".join(KMOD_LOAD[k] + f"    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/kano {k}\n"
                        for k in KANO_KMODS)
    hw_head = (hw_head[:-1] + kmod_load + "\n") if hw_head else ("on early-boot\n" + kmod_load + "\n")
# Profile [modules]: synchronous modprobe (no -b) from the existing module dirs. Empty for kano and generic-x86,
# so their kano-hw.rc head is unchanged. vendor_dlkm is not rewritten.
missing_ko = devprofile.missing_dev_modules(prof, os.environ.get("GBOS_DEV_KERNEL_DIR", ""))
if missing_ko:
    raise SystemExit("profile modules need these .ko files in GBOS_DEV_KERNEL_DIR/modules: " + ", ".join(missing_ko))
hw_head = devprofile.append_module_boot(hw_head, prof)
dev_kmod_boot = ""
dev_wifi_hook = ""
if os.environ.get("GBOS_DEV_KERNEL_DIR") and (Path(os.environ["GBOS_DEV_KERNEL_DIR"]) / "modules/amdgpu.ko").is_file():
    # amd-hx370 handles graphics in /vendor/etc/amd-hx370-graphics.sh on early-boot (safe entry: gbos_efifb +
    # pastel with amdgpu blacklisted; experimental native entry: modprobe amdgpu once, never gbos_efifb).
    # Other profiles keep the QEMU-proven boot_completed load of amdgpu + snd-sof-amd-acp70 + simpledrm.
    if prof["model"]["id"] != "amd-hx370":
        dev_kmod_boot = (
            "    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/dev-modules amdgpu\n"
            "    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/dev-modules snd-sof-amd-acp70\n"
            "    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/dev-modules simpledrm\n"
        )
    dev_wifi_hook = 'sleep 5; echo "<6>gbos-dev-modules: $(cut -d\' \' -f1 /proc/modules | tr \'\\n\' \' \')" > /dev/kmsg\n'
(tree / "etc/init/kano-hw.rc").write_text(hw_head + """# KANO: join the Wi-Fi network described in /data/kano-wifi.conf (written by the maintenance OS, never in this image).
service kano_wifi /system/bin/sh /vendor/etc/kano-wifi.sh
    oneshot
    disabled
    user root
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    start kano_wifi
""" + dev_kmod_boot)
(tree / "etc/kano-wifi.sh").write_text("""#!/system/bin/sh
# KANO: /data/kano-wifi.conf holds two lines, SSID and WPA2 passphrase (root 0600, written by the maintenance OS).
# cmd passes its stdio fds to system_server, so always pipe through cat (the policy lets system_server use our pipes).
C=/data/kano-wifi.conf
D=/data/kano-diag
mkdir -p $D
""" + dev_wifi_hook + """[ -r $C ] || { echo "no $C" > $D/wifi.txt; exit 0; }
SSID=$(sed -n 1p $C); PSK=$(sed -n 2p $C)
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  timeout 20 /system/bin/cmd wifi set-wifi-enabled enabled < /dev/null 2>&1 | cat > /dev/null
  timeout 30 /system/bin/cmd wifi connect-network "$SSID" wpa2 "$PSK" < /dev/null 2>&1 | cat > $D/wifi-connect.txt
  sleep 15
  timeout 20 /system/bin/cmd wifi status < /dev/null 2>&1 | cat > $D/wifi.txt
  grep -q -i "connected to" $D/wifi.txt && exit 0
done
exit 1
""")
for f in ("etc/init/kano-hw.rc", "etc/kano-wifi.sh"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# (safe16's diag additions are part of the safe19 kano-diag.sh above)
# Remove every firmware updater / flash tool from vendor (touch I2C-HID and Wi-Fi are live now; "disabled" is not enough).
REMOVE = ["bin/fwupd-binder", "bin/fwupd_blocking_update", "bin/fwupdmgr", "bin/fw-updater-wrapper", "etc/init/fwupd.rc",
          "etc/fwupd", "etc/pki/fwupd", "etc/pki/fwupd-metadata", "usr/share/fwupd",
          "bin/flashrom", "lib64/libflashrom.so", "etc/init/flashrom.rc",
          "bin/futility", "bin/hw/vendor.google.desktop.futility-executor", "etc/init/futility-executor.rc",
          "etc/vintf/manifest/vendor.google.desktop.futility-executor.xml",
          "bin/ectool", "bin/crossystem", "firmware/ap-ec-fw.zip"]
removed = []
for rel in REMOVE:
    p = tree / rel
    if not (p.exists() or p.is_symlink()):
        raise SystemExit("expected to remove, missing: " + rel)
    if p.is_dir() and not p.is_symlink():
        for sub in sorted(p.rglob("*"), reverse=True):
            removed.append(str(sub.relative_to(tree)))
            sub.unlink() if (sub.is_file() or sub.is_symlink()) else sub.rmdir()
        p.rmdir()
    else:
        p.unlink()
    removed.append(rel)
(tmp / "removed.txt").write_text("\n".join(sorted(removed)) + "\n")
print("removed", len(removed), "paths")
print("kano logcat label:", vmap["etc/init/kano-logcat.rc"].get("selinux"))
print("userdata:", new_ud)
# --- safe19 additions (inserted before rebuild.write_configs in build-safe19.sh) ---
# Serial logcat only on the serial-debug entry: the 115200-baud console is synchronous and slows everything else.
(tree / "etc/kano-logcat.sh").write_text("""#!/system/bin/sh
# Copy a filtered logcat to the kernel log (KANO serial console, 115200 baud): one kmsg record per line, at most ~30 lines/s.
# Only when booted from the serial-debug entry (keep_bootcon); otherwise idle so init does not restart us.
if ! grep -q keep_bootcon /proc/cmdline; then
  while true; do sleep 86400; done
fi
exec 3>/dev/kmsg
while true; do
  /system/bin/logcat -b main,system,crash -v time '*:E' SystemServer:I SystemServerTiming:I ActivityManager:I PackageManager:W Watchdog:I AndroidRuntime:I SystemServiceManager:I BootReceiver:I dras:I DEBUG:S cutils-trace:S prefetch:S timberslide:S DesktopChargeControlHal:S gscd:S secretkeeper-hal-trusty:S SystemServiceRegistry:S StatsCompanionService:S 2>&1 |
  while IFS= read -r line; do
    printf '<14>kano-logcat: %s\\n' "$line" >&3
    sleep 0.03
  done
  sleep 1
done
""")
# Diag: cheap state every 60 s, heavy dumps every third round. Readers have their own timeout: dumpsys hands the
# pipe to the service, so a stuck service would otherwise keep 'cat' (and the whole loop) waiting forever.
(tree / "etc/kano-diag.sh").write_text("""#!/system/bin/sh
# KANO debug: snapshots in /data/kano-diag (read later from the maintenance OS).
D=/data/kano-diag
mkdir -p $D
n=0
snap() { f=$1; shift; timeout -k 5 20 "$@" < /dev/null 2>&1 | timeout -k 5 30 cat > $D/$f.tmp; mv $D/$f.tmp $D/$f; }
iotop() {
  for p in /proc/[0-9]*; do
    w=$(sed -n 's/^write_bytes: //p' $p/io 2>/dev/null); r=$(sed -n 's/^read_bytes: //p' $p/io 2>/dev/null)
    [ -n "$w" ] && echo "$w $r ${p#/proc/} $(cat $p/comm 2>/dev/null)"
  done | sort -rn | head -n 30
}
while true; do
  { cat /proc/uptime /proc/loadavg; grep -E " sd[a-z]+ " /proc/diskstats; grep -E "^(cpu |procs_)" /proc/stat; head -n 4 /proc/meminfo; } > $D/load.txt 2>&1
  { echo "# write_bytes read_bytes pid comm (cumulative)"; iotop; } > $D/io-top.txt 2>&1
  { ls -l /dev/dri /sys/class/drm; for c in /sys/class/drm/card*-*; do echo "$c status=$(cat $c/status) enabled=$(cat $c/enabled) dpms=$(cat $c/dpms)"; head -n 3 $c/modes; done
    for b in /sys/class/backlight/* /sys/class/leds/*; do echo "$b brightness=$(cat $b/brightness) max=$(cat $b/max_brightness)"; done; } > $D/drm.txt 2>&1
  cat /proc/bus/input/devices > $D/input.txt 2>&1
  { cat /proc/asound/cards /proc/asound/pcm; ls -l /dev/snd; } > $D/asound.txt 2>&1
  ip addr > $D/ip.txt 2>&1
  for d in /sys/bus/pci/devices/0000:00:1e.0 /sys/bus/pci/devices/0000:00:15.* /sys/bus/pci/devices/0000:00:19.*; do echo "$d -> $(readlink $d/driver)"; done > $D/lpss.txt 2>&1
  if [ $((n % 3)) -eq 0 ]; then
    snap dmesg.txt /system/bin/dmesg
    snap logcat.txt /system/bin/logcat -d -b main,system,crash -v time '*:W'
    snap audio-log.txt /system/bin/logcat -d -b main,system -v time -s dras:V AudioFlinger:V AudioPolicyService:V APM_AudioPolicyManager:V AudioService:I
    snap display.txt /system/bin/dumpsys display
    snap wifi-status.txt /system/bin/cmd wifi status
    snap input-dump.txt /system/bin/dumpsys input
    snap audio-hal.txt /system/bin/dumpsys android.hardware.audio.core.IModule/default
    snap audioflinger.txt /system/bin/dumpsys media.audio_flinger
    snap props.txt /system/bin/getprop
  fi
  n=$((n + 1)); echo "$n $(cat /proc/uptime)" > $D/heartbeat.txt
  sync
  sleep 60
done
""")
# Model audio (DRAS card configuration) and keyboard backlight LED path, both from the profile's hooks.
if prof["audio"]["config_dir"] and not (hooks and hasattr(hooks, "audio")):
    raise SystemExit("profile audio.config_dir needs hooks.vendor with audio()")
if hooks and hasattr(hooks, "audio") and prof["audio"]["config_dir"]:
    hooks.audio(ctx)
if prof["input"]["kbd_backlight_leds"] and not (hooks and hasattr(hooks, "kbd_backlight")):
    raise SystemExit("profile input.kbd_backlight_leds needs hooks.vendor with kbd_backlight()")
if hooks and hasattr(hooks, "kbd_backlight"):
    hooks.kbd_backlight(ctx)
# --- safe20 additions (inserted before rebuild.write_configs in build-safe20.sh, after the safe19 additions) ---
# One-shot audio self-test: 120 s after boot_completed, dras_tool (the audio HAL's own CLI, same audio_config.xml) plays a
# 1 kHz tone at -20 dBFS on the Speaker and records the InternalMic; outputs and --help texts go to /data/kano-diag/audiotest.
import math as _math, struct as _struct, wave as _wave
tone = tree / "etc/kano-tone-1k.wav"
w = _wave.open(str(tone), "wb"); w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
fr = bytearray()
for i in range(48000 * 3):
    s = int(0.1 * 32767 * _math.sin(2 * _math.pi * 1000 * i / 48000)) if 4800 <= i < 48000 * 3 - 4800 else 0
    fr += _struct.pack("<hh", s, s)
w.writeframes(bytes(fr)); w.close()
(tree / "etc/kano-audiotest.sh").write_text("""#!/system/bin/sh
# KANO: one-shot audio self-test (see kano-audiotest.rc). Plays /vendor/etc/kano-tone-1k.wav (1 kHz, -20 dBFS, 3 s).
D=/data/kano-diag/audiotest
mkdir -p $D
T=/apex/com.android.hardware.audio.desktop/bin/dras_tool
W=/vendor/etc/kano-tone-1k.wav
run() { n=$1; shift; echo "\\$ $* (uptime $(cut -d' ' -f1 /proc/uptime))" > $D/$n.txt; timeout -k 5 30 "$@" < /dev/null >> $D/$n.txt 2>&1; echo "rc=$?" >> $D/$n.txt; }
sleep 120
run help $T --help
run help-aplay $T aplay --help
run help-arecord $T arecord --help
run help-aplayrecord $T aplayrecord --help
run aplayrecord $T aplayrecord $W $D/rec-tone.wav --play-device Speaker --record-device InternalMic
sleep 3
run arecord $T arecord $D/rec-silence.wav --device InternalMic --duration-sec 3
sleep 3
run aplay1 $T aplay $W --device Speaker
sleep 2
run aplay2 $T aplay $W --play-device Speaker
sleep 2
run aplay3 $T aplay $W
ls -l $D > $D/ls.txt
""")
(tree / "etc/init/kano-audiotest.rc").write_text("""# KANO: one-shot audio self-test 120 s after boot (see /vendor/etc/kano-audiotest.sh)
service kano_audiotest /system/bin/sh /vendor/etc/kano-audiotest.sh
    oneshot
    disabled
    user root
    group audio
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    start kano_audiotest
""")
for f in ("etc/kano-tone-1k.wav", "etc/kano-audiotest.sh", "etc/init/kano-audiotest.rc"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# Diag: no global sync (it stalls for minutes behind the stick's write queue and froze the loop at 406 s).
dg = tree / "etc/kano-diag.sh"
dt = dg.read_text()
if dt.count("\n  sync\n  sleep 60\n") != 1:
    raise SystemExit("diag sync line not as expected")
dg.write_text(dt.replace("\n  sync\n  sleep 60\n", "\n  sleep 60\n"))
# dumpsys of the audio HAL / audioserver hand our pipe to them (FAILED_TRANSACTION before)
cil3 = tree / "etc/selinux/vendor_sepolicy.cil"
cil3.write_text(cil3.read_text().rstrip("\n") + """
;; KANO diag: dumpsys of the audio HAL and audioserver write into the kano debug domain's pipes
(allow hal_audio_default kano_kmsg_logcat (fd (use)))
(allow hal_audio_default kano_kmsg_logcat (fifo_file (read write getattr ioctl append)))
(allow audioserver_202604 kano_kmsg_logcat (fd (use)))
(allow audioserver_202604 kano_kmsg_logcat (fifo_file (read write getattr ioctl append)))
""")
print("audiotest:", (tree / "etc/init/kano-audiotest.rc").read_text().splitlines()[1], tone.stat().st_size)
# The small state files are written with dd conv=fsync (one fsync each), so a power cut leaves them readable.
dt = dg.read_text()
for old, new in [
    ('head -n 4 /proc/meminfo; } > $D/load.txt 2>&1', 'head -n 4 /proc/meminfo; } 2>&1 | dd of=$D/load.txt conv=fsync status=none'),
    ('iotop; } > $D/io-top.txt 2>&1', 'iotop; } 2>&1 | dd of=$D/io-top.txt conv=fsync status=none'),
    ('echo "$n $(cat /proc/uptime)" > $D/heartbeat.txt', 'echo "$n $(cat /proc/uptime)" | dd of=$D/heartbeat.txt conv=fsync status=none'),
]:
    if dt.count(old) != 1:
        raise SystemExit("diag line not as expected: " + old)
    dt = dt.replace(old, new)
dg.write_text(dt)
# --- safe21 additions (after the safe19/safe20 additions) ---
# timberslide (EC log reader) exits 1 every few seconds without cros_ec; as an "updatable" crasher it makes init run
# flags_health_check synchronously ~200x per boot. gscd aborts without the Cr50 TPM and writes a tombstone each time.
# Neither can work on the KANO (EC/TPM blocked on purpose); stop both from a vendor action (stop is not an override).
(tree / "etc/init/kano-services.rc").write_text("""# KANO: stop system services that need the blocked ChromeOS EC / Cr50 TPM (they only crash-loop here)
on boot
    stop timberslide
    stop android.system.desktop.security.gscd

on property:sys.boot_completed=1
    stop timberslide
    stop android.system.desktop.security.gscd
""")
(tree / "etc/init/kano-services.rc").chmod(0o644)
vmap["etc/init/kano-services.rc"] = dict(vmap["etc/init/fwupd.rc"])
# Slow stick: give system_server's watchdog 5 minutes (Settings.Global, applies once SettingsProvider is up and persists).
ws = tree / "etc/kano-wifi.sh"
wt = ws.read_text()
anchor_w = "[ -r $C ] || {"
if wt.count(anchor_w) != 1:
    raise SystemExit("kano-wifi.sh not as expected")
wt = wt.replace(anchor_w, """timeout 20 /system/bin/cmd settings put global system_server_watchdog_timeout_ms 300000 < /dev/null 2>&1 | cat > $D/watchdog-setting.txt
timeout 20 /system/bin/cmd settings get global system_server_watchdog_timeout_ms < /dev/null 2>&1 | cat >> $D/watchdog-setting.txt
""" + anchor_w, 1)
ws.write_text(wt)
# Audio self-test only when the maintenance OS asked for it (/data/kano-audiotest.enable), so normal boots stay silent.
at = tree / "etc/kano-audiotest.sh"
t2 = at.read_text()
if t2.count("mkdir -p $D\n") != 1:
    raise SystemExit("audiotest script not as expected")
at.write_text(t2.replace("mkdir -p $D\n", "[ -e /data/kano-audiotest.enable ] || exit 0\nrm -f /data/kano-audiotest.enable\nmkdir -p $D\n", 1))
# More hardware state in the heavy diag round.
dg2 = tree / "etc/kano-diag.sh"
d2 = dg2.read_text()
anchor_d = "    snap props.txt /system/bin/getprop\n"
if d2.count(anchor_d) != 1:
    raise SystemExit("diag props line not as expected")
d2 = d2.replace(anchor_d, anchor_d + """    snap battery.txt /system/bin/dumpsys battery
    snap bluetooth.txt /system/bin/dumpsys bluetooth_manager
    snap camera.txt /system/bin/dumpsys media.camera
    snap sensors.txt /system/bin/dumpsys sensorservice
    snap power.txt /system/bin/dumpsys power
    /system/bin/getprop | grep -E 'init.svc.(timberslide|android.system.desktop.security.gscd)|updatable_crashing' > $D/services.txt 2>&1
""", 1)
dg2.write_text(d2)
print("kano-services.rc:", (tree / "etc/init/kano-services.rc").read_text())
# --- safe22 additions (after the safe19..safe21 additions) ---
# Key-only adb over TCP 5555 for the remote harness: the GBOS_ADB_PUBKEY public key goes to /data/misc/adb/adb_keys
# (ro.adb.secure stays 1, so only that key can connect). Lets the harness read state live and shut down cleanly
# instead of cutting power (which left the last diag files zero-filled on f2fs).
(tree / "etc/kano-adb_keys").write_text(Path(os.environ["GBOS_ADB_PUBKEY"]).read_text() if os.environ.get("GBOS_ADB_PUBKEY") else "")
(tree / "etc/kano-adb.sh").write_text("""#!/system/bin/sh
# KANO: install the harness adb key and start adbd on TCP 5555 (see /vendor/etc/init/kano-adb.rc).
D=/data/kano-diag
mkdir -p $D /data/misc/adb
cat /vendor/etc/kano-adb_keys > /data/misc/adb/adb_keys
chown system:shell /data/misc/adb/adb_keys
chmod 0640 /data/misc/adb/adb_keys
restorecon /data/misc/adb/adb_keys
for k in development_settings_enabled adb_enabled; do
  timeout 20 /system/bin/cmd settings put global $k 1 < /dev/null 2>&1 | cat
done
setprop service.adb.tcp.port 5555
sleep 5
[ "$(getprop init.svc.adbd)" = running ] || setprop ctl.start adbd
sleep 5
{ getprop init.svc.adbd; getprop service.adb.tcp.port; ls -lZ /data/misc/adb; } > $D/adb.txt 2>&1
""")
(tree / "etc/init/kano-adb.rc").write_text("""# KANO: key-only adb over TCP for the remote harness (see /vendor/etc/kano-adb.sh)
service kano_adb /system/bin/sh /vendor/etc/kano-adb.sh
    oneshot
    disabled
    user root
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    start kano_adb
""")
for f in ("etc/kano-adb_keys", "etc/kano-adb.sh", "etc/init/kano-adb.rc"):
    (tree / f).chmod(0o644)
    vmap[f] = dict(vmap["etc/init/fwupd.rc"])
# Heavy diag files are written with one fsync each as well.
dg3 = tree / "etc/kano-diag.sh"
d3 = dg3.read_text()
old_snap = '| timeout -k 5 30 cat > $D/$f.tmp; mv $D/$f.tmp $D/$f; }'
if d3.count(old_snap) != 1:
    raise SystemExit("diag snap() not as expected")
dg3.write_text(d3.replace(old_snap, '| timeout -k 5 30 dd of=$D/$f.tmp conv=fsync status=none; mv $D/$f.tmp $D/$f; }'))
print("kano-adb key:", ((tree / "etc/kano-adb_keys").read_text().split() or ["(none)"])[-1])
# --- safe23 additions (after the safe19..safe22 additions) ---
# system build.prop pins service.adb.listen_addrs=tcp:localhost:5555 (lapis' SSH forwarder), so adbd never listened on
# the LAN. Point it at tcp:5555 (key auth unchanged) and restart adbd.
ka = tree / "etc/kano-adb.sh"
kt = ka.read_text()
old_a = "setprop service.adb.tcp.port 5555\nsleep 5\n[ \"$(getprop init.svc.adbd)\" = running ] || setprop ctl.start adbd\n"
if kt.count(old_a) != 1:
    raise SystemExit("kano-adb.sh not as expected")
ka.write_text(kt.replace(old_a, """setprop service.adb.listen_addrs tcp:5555
setprop service.adb.tcp.port 5555
setprop ctl.stop adbd
sleep 3
setprop ctl.start adbd
""", 1))
ka.write_text(ka.read_text().replace("{ getprop init.svc.adbd; getprop service.adb.tcp.port;", "{ getprop init.svc.adbd; getprop service.adb.listen_addrs; getprop service.adb.tcp.port;"))
# udsattestation asks servicemanager to start the (stopped) GSC HAL once a second; it cannot work without the Cr50 either.
ks = tree / "etc/init/kano-services.rc"
st = ks.read_text()
if st.count("    stop android.system.desktop.security.gscd\n") != 2:
    raise SystemExit("kano-services.rc not as expected")
ks.write_text(st.replace("    stop android.system.desktop.security.gscd\n",
                         "    stop android.system.desktop.security.gscd\n    stop android.system.desktop.security.udsattestation\n"))
print("kano-adb.sh:", ka.read_text())
print("kano-services.rc:", ks.read_text())
# --- safe24 additions (after the safe19..safe23 additions) ---
# adb now gives live access, so the on-disk diag shrinks to what helps when adb is down: load/io-top/heartbeat every
# 120 s (heartbeat fsync'd), dmesg + logcat once at ~4 min. Less I/O on the slow stick (Bluetooth aborts after 2 s stalls).
(tree / "etc/kano-diag.sh").write_text("""#!/system/bin/sh
# KANO debug (light): state in /data/kano-diag for boots where adb is not reachable; read from the maintenance OS.
D=/data/kano-diag
mkdir -p $D
n=0
snap() { f=$1; shift; timeout -k 5 20 "$@" < /dev/null 2>&1 | timeout -k 5 30 dd of=$D/$f.tmp conv=fsync status=none; mv $D/$f.tmp $D/$f; }
while true; do
  { cat /proc/uptime /proc/loadavg; grep -E " sd[a-z]+ " /proc/diskstats; grep -E "^(cpu |procs_)" /proc/stat; } > $D/load.txt 2>&1
  ip addr show wlan0 > $D/ip.txt 2>&1
  if [ $n -eq 2 ]; then
    snap dmesg.txt /system/bin/dmesg
    snap logcat.txt /system/bin/logcat -d -b main,system,crash -v time '*:W'
  fi
  n=$((n + 1)); echo "$n $(cat /proc/uptime)" | dd of=$D/heartbeat.txt conv=fsync status=none
  sleep 120
done
""")
print("diag light:", len((tree / "etc/kano-diag.sh").read_text()), "bytes")
# --- safe25 additions (after the safe19..safe24 additions) ---
# Power button source (KANO: the ACPI Power Button, since the EC keyboard driver is blacklisted).
if prof["input"]["power_button"] == "acpi" and not (hooks and hasattr(hooks, "power_button")):
    raise SystemExit("profile input.power_button = acpi needs hooks.vendor with power_button()")
if hooks and hasattr(hooks, "power_button"):
    hooks.power_button(ctx)
# Offline setup is independent of hardware and the Wi-Fi autoconnect helper.
# Use the existing diagnostic domain, whose Binder stdio is allowed by system_server.
if os.environ.get("GBOS_SKIP_SETUP") == "1":
    cil = tree / "etc/selinux/vendor_sepolicy.cil"
    cil.write_text(cil.read_text().rstrip("\n") + "\n" + """;; cmd/settings shell callbacks run from enforcing system_server back to the helper.
(allow system_server_202604 kano_kmsg_logcat (binder (call transfer)))
""")
    offline = tree / "etc/gbos-offline-setup.sh"
    offline.write_bytes((Path(sys.argv[2]).parent / "offline-setup.sh").read_bytes())
    (tree / "etc/init/gbos-offline-setup.rc").write_text("""# Built with GBOS_SKIP_SETUP=1: skip initial online setup, including its OTA gate.
service gbos_offline_setup /system/bin/sh /vendor/etc/gbos-offline-setup.sh
    oneshot
    disabled
    user root
    seclabel u:r:kano_kmsg_logcat:s0

on property:sys.boot_completed=1
    start gbos_offline_setup
""")
    for rel in ("etc/gbos-offline-setup.sh", "etc/init/gbos-offline-setup.rc"):
        (tree / rel).chmod(0o644)
        vmap[rel] = dict(vmap["etc/init/fwupd.rc"])
    print("offline local setup enabled (SetupWizard skipped; Wi-Fi remains available)")
# GBOS_EC (opt-in; unset = nothing here runs and the vendor tree is as before): the profile's vendor side of the variant
EC = [f for f in os.environ.get("GBOS_EC", "").split(",") if f]
if EC:
    if not (hooks and hasattr(hooks, "ec")):
        raise SystemExit("GBOS_EC needs a profile whose hooks.vendor has ec() (profiles/kano/vendor.py)")
    hooks.ec(ctx, EC)
# Built-in camera (opt-in; KANO: profiles/kano/vendor.py camera()): GBOS_CAMERA_OVERLAY = the out/vendor directory of
# builder/camera/build-libcamera.sh. Unset = nothing here runs. A profile with camera.libcamera = true requires it.
CAM = os.environ.get("GBOS_CAMERA_OVERLAY", "")
if prof["camera"]["libcamera"] and not CAM:
    raise SystemExit("profile camera.libcamera = true needs GBOS_CAMERA_OVERLAY (builder/camera/build-libcamera.sh)")
if CAM:
    if not (hooks and hasattr(hooks, "camera")):
        raise SystemExit("GBOS_CAMERA_OVERLAY needs a profile whose hooks.vendor has camera() (profiles/kano/vendor.py)")
    hooks.camera(ctx, Path(CAM))
# GBOS_TEST_AUTOOFF (developer-only; empty = the autooff files are not in the image). The script and rc
# copy etc/init/fwupd.rc; the nonce is mode 0444. install() also writes $GBOS_OUT/image/autooff.nonce.
# The rc seclabel is u:r:kano_kmsg_logcat:s0; init starts it on nonencrypted (QEMU never sets
# boot_completed) and on boot_completed.
_autooff = os.environ.get("GBOS_TEST_AUTOOFF", "")
if _autooff:
    kano_autooff.install(tree, vmap, int(_autooff))
    print("kano-autooff:", _autooff, "seconds")
# A development-capable image must remain debuggable if Android framework startup fails.
# Install the authenticated key and start adbd after /data mounts; defer the
# settings-provider calls until the framework has finished booting.
if LAPIS.get("EARLY_ADB") == "1":
    # image.py requires key authentication in the platform ramdisk properties;
    # vendor_init cannot set the platform-owned ro.adb.secure property.
    # The DEV build puts GSC services in early_hal, before the boot action.
    # Stop them before that class starts, since this image blocks their TPM.
    services = tree / "etc/init/kano-services.rc"
    services.write_text(services.read_text() + "\non init\n    stop timberslide\n    stop android.system.desktop.security.gscd\n    stop android.system.desktop.security.udsattestation\n")
    ka = tree / "etc/kano-adb.sh"
    kt = ka.read_text()
    start = kt.index("for k in development_settings_enabled adb_enabled; do")
    end = kt.index("\ndone\n", start) + len("\ndone\n")
    kt = kt[:start] + 'if [ "$(getprop sys.boot_completed)" = 1 ]; then\n' + kt[start:end] + "fi\n" + kt[end:]
    ka.write_text(kt)
    rc = tree / "etc/init/kano-adb.rc"
    rc.write_text(rc.read_text() + "\non post-fs-data\n    start kano_adb\n")
# adb over TCP is opt-in: without GBOS_ADB_PUBKEY the three adb files are not part of the image.
if not os.environ.get("GBOS_ADB_PUBKEY"):
    for f in ("etc/kano-adb_keys", "etc/kano-adb.sh", "etc/init/kano-adb.rc"):
        (tree / f).unlink()
        vmap.pop(f, None)
    # none of this project's other vendor files (and not build.prop) may start adbd or open it to the network
    for f in [tree / "build.prop", *tree.glob("etc/init/kano-*.rc"), *tree.glob("etc/kano-*.sh")]:
        if f.is_file():
            t = f.read_bytes()
            if b"service.adb.listen_addrs" in t or b"kano-adb" in t or b"ro.adb.secure=0" in t:
                raise SystemExit(f"adb left enabled in {f.relative_to(tree)}")
    print("adb: not included (GBOS_ADB_PUBKEY not set)")
# --- generic-x86 target: machines whose GPU has no driver in this kernel (for example AMD, QEMU; CPU: Intel 12th gen / AMD Zen 5 or newer, see docs/COMPATIBILITY.en.md "Minimum CPU") ---
# Display: gbos_efifb.ko drives the UEFI GOP framebuffer as a DRM device named "vkms" (minigbm's dumb-buffer backend);
# vendor init reads the framebuffer geometry from /sys/kernel/boot_params/data and passes it as module parameters.
# Drawing: SwiftShader (vulkan.pastel.so, from Cuttlefish), whose JIT needs execmem in the four domains Cuttlefish allows.
if prof["display"]["stack"] == "gop-swiftshader" or prof["display"]["fallback"] == "gop-swiftshader":
    import hashlib, re, shutil
    ko, pastel = Path(os.environ["GBOS_GENERIC_KO"]), Path(os.environ["GBOS_GENERIC_PASTEL"])
    if hashlib.sha256(pastel.read_bytes()).hexdigest() != "5232e4829d8e6bba2d81cc0ff64d227504601090d2ac7b9fa105536e38c680a3":
        raise SystemExit("vulkan.pastel.so is not the pinned Cuttlefish 16373615 build")
    kb = ko.read_bytes()
    if ("vermagic=" + GKI_RELEASE + " SMP preempt mod_unload modversions ").encode() not in kb or b"name=gbos_efifb" not in kb:
        raise SystemExit("GBOS_GENERIC_KO is not a gbos_efifb.ko for this kernel")
    pin = re.search(r"^EFIFB_KO_SHA=([0-9a-f]{64})$", (Path(sys.argv[2]).parent / "kmod/build-gbos_efifb.sh").read_text(), re.M)
    if hashlib.sha256(kb).hexdigest() != LAPIS.get("EFIFB_KO_SHA", pin.group(1) if pin else None):
        raise SystemExit("GBOS_GENERIC_KO is not the gbos_efifb.ko pinned in builder/kmod/build-gbos_efifb.sh")
    print("gbos_efifb.ko sha256", hashlib.sha256(kb).hexdigest())
    for rel, src, like in (("lib64/hw/vulkan.pastel.so", pastel, "lib64/hw/vulkan.intel.so"),
                           ("lib64/gbos_efifb.ko", ko, "lib64/libhardware.so")):
        if (tree / rel).exists() or like not in vmap:
            raise SystemExit("generic: unexpected vendor layout at " + rel)
        shutil.copyfile(src, tree / rel)
        (tree / rel).chmod(0o644)
        vmap[rel] = dict(vmap[like])
    bt = (tree / "build.prop").read_text()
    if bt.splitlines().count("ro.hardware.vulkan=intel") != 1:
        raise SystemExit("generic: ro.hardware.vulkan=intel not exactly once")
    if "vendor.hwc.drm.device=" in bt:
        raise SystemExit("generic: vendor.hwc.drm.device already set")
    native_fallback = prof["display"]["fallback"] == "gop-swiftshader"
    bt = bt.replace("\nro.hardware.vulkan=intel\n", "\n" if native_fallback else "\nro.hardware.vulkan=pastel\n", 1)
    # SwiftShader setup as on Cuttlefish (guest_swiftshader): apps (hwui) and SurfaceFlinger draw with GLES through
    # ANGLE on SwiftShader Vulkan; hwui's and RenderEngine's own Vulkan paths need external semaphore sync fds,
    # which SwiftShader does not export
    renderengine_prop = ("ro.surface_flinger.renderengine_graphite_vulkan"
                         if LAPIS.get("RENDERENGINE_LAYOUT") == "graphite" else "debug.renderengine.vulkan")
    for k in ("ro.hwui.use_vulkan", renderengine_prop):
        if bt.splitlines().count(k + "=true") != 1:
            raise SystemExit("generic: " + k + "=true not exactly once")
        bt = bt.replace("\n" + k + "=true\n", "\n" if native_fallback else "\n" + k + "=false\n", 1)
    if LAPIS.get("RENDERENGINE_LAYOUT") == "graphite" and not native_fallback:
        bt += "\ndebug.renderengine.backend=skiaglthreaded\n"
    # gbos_efifb: card0 is minigbm's allocator device, card1 the scanout device the composer must be DRM master of
    (tree / "build.prop").write_text(bt.rstrip("\n") + ("\n" if native_fallback else "\n# generic-x86: composer on the gbos_efifb scanout device\nvendor.hwc.drm.device=/dev/dri/card1\n"))
    (tree / "etc/gbos-efifb.sh").write_text("""#!/system/bin/sh
# generic-x86: load gbos_efifb.ko for the framebuffer the UEFI firmware set up (GOP), described by the screen_info at the
# start of boot_params. mksh arithmetic is 32-bit, so the (possibly 64-bit) address is assembled as hex text.
log() { echo "<6>gbos-efifb: $*" > /dev/kmsg; }
B=/sys/kernel/boot_params/data
set -- $(od -An -v -tu1 -N64 $B 2> /dev/null)
[ $# -eq 64 ] || { log "cannot read $B"; exit 1; }
i=0; for v in "$@"; do eval "b$i=$v"; i=$((i + 1)); done
# orig_video_isVGA 0x70 = VIDEO_TYPE_EFI (a GOP framebuffer)
[ "$b15" -eq 112 ] || { log "no EFI framebuffer (video type $b15)"; exit 0; }
w=$((b18 + b19 * 256)); h=$((b20 + b21 * 256)); depth=$((b22 + b23 * 256)); stride=$((b36 + b37 * 256))
lo=$(printf '%02x%02x%02x%02x' $b27 $b26 $b25 $b24)
hi=00000000; [ $((b54 & 2)) -ne 0 ] && hi=$(printf '%02x%02x%02x%02x' $b61 $b60 $b59 $b58)
log "GOP ${w}x${h} depth $depth stride $stride base 0x$hi$lo red_pos $b39"
/system/bin/insmod /vendor/lib64/gbos_efifb.ko fb_base=0x$hi$lo fb_width=$w fb_height=$h fb_stride=$stride fb_bpp=$depth fb_red_pos=$b39 \\
    || log "insmod failed"
""")
    if native_fallback:
        loader = tree / "etc/gbos-efifb.sh"
        text = loader.read_text()
        select = """# Native Intel DRM is preferred; software rendering also boots the recovery image in QEMU.
native_drm() {
    for card in /sys/class/drm/card[0-9]*/device/driver; do
        driver=$(readlink "$card" 2>/dev/null)
        case "$driver" in */xe|*/i915) return 0 ;; esac
    done
    return 1
}
has_intel_gpu=false
for dev in /sys/bus/pci/devices/*; do
    [ "$(cat "$dev/vendor" 2>/dev/null)" = 0x8086 ] || continue
    case "$(cat "$dev/class" 2>/dev/null)" in 0x03*) has_intel_gpu=true ;; esac
done
if $has_intel_gpu; then
    n=0
    while ! native_drm && [ "$n" -lt 80 ]; do sleep 0.25; n=$((n + 1)); done
fi
if native_drm; then
    setprop ro.hardware.vulkan intel
    setprop ro.hwui.use_vulkan true
    setprop debug.renderengine.vulkan true
    echo '<6>gbos-graphics: native Intel DRM selected' > /dev/kmsg
    exit 0
fi
setprop ro.hardware.vulkan pastel
setprop ro.hwui.use_vulkan false
setprop debug.renderengine.vulkan false
setprop vendor.hwc.drm.device /dev/dri/card1
echo '<6>gbos-graphics: GOP software rendering selected' > /dev/kmsg
"""
        if LAPIS.get("RENDERENGINE_LAYOUT") == "graphite":
            select = select.replace("setprop debug.renderengine.vulkan true", "setprop ro.surface_flinger.renderengine_graphite_vulkan true")
            select = select.replace("setprop debug.renderengine.vulkan false", "setprop ro.surface_flinger.renderengine_graphite_vulkan false\nsetprop debug.renderengine.backend skiaglthreaded")
        loader.write_text(text.replace("B=/sys/kernel/boot_params/data", select + "\nB=/sys/kernel/boot_params/data", 1))
    (tree / "etc/init/gbos-efifb.rc").write_text("""# generic-x86: DRM device on the UEFI GOP framebuffer, before the composer HAL starts (see /vendor/etc/gbos-efifb.sh)
service gbos_efifb /system/bin/sh /vendor/etc/gbos-efifb.sh
    oneshot
    disabled
    user root
    seclabel u:r:gbos_efifb:s0

on init
    exec_start gbos_efifb
""")
    for f in ("etc/gbos-efifb.sh", "etc/init/gbos-efifb.rc"):
        (tree / f).chmod(0o644)
        vmap[f] = dict(vmap["etc/init/fwupd.rc"])
    cg = tree / "etc/selinux/vendor_sepolicy.cil"
    cg.write_text(cg.read_text().rstrip("\n") + """
;; generic-x86: the GOP framebuffer loader (reads boot_params, insmod with the geometry), permissive like kano_bootfix
(type gbos_efifb)
(roletype object_r gbos_efifb)
(typeattributeset domain (gbos_efifb))
(typepermissive gbos_efifb)
(allow init_202604 gbos_efifb (process (transition siginh rlimitinh)))
(dontaudit init_202604 gbos_efifb (process (noatsecure)))
;; SwiftShader's Reactor JIT needs writable+executable memory; Cuttlefish allows the same four domains
;; (device/google/cuttlefish/shared/swiftshader/sepolicy). init compiles this policy with secilc -N.
(allow bootanim_202604 self (process (execmem)))
(allow surfaceflinger_202604 self (process (execmem)))
(allow system_server_202604 self (process (execmem)))
(allow zygote_202604 self (process (execmem)))
""")
    print("generic-x86: gbos_efifb + SwiftShader added")
if os.environ.get("GBOS_DEV_KERNEL_DIR"):
    import shutil
    dev_mod_dir = Path(os.environ["GBOS_DEV_KERNEL_DIR"]) / "modules"
    ref = next(k for k in sorted(vmap) if k.startswith("lib64/") and vmap[k]["selinux"] == "u:object_r:vendor_file:s0"
               and vmap[k]["mode"] == 0o644 and (tree / k).is_file() and not (tree / k).is_symlink())
    (tree / "lib/dev-modules").mkdir()
    (tree / "lib/dev-modules").chmod(0o755)
    vmap["lib/dev-modules"] = dict(vmap["lib"])
    dev_files = sorted(
        [p.name for p in dev_mod_dir.glob("*.ko")]
        + [m for m in ("modules.dep", "modules.alias", "modules.softdep", "modules.order") if (dev_mod_dir / m).is_file()]
    )
    if not any(f.endswith(".ko") for f in dev_files) or "modules.dep" not in dev_files:
        raise SystemExit("GBOS_DEV_KERNEL_DIR/modules missing .ko or modules.dep")
    for fname in dev_files:
        src = dev_mod_dir / fname
        if fname.endswith(".ko") and b"SMP preempt mod_unload modversions " not in src.read_bytes():
            raise SystemExit(f"dev-modules: {fname} missing expected vermagic flags")
        rel = f"lib/dev-modules/{fname}"
        shutil.copyfile(src, tree / rel)
        (tree / rel).chmod(0o644)
        vmap[rel] = dict(vmap[ref])
    print(f"dev-modules: {len(dev_files)} files staged in /vendor/lib/dev-modules")
if prof["model"]["id"] == "amd-hx370":
    import hashlib, re, shutil, subprocess
    root = Path(sys.argv[2]).resolve().parents[1]
    script = (root / "tools/mesa/build-radv.sh").read_text()
    checker = root / "tools/mesa/check-radv-elf.py"

    def pinned(name):
        m = re.search(rf"^{name}=([0-9a-f]{{64}})$", script, re.M)
        if not m:
            raise SystemExit(f"{name} is not a 64-hex pin in tools/mesa/build-radv.sh")
        return m.group(1)

    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def place(rel, src, like, mode):
        if (tree / rel).exists() or like not in vmap:
            raise SystemExit("amd-hx370: unexpected vendor layout at " + rel)
        shutil.copyfile(src, tree / rel)
        (tree / rel).chmod(mode)
        meta = dict(vmap[like])
        meta["mode"] = mode
        vmap[rel] = meta

    radv = Path(os.environ["GBOS_RADV"])
    probe = Path(os.environ["GBOS_RADV_PROBE"])
    vkprop = Path(os.environ["GBOS_RADV_VKPROP"])
    pastel = Path(os.environ["GBOS_GENERIC_PASTEL"])
    ko = Path(os.environ["GBOS_GENERIC_KO"])
    if sha(radv) not in {pinned("RADV_SHA"), pinned("RADV_STRIP_UNNEEDED_SHA")}:
        raise SystemExit("vulkan.radeon.so is not the build pinned in tools/mesa/build-radv.sh")
    for label, path, pin_name in (
        ("radv-probe", probe, "PROBE_SHA"),
        ("libvkprop.so", vkprop, "VKPROP_SHA"),
    ):
        if sha(path) != pinned(pin_name):
            raise SystemExit(f"{label} is not the build pinned in tools/mesa/build-radv.sh")
    if sha(pastel) != "5232e4829d8e6bba2d81cc0ff64d227504601090d2ac7b9fa105536e38c680a3":
        raise SystemExit("vulkan.pastel.so is not the pinned Cuttlefish 16373615 build")
    kb = ko.read_bytes()
    if ("vermagic=" + GKI_RELEASE + " SMP preempt mod_unload modversions ").encode() not in kb or b"name=gbos_efifb" not in kb:
        raise SystemExit("GBOS_GENERIC_KO is not a gbos_efifb.ko for this kernel")
    efifb_pin = re.search(r"^EFIFB_KO_SHA=([0-9a-f]{64})$", (Path(sys.argv[2]).parent / "kmod/build-gbos_efifb.sh").read_text(), re.M)
    if sha(ko) != LAPIS.get("EFIFB_KO_SHA", efifb_pin.group(1) if efifb_pin else None):
        raise SystemExit("GBOS_GENERIC_KO is not the gbos_efifb.ko pinned in builder/kmod/build-gbos_efifb.sh")
    chk = subprocess.run([sys.executable, str(checker), str(radv)], capture_output=True, text=True)
    if chk.returncode != 0:
        raise SystemExit("vulkan.radeon.so ELF check failed:\n" + chk.stdout + chk.stderr)
    notices = root / "tools/mesa/NOTICES.txt"
    if not notices.is_file():
        raise SystemExit("missing tools/mesa/NOTICES.txt")
    notices_text = notices.read_text()
    for pin_name in ("MESA_SHA256", "LIBDRM_SHA256", "NDK_SHA256", "GLSLANG_SHA256"):
        if pinned(pin_name) not in notices_text:
            raise SystemExit(f"tools/mesa/NOTICES.txt does not list the pinned {pin_name}")
    bin_like = next(k for k in sorted(vmap) if k.startswith("bin/") and not k.startswith("bin/hw/")
                    and vmap[k]["mode"] & 0o111 and (tree / k).is_file() and not (tree / k).is_symlink())
    lib_like = next(k for k in sorted(vmap) if k.startswith("lib64/") and vmap[k]["selinux"] == "u:object_r:vendor_file:s0"
                    and vmap[k]["mode"] == 0o644 and (tree / k).is_file() and not (tree / k).is_symlink())
    place("lib64/hw/vulkan.radeon.so", radv, "lib64/hw/vulkan.intel.so", 0o644)
    place("lib64/hw/vulkan.pastel.so", pastel, "lib64/hw/vulkan.intel.so", 0o644)
    place("lib64/gbos_efifb.ko", ko, "lib64/libhardware.so", 0o644)
    place("lib64/libvkprop.so", vkprop, lib_like, 0o644)
    place("bin/radv-probe", probe, bin_like, 0o755)
    place("etc/NOTICES-amd-hx370-radv.txt", notices, "etc/init/fwupd.rc", 0o644)
    (tree / "etc/gbos-efifb.sh").write_text("""#!/system/bin/sh
# GOP framebuffer as a DRM device named vkms (minigbm). Loaded only by the default safe entry.
log() { echo "<6>gbos-efifb: $*" > /dev/kmsg; }
B=/sys/kernel/boot_params/data
set -- $(od -An -v -tu1 -N64 $B 2> /dev/null)
[ $# -eq 64 ] || { log "cannot read $B"; exit 1; }
i=0; for v in "$@"; do eval "b$i=$v"; i=$((i + 1)); done
[ "$b15" -eq 112 ] || { log "no EFI framebuffer (video type $b15)"; exit 0; }
w=$((b18 + b19 * 256)); h=$((b20 + b21 * 256)); depth=$((b22 + b23 * 256)); stride=$((b36 + b37 * 256))
lo=$(printf '%02x%02x%02x%02x' $b27 $b26 $b25 $b24)
hi=00000000; [ $((b54 & 2)) -ne 0 ] && hi=$(printf '%02x%02x%02x%02x' $b61 $b60 $b59 $b58)
log "GOP ${w}x${h} depth $depth stride $stride base 0x$hi$lo red_pos $b39"
/system/bin/insmod /vendor/lib64/gbos_efifb.ko fb_base=0x$hi$lo fb_width=$w fb_height=$h fb_stride=$stride fb_bpp=$depth fb_red_pos=$b39 \\
    || log "insmod failed"
""")
    (tree / "etc/amd-hx370-graphics.sh").write_text("""#!/system/bin/sh
# amd-hx370 boot-entry graphics selector (run synchronously by init on early-boot, before surfaceflinger/hwcomposer/zygote).
# No runtime graphics switching: the systemd-boot entry chosen at boot time decides the stack.
#   - Default "Googlebook AMD HX370 safe" (no amdgpu.modeset=1 on /proc/cmdline; module_blacklist includes amdgpu):
#     never modprobes amdgpu; loads gbos_efifb.ko for the UEFI GOP framebuffer, selects the vkms scanout card
#     (the later gbos_efifb device: id 1, not the id 0 allocator), and selects SwiftShader (pastel).
#   - "Googlebook AMD HX370 native amdgpu (experimental)" (amdgpu.modeset=1 on /proc/cmdline):
#     never loads gbos_efifb.ko; modprobes amdgpu once from /vendor/lib/dev-modules, polls sysfs up to 20 x 0.1s for
#     DRIVER=amdgpu, selects RADV (radeon), and points vendor.hwc.drm.device at the DRM card whose sysfs device/uevent
#     reports DRIVER=amdgpu (preferring a connected output).
KMSG="${GBOS_KMSG_FILE:-/dev/kmsg}"
CMDLINE="${GBOS_CMDLINE_FILE:-/proc/cmdline}"
MODULES="${GBOS_MODULES_FILE:-/proc/modules}"
SYSFS_DRM="${GBOS_SYSFS_DRM:-/sys/class/drm}"
MODPROBE="${GBOS_MODPROBE_BIN:-/vendor/bin/modprobe}"
SETPROP="${GBOS_SETPROP_BIN:-setprop}"
GETPROP="${GBOS_GETPROP_BIN:-getprop}"
EFIFB_SH="${GBOS_EFIFB_SH:-/vendor/etc/gbos-efifb.sh}"
SH_BIN="${GBOS_SH_BIN:-/system/bin/sh}"
SLEEP_BIN="${GBOS_SLEEP_BIN:-sleep}"
log() { echo "<6>amd-hx370: $*" >> "$KMSG"; }

select_amdgpu_card() {
  first=""
  connected=""
  for u in "$SYSFS_DRM"/card[0-9]*/device/uevent; do
    [ -f "$u" ] || continue
    grep -q '^DRIVER=amdgpu$' "$u" || continue
    c_dir="${u%/device/uevent}"
    c_name="${c_dir##*/}"
    case "$c_name" in
      card[0-9]*-*) continue ;;
      card[0-9]*) ;;
      *) continue ;;
    esac
    [ -n "$first" ] || first="/dev/dri/$c_name"
    for st in "$c_dir"/"$c_name"-*/status "$SYSFS_DRM"/"$c_name"-*/status; do
      [ -f "$st" ] || continue
      if [ "$(cat "$st" 2>/dev/null)" = "connected" ]; then
        connected="/dev/dri/$c_name"
        break
      fi
    done
    [ -n "$connected" ] && break
  done
  if [ -n "$connected" ]; then
    echo "$connected"
  elif [ -n "$first" ]; then
    echo "$first"
  fi
}

wait_amdgpu_card() {
  i=0
  while [ "$i" -lt 20 ]; do
    c="$(select_amdgpu_card)"
    if [ -n "$c" ]; then
      echo "$c"
      return 0
    fi
    i=$((i + 1))
    "$SLEEP_BIN" 0.1
  done
  select_amdgpu_card
}

select_vkms_scanout_card() {
  # gbos_efifb registers two DRM devices, both DRIVER=gbos_efifb with a Virtual connector:
  # id 0 (allocator, lower card number; minigbm takes DRM master) then id 1 (scanout).
  # Keep the highest card index whose device/uevent matches DRIVER=(gbos_efifb|vkms).
  # If sysfs uevent files are unreadable, fall back to /dev/dri/card1 (the scanout index).
  scanout=""
  scanout_n=-1
  for v in "$SYSFS_DRM"/card[0-9]*/card[0-9]*-Virtual-* "$SYSFS_DRM"/card[0-9]*-Virtual-*; do
    [ -e "$v" ] || continue
    v_name="${v##*/}"
    c_name="${v_name%%-Virtual-*}"
    case "$c_name" in
      card[0-9]*-*) continue ;;
      card[0-9]*) ;;
      *) continue ;;
    esac
    n="${c_name#card}"
    case "$n" in
      *[!0-9]*) continue ;;
    esac
    u="$SYSFS_DRM/$c_name/device/uevent"
    if [ -f "$u" ] && grep -Eq '^DRIVER=(gbos_efifb|vkms)$' "$u"; then
      if [ "$n" -gt "$scanout_n" ]; then
        scanout="/dev/dri/$c_name"
        scanout_n=$n
      fi
    fi
  done
  if [ -n "$scanout" ]; then
    echo "$scanout"
  else
    echo "/dev/dri/card1"
  fi
}

"$SETPROP" vendor.gbos.radv_probe radeon
if grep -Eq '(^| )amdgpu\\.modeset=1( |$)' "$CMDLINE"; then
  "$MODPROBE" --dirname=/vendor/lib/dev-modules amdgpu
  log "native entry: modprobe amdgpu exit $?"
  card="$(wait_amdgpu_card)"
  echo "<6>gbos-dev-modules: $(cut -d' ' -f1 "$MODULES" | tr '\\n' ' ')" >> "$KMSG"
  "$SETPROP" ro.hardware.vulkan radeon
  "$SETPROP" ro.hwui.use_vulkan true
  "$SETPROP" debug.renderengine.vulkan true
  if [ -n "$card" ]; then
    "$SETPROP" vendor.hwc.drm.device "$card"
    log "native entry: amdgpu card=$card ro.hardware.vulkan=$("$GETPROP" ro.hardware.vulkan) hwui=$("$GETPROP" ro.hwui.use_vulkan) renderengine=$("$GETPROP" debug.renderengine.vulkan)"
  else
    log "native entry: no DRIVER=amdgpu card in $SYSFS_DRM (black screen expected; reboot into 'Googlebook AMD HX370 safe')"
  fi
  exit 0
fi

"$SETPROP" ro.hardware.vulkan pastel
"$SETPROP" ro.hwui.use_vulkan false
"$SETPROP" debug.renderengine.vulkan false
log "safe entry: amdgpu blacklisted; vulkan=pastel; gbos_efifb for the GOP framebuffer"
"$SH_BIN" "$EFIFB_SH"
scanout_card="$(select_vkms_scanout_card)"
"$SETPROP" vendor.hwc.drm.device "$scanout_card"
echo "<6>gbos-dev-modules: $(cut -d' ' -f1 "$MODULES" | tr '\\n' ' ')" >> "$KMSG"
log "safe entry: ro.hardware.vulkan=$("$GETPROP" ro.hardware.vulkan) hwui=$("$GETPROP" ro.hwui.use_vulkan) renderengine=$("$GETPROP" debug.renderengine.vulkan) drm.device=$scanout_card"
""")
    (tree / "etc/init/amd-hx370-graphics.rc").write_text("""# amd-hx370: boot-entry graphics selector (safe: GOP + pastel; native: amdgpu + radeon).
# early-boot runs synchronously before surfaceflinger, drm_hwcomposer, graphics.allocator, and zygote.
service amd_hx370_graphics /system/bin/sh /vendor/etc/amd-hx370-graphics.sh
    oneshot
    disabled
    user root
    seclabel u:r:gbos_efifb:s0

service amd_hx370_radv_probe /vendor/bin/radv-probe
    oneshot
    disabled
    user root
    seclabel u:r:gbos_efifb:s0
    setenv LD_PRELOAD /vendor/lib64/libvkprop.so

on early-boot
    exec_start amd_hx370_graphics

on property:sys.boot_completed=1
    start amd_hx370_radv_probe
""")
    for f in ("etc/gbos-efifb.sh", "etc/amd-hx370-graphics.sh", "etc/init/amd-hx370-graphics.rc"):
        (tree / f).chmod(0o644)
        vmap[f] = dict(vmap["etc/init/fwupd.rc"])
    logcat = tree / "etc/kano-logcat.sh"
    old = "'*:E' SystemServer:I"
    text = logcat.read_text()
    if old not in text:
        raise SystemExit("amd-hx370: kano-logcat filter not found")
    logcat.write_text(text.replace(old, "'*:E' vulkan:V radv-probe:V SystemServer:I", 1))
    cg = tree / "etc/selinux/vendor_sepolicy.cil"
    cg.write_text(cg.read_text().rstrip("\n") + """
;; amd-hx370: GOP safe-entry loader and the RADV probe. Permissive like kano_bootfix.
;; noatsecure is allowed so the kernel does not set AT_SECURE=1 and strip LD_PRELOAD=libvkprop.so on amd_hx370_radv_probe.
;; SwiftShader (pastel) needs the same execmem allows as profiles/generic-x86.
(type gbos_efifb)
(roletype object_r gbos_efifb)
(typeattributeset domain (gbos_efifb))
(typepermissive gbos_efifb)
(allow init_202604 gbos_efifb (process (transition siginh rlimitinh noatsecure)))
(allow bootanim_202604 self (process (execmem)))
(allow surfaceflinger_202604 self (process (execmem)))
(allow system_server_202604 self (process (execmem)))
(allow zygote_202604 self (process (execmem)))
""")
    import gralloc
    if gralloc.install_gralloc(prof, tree, vmap, place):
        notices = root / "tools/minigbm/NOTICES.txt"
        if not notices.is_file():
            raise SystemExit("missing tools/minigbm/NOTICES.txt")
        place("etc/NOTICES-amd-hx370-minigbm.txt", notices, "etc/init/fwupd.rc", 0o644)
        installed = hashlib.sha256((tree / gralloc.REL).read_bytes()).hexdigest()
        if installed != prof["gralloc"]["sha256"]:
            raise SystemExit(
                "installed libminigbm_gralloc.so sha256 "
                + installed
                + " differs from profile gralloc.sha256 "
                + prof["gralloc"]["sha256"])
        print("amd-hx370: libminigbm_gralloc.so", installed)
    print("amd-hx370: vulkan.radeon.so", sha(radv), "safe default (pastel + gbos_efifb) + native amdgpu entry")
policy_version = (tree / "etc/selinux/plat_sepolicy_vers.txt").read_text().strip()
if policy_version != LAPIS.get("PLAT_SEPOLICY_VERSION", "202604"):
    raise SystemExit("vendor SELinux version does not match the selected recovery")
if policy_version != "202604":
    cil.write_text(cil.read_text().replace("_202604", "_" + policy_version))
n = rebuild.write_configs(
    "/vendor",
    tree,
    vmap,
    tmp / "vendor_file_contexts",
    tmp / "vendor_fs_config",
)
print(f"file_contexts {n}")
PY

echo "mkfs vendor"
rm -f "$TMP/vendor.erofs"
ot mkfs.erofs \
    -z lz4hc,level=9 \
    -b 4096 \
    -T 1230768000 \
    -U "$VENDOR_UUID" \
    --mount-point /vendor \
    --fs-config-file "$TMP/vendor_fs_config" \
    --file-contexts "$TMP/vendor_file_contexts" \
    "$TMP/vendor.erofs" \
    "$TMP/vendor-tree"

python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import rebuild; u = rebuild.fs_uuid(sys.argv[2]); print("Filesystem UUID:", u); sys.exit(u != sys.argv[3])' \
    "$HERE/lib" "$TMP/vendor.erofs" "$VENDOR_UUID"

echo "compare extracted trees"
rm -rf "$TMP/vendor-tree-base" "$TMP/vendor-tree-new"
mkdir -p "$TMP/vendor-tree-base" "$TMP/vendor-tree-new"
ot fsck.erofs --extract="$TMP/vendor-tree-base" --overwrite --preserve-perms "$TMP/vendor.erofs.src"
ot fsck.erofs --extract="$TMP/vendor-tree-new" --overwrite --preserve-perms "$TMP/vendor.erofs"

python3 - "$TMP" "$HERE/lib" << 'PY'
import hashlib, importlib.util, os, sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
import rebuild
import devprofile
import gralloc

tmp = Path(sys.argv[1])

def index(root: Path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        real_dirs = []
        for name in dirnames:
            p = Path(dirpath) / name
            rel = name if rel_dir == "" else rel_dir + "/" + name
            if p.is_symlink():
                out[rel] = ("symlink", os.readlink(p))
            else:
                real_dirs.append(name)
                out[rel] = ("dir", None)
        dirnames[:] = real_dirs
        for name in filenames:
            p = Path(dirpath) / name
            rel = name if rel_dir == "" else rel_dir + "/" + name
            if p.is_symlink():
                out[rel] = ("symlink", os.readlink(p))
            elif p.is_file():
                out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
            else:
                raise SystemExit("special file " + rel)
    return out

old = index(tmp / "vendor-tree-base")
new = index(tmp / "vendor-tree-new")
only_old = sorted(set(old) - set(new))
only_new = sorted(set(new) - set(old))
changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
print("only in kano-real vendor:", only_old)
print("only in rebuilt vendor:", only_new)
print("content differs:", changed)
expect_old = sorted(["etc/init/desktop_kernel_max_rollforward_boot.rc", "etc/selinux/precompiled_sepolicy", "etc/vintf/manifest/android.hardware.security.secretkeeper.trusty.xml", "etc/vintf/manifest/android.hardware.weaver-service.android-desktop.xml"] + (tmp / "removed.txt").read_text().split())
# what this project changes on every model, plus what the profile adds (firmware, hooks)
prof = devprofile.load(os.environ["GBOS_PROFILE"])
p_new, p_changed = [], []
if prof["hooks"]["vendor"]:
    spec = importlib.util.spec_from_file_location("profile_vendor_hooks", Path(prof["_dir"]) / prof["hooks"]["vendor"])
    hooks = importlib.util.module_from_spec(spec); spec.loader.exec_module(hooks)
    if hasattr(hooks, "expected"):
        p_new, p_changed = hooks.expected(prof)
    EC = [f for f in os.environ.get("GBOS_EC", "").split(",") if f]
    if EC and hasattr(hooks, "ec_expected"):
        e_new, e_changed = hooks.ec_expected(EC)
        p_new, p_changed = p_new + e_new, p_changed + e_changed
    if os.environ.get("GBOS_CAMERA_OVERLAY") and hasattr(hooks, "camera_expected"):
        c_new, c_changed = hooks.camera_expected(Path(os.environ["GBOS_CAMERA_OVERLAY"]), set(old))
        p_new, p_changed = p_new + c_new, p_changed + c_changed
fw_new = []
for fwe in prof["firmware"]:
    parts = fwe["path"].split("/")
    fw_new += ["firmware/" + "/".join(parts[:i]) for i in range(1, len(parts)) if "firmware/" + "/".join(parts[:i]) not in old]
    fw_new.append("firmware/" + fwe["path"])
expect_changed = sorted(set(["build.prop", "etc/fstab.android-desktop", "etc/init/android.hardware.security.secretkeeper.trusty.desktop.rc", "etc/init/hal_desktop_charge_control_default.rc", "etc/selinux/vendor_sepolicy.cil"] + p_changed))
expect_new = sorted(set(["etc/kano-adb_keys", "etc/kano-adb.sh", "etc/init/kano-adb.rc", "etc/init/kano-services.rc", "etc/kano-tone-1k.wav", "etc/kano-audiotest.sh", "etc/init/kano-audiotest.rc", "etc/init/kano-bootfix.rc", "etc/init/kano-diag.rc", "etc/init/kano-hw.rc", "etc/init/kano-logcat.rc", "etc/kano-diag.sh", "etc/kano-logcat.sh", "etc/kano-wifi.sh"] + p_new + fw_new))
if os.environ.get("GBOS_SKIP_SETUP") == "1":
    expect_new = sorted(expect_new + ["etc/gbos-offline-setup.sh", "etc/init/gbos-offline-setup.rc"])
if not os.environ.get("GBOS_ADB_PUBKEY"):
    expect_new = sorted(x for x in expect_new if "kano-adb" not in x)
kano_kmods = [k for k in ("ac", "gbos_wakefilter") if k in prof["kmods"]]
if kano_kmods:
    expect_new = sorted(expect_new + ["lib/kano", "lib/kano/modules.dep"] + [f"lib/kano/{k}.ko" for k in kano_kmods])
if prof["display"]["stack"] == "gop-swiftshader" or prof["display"]["fallback"] == "gop-swiftshader":
    expect_new = sorted(expect_new + ["etc/gbos-efifb.sh", "etc/init/gbos-efifb.rc", "lib64/gbos_efifb.ko", "lib64/hw/vulkan.pastel.so"])
if os.environ.get("GBOS_TEST_AUTOOFF"):
    expect_new = sorted(expect_new + ["etc/kano-autooff.sh", "etc/init/kano-autooff.rc", "etc/kano-autooff.nonce"])
if prof["model"]["id"] == "amd-hx370":
    expect_new = sorted(expect_new + [
        "bin/radv-probe", "etc/NOTICES-amd-hx370-radv.txt", "etc/amd-hx370-graphics.sh", "etc/gbos-efifb.sh",
        "etc/init/amd-hx370-graphics.rc",
        "lib64/gbos_efifb.ko", "lib64/hw/vulkan.pastel.so", "lib64/hw/vulkan.radeon.so", "lib64/libvkprop.so"])
if os.environ.get("GBOS_DEV_KERNEL_DIR"):
    dev_mod_dir = Path(os.environ["GBOS_DEV_KERNEL_DIR"]) / "modules"
    dev_files = sorted(
        [p.name for p in dev_mod_dir.glob("*.ko")]
        + [m for m in ("modules.dep", "modules.alias", "modules.softdep", "modules.order") if (dev_mod_dir / m).is_file()]
    )
    expect_new = sorted(expect_new + ["lib/dev-modules"] + [f"lib/dev-modules/{f}" for f in dev_files])
import gralloc
expect_changed = gralloc.changed_paths(prof, expect_changed)
if prof.get("gralloc"):
    expect_new = sorted(set(expect_new + ["etc/NOTICES-amd-hx370-minigbm.txt"]))
    rel = gralloc.REL
    got = new.get(rel)
    want_sha = prof["gralloc"]["sha256"]
    if got != ("file", want_sha):
        raise SystemExit(
            "rebuilt vendor lib64/libminigbm_gralloc.so is "
            + repr(got)
            + ", profile gralloc.sha256 is "
            + want_sha)
    # A pin equal to the bytes already in the base image is not a content change.
    if old.get(rel) == ("file", want_sha):
        expect_changed = [p for p in expect_changed if p != rel]
if only_old != expect_old or only_new != expect_new or changed != expect_changed:
    raise SystemExit("vendor tree diff is not the expected set of KANO changes (see the three lists above)")

block = (
    "on boot\n"
    "    start fwupd\n"
    "    # Start the daemon in the background first\n"
    "    start fwupd_script\n"
    "\n"
)
if (tmp / "vendor-tree-new/etc/init/fwupd.rc").exists() or (tmp / "vendor-tree-new/bin/fwupd-binder").exists():
    raise SystemExit("fwupd still present in the rebuilt vendor")

def meta(path):
    data, blksz, meta_off, xattr, root = rebuild.load_image(path)
    m, _h = rebuild.walk_image(str(path), root, data, blksz, meta_off, xattr)
    return m

oldm = meta(tmp / "vendor.erofs.src")
newm = meta(tmp / "vendor.erofs")
gone = sorted(set(oldm) - set(newm))
extra = sorted(set(newm) - set(oldm))
bad = []
for key in set(oldm) & set(newm):
    a, b = oldm[key], newm[key]
    if (a["mode"], a["uid"], a["gid"], a["selinux"], a["caps"]) != (b["mode"], b["uid"], b["gid"], b["selinux"], b["caps"]):
        bad.append(key)
print("metadata only old:", gone)
print("metadata only new:", extra)
print("metadata differs:", bad[:20], "count", len(bad))
if gone != expect_old or extra != expect_new or bad:
    raise SystemExit("vendor uid/gid/mode/selinux/capabilities drifted")
print("vendor tree ok")
PY

echo "avb hashtree footer"
cp "$TMP/vendor.erofs" "$TMP/vendor.img"
ot avbtool add_hashtree_footer \
    --image "$TMP/vendor.img" \
    --partition_name vendor \
    --partition_size "$VENDOR_SIZE" \
    --hash_algorithm sha256 \
    --salt "$SALT" \
    --block_size 4096 \
    --fec_num_roots 2 \
    --prop com.android.build.vendor.os_version:17 \
    --prop "com.android.build.vendor.fingerprint:$VENDOR_FINGERPRINT" \
    --prop "com.android.build.vendor.security_patch:$VENDOR_SECURITY_PATCH"

python3 -c 'import os,sys; n=os.path.getsize(sys.argv[1]); w=int(sys.argv[2]); print(n,w); assert n==w' \
    "$TMP/vendor.img" "$VENDOR_SIZE"

echo "re-sign vbmeta"
VBMETA_STRUCT_SIZE=$(python3 "$HERE/lib/lapis.py" --get VBMETA_STRUCT_SIZE)
dd if="$IMG" of="$TMP/vbmeta.old.img" bs=1 skip="$VBMETA_OFF" count="${VBMETA_STRUCT_SIZE:-6720}" status=none
ot avbtool update_partition_descriptor \
    --image "$TMP/vbmeta.old.img" \
    --partition_image "$TMP/vendor.img" \
    --output "$TMP/vbmeta.new.img" \
    --algorithm SHA256_RSA4096 \
    --key "$KEY" \
    --rollback_index "$VBMETA_ROLLBACK_INDEX"
ot avbtool info_image --image "$TMP/vbmeta.new.img" > "$TMP/vbmeta.new.info"
ot avbtool calculate_vbmeta_digest --image "$TMP/vbmeta.new.img" --hash_algorithm sha256 \
    | tee "$WORK/logs/vbmeta.digest.txt"

echo "read ESP initrd"
mcopy -i "$IMG@@$ESP_OFF" ::/initrd.img "$TMP/esp-initrd.img"

python3 - "$IMG" "$TMP" "$STANDALONE" "$HERE/lib" << 'PY'
import os, struct, sys
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from early_isa import wrap_init

img = Path(sys.argv[1])
tmp = Path(sys.argv[2])
standalone = Path(sys.argv[3])
env = {}
for line in (tmp / "offsets.env").read_text().splitlines():
    k, v = line.split("=", 1)
    env[k] = v
vendor_off = int(env["VENDOR_OFF"])
vendor_size = int(env["VENDOR_SIZE"])
vbmeta_off = int(env["VBMETA_OFF"])
boot_uuid = env["BOOT_UUID"]
vendor = (tmp / "vendor.img").read_bytes()
if len(vendor) != vendor_size:
    raise SystemExit("vendor size")
vb = (tmp / "vbmeta.new.img").read_bytes()
if vb[:4] != b"AVB0":
    raise SystemExit("vbmeta magic")
auth, aux = struct.unpack_from(">QQ", vb, 12)
vb_size = 256 + auth + aux
if len(vb) != vb_size:
    raise SystemExit(f"vbmeta file {len(vb)} != struct {vb_size}")
digest = (tmp.parent / "logs/vbmeta.digest.txt").read_text().strip().split()[0]
if len(digest) != 64:
    raise SystemExit("bad digest")

def split_bc(data: bytes):
    magic = b"#BOOTCONFIG\n"
    if not data.endswith(magic):
        raise SystemExit("bootconfig magic")
    end = len(data) - len(magic)
    size, csum = struct.unpack_from("<II", data, end - 8)
    start = end - 8 - size
    bc = data[start:start + size]
    if (sum(bc) & 0xFFFFFFFF) != csum:
        raise SystemExit("bootconfig checksum")
    return data[:start], bc.decode()

archive, _esp_bc = split_bc((tmp / "esp-initrd.img").read_bytes())
_std_arch, std_bc = split_bc(standalone.read_bytes())

# Ship first-stage ISA compatibility before the firmware gate inspects the ESP.
# Stage 2 sets modules.load and replaces this payload with the selected module.
isa_path = os.environ.get("GBOS_ISA_EMU_KO", "")
if isa_path:
    def newc(name, payload, inode, mode):
        nm = name.encode() + b"\0"
        fields = [inode, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(nm), 0]
        header = b"070701" + b"".join(b"%08X" % v for v in fields)
        return header + nm + b"\0" * ((-(110 + len(nm))) % 4) + payload + b"\0" * ((-len(payload)) % 4)
    archive += b"\0" * ((-len(archive)) % 4)
    archive += newc("lib/modules/gbos_isa_emu.ko", Path(isa_path).read_bytes(), 0x7FFF0002, 0o100644)
    archive += newc("TRAILER!!!", b"", 0, 0)
    archive = wrap_init(archive, Path(os.environ["GBOS_STAGE0_INIT"]).read_bytes())

def set_line(text, key, value):
    lines = text.split("\n")
    found = False
    out = []
    for line in lines:
        if line.startswith(key + "="):
            if found:
                raise SystemExit("duplicate " + key)
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        raise SystemExit("missing " + key)
    return "\n".join(out)

text = set_line(std_bc, "androidboot.vbmeta.digest", digest)
text = set_line(text, "androidboot.vbmeta.size", str(vb_size))
text = set_line(text, "androidboot.boot_part_uuid", boot_uuid)
for need in (
    "androidboot.hypervisor.protected_vm.supported=0\n",
    "androidboot.vendor.apex.com.android.hardware.keymint=com.android.hardware.keymint.rust_nonsecure\n",
    "androidboot.vendor.apex.com.android.hardware.gatekeeper=com.android.hardware.gatekeeper.nonsecure\n",
):
    if need not in text:
        raise SystemExit("bootconfig missing " + need.strip())
raw = text.encode()
footer = struct.pack("<II", len(raw), sum(raw) & 0xFFFFFFFF) + b"#BOOTCONFIG\n"
(tmp / "initrd.new.img").write_bytes(archive + raw + footer)
print("initrd bytes", (tmp / "initrd.new.img").stat().st_size, "vbmeta", vb_size)

with img.open("r+b") as f:
    f.seek(vendor_off)
    f.write(vendor)
    f.seek(vbmeta_off)
    f.write(b"\x00" * 8192)
    f.seek(vbmeta_off)
    f.write(vb)
print("spliced vendor and vbmeta")
PY

# Check the final vendor readback, both unchanged stock module partitions, and
# the actual ESP archive (GBOS_INITRD only supplies bootconfig, not its modules).
# The original vendor tree identifies builder additions; other stock modules are report-only.
echo "check module firmware"
for partition in vendor_dlkm system_dlkm; do
    python3 "$HERE/lib/lp-dump.py" "$IMG" "$TMP" "${partition}_a"
    ot fsck.erofs --extract="$TMP/firmware-$partition" --overwrite "$TMP/${partition}_a.img"
done
python3 "$HERE/kmod/check-firmware.py" --profile "$GBOS_PROFILE" \
    --vendor "$TMP/vendor-tree-new" --base-vendor "$TMP/vendor-tree-base" \
    --module-tree "$TMP/firmware-vendor_dlkm" --module-tree "$TMP/firmware-system_dlkm" \
    --initrd "$TMP/initrd.new.img" --report "$GBOS_OUT/firmware-report.json"

echo "replace ESP initrd"
mcopy -o -i "$IMG@@$ESP_OFF" "$TMP/initrd.new.img" ::/initrd.img

echo "sha256"
sha256sum "$IMG" | tee "$IMG.sha256"

rm -rf "$TMP"
echo "build done"
