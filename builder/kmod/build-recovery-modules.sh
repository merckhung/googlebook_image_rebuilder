#!/usr/bin/env bash
# Rebuild CX54 modules from the selected recovery's own headers and exported CRCs.
# Usage: build-recovery-modules.sh BASE OTATOOLS OUT TOOLCHAIN [--allow-drift]
# --allow-drift is only for pin authoring; normal builds verify all module hashes.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BASE=${1:?base image}; OTA=${2:?otatools}; OUT=${3:?new output dir}; TC=${4:?toolchain dir}
DRIFT=${5:-}
[ -z "$DRIFT" ] || [ "$DRIFT" = --allow-drift ] || exit 2
[ "$(python3 "$HERE/../lib/lapis.py" --get MODULE_BUILD)" = recovery-headers ] || {
    echo "the selected Lapis build does not use the recovery-header module recipe"; exit 2;
}
[ ! -e "$OUT" ] || { echo "$OUT must be new"; exit 2; }
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
CLANG_URL=https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/4a7d0a5e443c27d8f68fbd46abfab464e1355a69/clang-r584948c.tar.gz
CLANG_TREE_SHA=66d6efb5a82c10e3750104276b0140e852744ec4234487aabe5ce21af08341ca
AC_URL=https://android.googlesource.com/kernel/common/+/3938c8d97142d5cf31543aa96f864a2cf832dda0/drivers/acpi/ac.c?format=TEXT
AC_SOURCE_SHA=a88337f55d33b2841fed7c7404ef2c42ead8f35449a08fc0b599a1c03a74b1b9
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
extra=()
[ -z "${GBOS_RECOVERY_KERNEL:-}" ] || extra+=("$GBOS_RECOVERY_KERNEL")
python3 "$HERE/prepare.py" "$BASE" "$OTA" "$OUT/prep" "${extra[@]}"
python3 - "$HERE" "$OUT" <<'PYPINS'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent / "lib"))
import lapis
pins = lapis.load()
out = Path(sys.argv[2])
release = pins["GKI_RELEASE"]
assert (out / "prep/kh/include/generated/utsrelease.h").read_text().strip() == '#define UTS_RELEASE "' + release + '"'
(out / "vermagic.h").write_text('#define GBOS_VERMAGIC "' + release + ' SMP preempt mod_unload modversions "\n#define GBOS_FAULT_RETRY 1\n')
PYPINS
curl -fsSL --retry 3 "$AC_URL" | base64 -d > "$OUT/ac.c"
[ "$(sha256sum "$OUT/ac.c" | cut -d' ' -f1)" = "$AC_SOURCE_SHA" ] || { echo "AC source hash mismatch"; exit 1; }
for name in ac gbos_wakefilter gbos_isa_emu gbos_efifb; do
    src="$HERE/$name.c"
    [ "$name" != ac ] || src="$OUT/ac.c"
    bash "$HERE/build-gki-module.sh" "$src" "$name" "$OUT/prep/kh" "$OUT/prep/symvers.txt" "$OUT/$name.ko" "$CLANG_BIN" "$OUT/vermagic.h"
done
python3 - "$HERE" "$OUT" "$DRIFT" <<'PYVERIFY'
import hashlib,sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent / "lib"))
import lapis
pins = lapis.load()
for name, key in [("ac", "AC_KO_SHA"), ("gbos_wakefilter", "WAKEFILTER_KO_SHA"), ("gbos_isa_emu", "ISA_KO_SHA"), ("gbos_efifb", "EFIFB_KO_SHA")]:
    got = hashlib.sha256((Path(sys.argv[2]) / (name + ".ko")).read_bytes()).hexdigest()
    if got != pins.get(key):
        if sys.argv[3] != "--allow-drift":
            raise SystemExit(f"{name}: {got} differs from the selected build's pin")
        print(f"WARNING: pin authoring: {key}={got}")
PYVERIFY
