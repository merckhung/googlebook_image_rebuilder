#!/usr/bin/env bash
# Build gbos_efifb.ko out of tree for the Googlebook GKI kernel (6.18.21-android17-5-g3938c8d97142-ab16302624).
# usage: build-gbos_efifb.sh <kernel headers dir> <symvers: "crc name" per line> <out.ko> <toolchain dir>
#   kernel headers: the kernel's own kheaders archive (kheaders.ko in the image), unpacked (builder/kmod/prepare.py)
#   symvers: the CRCs of the vmlinux exports (__kcrctab), one "0x<crc> <name>" line each (builder/kmod/prepare.py)
#   toolchain dir: the pinned compiler is downloaded to (or reused from) <toolchain dir>/clang
# Every input is pinned: the headers and CRCs come from the pinned base image, the compiler is the GKI's own Android
# clang r584948c (the same tree as builder/kmod/build-ac.sh, about 1 GB to download, 3.5 GB unpacked), so the module is
# the same byte for byte on every Linux x86-64 host (EFIFB_KO_SHA). No kbuild/modpost: modversions, the kCFI preamble
# list (.cfi_sites) and the single __patchable_function_entries section are made here; builder/kmod/check-ko.py then
# checks vermagic, imports and CRCs against the CRC list.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KH=${1:?kernel headers dir}; SYMVERS=${2:?symvers file}; OUT=${3:?out.ko}; TC=${4:?toolchain dir}
SRC="$HERE/gbos_efifb.c"
CLANG_URL=https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/4a7d0a5e443c27d8f68fbd46abfab464e1355a69/clang-r584948c.tar.gz
CLANG_TREE_SHA=66d6efb5a82c10e3750104276b0140e852744ec4234487aabe5ce21af08341ca
EFIFB_KO_SHA=2b4a97781c2a24927413dd4a78ec1f624fc7c5aaaa6ddbb25574c66ab65fd1d9
[ "$(uname -sm)" = "Linux x86_64" ] || { echo "build-gbos_efifb.sh runs on Linux x86-64 only"; exit 2; }
for c in curl tar python3 sha256sum xargs; do command -v $c > /dev/null || { echo "missing $c"; exit 2; }; done
[ -f "$KH/include/generated/autoconf.h" ] || { echo "$KH has no include/generated/autoconf.h"; exit 2; }
# the pinned clang: the gitiles tarball is generated on the fly, so the pin is on the extracted files (as in build-ac.sh)
tree_sha() {
    ( cd "$1" && { find . -type f -print0 | sort -z | xargs -0 sha256sum; find . -type l -printf '%p -> %l\n' | sort; } ) | sha256sum | cut -d' ' -f1
}
mkdir -p "$TC"
if [ -d "$TC/clang" ] && [ "$(tree_sha "$TC/clang")" = "$CLANG_TREE_SHA" ]; then
    echo "ok (cached) Android clang r584948c"
else
    rm -rf "${TC:?}/clang" "$TC/clang.part"; mkdir -p "$TC/clang.part"
    curl -fsSL --retry 3 --max-time 3600 "$CLANG_URL" | tar -xz -C "$TC/clang.part"
    got=$(tree_sha "$TC/clang.part")
    [ "$got" = "$CLANG_TREE_SHA" ] || { echo "the clang tree differs from the pinned one ($got)"; exit 1; }
    mv "$TC/clang.part" "$TC/clang"; echo "ok Android clang r584948c"
