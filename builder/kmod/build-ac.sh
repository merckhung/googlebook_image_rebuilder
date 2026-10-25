#!/usr/bin/env bash
# Build the upstream ACPI AC adapter driver (drivers/acpi/ac.c, matches ACPI0003) as ac.ko for the lapis GKI kernel, so
# Android sees the charger as a "Mains" power supply. It reads AC._PSR, the same EC memory window the ACPI battery driver
# already reads; it sends no EC host command. Every input is pinned:
#   kernel source  android kernel/common at the GKI commit (git object id checked)
#   kernel config  extracted from the GKI bzImage of ci.android.com build 16302624 (= the image's kernel.efi, SHA-256 checked)
#   CRCs           vmlinux.symvers of the same build (SHA-256 checked); modpost copies them into ac.ko
#   toolchain      the revisions the GKI manifest pins: Android clang r584948c (the GKI's compiler), rustc 1.91.1.p3 and
#                  bindgen (the GKI config has CONFIG_RUST=y; without them Kconfig would drop it and change the C build
#                  flags, e.g. kCFI arity), and the Android bc
# usage: builder/kmod/build-ac.sh <work dir> <output dir>      writes <output dir>/ac.ko (Linux x86-64 host)
set -euo pipefail
export LC_ALL=C
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
WORK=${1:?usage: build-ac.sh <work dir> <output dir>}
OUTDIR=${2:?usage: build-ac.sh <work dir> <output dir>}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
GKI_RAW=https://ci.android.com/builds/submitted/16302624/kernel_x86_64/latest/raw
BZIMAGE_SHA=7816c5d5b3500093e597b7362ecab103d5a32141b100d3eb7a8b33221fce7f41
SYMVERS_SHA=db64611c9710f624c60f57219649645d5539c392579a7a8d8d5066b1cc099598
KSRC_URL=https://android.googlesource.com/kernel/common
KSRC_COMMIT=3938c8d97142d5cf31543aa96f864a2cf832dda0
CLANG_URL=https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/4a7d0a5e443c27d8f68fbd46abfab464e1355a69/clang-r584948c.tar.gz
CLANG_TREE_SHA=66d6efb5a82c10e3750104276b0140e852744ec4234487aabe5ce21af08341ca
RUST_URL=https://android.googlesource.com/platform/prebuilts/rust-toolchain/linux-x86/+archive/a04f70c5179f23eff11c58cb480acee4afb1334b/1.91.1.p3.tar.gz
RUST_TREE_SHA=c1f13faac52876c5875c0b86bf630975e6991c6080f2579cbc73840f8e78e757
BINDGEN_URL="https://android.googlesource.com/platform/prebuilts/clang-tools/+/4155cb466dcd9b53c6f873829c53aa6f905449dd/linux-x86/bin/bindgen?format=TEXT"
BINDGEN_SHA=066435d1670d6680a4363ad4e3a12e7d6b9dc866fa956b24069e5d2cd08d0258
BC_URL="https://android.googlesource.com/platform/prebuilts/build-tools/+/e25d4821f50af3f6e0b87d7751dfd27037445fe5/linux-x86/bin/gavinhoward-bc?format=TEXT"
BC_SHA=3bee4353161cbe49a417fda21c4ea932f509ba63b3f3d56c4cac447731673997
CONFIG_SHA=046971f3fb011e5ec31b4ddad2610ee4a459d2000aba4520b10faafe6a27be40
AC_KO_SHA=db694c38d713e54ba1876d454fd4001cf77889173c44a20d31d9d34d201b5c7b
VERMAGIC_TAIL="SMP preempt mod_unload modversions "

case "$WORK$OUTDIR" in *[[:space:]:]*) echo "work and output paths must not contain spaces or ':'"; exit 2 ;; esac
[ ! -e "$OUTDIR/ac.ko" ] || { echo "refusing: $OUTDIR/ac.ko already exists"; exit 2; }
[ "$(uname -sm)" = "Linux x86_64" ] || { echo "build-ac.sh runs on Linux x86-64 only"; exit 2; }
for t in git curl tar python3 make perl flex bison sha256sum base64 cmp nice xargs; do
    command -v "$t" > /dev/null || { echo "missing tool: $t"; exit 2; }
