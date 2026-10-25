#!/usr/bin/env bash
# Build the pahole version used by the pinned GKI configuration.
set -euo pipefail
WORK=${1:?usage: build-pahole.sh <kernel module work directory>}
REV=d884153887ce06fec729061016f7a0b412f08fc6
SRC=$WORK/pahole-src
OUT=$WORK/pahole-build
for tool in git cmake pkg-config make cc; do
    command -v "$tool" >/dev/null || { echo "pahole build needs $tool" >&2; exit 2; }
done
pkg-config --exists libbpf libdw libelf || {
    echo 'pahole 1.31 build needs libbpf, libdw and libelf development packages (Ubuntu: libbpf-dev libdw-dev libelf-dev).' >&2
    exit 2
}
mkdir -p "$WORK/bin"
if [ "$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)" != "$REV" ] ||
   [ -n "$(git -C "$SRC" status --porcelain --untracked-files=no 2>/dev/null || true)" ]; then
    rm -rf "$SRC" "$OUT"
    git init -q "$SRC"
    git -C "$SRC" fetch -q --depth 1 https://android.googlesource.com/platform/external/dwarves "$REV"
    git -C "$SRC" checkout -q --detach FETCH_HEAD
fi
cmake -S "$SRC" -B "$OUT" -DLIBBPF_EMBEDDED=OFF -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON "-DCMAKE_C_FLAGS=-ffile-prefix-map=$WORK/=/gbos-pahole/"
cmake --build "$OUT" --target pahole --parallel "$(nproc)"
[ "$("$OUT/pahole" --version)" = v1.31 ] || { echo 'unexpected pahole version' >&2; exit 1; }
ln -sfn ../pahole-build/pahole "$WORK/bin/pahole"
echo 'ok pahole 1.31 (pinned Android source)'