fi
CLANG_BIN=$(cd "$TC/clang/bin" && pwd -P); export PATH="$CLANG_BIN:$PATH"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cc() {
  clang -target x86_64-linux-gnu \
    -mcmodel=kernel -mno-red-zone -mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx \
    -fcf-protection=branch \
    -fsanitize=kcfi -fsanitize-cfi-icall-experimental-normalize-integers \
    -fpatchable-function-entry=16,11 \
    -fno-strict-aliasing -fno-common -fshort-wchar -funsigned-char \
    -fno-PIE -fno-asynchronous-unwind-tables -fno-unwind-tables \
    -fno-delete-null-pointer-checks -fno-stack-protector -fno-jump-tables \
    -O2 -g0 -std=gnu11 \
    -ffile-prefix-map="$HERE"=gbos -ffile-prefix-map="$KH"=kh \
    -D__KERNEL__ -DMODULE -DKBUILD_MODNAME='"gbos_efifb"' -DKBUILD_BASENAME='"gbos_efifb"' \
    -nostdinc \
    -I"$KH/arch/x86/include" -I"$KH/arch/x86/include/generated" \
    -I"$KH/include" -I"$KH/include/generated" \
    -I"$KH/arch/x86/include/uapi" -I"$KH/arch/x86/include/generated/uapi" \
    -I"$KH/include/uapi" -I"$KH/include/generated/uapi" \
    -include "$KH/include/linux/kconfig.h" \
    -Wno-unused-function -Wno-unused-variable -Wno-address-of-packed-member -Wno-gnu-variable-sized-type-not-at-end \
    "$@"
}
# pass 1: find every kCFI preamble (__cfi_<function>)
cc -c "$SRC" -o "$T/pass1.o"
llvm-nm "$T/pass1.o" | awk '$3 ~ /^__cfi_/ {print $3}' | sort -u > "$T/cfi.txt"
[ -s "$T/cfi.txt" ] || { echo "no __cfi_ symbols: kCFI preambles missing"; exit 1; }
# pass 2: same source plus a .cfi_sites table (PC32 to each preamble), the boot-time hash rewrite reads it
{ echo 'asm(".pushsection .cfi_sites,\"a\"\n"'; sed 's/.*/".long & - .\\n"/' "$T/cfi.txt"; echo '".popsection\n");'; } > "$T/cfi_sites.h"
cc -include "$T/cfi_sites.h" -c "$SRC" -o "$T/gbos.o"
cmp -s <(llvm-nm "$T/gbos.o" | awk '$3 ~ /^__cfi_/ {print $3}' | sort -u) "$T/cfi.txt" || { echo "pass 2 changed the preamble set"; exit 1; }
# no indirect calls or jumps from module code (their kCFI hashes would not be rewritten at boot)
if llvm-objdump -d --no-show-raw-insn "$T/gbos.o" | grep -E '\b(call|jmp)[a-z]*\s+\*' ; then echo "indirect call/jump in module code"; exit 1; fi
python3 - "$SYMVERS" "$T" << 'PY'
import struct, subprocess, sys
from pathlib import Path
symvers, t = sys.argv[1], Path(sys.argv[2])
by = {}
for line in open(symvers):
    c, n = line.split(maxsplit=1)
    by[n.strip()] = int(c, 16)
syms = []
for line in subprocess.check_output(["llvm-nm", "-u", str(t / "gbos.o")], text=True).splitlines():
    name = line.split()[-1]
    if name == "__this_module":
        continue
    if name not in by:
        raise SystemExit("not exported by this kernel: " + name)
    syms.append(name)
syms = sorted(set(syms)) + ["module_layout"]
rec = bytearray(); crcs = bytearray(); names = bytearray()
for name in syms:
    crc = by[name]
    rec += (struct.pack("<II", crc, 0) + name.encode() + b"\0").ljust(64, b"\0")
    crcs += struct.pack("<I", crc); names += name.encode() + b"\0"
(t / "versions.bin").write_bytes(rec); (t / "extcrc.bin").write_bytes(crcs); (t / "extnames.bin").write_bytes(names)
print("imports", len(syms))
PY
llvm-objcopy \
  --add-section __versions="$T/versions.bin" --set-section-flags __versions=alloc,readonly \
  --add-section __version_ext_crcs="$T/extcrc.bin" --set-section-flags __version_ext_crcs=alloc,readonly \
  --add-section __version_ext_names="$T/extnames.bin" --set-section-flags __version_ext_names=alloc,readonly \
  "$T/gbos.o" "$T/gbos2.o"
clang -target x86_64-linux-gnu -r -nostdlib -o "$T/out.ko" "$T/gbos2.o"
python3 - "$T/out.ko" "$(wc -l < "$T/cfi.txt")" << 'PY'
import re, subprocess, sys
from collections import Counter
ko, ncfi = sys.argv[1], int(sys.argv[2])
names = [m.group(1) for m in re.finditer(r"^\s*\[\s*\d+\]\s+(\S+)", subprocess.check_output(["llvm-readelf", "-S", "-W", ko], text=True), re.M)]
dups = [n for n, k in Counter(names).items() if k > 1]
if dups:
    raise SystemExit(f"duplicate sections {dups}")
for need in (".cfi_sites", "__versions", "__patchable_function_entries", ".gnu.linkonce.this_module", "__param"):
    if need not in names:
        raise SystemExit("missing section " + need)
rel = subprocess.check_output(["llvm-readelf", "-r", "-W", ko], text=True)
sec = rel.split("'.rela.cfi_sites'")[1].split("Relocation section")[0] if "'.rela.cfi_sites'" in rel else ""
n = sum(1 for l in sec.splitlines() if "R_X86_64_PC32" in l)
if n != ncfi:
    raise SystemExit(f".cfi_sites has {n} PC32 relocations, expected {ncfi}")
print("sections ok, cfi_sites", n)
PY
python3 "$HERE/check-ko.py" "$T/out.ko" "$SYMVERS" "SMP preempt mod_unload modversions "
got=$(sha256sum "$T/out.ko" | cut -d' ' -f1)
[ "$got" = "$EFIFB_KO_SHA" ] || { echo "gbos_efifb.ko is not the pinned build ($got)"; exit 1; }
mv "$T/out.ko" "$OUT"
echo "gbos_efifb.ko $got -> $OUT"