done
mkdir -p "$WORK" "$OUTDIR"
WORK=$(cd "$WORK" && pwd -P); OUTDIR=$(cd "$OUTDIR" && pwd -P)
# Detect BTF tool requirements before the large compiler downloads.
if [ "$("$WORK/bin/pahole" --version 2>/dev/null || true)" != v1.31 ] && \
   [ "$(pahole --version 2>/dev/null || true)" != v1.31 ]; then
    bash "$HERE/build-pahole.sh" "$WORK"
fi
sha() { sha256sum "$1" | cut -d' ' -f1; }
# fetch <url> <file> <sha256>: cached copies are used only when they still match
fetch() {
    if [ -f "$2" ] && [ "$(sha "$2")" = "$3" ]; then echo "ok (cached) $(basename "$2")"; return; fi
    curl -fsSL --retry 6 --retry-delay 3 --retry-all-errors --max-time 1800 -o "$2.part" "$1"
    [ "$(sha "$2.part")" = "$3" ] || { echo "sha256 mismatch: $1"; rm -f "$2.part"; exit 1; }
    mv "$2.part" "$2"; echo "ok $(basename "$2")"
}
# a directory's identity: sorted "<sha256>  <path>" of every file plus every symlink's target
tree_sha() {
    ( cd "$1" && { find . -type f -print0 | sort -z | xargs -0 sha256sum; find . -type l -printf '%p -> %l\n' | sort; } ) | sha256sum | cut -d' ' -f1
}

fetch "$GKI_RAW/bzImage" "$WORK/bzImage" "$BZIMAGE_SHA"
fetch "$GKI_RAW/vmlinux.symvers" "$WORK/vmlinux.symvers" "$SYMVERS_SHA"
# blob <url of a gitiles ?format=TEXT file> <name in $WORK/bin> <sha256>
blob() {
    if [ -f "$WORK/bin/$2" ] && [ "$(sha "$WORK/bin/$2")" = "$3" ]; then echo "ok (cached) $2"; return; fi
    curl -fsSL --retry 6 --retry-delay 3 --retry-all-errors --max-time 600 "$1" | base64 -d > "$WORK/bin/$2.part"
    [ "$(sha "$WORK/bin/$2.part")" = "$3" ] || { echo "sha256 mismatch: $1"; rm -f "$WORK/bin/$2.part"; exit 1; }
    chmod 0755 "$WORK/bin/$2.part"; mv "$WORK/bin/$2.part" "$WORK/bin/$2"; echo "ok $2"
}
mkdir -p "$WORK/bin"
blob "$BC_URL" bc "$BC_SHA"
blob "$BINDGEN_URL" bindgen "$BINDGEN_SHA"
# tree <url of a gitiles +archive tarball> <dir in $WORK> <tree sha>: the tarball is generated on the fly, so the pin is
# on the extracted files, not on the archive bytes
tree() {
    if [ -d "$WORK/$2" ] && [ "$(tree_sha "$WORK/$2")" = "$3" ]; then echo "ok (cached) $2"; return; fi
    rm -rf "${WORK:?}/$2" "$WORK/$2.part"; mkdir -p "$WORK/$2.part"
    curl -fsSL --retry 6 --retry-delay 3 --retry-all-errors --max-time 3600 "$1" | tar -xz -C "$WORK/$2.part"
    got=$(tree_sha "$WORK/$2.part")
    [ "$got" = "$3" ] || { echo "$2 tree differs from the pinned one ($got)"; exit 1; }
    mv "$WORK/$2.part" "$WORK/$2"; echo "ok $2"
}
tree "$CLANG_URL" clang "$CLANG_TREE_SHA"
tree "$RUST_URL" rust "$RUST_TREE_SHA"

