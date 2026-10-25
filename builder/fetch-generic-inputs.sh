#!/usr/bin/env bash
# Fetch the extra inputs of the generic-x86 target (machines without a GPU driver in the Googlebook kernel), each
# checked against a pinned SHA-256:
#   <dir>/generic/vulkan.pastel.so   SwiftShader Vulkan ICD from the public Cuttlefish build 16373615
#                                    (public AOSP build, VENDOR/apex/com.google.cf.vulkan.apex)
# usage: builder/fetch-generic-inputs.sh <dir>        (the same <dir> as builder/fetch-inputs.sh)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTDIR=${1:?usage: fetch-generic-inputs.sh <dir>}
CF_TARGET_FILES=https://ci.android.com/builds/submitted/16373615/aosp_cf_x86_64_only_phone-userdebug/latest/raw/aosp_cf_x86_64_only_phone-target_files-16373615.zip
VULKAN_APEX_MEMBER=VENDOR/apex/com.google.cf.vulkan.apex
VULKAN_APEX_SHA=b6b33f2f54a682b11ef69ee9d61344ca26a8b148ae0944f06674f94369b2025d
PASTEL_SHA=5232e4829d8e6bba2d81cc0ff64d227504601090d2ac7b9fa105536e38c680a3
sha() { sha256sum "$1" | cut -d' ' -f1; }
for c in python3 unzip debugfs sha256sum; do command -v $c > /dev/null || { echo "missing command: $c (debugfs: e2fsprogs)"; exit 2; }; done
DEST="$OUTDIR/generic"; mkdir -p "$DEST"
if [ -f "$DEST/vulkan.pastel.so" ] && [ "$(sha "$DEST/vulkan.pastel.so")" = "$PASTEL_SHA" ]; then
    echo "ok (cached) generic/vulkan.pastel.so"; exit 0
fi
TMP=$(mktemp -d "${TMPDIR:-/tmp}/gbos-generic.XXXXXX"); trap 'rm -rf "$TMP"' EXIT
python3 "$HERE/../base/gbos_base.py" fetch-zip "$CF_TARGET_FILES" "$TMP" "$VULKAN_APEX_MEMBER=$VULKAN_APEX_SHA"
[ "$(sha "$TMP/com.google.cf.vulkan.apex")" = "$VULKAN_APEX_SHA" ] || { echo "sha256 mismatch: $VULKAN_APEX_MEMBER"; exit 1; }
unzip -q -o "$TMP/com.google.cf.vulkan.apex" apex_payload.img -d "$TMP"
debugfs -R "dump /lib64/hw/vulkan.pastel.so $TMP/vulkan.pastel.so" "$TMP/apex_payload.img" 2> /dev/null
[ "$(sha "$TMP/vulkan.pastel.so")" = "$PASTEL_SHA" ] || { echo "sha256 mismatch: vulkan.pastel.so in the APEX payload"; exit 1; }
mv "$TMP/vulkan.pastel.so" "$DEST/vulkan.pastel.so"
echo "ok generic/vulkan.pastel.so"
