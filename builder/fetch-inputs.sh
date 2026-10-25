#!/usr/bin/env bash
# Fetch the third-party inputs the builder needs besides the base image, each checked against a pinned SHA-256:
#   <dir>/firmware   the profile's device firmware ([[firmware]] in profiles/<model>/profile.toml; RAM-loaded by the
#                    drivers, nothing is flashed), laid out like /lib/firmware, from linux-firmware and sof-bin releases
#   <dir>/audio-src  the profile's ChromeOS audio configuration (its audio.sources list), from board-overlays
# usage: builder/fetch-inputs.sh <dir> [profile dir]     then: export GBOS_INPUTS=<dir>
set -euo pipefail
OUTDIR=${1:?usage: fetch-inputs.sh <dir> [profile dir]}
PROFILE=${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../profiles/marasov" && pwd)}
DEST="$OUTDIR/firmware"
BO=https://chromium.googlesource.com/chromiumos/overlays/board-overlays
LF=https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain
# the profile pins every file: <path in DEST> <linux-firmware|sof-bin> <path in the source> <sha256>
DP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/devprofile.py"
python3 "$DP" check "$PROFILE" >/dev/null || exit 1
FWLIST=$(python3 "$DP" firmware "$PROFILE")
LF_TAG=$(python3 "$DP" get "$PROFILE" firmware_sources.linux_firmware_tag)
SOF_URL=$(python3 "$DP" get "$PROFILE" firmware_sources.sof_bin_url)
SOF_SHA=$(python3 "$DP" get "$PROFILE" firmware_sources.sof_bin_sha256)
AUDIO_LIST=$(python3 "$DP" get "$PROFILE" audio.sources)
LF_FILES=$(printf '%s\n' "$FWLIST" | awk '$2 == "linux-firmware" {print $1, $3, $4}')
SOF_FILES=$(printf '%s\n' "$FWLIST" | awk '$2 == "sof-bin" {print $1, $3, $4}')
sha() { sha256sum "$1" | cut -d' ' -f1; }
mkdir -p "$DEST"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/gbos-fw.XXXXXX"); trap 'rm -rf "$TMP"' EXIT
echo "$LF_FILES" | while read -r dst src want; do
    [ -n "$dst" ] || continue
    if [ -f "$DEST/$dst" ] && [ "$(sha "$DEST/$dst")" = "$want" ]; then echo "ok (cached) $dst"; continue; fi
    curl -fsSL --retry 3 --max-time 300 -o "$TMP/f" "$LF/$src?h=$LF_TAG"
    [ "$(sha "$TMP/f")" = "$want" ] || { echo "sha256 mismatch: linux-firmware $LF_TAG $src"; exit 1; }
    mkdir -p "$(dirname "$DEST/$dst")"; mv "$TMP/f" "$DEST/$dst"; echo "ok $dst"
done
if [ -n "$SOF_FILES" ]; then
curl -fsSL --retry 3 --max-time 600 -o "$TMP/sof.tgz" "$SOF_URL"
[ "$(sha "$TMP/sof.tgz")" = "$SOF_SHA" ] || { echo "sha256 mismatch: $SOF_URL"; exit 1; }
echo "$SOF_FILES" | while read -r dst src want; do
    [ -n "$dst" ] || continue
    tar -xzf "$TMP/sof.tgz" -C "$TMP" "$src"
    f=$(readlink -f "$TMP/$src")
    [ "$(sha "$f")" = "$want" ] || { echo "sha256 mismatch: $src"; exit 1; }
    mkdir -p "$(dirname "$DEST/$dst")"; cp "$f" "$DEST/$dst"; echo "ok $dst"
done
fi
mkdir -p "$OUTDIR/audio-src"
[ -n "$AUDIO_LIST" ] || { echo "inputs ready in $OUTDIR (export GBOS_INPUTS=$OUTDIR)"; exit 0; }
commit=$(sed -n 's/.* at commit \([0-9a-f]\{40\}\).*/\1/p' "$PROFILE/$AUDIO_LIST" | head -1)
[ -n "$commit" ] || { echo "no pinned commit in $PROFILE/$AUDIO_LIST"; exit 1; }
grep -v '^#' "$PROFILE/$AUDIO_LIST" | while read -r dst src want; do
    [ -n "$dst" ] || continue
    if [ -f "$OUTDIR/audio-src/$dst" ] && [ "$(sha "$OUTDIR/audio-src/$dst")" = "$want" ]; then echo "ok (cached) audio-src/$dst"; continue; fi
    curl -fsSL --retry 3 --max-time 120 "$BO/+/$commit/$src?format=TEXT" | base64 -d > "$TMP/a"
    [ "$(sha "$TMP/a")" = "$want" ] || { echo "sha256 mismatch: board-overlays $commit $src"; exit 1; }
    mv "$TMP/a" "$OUTDIR/audio-src/$dst"; echo "ok audio-src/$dst"
done
echo "inputs ready in $OUTDIR (export GBOS_INPUTS=$OUTDIR)"
