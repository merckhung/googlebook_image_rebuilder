#!/usr/bin/env bash
# Build gbos_wakefilter.ko (builder/kmod/gbos_wakefilter.c) for the lapis GKI kernel in a work directory that
# builder/kmod/build-ac.sh has prepared (pinned kernel/common at the GKI commit, the GKI config, modules_prepare, the
# pinned clang/rust/bindgen/bc, vmlinux.symvers): same kbuild flags, same checks (check-ko.py: vermagic tail, every
# import exported by vmlinux with the GKI's CRC), debug info stripped, the result pinned by SHA-256.
# gbos_wakefilter drops the resume KEY_WAKEUP of the ACPI power button when the button did not wake the system (see the
# source): without it every dark resume (an RTC alarm, an EC event) turns the KANO's screen on.
# usage: builder/kmod/build-gbos_wakefilter.sh <work dir prepared by build-ac.sh> <output dir>   (Linux x86-64 host)
set -euo pipefail
WORK=${1:?usage: build-gbos_wakefilter.sh <work dir prepared by build-ac.sh> <output dir>}
OUTDIR=${2:?usage: build-gbos_wakefilter.sh <work dir prepared by build-ac.sh> <output dir>}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KSRC_COMMIT=3938c8d97142d5cf31543aa96f864a2cf832dda0
WAKEFILTER_KO_SHA=3423c3e983bff3d7d35486c475bde473cb7a54dd4115bf7933678f77861cf595
VERMAGIC_TAIL="SMP preempt mod_unload modversions "
case "$WORK$OUTDIR" in *[[:space:]:]*) echo "work and output paths must not contain spaces or ':'"; exit 2 ;; esac
[ ! -e "$OUTDIR/gbos_wakefilter.ko" ] || { echo "refusing: $OUTDIR/gbos_wakefilter.ko already exists"; exit 2; }
[ "$(uname -sm)" = "Linux x86_64" ] || { echo "build-gbos_wakefilter.sh runs on Linux x86-64 only"; exit 2; }
K="$WORK/ksrc"
[ "$(git -C "$K" rev-parse HEAD 2> /dev/null)" = "$KSRC_COMMIT" ] || { echo "$K is not kernel/common at $KSRC_COMMIT (run build-ac.sh first)"; exit 2; }
[ -f "$K/Module.symvers" ] && [ -f "$K/include/generated/autoconf.h" ] && [ -x "$WORK/clang/bin/clang" ] && [ -f "$WORK/gki.config" ] ||
    { echo "$WORK is not prepared (run build-ac.sh first)"; exit 2; }
cmp -s "$K/.config" "$WORK/gki.config" || { echo "$K/.config is not the GKI config"; exit 2; }
mkdir -p "$OUTDIR"; OUTDIR=$(cd "$OUTDIR" && pwd -P); WORK=$(cd "$WORK" && pwd -P)
export PATH="$WORK/clang/bin:$WORK/rust/bin:$WORK/bin:$PATH" LIBCLANG_PATH="$WORK/clang/lib"
M=(nice -n 19 make -C "$K" -s ARCH=x86_64 LLVM=1 HOSTCC=clang HOSTLD=ld.lld "RUSTC=$WORK/rust/bin/rustc" "BINDGEN=$WORK/bin/bindgen"
   LOCALVERSION=-android17-5 "-j$(nproc)" KBUILD_BUILD_TIMESTAMP=0 KBUILD_BUILD_USER=gbos KBUILD_BUILD_HOST=gbos)
E="$WORK/ext-gbos_wakefilter"
rm -rf "$E"; mkdir -p "$E"
cp "$HERE/gbos_wakefilter.c" "$E/"
echo "obj-m := gbos_wakefilter.o" > "$E/Kbuild"
# as for ac.ko: map the work directory away so the build-id does not depend on where it was built
"${M[@]}" M="$E" KCFLAGS="-ffile-prefix-map=$WORK/=/gbos-kmod/" modules
llvm-strip --strip-debug -o "$E/gbos_wakefilter.stripped.ko" "$E/gbos_wakefilter.ko"
python3 "$HERE/check-ko.py" "$E/gbos_wakefilter.stripped.ko" "$WORK/vmlinux.symvers" "$VERMAGIC_TAIL"
got=$(sha256sum "$E/gbos_wakefilter.stripped.ko" | cut -d' ' -f1)
[ "$got" = "$WAKEFILTER_KO_SHA" ] || { echo "gbos_wakefilter.ko is not the pinned build ($got)"; exit 1; }
cp "$E/gbos_wakefilter.stripped.ko" "$OUTDIR/gbos_wakefilter.ko.part"; mv "$OUTDIR/gbos_wakefilter.ko.part" "$OUTDIR/gbos_wakefilter.ko"
echo "gbos_wakefilter.ko $got -> $OUTDIR/gbos_wakefilter.ko"
