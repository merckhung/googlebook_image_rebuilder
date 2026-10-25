#!/usr/bin/env bash
# Write a Googlebook installer-stick image to a regular file.
#
#   make-installer-stick.sh --image IMAGE --out FILE --xbootldr DIR --root DIR --pristine DIR [--disk-sectors N] [--default-action install|update]
#
# This command writes only a file. It does not open a block device.
# 1. Put that file on a USB stick with install/write-usb.sh. write-usb.sh still
#    asks for the disk serial and reads the image back. Do not pass --keep-data:
#    install/gbos-update.py refuses a disk that has partitions the release does
#    not have (XBOOTLDR and kano-maint are not part of the release layout).
# 2. The maintenance payload is already on kano-maint. Boot the stick and
#    choose Install Googlebook to the internal disk.
#    Use --default-action update when preparing a stick for an existing installation;
#    its default is then Update Googlebook on the internal disk (keep data).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
command -v python3 >/dev/null 2>&1 || { echo "make-installer-stick: python3 is required" >&2; exit 2; }
exec python3 -I "$HERE/stick/maint-stick.py" "$@"
