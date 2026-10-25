#!/usr/bin/env bash
# One command from Google's official lapis recovery image to an ASUS Chromebook Plus CX34 (CX3402 / marasov) USB image, as a normal user (no sudo):
#   1. download the official recovery zip (or use --zip) and check its SHA-256
#   2. base/build-base.sh: the intermediate base image (software KeyMint, signed with your own AVB key); it is only an
#      input for step 4 and does not boot on the KANO by itself
#   3. builder/fetch-inputs.sh: pinned linux-firmware / sof-bin files and the ChromeOS audio configuration; the profile's
#      extra kernel modules (KANO: ac.ko and gbos_wakefilter.ko, builder/kmod/build-ac.sh and build-gbos_wakefilter.sh, from
#      the pinned kernel source and toolchain)
#   4. builder/build.sh: the KANO image plus its QEMU self-tests
# Then write it with: sudo install/write-usb.sh <image> /dev/disk/by-id/usb-...
#
# usage: install/install.sh --work DIR [--zip recovery.zip | --recovery-bin recovery.bin] [--key avb-rsa4096.pem] [--adb-pubkey ~/.android/adbkey.pub]
#                           [--skip-space-check] [--no-selftest] [--update]
# A finished base or image in DIR is reused only when everything that made it is the same: the files under base/,
# builder/ and the profile (also uncommitted ones), the keys, and the settings, including the GBOS_* variables that
# builder/build.sh reads (GBOS_DEFAULT_ENTRY, GBOS_LOADER_EDITOR, ...). Otherwise it stops and says what to delete.
# The AVB key that signs the images is kept in DIR/keys/vbmeta.pem as well (copied from DIR/base/keys after the first
# base build): an installed system can only be updated by images signed with its key, so a rebuilt base uses it again.
# --update (install/update.sh): a base or build made from other sources or settings is moved to DIR/base.prev or
# DIR/build.prev instead of stopping, and the new one is built with the kept key.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die() { echo "install.sh: $*" >&2; exit 2; }
# Developer-only. A release install never powers itself off; pass this only to builder/build.sh for a TEST image.
if [ -n "${GBOS_TEST_AUTOOFF:-}" ]; then
    die "GBOS_TEST_AUTOOFF is developer-only (TEST images); install.sh does not accept it"
fi
# the Google build and its pins (base/builds/<build>.sh; GBOS_LAPIS_BUILD selects one, default 16658739)
LAPIS_BUILD=${GBOS_LAPIS_BUILD:-16658739}
export GBOS_LAPIS_BUILD="$LAPIS_BUILD"
[[ $LAPIS_BUILD =~ ^[0-9]+$ ]] && [ -f "$ROOT/base/builds/$LAPIS_BUILD.sh" ] || die "no pins for Google build '$LAPIS_BUILD' in base/builds"
# plain NAME=value lines (read, not sourced: the pin file also holds bash-4 arrays for base/build-base.sh)
ZIP_URL=$(sed -n 's/^ZIP_URL=//p' "$ROOT/base/builds/$LAPIS_BUILD.sh"); ZIP_SHA256=$(sed -n 's/^ZIP_SHA256=//p' "$ROOT/base/builds/$LAPIS_BUILD.sh")
INPUT_KIND=$(python3 "$ROOT/builder/lib/lapis.py" --get INPUT_KIND)
INPUT_KIND=${INPUT_KIND:-zip}
if [ "$INPUT_KIND" = zip ]; then
    [[ ${ZIP_SHA256:-} =~ ^[0-9a-f]{64}$ ]] && [[ ${ZIP_URL:-} =~ ^https://dl\.google\.com/ ]] || die "base/builds/$LAPIS_BUILD.sh: odd ZIP_URL or ZIP_SHA256"
elif [ "$INPUT_KIND" != raw ]; then die "unsupported recovery INPUT_KIND $INPUT_KIND"; fi
log() { printf '\n[%s] == %s\n' "$(date +%H:%M:%S)" "$*"; }

WORK='' ZIP='' RECOVERY_BIN='' KEY='' ADB='' SKIP_SPACE=0 SELFTEST=1 UPDATE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --work) [ $# -ge 2 ] || die "--work needs a directory"; WORK=$2; shift 2 ;;
        --zip|--recovery-bin|--key|--adb-pubkey)
            [ $# -ge 2 ] && [ -f "$2" ] || die "$1: not a file: ${2:-}"
            case "$1" in --zip) ZIP=$2 ;; --recovery-bin) RECOVERY_BIN=$2 ;; --key) KEY=$2 ;; *) ADB=$2 ;; esac
            shift 2 ;;
        --skip-space-check) SKIP_SPACE=1; shift ;;
        --no-selftest) SELFTEST=0; shift ;;
        --update) UPDATE=1; shift ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
