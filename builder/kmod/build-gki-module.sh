#!/usr/bin/env bash
# Build a module from C using the selected recovery kernel headers and CRCs.
# Arguments: source.c name headers symvers output.ko clang-bin vermagic-header
# Call through build-recovery-modules.sh to verify the inputs and output pins.
set -euo pipefail
SRC=${1:?src.c}; MOD=${2:?modname}; KH=${3:?kh dir}; SYMVERS=${4:?symvers}
OUT=${5:?out.ko}; CLANG_BIN=${6:?clang bin dir}; PRE=${7:?pre-include.h}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[ -f "$KH/include/generated/autoconf.h" ] || { echo "$KH has no include/generated/autoconf.h"; exit 2; }
export PATH="$CLANG_BIN:$PATH"
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
    -O2 -g0 -std=gnu11 -ffile-prefix-map="$(dirname "$SRC")/"=guest/kmod/ -ffile-prefix-map="$KH/"=kernel-headers/ \
    -D__KERNEL__ -DMODULE -DKBUILD_MODNAME="\"$MOD\"" -DKBUILD_BASENAME="\"$MOD\"" \
    -nostdinc \
    -I"$KH/arch/x86/include" -I"$KH/arch/x86/include/generated" \
    -I"$KH/include" -I"$KH/include/generated" \
    -I"$KH/arch/x86/include/uapi" -I"$KH/arch/x86/include/generated/uapi" \
    -I"$KH/include/uapi" -I"$KH/include/generated/uapi" \
    -include "$KH/include/linux/kconfig.h" ${PRE:+-include "$PRE"} \
    -Wno-unused-function -Wno-unused-variable -Wno-address-of-packed-member \
    -Wno-gnu-variable-sized-type-not-at-end \
    "$@"
}
# kbuild normally generates this translation unit. Compile it against the
# recovery kernel's struct module, rather than editing another kernel's binary.
if [ "$MOD" != gbos_efifb ]; then
    cat > "$T/metadata.c" <<EOF
#include <linux/module.h>
extern int init_module(void);
extern void cleanup_module(void);
struct module __this_module
__attribute__((section(".gnu.linkonce.this_module"), aligned(64), used)) = {
    .name = "$MOD", .init = init_module, .exit = cleanup_module,
};
MODULE_INFO(name, "$MOD");
MODULE_INFO(vermagic, GBOS_VERMAGIC);
MODULE_INFO(retpoline, "Y");
EOF
    cc -c "$T/metadata.c" -o "$T/metadata.o"
else
    # gbos_efifb supplies its own module metadata.
    cc -x c -c /dev/null -o "$T/metadata.o"
fi
cc -c "$SRC" -o "$T/pass1.o"
llvm-nm "$T/pass1.o" | awk '$3 ~ /^__cfi_/ {print $3}' | sort -u > "$T/cfi.txt"
[ -s "$T/cfi.txt" ] || { echo "no __cfi_ symbols: kCFI preambles missing"; exit 1; }
{ echo 'asm(".pushsection .cfi_sites,\"a\"\n"'; sed 's/.*/".long & - .\\n"/' "$T/cfi.txt"; echo '".popsection\n");'; } > "$T/cfi_sites.h"
cc -include "$T/cfi_sites.h" -c "$SRC" -o "$T/mod.o"
if llvm-objdump -d --no-show-raw-insn "$T/mod.o" | grep -E '\b(call|jmp)[a-z]*\s+\*'; then
  echo "indirect call/jump in module code"; exit 1
fi
python3 - "$SYMVERS" "$T" << 'PY'
import struct, subprocess, sys
from pathlib import Path
symvers, t = sys.argv[1], Path(sys.argv[2])
by = {}
for line in open(symvers):
    c, n = line.split(maxsplit=1)
    by[n.strip()] = int(c, 16)
syms = []
for line in subprocess.check_output(["llvm-nm", "-u", str(t / "mod.o")], text=True).splitlines():
    name = line.split()[-1]
    if name == "__this_module":
        continue
    if name not in by:
        raise SystemExit("not an unprotected vmlinux export: " + name)
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
  "$T/mod.o" "$T/mod2.o"
cat > "$T/module.lds" <<'LDS'
SECTIONS { __patchable_function_entries : { *(__patchable_function_entries) } }
LDS
clang -target x86_64-linux-gnu -fuse-ld=lld -r -nostdlib -Wl,-T,"$T/module.lds" -o "$T/out.ko" "$T/mod2.o" "$T/metadata.o"
dups=$(llvm-readelf -S -W "$T/out.ko" | sed -n 's/^ *\[ *[0-9]*\] \([^ ]*\).*/\1/p' | sort | uniq -d)
[ -z "$dups" ] || { echo "duplicate sections: $dups"; exit 1; }
python3 "$HERE/check-ko.py" "$T/out.ko" "$SYMVERS" "SMP preempt mod_unload modversions "
mv "$T/out.ko" "$OUT"
echo "$MOD.ko $(sha256sum "$OUT" | cut -d' ' -f1) -> $OUT"
