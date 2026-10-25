#!/usr/bin/env bash
# Update Googlebook to this checkout's release and keep your data (Linux or WSL, as a normal user; the final write
# asks for sudo):
#   install/update.sh --work DIR [--disk /dev/disk/by-id/usb-...] [--without-fallback] [install.sh options]
# DIR must be the work directory that built what is installed: its AVB key (DIR/keys/vbmeta.pem, or DIR/base/keys for
# work directories from v0.1.0) signs the update, and an installed system takes updates only with its own key.
#   1. install/install.sh --work DIR --update ...: builds the new image; an older base or build is moved to *.prev;
#   2. with --disk: sudo install/write-usb.sh --keep-data <image> <disk> updates that USB stick in place: the new
#      release goes into the stick's other slot, /data stays, the running version stays on the boot menu as
#      "(previous version)" (see docs/UPGRADE.md). Without --disk it prints that command.
# The internal disk is updated from the installer stick instead (install/stick/README.md, "Update").
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die() { echo "update.sh: $*" >&2; exit 2; }
WORK='' DISK='' NOFALLBACK=() PASS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --work) [ $# -ge 2 ] || die "--work needs a directory"; WORK=$2; PASS+=(--work "$2"); shift 2 ;;
        --disk) [ $# -ge 2 ] || die "--disk needs a device"; DISK=$2; shift 2 ;;
        --without-fallback) NOFALLBACK=(--without-fallback); shift ;;
        --update) shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) PASS+=("$1"); shift ;;
    esac
done
[ -n "$WORK" ] || die "--work DIR is required: the work directory that built the installed system"
[ -d "$WORK" ] || die "$WORK does not exist; an update needs the work directory (and AVB key) of the installed system"
[ -f "$WORK/keys/vbmeta.pem" ] || [ -f "$WORK/base/keys/vbmeta.pem" ] || printf '%s\n' "${PASS[@]}" | grep -qx -- --key \
    || die "no AVB key in $WORK (keys/ or base/keys/): an update must be signed with the key that built the installed system (--key)"
[ "$(id -u)" -ne 0 ] || die "run as a normal user; the write asks for sudo"
bash "$ROOT/install/install.sh" "${PASS[@]}" --update
NAME=${GBOS_IMAGE_NAME:-$(python3 "$ROOT/builder/lib/devprofile.py" get "${GBOS_PROFILE:-$ROOT/profiles/kano}" model.image_name)} \
    || die "the device profile ${GBOS_PROFILE:-$ROOT/profiles/kano} is not valid"
IMG="$(cd "$WORK" && pwd)/build/image/$NAME"
[ -f "$IMG" ] || die "no image at $IMG"
if [ -z "$DISK" ]; then
    echo "next: sudo $ROOT/install/write-usb.sh --keep-data ${NOFALLBACK[*]+${NOFALLBACK[*]} }$IMG /dev/disk/by-id/usb-<your-drive>"
    exit 0
fi
exec sudo "$ROOT/install/write-usb.sh" --keep-data ${NOFALLBACK[@]+"${NOFALLBACK[@]}"} "$IMG" "$DISK"