done
[ -n "$WORK" ] || die "--work DIR is required (about 80 GB of space)"
[ "$(id -u)" -ne 0 ] || die "run as a normal user; only the final USB write needs root"
[ "$(uname -s)/$(uname -m)" = Linux/x86_64 ] || die "needs Linux on x86_64 (KVM for the self-test)"
mkdir -p "$WORK"; WORK="$(cd "$WORK" && pwd)"

log "host check"
missing=()
for c in curl python3 lz4 mkfs.fat mmd mcopy mtype mdir openssl sha256sum truncate tar zstd cmp; do
    command -v "$c" >/dev/null || missing+=("$c")
done
QEMU=${GBOS_QEMU:-$(command -v qemu-system-x86_64 || true)}
[ "$SELFTEST" = 0 ] || [ -n "$QEMU" ] || missing+=(qemu-system-x86_64)
if [ ${#missing[@]} -gt 0 ]; then
    echo "missing: ${missing[*]}"
    echo "Arch:          sudo pacman -S --needed curl python lz4 dosfstools mtools openssl coreutils tar zstd diffutils qemu-base edk2-ovmf"
    echo "Debian/Ubuntu: sudo apt install curl python3 lz4 dosfstools mtools openssl coreutils tar zstd diffutils qemu-system-x86 ovmf"
    echo "Fedora:        sudo dnf install curl python3 lz4 dosfstools mtools openssl coreutils tar zstd diffutils qemu-system-x86 edk2-ovmf"
    exit 2
fi
if [ "$SELFTEST" = 1 ] && [ -z "${GBOS_OVMF_CODE:-}" ]; then
    for pair in /usr/share/edk2/x64/OVMF_CODE.4m.fd:/usr/share/edk2/x64/OVMF_VARS.4m.fd \
                /usr/share/OVMF/OVMF_CODE_4M.fd:/usr/share/OVMF/OVMF_VARS_4M.fd \
                /usr/share/edk2/ovmf/OVMF_CODE.fd:/usr/share/edk2/ovmf/OVMF_VARS.fd \
                /usr/share/OVMF/OVMF_CODE.fd:/usr/share/OVMF/OVMF_VARS.fd; do
        if [ -f "${pair%%:*}" ] && [ -f "${pair##*:}" ]; then GBOS_OVMF_CODE=${pair%%:*}; GBOS_OVMF_VARS=${pair##*:}; break; fi
    done
fi
[ "$SELFTEST" = 0 ] || [ -n "${GBOS_OVMF_CODE:-}" ] || die "OVMF (UEFI for QEMU) not found; install edk2-ovmf/ovmf or set GBOS_OVMF_CODE and GBOS_OVMF_VARS"
if [ "$SELFTEST" = 1 ]; then
    [ -r /dev/kvm ] && [ -w /dev/kvm ] || die "/dev/kvm is not usable by $(id -un) (add yourself to the kvm group; without KVM use --no-selftest)"
else
    echo "WARNING: --no-selftest: the image will NOT be booted in QEMU before you write it"
fi
if [ "$SKIP_SPACE" != 1 ]; then
    free=$(df -Pk "$WORK" | awk 'NR==2 {print $4}')
    [ "$free" -ge $((80 * 1024 * 1024)) ] || die "less than 80 GB free in $WORK (--skip-space-check if most of it is already built)"
fi
export GBOS_QEMU="$QEMU" GBOS_OVMF_CODE="${GBOS_OVMF_CODE:-}" GBOS_OVMF_VARS="${GBOS_OVMF_VARS:-}"
[ "$SELFTEST" = 0 ] || echo "QEMU $GBOS_QEMU, OVMF $GBOS_OVMF_CODE"

# content identity of source directories and files: names and contents of every file except Markdown documents, committed or not
tree_digest() {
    local d
    for d in "$@"; do
        if [ -f "$d" ]; then
            echo "file $d"
            sha256sum < "$d" || die "cannot read $d"
        else
            [ -d "$d" ] || die "missing source directory $d"
            echo "dir $d"
            (cd "$d" && find . -type f ! -name '*.md' ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 \
                | LC_ALL=C sort -z | xargs -0 -r sha256sum) || die "cannot read $d"
        fi
    done | sha256sum | cut -c1-64
}

BASE_INPUT=()
if [ "$INPUT_KIND" = raw ]; then
    [ -n "$RECOVERY_BIN" ] && [ -z "$ZIP" ] || die "this build requires --recovery-bin recovery.bin (decompress .zst first); no --zip"
    BASE_INPUT=(--recovery-bin "$RECOVERY_BIN")
    log "1/4 locally supplied recovery image (verified by build-base.sh)"
else
log "1/4 official recovery zip (7.7 GB)"
if [ -z "$ZIP" ]; then
    ZIP="$WORK/downloads/lapis-user-$LAPIS_BUILD-recovery.zip"
    mkdir -p "$WORK/downloads"
    if [ ! -f "$ZIP.ok" ]; then
        curl -fL --retry 5 -C - -o "$ZIP" "$ZIP_URL"
    fi
fi
# the .ok marker remembers a verified hash for this exact file (size and modification time); anything else is re-hashed
stamp() { echo "$ZIP_SHA256 $(stat -c '%s %Y' "$ZIP")"; }
if [ ! -f "$ZIP.ok" ] || [ "$(cat "$ZIP.ok")" != "$(stamp)" ]; then
    echo "checking SHA-256"
    [ "$(sha256sum < "$ZIP" | cut -d' ' -f1)" = "$ZIP_SHA256" ] || die "$ZIP is not the expected lapis build $LAPIS_BUILD recovery zip"
    if [ -w "$(dirname "$ZIP")" ]; then stamp > "$ZIP.ok"; fi
fi

    BASE_INPUT=(--zip "$ZIP")
    [ -z "$RECOVERY_BIN" ] || BASE_INPUT+=(--recovery-bin "$RECOVERY_BIN")
fi

log "2/4 base image (base/build-base.sh, a few minutes)"
B="$WORK/base"
BASE_ID=$(tree_digest "$ROOT/base" "$ROOT/builder/lib/lapis.py"); BASE_SOURCES="base-sources $BASE_ID lapis-build $LAPIS_BUILD"
KEYS="$WORK/keys"
keep_key() {  # keep_key <pem>: DIR/keys/vbmeta.pem is the key of everything built in DIR; never replaced silently
    if [ -f "$KEYS/vbmeta.pem" ]; then
        cmp -s "$1" "$KEYS/vbmeta.pem" || echo "NOTE: $1 is not the kept key $KEYS/vbmeta.pem; systems installed from" \
            "this work directory can only be updated with images signed by the key that built them"
        return 0
    fi
    (umask 077; mkdir -p "$KEYS" && cp "$1" "$KEYS/vbmeta.pem.new" && mv "$KEYS/vbmeta.pem.new" "$KEYS/vbmeta.pem") \
        || die "cannot keep a copy of the AVB key in $KEYS"
    cmp -s "$1" "$KEYS/vbmeta.pem" || die "the copy of the AVB key in $KEYS differs from $1"
    echo "kept the AVB key in $KEYS/vbmeta.pem (needed for every later update of what you install)"
}
if [ -z "$KEY" ]; then
    if [ -f "$KEYS/vbmeta.pem" ]; then KEY="$KEYS/vbmeta.pem"
    elif [ -f "$B/keys/vbmeta.pem" ]; then keep_key "$B/keys/vbmeta.pem"; KEY="$KEYS/vbmeta.pem"; fi
fi
if [ "$UPDATE" = 1 ] && [ -e "$B" ] && { [ ! -f "$B/BUILD-DONE" ] || [ "$(cat "$B/install-sources" 2>/dev/null)" != "$BASE_SOURCES" ]; }; then
    [ -n "$KEY" ] || die "--update: no AVB key found ($KEYS/vbmeta.pem or $B/keys/vbmeta.pem); an update must be signed with the installed system's key (--key)"
    echo "update: $B was made from other base/ sources (or is unfinished); moving it to $B.prev"
    rm -rf "$B.prev"; mv "$B" "$B.prev"
fi
# what the base files looked like when they last matched SHA256SUMS; anything else is hashed again
base_stamp() { (cd "$B" && sha256sum SHA256SUMS | cut -c1-64 && stat -c '%n %s %Y %i' kano-real.img initrd.img); }
if [ -f "$B/BUILD-DONE" ]; then
    [ -f "$B/install-sources" ] \
        || die "$B was built without a record of its sources (older install.sh); delete $B or use a new --work"
    [ "$(cat "$B/install-sources")" = "$BASE_SOURCES" ] \
        || die "$B was built from other base/ sources than this checkout; delete $B or use a new --work"
    [ "$(grep -cE '^[0-9a-f]{64}  (kano-real|initrd)\.img$' "$B/SHA256SUMS" 2>/dev/null)" = 2 ] \
        || die "$B/SHA256SUMS does not list kano-real.img and initrd.img; delete $B"
    if [ ! -f "$B/install-verified" ] || [ "$(cat "$B/install-verified")" != "$(base_stamp)" ]; then
        echo "checking $B against its SHA256SUMS (about a minute)"
        (cd "$B" && sha256sum --quiet -c SHA256SUMS) || die "$B does not match its SHA256SUMS; delete $B and run again"
        base_stamp > "$B/install-verified"
    fi
    echo "reusing $B"
else
    bash "$ROOT/base/build-base.sh" "${BASE_INPUT[@]}" --out "$B" ${KEY:+--key "$KEY"}
    printf '%s\n' "$BASE_SOURCES" > "$B/install-sources"
    base_stamp > "$B/install-verified"   # build-base.sh hashed exactly these files a moment ago
fi
AVB_KEY=${KEY:-$WORK/base/keys/vbmeta.pem}
[ -f "$AVB_KEY" ] || die "AVB key not found: $AVB_KEY (pass the --key that built $WORK/base)"
keep_key "$AVB_KEY"
# the base must have been signed with this key: compare the public key fingerprint recorded by build-base.sh
OT="$WORK/base/cache/otatools"; PUB=$(mktemp)
LD_LIBRARY_PATH="$OT/lib64" "$OT/bin/avbtool" extract_public_key --key "$AVB_KEY" --output "$PUB" || die "cannot read $AVB_KEY"
KEY_FP=$(sha1sum < "$PUB" | cut -c1-40); rm -f "$PUB"
grep -q "public key sha1 $KEY_FP" "$WORK/base/build-info.txt" \
    || die "$WORK/base was signed with a different AVB key than $AVB_KEY; use a new --work (or the key that built it)"

PROFILE=${GBOS_PROFILE:-$ROOT/profiles/marasov}
IMAGE_NAME=${GBOS_IMAGE_NAME:-$(python3 "$ROOT/builder/lib/devprofile.py" get "$PROFILE" model.image_name)} \
    || die "the device profile $PROFILE is not valid (builder/lib/devprofile.py check $PROFILE)"
STATUS=$(python3 "$ROOT/builder/lib/devprofile.py" get "$PROFILE" model.status) || die "the device profile $PROFILE is not valid"
if [ "$STATUS" != verified ]; then
    echo "WARNING: the device profile $PROFILE is '$STATUS': it was not verified on that hardware (only what the profile"
    echo "         says it was tested on works as described; see its profile.toml)"
fi
log "3/4 device firmware and audio configuration of the profile ($PROFILE)"
bash "$ROOT/builder/fetch-inputs.sh" "$WORK/inputs" "$PROFILE"
MODEL_ID=$(python3 "$ROOT/builder/lib/devprofile.py" get "$PROFILE" model.id) || die "the device profile $PROFILE is not valid"
DISPLAY_STACK=$(python3 "$ROOT/builder/lib/devprofile.py" get "$PROFILE" display.stack) || die "the device profile $PROFILE is not valid"
DISPLAY_FALLBACK=$(python3 "$ROOT/builder/lib/devprofile.py" get "$PROFILE" display.fallback)
if [ "$DISPLAY_STACK" = gop-swiftshader ] || [ "$DISPLAY_FALLBACK" = gop-swiftshader ] || [ "$MODEL_ID" = amd-hx370 ]; then
    # machines with GOP framebuffer + SwiftShader (profiles/generic-x86, and amd-hx370's safe default entry):
    # SwiftShader from the public Cuttlefish build + gbos_efifb.ko
    bash "$ROOT/builder/fetch-generic-inputs.sh" "$WORK/inputs" || die "fetching the generic display inputs failed"
fi
# extra kernel modules of the profile (kmods), built from pinned sources; ac = the ACPI AC adapter driver (charger shown),
# gbos_wakefilter = keeps dark resumes dark (both from the same pinned kernel tree, so they are built together)
KMOD_AC=${GBOS_KMOD_AC:-}; KMOD_WF=${GBOS_KMOD_WAKEFILTER:-}
KMOD_ISA=${GBOS_ISA_EMU_KO:-}
KMODS=$(python3 "$ROOT/builder/lib/devprofile.py" kmods "$PROFILE") || die "the device profile $PROFILE is not valid"
WANT_WF=0; printf '%s\n' "$KMODS" | grep -qx gbos_wakefilter && [ -z "$KMOD_WF" ] && WANT_WF=1
[ "$WANT_WF" = 0 ] || KMOD_WF="$WORK/inputs/kmod/gbos_wakefilter.ko"
WANT_AC=0; printf '%s\n' "$KMODS" | grep -qx ac && [ -z "$KMOD_AC" ] && WANT_AC=1
WANT_ISA=0; printf '%s\n' "$KMODS" | grep -qx gbos_isa_emu && [ -z "$KMOD_ISA" ] && WANT_ISA=1
[ "$WANT_ISA" = 0 ] || KMOD_ISA="$WORK/inputs/kmod/gbos_isa_emu.ko"
if [ "$(python3 "$ROOT/builder/lib/lapis.py" --get MODULE_BUILD)" = recovery-headers ]; then
    RECOVERY_MODS="$WORK/inputs/kmod-$LAPIS_BUILD"
    modules_ok() {
        local name pin want
        for name in ac gbos_wakefilter gbos_isa_emu gbos_efifb; do
            case "$name" in ac) pin=AC_KO_SHA ;; gbos_wakefilter) pin=WAKEFILTER_KO_SHA ;; gbos_isa_emu) pin=ISA_KO_SHA ;; *) pin=EFIFB_KO_SHA ;; esac
            want=$(python3 "$ROOT/builder/lib/lapis.py" --get "$pin")
            [ -n "$want" ] && [ -f "$RECOVERY_MODS/$name.ko" ] && [ "$(sha256sum "$RECOVERY_MODS/$name.ko" | cut -c1-64)" = "$want" ] || return 1
        done
    }
    if ! modules_ok; then
        rm -rf "$RECOVERY_MODS.new"
        bash "$ROOT/builder/kmod/build-recovery-modules.sh" "$B/kano-real.img" "$B/cache/otatools" "$RECOVERY_MODS.new" "$WORK/inputs/kmod-toolchain"
        rm -rf "$RECOVERY_MODS"
        mv "$RECOVERY_MODS.new" "$RECOVERY_MODS"
    fi
    [ "$WANT_AC" = 0 ] || KMOD_AC="$RECOVERY_MODS/ac.ko"
    [ "$WANT_WF" = 0 ] || KMOD_WF="$RECOVERY_MODS/gbos_wakefilter.ko"
    [ "$WANT_ISA" = 0 ] || KMOD_ISA="$RECOVERY_MODS/gbos_isa_emu.ko"
    export GBOS_GENERIC_KO="${GBOS_GENERIC_KO:-$RECOVERY_MODS/gbos_efifb.ko}"
    WANT_AC=0 WANT_WF=0 WANT_ISA=0
