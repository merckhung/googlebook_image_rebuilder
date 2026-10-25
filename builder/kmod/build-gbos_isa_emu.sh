#!/usr/bin/env bash
# Build gbos_isa_emu.ko (builder/kmod/gbos_isa_emu.c) for the lapis GKI kernel in a work directory that
# builder/kmod/build-ac.sh has prepared (pinned kernel/common at the GKI commit, the GKI config, modules_prepare,
# the pinned clang/rust/bindgen/bc, vmlinux.symvers).
set -euo pipefail
WORK=${1:?usage: build-gbos_isa_emu.sh <work dir prepared by build-ac.sh> <output dir>}
OUTDIR=${2:?usage: build-gbos_isa_emu.sh <work dir prepared by build-ac.sh> <output dir>}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KSRC_COMMIT=3938c8d97142d5cf31543aa96f864a2cf832dda0
VERMAGIC_TAIL="SMP preempt mod_unload modversions "
case "$WORK$OUTDIR" in *[[:space:]:]*) echo "work and output paths must not contain spaces or ':'"; exit 2 ;; esac
[ ! -e "$OUTDIR/gbos_isa_emu.ko" ] || { echo "refusing: $OUTDIR/gbos_isa_emu.ko already exists"; exit 2; }
[ "$(uname -sm)" = "Linux x86_64" ] || { echo "build-gbos_isa_emu.sh runs on Linux x86_64 only"; exit 2; }
K="$WORK/ksrc"
[ "$(git -C "$K" rev-parse HEAD 2> /dev/null)" = "$KSRC_COMMIT" ] || { echo "$K is not kernel/common at $KSRC_COMMIT (run build-ac.sh first)"; exit 2; }
[ -f "$K/Module.symvers" ] && [ -f "$K/include/generated/autoconf.h" ] && [ -x "$WORK/clang/bin/clang" ] && [ -f "$WORK/gki.config" ] ||
    { echo "$WORK is not prepared (run build-ac.sh first)"; exit 2; }
cmp -s "$K/.config" "$WORK/gki.config" || { echo "$K/.config is not the GKI config"; exit 2; }
mkdir -p "$OUTDIR"; OUTDIR=$(cd "$OUTDIR" && pwd -P); WORK=$(cd "$WORK" && pwd -P)
export PATH="$WORK/clang/bin:$WORK/rust/bin:$WORK/bin:$PATH" LIBCLANG_PATH="$WORK/clang/lib"
M=(nice -n 19 make -C "$K" -s ARCH=x86_64 LLVM=1 HOSTCC=clang HOSTLD=ld.lld "RUSTC=$WORK/rust/bin/rustc" "BINDGEN=$WORK/bin/bindgen"
   LOCALVERSION=-android17-5 "-j$(nproc)" KBUILD_BUILD_TIMESTAMP=0 KBUILD_BUILD_USER=gbos KBUILD_BUILD_HOST=gbos)
E="$WORK/ext-gbos_isa_emu"
rm -rf "$E"; mkdir -p "$E"
cp "$HERE/gbos_isa_emu.c" "$E/"
cp "$HERE/isa-user-memory.h" "$E/"
echo "obj-m := gbos_isa_emu.o" > "$E/Kbuild"
"${M[@]}" M="$E" KCFLAGS="-ffile-prefix-map=$WORK/=/gbos-kmod/" modules
llvm-strip --strip-debug -o "$E/gbos_isa_emu.stripped.ko" "$E/gbos_isa_emu.ko"
python3 "$HERE/check-ko.py" "$E/gbos_isa_emu.stripped.ko" "$WORK/vmlinux.symvers" "$VERMAGIC_TAIL"
got=$(sha256sum "$E/gbos_isa_emu.stripped.ko" | cut -d' ' -f1)
cp "$E/gbos_isa_emu.stripped.ko" "$OUTDIR/gbos_isa_emu.ko.part"; mv "$OUTDIR/gbos_isa_emu.ko.part" "$OUTDIR/gbos_isa_emu.ko"
echo "gbos_isa_emu.ko $got -> $OUTDIR/gbos_isa_emu.ko"