if [ "$(git -C "$WORK/ksrc" rev-parse HEAD 2> /dev/null)" != "$KSRC_COMMIT" ] || [ -n "$(git -C "$WORK/ksrc" status --porcelain --untracked-files=no)" ]; then
    rm -rf "$WORK/ksrc"; git init -q "$WORK/ksrc"
    git -C "$WORK/ksrc" fetch -q --depth 1 "$KSRC_URL" "$KSRC_COMMIT"
    git -C "$WORK/ksrc" checkout -q FETCH_HEAD
fi
[ "$(git -C "$WORK/ksrc" rev-parse HEAD)" = "$KSRC_COMMIT" ] || { echo "kernel source is not at $KSRC_COMMIT"; exit 1; }
echo "ok kernel/common $KSRC_COMMIT"

K="$WORK/ksrc"
"$K/scripts/extract-ikconfig" "$WORK/bzImage" > "$WORK/gki.config"
[ "$(sha "$WORK/gki.config")" = "$CONFIG_SHA" ] || { echo "config extracted from the GKI bzImage is not the pinned one ($(sha "$WORK/gki.config"))"; exit 1; }

export PATH="$WORK/clang/bin:$WORK/rust/bin:$WORK/bin:$PATH" LIBCLANG_PATH="$WORK/clang/lib"
# LOCALVERSION gives the GKI's "-android17-5" (the kernel compares only the vermagic after the release when CRCs are present)
M=(nice -n 19 make -C "$K" -s ARCH=x86_64 LLVM=1 HOSTCC=clang HOSTLD=ld.lld "RUSTC=$WORK/rust/bin/rustc" "BINDGEN=$WORK/bin/bindgen"
   LOCALVERSION=-android17-5 "-j$(nproc)" KBUILD_BUILD_TIMESTAMP=0 KBUILD_BUILD_USER=gbos KBUILD_BUILD_HOST=gbos)
cp "$WORK/gki.config" "$K/.config"
"${M[@]}" olddefconfig
cmp -s "$K/.config" "$WORK/gki.config" || { diff "$WORK/gki.config" "$K/.config" | head -20 || true; echo "olddefconfig changed the GKI config (different compiler?)"; exit 1; }
"${M[@]}" modules_prepare
# modules_prepare writes include/config/kernel.release. gbos-report.sh does not print that string.
# A dev kernel is dev-kernel. When this tree's .config matches the pinned GKI hash it also says config pass;
# a readable config that does not match says config fail. A release that is not pinned and not this tree is "other".
# modpost: the GKI's own exports with their CRCs, and the MODULE_SIG_PROTECT list. That list only names GKI modules
# whose exports get protected; ac.ko is not one of them, so an empty list changes nothing for it (check-ko.py below
# still refuses any symbol that a GKI module exports).
cp "$WORK/vmlinux.symvers" "$K/Module.symvers"
: > "$K/protected_module_names_list"
rm -rf "$WORK/ext"; mkdir -p "$WORK/ext"
cp "$K/drivers/acpi/ac.c" "$WORK/ext/ac.c"
echo "obj-m := ac.o" > "$WORK/ext/Kbuild"
# the linker's build-id hashes the debug info too, which names the work directory: map it away so ac.ko does not depend
# on where it was built (the debug info itself is stripped below)
"${M[@]}" M="$WORK/ext" KCFLAGS="-ffile-prefix-map=$WORK/=/gbos-ac/" modules
llvm-strip --strip-debug -o "$WORK/ext/ac.stripped.ko" "$WORK/ext/ac.ko"
python3 "$HERE/check-ko.py" "$WORK/ext/ac.stripped.ko" "$WORK/vmlinux.symvers" "$VERMAGIC_TAIL"
got=$(sha "$WORK/ext/ac.stripped.ko")
[ "$got" = "$AC_KO_SHA" ] || { echo "ac.ko is not the pinned build ($got)"; exit 1; }
cp "$WORK/ext/ac.stripped.ko" "$OUTDIR/ac.ko.part"; mv "$OUTDIR/ac.ko.part" "$OUTDIR/ac.ko"
echo "ac.ko $got -> $OUTDIR/ac.ko"