fi
if [ "$WANT_AC" = 1 ] || [ "$WANT_WF" = 1 ] || [ "$WANT_ISA" = 1 ]; then
    # build-ac.sh prepares the pinned kernel tree and always builds ac.ko; it is shipped only if the profile lists it
    [ "$WANT_AC" = 0 ] || KMOD_AC="$WORK/inputs/kmod/ac.ko"
    AC_PIN=$(sed -n 's/^AC_KO_SHA=\([0-9a-f]\{64\}\)$/\1/p' "$ROOT/builder/kmod/build-ac.sh")
    WF_PIN=$(sed -n 's/^WAKEFILTER_KO_SHA=\([0-9a-f]\{64\}\)$/\1/p' "$ROOT/builder/kmod/build-gbos_wakefilter.sh")
    if { [ -f "$WORK/inputs/kmod/ac.ko" ] && [ "$(sha256sum < "$WORK/inputs/kmod/ac.ko" | cut -c1-64)" != "$AC_PIN" ]; } \
        || { [ "$WANT_WF" = 1 ] && [ -f "$KMOD_WF" ] && [ "$(sha256sum < "$KMOD_WF" | cut -c1-64)" != "$WF_PIN" ]; }; then
        echo "    $WORK/inputs/kmod does not hold the modules pinned in builder/kmod/ (an older build?): building them again"
        rm -rf "$WORK/inputs/kmod"
    fi
    if [ ! -f "$WORK/inputs/kmod/ac.ko" ] || { [ "$WANT_WF" = 1 ] && [ ! -f "$KMOD_WF" ]; } || { [ "$WANT_ISA" = 1 ] && [ ! -f "$KMOD_ISA" ]; }; then
        missing=()
        for c in git make perl flex bison nice xargs base64; do command -v "$c" >/dev/null || missing+=("$c"); done
        # host headers the kernel build compiles against: libelf (gelf.h), libdw (dwarf.h), OpenSSL
        # (GBOS_HOST_INCLUDE: the include directories looked in; tests point it at a fake tree)
        for h in gelf.h dwarf.h openssl/opensslv.h; do
            found=0
            for d in ${GBOS_HOST_INCLUDE:-/usr/include /usr/local/include}; do [ ! -f "$d/$h" ] || found=1; done
            [ "$found" = 1 ] || missing+=("$h")
        done
        if [ ${#missing[@]} -gt 0 ]; then
            echo "missing (to build ac.ko): ${missing[*]}; the kernel build needs these tools and the libelf, libdw (dwarf.h) and OpenSSL headers"
            echo "Arch:          sudo pacman -S --needed git make perl flex bison libelf openssl"
            echo "Debian/Ubuntu: sudo apt install git make perl flex bison libelf-dev libdw-dev libssl-dev"
            echo "Fedora:        sudo dnf install git make perl flex bison elfutils-libelf-devel elfutils-devel openssl-devel"
            exit 2
        fi
        log "    ac.ko from the pinned kernel source and toolchain (about 2.5 GB to download, 5-15 minutes)"
        rm -rf "$WORK/inputs/kmod.new"
        bash "$ROOT/builder/kmod/build-ac.sh" "$WORK/inputs/kmod-work" "$WORK/inputs/kmod.new" || die "building ac.ko failed (see above)"
        if [ "$WANT_WF" = 1 ]; then
            log "    gbos_wakefilter.ko from the same kernel tree"
            bash "$ROOT/builder/kmod/build-gbos_wakefilter.sh" "$WORK/inputs/kmod-work" "$WORK/inputs/kmod.new" || die "building gbos_wakefilter.ko failed (see above)"
        fi
        if [ "$WANT_ISA" = 1 ]; then
            log "    gbos_isa_emu.ko from the same kernel tree"
            bash "$ROOT/builder/kmod/build-gbos_isa_emu.sh" "$WORK/inputs/kmod-work" "$WORK/inputs/kmod.new" || die "building ISA compatibility module failed"
        fi
        rm -rf "$WORK/inputs/kmod"
        mv "$WORK/inputs/kmod.new" "$WORK/inputs/kmod"
        rm -rf "$WORK/inputs/kmod-work"   # about 9 GB of toolchain and kernel tree; the pinned build gives the same ac.ko again
    fi
fi

log "4/4 image ($IMAGE_NAME) + QEMU self-tests (about 10 minutes)"
# what this build is made of; an existing build is reused only when all of it is the same. The firmware and audio inputs
# are pinned by SHA-256 in the profile (checked by builder/vendor.sh), so their paths are enough, unless
# GBOS_ALLOW_FIRMWARE_DRIFT=1.
if [ -n "$ADB" ]; then ADB_ID=$(sha256sum < "$ADB" | cut -c1-64) || die "cannot read $ADB"; else ADB_ID=none; fi
SRC_ID=$(tree_digest "$ROOT/builder" "$ROOT/tools/mesa" "$ROOT/profiles/_safety.toml" "$PROFILE")
BASE_SUMS=$(tr '\n' ' ' < "$B/SHA256SUMS")
PARAMS="sources $SRC_ID
base $BASE_SUMS
avb-key $KEY_FP
adb-key $ADB_ID
selftest $SELFTEST
qemu-seconds ${GBOS_QEMU_SECONDS:-200}
default-entry ${GBOS_DEFAULT_ENTRY:-lapis-quiet.conf}
loader-editor ${GBOS_LOADER_EDITOR:-no}
ec-features ${GBOS_EC:-none}
skip-setup ${GBOS_SKIP_SETUP:-0}
image-name $IMAGE_NAME
profile $PROFILE
firmware ${GBOS_FIRMWARE:-$WORK/inputs/firmware}
audio-src ${GBOS_AUDIO_SRC:-$WORK/inputs/audio-src}
firmware-drift ${GBOS_ALLOW_FIRMWARE_DRIFT:-0}"
if [ -n "$KMOD_AC" ]; then
    PARAMS="$PARAMS
kmod-ac $(sha256sum < "$KMOD_AC" | cut -c1-64)" || die "cannot read $KMOD_AC"
fi
# the camera overlay (GBOS_CAMERA_OVERLAY, builder/camera/build-libcamera.sh): its file list and checksums
if [ -n "${GBOS_CAMERA_OVERLAY:-}" ]; then
    PARAMS="$PARAMS
camera-overlay $(sha256sum < "$GBOS_CAMERA_OVERLAY/../SHA256SUMS" | cut -c1-64)" || die "GBOS_CAMERA_OVERLAY has no ../SHA256SUMS"
elif [ "$(python3 "$ROOT/builder/lib/devprofile.py" get "$PROFILE" camera.libcamera)" = true ]; then
    die "the profile builds the camera (camera.libcamera = true): set GBOS_CAMERA_OVERLAY to the out/vendor of builder/camera/build-libcamera.sh"
fi
# profiles/generic-x86 and profiles/amd-hx370: resolve default input paths before hashing into PARAMS (same defaults as builder/build.sh)
if [ "$DISPLAY_STACK" = gop-swiftshader ] || [ "$DISPLAY_FALLBACK" = gop-swiftshader ]; then
    export GBOS_GENERIC_PASTEL="${GBOS_GENERIC_PASTEL:-$WORK/inputs/generic/vulkan.pastel.so}"
    [ -f "$GBOS_GENERIC_PASTEL" ] || die "this profile needs vulkan.pastel.so at $GBOS_GENERIC_PASTEL (builder/fetch-generic-inputs.sh)"
    if [ -z "${GBOS_GENERIC_KO:-}" ] && [ -f "$WORK/inputs/generic/gbos_efifb.ko" ]; then
        export GBOS_GENERIC_KO="$WORK/inputs/generic/gbos_efifb.ko"
    fi
fi
if [ "$MODEL_ID" = amd-hx370 ]; then
    export GBOS_RADV="${GBOS_RADV:-$WORK/inputs/amd-hx370/vulkan.radeon.so}"
    export GBOS_RADV_PROBE="${GBOS_RADV_PROBE:-$WORK/inputs/amd-hx370/radv-probe}"
    export GBOS_RADV_VKPROP="${GBOS_RADV_VKPROP:-$WORK/inputs/amd-hx370/libvkprop.so}"
    export GBOS_GENERIC_PASTEL="${GBOS_GENERIC_PASTEL:-$WORK/inputs/generic/vulkan.pastel.so}"
    export GBOS_GENERIC_KO="${GBOS_GENERIC_KO:-$WORK/inputs/generic/gbos_efifb.ko}"
    for f in "$GBOS_RADV" "$GBOS_RADV_PROBE" "$GBOS_RADV_VKPROP" "$GBOS_GENERIC_PASTEL" "$GBOS_GENERIC_KO"; do
        [ -f "$f" ] || die "profiles/amd-hx370 needs $(basename "$f") at $f (tools/mesa/build-radv.sh, builder/fetch-generic-inputs.sh, builder/kmod/build-gbos_efifb.sh)"
    done
fi
if [ -n "$KMOD_ISA" ]; then
    export GBOS_ISA_EMU_KO="$KMOD_ISA"
fi
for v in GBOS_ISA_EMU_KO GBOS_GENERIC_KO GBOS_GENERIC_PASTEL GBOS_RADV GBOS_RADV_VKPROP GBOS_RADV_PROBE; do
    if [ -n "${!v:-}" ]; then
        [ -f "${!v}" ] || die "cannot read ${!v}"
        PARAMS="$PARAMS
$v $(sha256sum < "${!v}" | cut -c1-64)" || die "cannot read ${!v}"
    fi
done
if [ -n "${GBOS_DEV_KERNEL_DIR:-}" ]; then
    PARAMS="$PARAMS
GBOS_DEV_KERNEL_DIR $(tree_digest "$GBOS_DEV_KERNEL_DIR")"
fi
if [ -n "$KMOD_WF" ]; then
    [ -f "$KMOD_WF" ] || die "gbos_wakefilter.ko missing: $KMOD_WF (the profile lists it; it is built together with ac.ko)"
    PARAMS="$PARAMS
kmod-gbos_wakefilter $(sha256sum < "$KMOD_WF" | cut -c1-64)" || die "cannot read $KMOD_WF"
fi
IMG="$WORK/build/image/$IMAGE_NAME"
if [ "$UPDATE" = 1 ] && [ -e "$WORK/build" ] && { [ ! -f "$WORK/build/params" ] || [ "$(cat "$WORK/build/params")" != "$PARAMS" ] \
        || [ "${GBOS_ALLOW_FIRMWARE_DRIFT:-0}" != 0 ]; }; then
    echo "update: $WORK/build was made from other sources or settings; moving it to $WORK/build.prev"
    rm -rf "$WORK/build.prev"; mv "$WORK/build" "$WORK/build.prev"
fi
if [ -e "$WORK/build" ]; then
    [ -f "$IMG" ] && [ -f "$WORK/build/params" ] || die "$WORK/build holds an unfinished build; look at $WORK/build/*.log, then delete $WORK/build and run again"
    [ "${GBOS_ALLOW_FIRMWARE_DRIFT:-0}" = 0 ] \
        || die "GBOS_ALLOW_FIRMWARE_DRIFT=1: the inputs are not pinned, so $WORK/build is never reused; delete it"
    if [ "$(cat "$WORK/build/params")" != "$PARAMS" ]; then
        diff "$WORK/build/params" <(printf '%s\n' "$PARAMS") | sed -n 's/^< /  was: /p; s/^> /  now: /p' || true
        die "$WORK/build was made with other settings or sources (above); delete $WORK/build or use a new --work"
    fi
    echo "reusing $WORK/build (same sources and settings)"
else
    GBOS_BASE="$WORK/base/kano-real.img" GBOS_INITRD="$WORK/base/initrd.img" GBOS_OUT="$WORK/build" \
    GBOS_OTATOOLS="$WORK/base/cache/otatools" GBOS_AVB_KEY="$AVB_KEY" GBOS_INPUTS="$WORK/inputs" \
    GBOS_ADB_PUBKEY="$ADB" GBOS_SKIP_SPACE_CHECK=$SKIP_SPACE GBOS_SKIP_SELFTEST=$((1 - SELFTEST)) GBOS_PROFILE="$PROFILE" \
    GBOS_IMAGE_NAME="$IMAGE_NAME" GBOS_KMOD_AC="$KMOD_AC" GBOS_KMOD_WAKEFILTER="$KMOD_WF" bash "$ROOT/builder/build.sh"
    printf '%s\n' "$PARAMS" > "$WORK/build/params"
    git -C "$ROOT" describe --always --dirty > "$WORK/build/revision" 2>/dev/null || echo unknown > "$WORK/build/revision"
fi
echo "checking the image against its SHA-256"
[ "$(sha256sum < "$IMG" | cut -c1-64)" = "$(cut -c1-64 "$IMG.sha256")" ] || die "$IMG does not match $IMG.sha256"
echo
echo "done: $IMG"
cat "$IMG.sha256"
[ ! -f "$IMG.not-self-tested" ] || echo "NOTE: built with --no-selftest; it was not booted in QEMU before release."
echo
if [ "$UPDATE" = 1 ]; then
    echo "Update a stick that runs Googlebook from this work directory (your data stays):"
    echo "  sudo $ROOT/install/write-usb.sh --keep-data $IMG /dev/disk/by-id/usb-<your-drive>"
    exit 0
fi
echo "Write it to a USB drive (everything on that drive is erased):"
echo "  ls -l /dev/disk/by-id/ | grep usb-"
echo "  sudo $ROOT/install/write-usb.sh $IMG /dev/disk/by-id/usb-<your-drive>"
