#!/bin/bash
# build-base.sh - rebuild the KANO base image (kano-real.img) and the standalone
# initrd.img from the public lapis recovery zip.
#
# What it reproduces: the KANO base image (kano-real.img)
# (sha256 1ce7e958..., built by hand on 2026-10-11, REPORT.md milestones A-N)
# and work/img/initrd.img.  With the original AVB key the output is
# byte-identical; with any other key only vbmeta_a, the bootconfig of the
# standalone initrd and the whole-image hash change.  See README.md.
#
# No sudo, no mounts, no block devices: every write goes to regular files under
# --out.  Long run (~5-10 min, ~45 GB peak in --out); run it with nohup.
set -euo pipefail
umask 022

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HELPER="$HERE/gbos_base.py"
py() { python3 -I -B "$HELPER" "$@"; }
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNING: %s\n' "$*" >&2; WARNINGS+=("$*"); }
WARNINGS=()

# ---------------------------------------------------------------- pinned inputs
# The recovery build: recovery zip, its image, the expected intermediates and partition sizes (base/builds/<build>.sh;
# GBOS_LAPIS_BUILD selects one, default 16658739).
LAPIS_BUILD=${GBOS_LAPIS_BUILD:-16658739}
export GBOS_LAPIS_BUILD="$LAPIS_BUILD"
[[ $LAPIS_BUILD =~ ^[0-9]+$ ]] && [ -f "$HERE/builds/$LAPIS_BUILD.sh" ] || die "no pins for recovery build '$LAPIS_BUILD' in $HERE/builds"
declare -A EXPECT=() EXPECT_REFKEY=()   # filled by the build file
# shellcheck source=builds/16452207.sh
. "$HERE/builds/$LAPIS_BUILD.sh"
for v in BIN_SHA256 BIN_SIZE REF_PUBKEY_SHA1 VENDOR_PART_SIZE SYSEXT_PART_SIZE; do
  [ -n "${!v:-}" ] || die "base/builds/$LAPIS_BUILD.sh does not set $v"
done
INPUT_KIND=${INPUT_KIND:-zip}
if [ "$INPUT_KIND" = zip ]; then
  for v in ZIP_URL ZIP_SHA256 ZIP_MEMBER; do
    [ -n "${!v:-}" ] || die "base/builds/$LAPIS_BUILD.sh does not set $v"
  done
elif [ "$INPUT_KIND" != raw ]; then
  die "unsupported INPUT_KIND $INPUT_KIND"
fi
[ "${#EXPECT[@]}" -gt 0 ] && [ "${#EXPECT_REFKEY[@]}" -gt 0 ] || die "base/builds/$LAPIS_BUILD.sh has no expected intermediates"

# Public Cuttlefish CI build (REPORT.md milestones J line 339, L line 374).
CF_RAW=https://ci.android.com/builds/submitted/16373615/aosp_cf_x86_64_only_phone-userdebug/latest/raw
CF_TARGET_FILES=$CF_RAW/aosp_cf_x86_64_only_phone-target_files-16373615.zip
CF_OTATOOLS=$CF_RAW/otatools.zip
KEYMINT_MEMBER=VENDOR/apex/com.android.hardware.keymint.rust_nonsecure.apex
KEYMINT_SHA=01387704ab1ee39d4d70aab09eaf865e83e896ad4f79ed161707ce78da7ef697
GATEKEEPER_MEMBER=VENDOR/apex/com.android.hardware.gatekeeper.nonsecure.apex
GATEKEEPER_SHA=17ed9be4744ff86e9151af3fc82aff508a5e4bd54e9b5e679d45ecb499b930b3
# otatools.zip members used (avbtool 1.4.0, lpmake, erofs-utils 1.8.3, fec + libs)
OTA_FILES=(
  bin/avbtool=e32e02591c36e4c2871578c3f57f377766b1366f84e6197b4cfb7f34c63fa6d5
  bin/fec=9149f8c809f20db278b22fe81ec624b1ecfb7908edce27d14088cba6ae06f58e
  bin/fsck.erofs=dd61160aa49a3d63da41ed2ff13e0e42be37d4a09a88f9d459002b2ca0cc2db7
  bin/lpmake=28eb0383c8298bd4bbb6030e6f03e1a8679985e0ad3ecfb81a722868864d529e
  bin/mkfs.erofs=b254640c783f9626b81b90d483c35d0e998c42113ba1259d1f3f5accfd0dc8e0
  lib64/libbase.so=64ed09763bc786598efd694dbed64219704915754859c472db2ca88f89aaae93
  lib64/libc++.so=acca15da090d439bbd4f6d7bbfb43bd53652639ad20487ea87304c8d00edb306
  lib64/libcrypto-host.so=d7ddef4e543fa1c3d00c6d368ba9f2e58b4c01aa8fb4a1f7d883fbec75fbe69a
  lib64/libcrypto_utils.so=99089f13a0987708f9adf84b21ac7e614e3f8b4851b08ca7a03b58f434db6314
  lib64/libext4_utils.so=541ccc944435959bc18a3ca8ae349d579e98826fad093a45ca8aa68cb4a7bb38
  lib64/libfec_rs.so=e4c6f952222ed1782a73dcae12dead94501913e817aa47b593b1a7ad821dbb35
  lib64/liblog.so=936872310a280e215e254c0b327731ebfd0d4000c8b98e92258ab4278e74a912
  lib64/liblp.so=eb4d7b4b335d6a60a1e1e16a06d2878690feb70b48ff621e1a6503f3b1acff97
  lib64/libsparse-host.so=b6633c55218fa486f836a01d4464d81da87d88e171f40ede24beb1c9529411c7
  lib64/libz-host.so=633288a14a2de101cbb4353da563bcedaa1d22c1aa31c8cabce20ac2508ffd2d
)
# systemd-boot (Arch Linux systemd 261.2-1, usr/lib/systemd/boot/efi/systemd-bootx64.efi).  A separate upstream artifact.
# Taken from the build host when it is that exact file, otherwise from the Arch package archive (pinned below).
SDBOOT_SHA=1c09c781f6a492807d1f96e4e064c0638caf553a1319e4de8315a40c2d224d9c
SDBOOT_PKG_URL=https://archive.archlinux.org/packages/s/systemd/systemd-261.2-1-x86_64.pkg.tar.zst
SDBOOT_PKG_SHA=88ed13de43e53e6b8ba6039e7e58c1bc2a15c47234b05f48e68319c93b661cd7

# FAT timestamps of the original session.  The ESP was made on 2026-10-11 at
# 04:47:28 (+0800, milestone E) and its loader files replaced at 07:55:02
# (milestone G).  mtools converts SOURCE_DATE_EPOCH with localtime(), so TZ is
# pinned to +08:00; mkfs.fat 4.2 converts it with gmtime(), so it gets the
# same wall-clock time expressed as UTC.
export TZ='<+08>-8'
ESP_EPOCH_E=1791665248      # 2026-10-11 04:47:28 +0800
ESP_EPOCH_E_MKFS=1791694048 # 2026-10-11 04:47:28 interpreted as UTC (mkfs.fat)
ESP_EPOCH_G=1791676502      # 2026-10-11 07:55:02 +0800
ESP_SERIAL=461813AC
ESP_LABEL=ESP

# ---------------------------------------------------------------- arguments
usage() {
  cat <<EOF
Usage: $0 --zip recovery.zip --out DIR [options]
       $0 --recovery-bin recovery.bin --out DIR [options]  (verified public image)

  --zip PATH            official recovery zip (${ZIP_URL:-not available for this build})
  --recovery-bin PATH   already-extracted recovery image; its SHA-256
                        and size must match the selected build's pins
  --out DIR             output directory (must not already hold a finished image)
  --key PEM             RSA-4096 AVB key for vbmeta_a; if omitted a new one is
                        generated with 'openssl genrsa' into DIR/keys/vbmeta.pem
  --otatools DIR        AOSP otatools (bin/, lib64/) to use; if omitted the needed
                        members are fetched from $CF_OTATOOLS
  --keymint-apex PATH   com.android.hardware.keymint.rust_nonsecure.apex
  --gatekeeper-apex PATH com.android.hardware.gatekeeper.nonsecure.apex
                        (if omitted both are fetched from the Cuttlefish
                        target_files zip of build 16373615 with HTTP Range)
  --systemd-boot PATH   systemd-bootx64.efi; must be the pinned one (default: the host
                        copy if it is, else fetched from the Arch package archive)
  --cache DIR           where fetched artefacts go (default DIR/cache)
  --allow-drift         treat hash mismatches of intermediates as warnings (never for
                        downloaded inputs, otatools or systemd-boot)
  --keep-work           keep DIR/work (about 25 GB) after a successful build
EOF
}

ZIP='' RECOVERY_BIN='' OUT='' KEY='' OTATOOLS='' KEYMINT='' GATEKEEPER='' CACHE='' ALLOW_DRIFT=0 KEEP_WORK=0
SDBOOT=''
while [ $# -gt 0 ]; do
  case "$1" in
    --zip) ZIP=$2; shift 2 ;;
    --recovery-bin) RECOVERY_BIN=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --key) KEY=$2; shift 2 ;;
    --otatools) OTATOOLS=$2; shift 2 ;;
    --keymint-apex) KEYMINT=$2; shift 2 ;;
    --gatekeeper-apex) GATEKEEPER=$2; shift 2 ;;
    --systemd-boot) SDBOOT=$2; shift 2 ;;
    --cache) CACHE=$2; shift 2 ;;
    --allow-drift) ALLOW_DRIFT=1; shift ;;
    --keep-work) KEEP_WORK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument $1" ;;
  esac
done
[ -n "$OUT" ] || { usage >&2; die "--out is required"; }
if [ "$INPUT_KIND" = zip ]; then
  { [ -n "$ZIP" ] || [ -n "$RECOVERY_BIN" ]; } || die "--zip or a hash-pinned public --recovery-bin is required"
  { [ -z "$ZIP" ] || [ -z "$RECOVERY_BIN" ]; } || die "choose --zip or --recovery-bin"
else
  [ -n "$RECOVERY_BIN" ] && [ -z "$ZIP" ] || die "raw build requires --recovery-bin and no --zip"
fi

# ---------------------------------------------------------------- guards
[ "$(id -u)" -ne 0 ] || die "do not run as root; nothing here needs it"
[ -z "$ZIP" ] || [ -f "$ZIP" ] || die "zip not found: $ZIP"
[ -z "$RECOVERY_BIN" ] || [ -f "$RECOVERY_BIN" ] || die "recovery image not found: $RECOVERY_BIN"
if [ -e "$OUT" ] && [ ! -d "$OUT" ]; then die "--out exists and is not a directory: $OUT"; fi
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
# --out must be new, empty, or an earlier build-base.sh output (marker): never clean up a directory this did not make
if [ ! -e "$OUT/.gbos-build-base" ]; then
  [ -z "$(ls -A "$OUT")" ] || die "--out $OUT is not empty and was not made by build-base.sh; use a new directory"
  : > "$OUT/.gbos-build-base"
fi
IMG_FINAL="$OUT/kano-real.img"
IMG="$OUT/kano-real.img.partial"
INITRD_FINAL="$OUT/initrd.img"
for f in "$IMG_FINAL" "$OUT/BUILD-DONE" "$INITRD_FINAL"; do
  [ -e "$f" ] && die "$OUT already contains a finished image ($f); use a new --out"
done
for f in "$IMG" "$OUT/work"; do
  if [ -e "$f" ] && [ ! -f "$f" ] && [ ! -d "$f" ]; then die "$f is not a regular file/dir"; fi
done
W="$OUT/work"
if [ -e "$W" ] || [ -e "$IMG" ]; then
  log "removing leftovers of an unfinished run in $OUT"
  [ -d "$W" ] && chmod -R u+rwX "$W" && rm -rf "$W"
  rm -f "$IMG"
fi
mkdir -p "$W"
CACHE=${CACHE:-$OUT/cache}
mkdir -p "$CACHE"

check() {  # check NAME FILE  (against EXPECT[NAME])
  local name=$1 file=$2 want=${EXPECT[$1]} got
  got=$(sha256sum "$file" | cut -d' ' -f1)
  if [ "$got" = "$want" ]; then
    log "  ok   $name $got"
  elif [ "$ALLOW_DRIFT" = 1 ]; then
    warn "$name differs from kano-real build: $got (expected $want)"
  else
    die "$name sha256 $got != expected $want (use --allow-drift to continue)"
  fi
}

# ---------------------------------------------------------------- host tools
for t in python3 lz4 mkfs.fat mmd mcopy openssl sha256sum truncate; do
  command -v "$t" >/dev/null || die "missing host tool: $t"
done
MKFSFAT_V=$(mkfs.fat --help 2>&1 | sed -n 's/^mkfs.fat \([0-9.]*\).*/\1/p' | head -1 || true)
[ -n "$MKFSFAT_V" ] || MKFSFAT_V=$(mkfs.fat 2>&1 | sed -n 's/^mkfs.fat \([0-9.]*\).*/\1/p' | head -1 || true)
MTOOLS_V=$(mtools --version 2>/dev/null | sed -n 's/.*mtools) \([0-9.]*\).*/\1/p' | head -1)
[ "$MKFSFAT_V" = 4.2 ] || warn "mkfs.fat $MKFSFAT_V (reference 4.2): ESP bytes may differ"
MTOOLS_REF_VERSION=${MTOOLS_REF_VERSION:-4.0.49}
[ "$MTOOLS_V" = "$MTOOLS_REF_VERSION" ] || warn "mtools $MTOOLS_V (reference $MTOOLS_REF_VERSION): ESP bytes may differ"
# mtools reads /etc/mtools.conf and ~/.mtoolsrc; pin the options that change bytes.
export MTOOLS_SKIP_CHECK=1 MTOOLS_LOWER_CASE=0 MTOOLS_NO_VFAT=0 MTOOLS_NAME_NUMERIC_TAIL=1 \
       MTOOLS_FAT_COMPATIBILITY=0

# ---------------------------------------------------------------- otatools
if [ -z "$OTATOOLS" ]; then
  OTATOOLS="$CACHE/otatools"
  specs=()
  for e in "${OTA_FILES[@]}"; do
    f=${e%%=*}; s=${e#*=}
    [ -f "$OTATOOLS/$f" ] || specs+=("$f=$s:$f")
  done
  if [ ${#specs[@]} -gt 0 ]; then
    log "fetching ${#specs[@]} otatools members from $CF_OTATOOLS"
    py fetch-zip "$CF_OTATOOLS" "$OTATOOLS" "${specs[@]}"
  fi
  chmod +x "$OTATOOLS"/bin/*
fi
OTATOOLS=$(cd "$OTATOOLS" && pwd)
for e in "${OTA_FILES[@]}"; do
  f=${e%%=*}; s=${e#*=}
  [ -f "$OTATOOLS/$f" ] || die "otatools file missing: $OTATOOLS/$f"
  got=$(sha256sum "$OTATOOLS/$f" | cut -d' ' -f1)
  [ "$got" = "$s" ] || die "otatools $f sha256 $got != $s (executables are never allowed to drift)"
done
export PATH="$OTATOOLS/bin:$PATH"
export LD_LIBRARY_PATH="$OTATOOLS/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
AVB_V=$(avbtool version 2>/dev/null)
EROFS_V=$(mkfs.erofs --version 2>&1 | sed -n 's/.*(erofs-utils) \([0-9.]*\).*/\1/p' | head -1)
[ "$AVB_V" = "avbtool 1.4.0" ] || warn "$AVB_V (reference avbtool 1.4.0)"
[ "$EROFS_V" = 1.8.3 ] || warn "mkfs.erofs $EROFS_V (reference 1.8.3)"

# ---------------------------------------------------------------- Cuttlefish APEXes
if [ -z "$KEYMINT" ] || [ -z "$GATEKEEPER" ]; then
  APEXDIR="$CACHE/cf-16373615"
  specs=()
  [ -f "$APEXDIR/${KEYMINT_MEMBER##*/}" ] || specs+=("$KEYMINT_MEMBER=$KEYMINT_SHA")
  [ -f "$APEXDIR/${GATEKEEPER_MEMBER##*/}" ] || specs+=("$GATEKEEPER_MEMBER=$GATEKEEPER_SHA")
  if [ ${#specs[@]} -gt 0 ]; then
    log "fetching Cuttlefish APEXes from $CF_TARGET_FILES"
    py fetch-zip "$CF_TARGET_FILES" "$APEXDIR" "${specs[@]}"
  fi
  KEYMINT=${KEYMINT:-$APEXDIR/${KEYMINT_MEMBER##*/}}
  GATEKEEPER=${GATEKEEPER:-$APEXDIR/${GATEKEEPER_MEMBER##*/}}
fi
py check-sha "$KEYMINT=$KEYMINT_SHA" "$GATEKEEPER=$GATEKEEPER_SHA" || die "APEX sha256 mismatch"

# ---------------------------------------------------------------- systemd-boot
if [ -z "$SDBOOT" ]; then
  host=/usr/lib/systemd/boot/efi/systemd-bootx64.efi
  if [ -f "$host" ] && [ "$(sha256sum "$host" | cut -d' ' -f1)" = "$SDBOOT_SHA" ]; then
    SDBOOT=$host
  else
    SDBOOT="$CACHE/systemd-bootx64.efi"
    if [ ! -f "$SDBOOT" ] || [ "$(sha256sum "$SDBOOT" | cut -d' ' -f1)" != "$SDBOOT_SHA" ]; then
      pkg="$CACHE/${SDBOOT_PKG_URL##*/}"
      log "fetching systemd-boot from $SDBOOT_PKG_URL"
      curl -fL --retry 3 -o "$pkg.part" "$SDBOOT_PKG_URL" || die "systemd package download failed"
      [ "$(sha256sum "$pkg.part" | cut -d' ' -f1)" = "$SDBOOT_PKG_SHA" ] || die "systemd package sha256 mismatch"
      mv "$pkg.part" "$pkg"
      tar --zstd -xOf "$pkg" usr/lib/systemd/boot/efi/systemd-bootx64.efi > "$SDBOOT.part" || die "systemd-boot not in the package"
      mv "$SDBOOT.part" "$SDBOOT"
    fi
  fi
fi
[ -f "$SDBOOT" ] || die "systemd-boot EFI not found: $SDBOOT"
SDBOOT_GOT=$(sha256sum "$SDBOOT" | cut -d' ' -f1)
[ "$SDBOOT_GOT" = "$SDBOOT_SHA" ] || die "systemd-bootx64.efi sha256 $SDBOOT_GOT is not the pinned $SDBOOT_SHA (Arch systemd 261.2-1)"
SDBOOT_MATCH=1

# ---------------------------------------------------------------- AVB key
if [ -z "$KEY" ]; then
  KEY="$OUT/keys/vbmeta.pem"
  if [ ! -f "$KEY" ]; then
    log "generating a new RSA-4096 AVB key at $KEY (not printed)"
    mkdir -p "$OUT/keys"; chmod 700 "$OUT/keys"
    ( umask 077; openssl genrsa -out "$KEY" 4096 2>/dev/null )
  fi
fi
[ -f "$KEY" ] || die "key not found: $KEY"
KEYBITS=$(openssl pkey -in "$KEY" -noout -text_pub 2>/dev/null | sed -n 's/.*(\([0-9]*\) bit).*/\1/p' | head -1)
[ "$KEYBITS" = 4096 ] || die "AVB key must be RSA-4096 (got '${KEYBITS:-unreadable}')"
avbtool extract_public_key --key "$KEY" --output "$W/vbmeta.pub.bin"
PUBKEY_SHA1=$(sha1sum "$W/vbmeta.pub.bin" | cut -d' ' -f1)
REFKEY=0; [ "$PUBKEY_SHA1" = "$REF_PUBKEY_SHA1" ] && REFKEY=1
log "AVB public key sha1 $PUBKEY_SHA1 (reference key: $([ $REFKEY = 1 ] && echo yes || echo no))"

# ---------------------------------------------------------------- step 1: official image
log "step 1: verify recovery input for Lapis $LAPIS_BUILD"
if [ -n "$ZIP" ]; then
  ZIP_GOT=$(sha256sum "$ZIP" | cut -d' ' -f1)
  [ "$ZIP_GOT" = "$ZIP_SHA256" ] || die "zip sha256 $ZIP_GOT != $ZIP_SHA256 (expected lapis $LAPIS_BUILD)"
fi
if [ -n "$RECOVERY_BIN" ]; then
  [ "$(readlink -f "$RECOVERY_BIN")" != "$(readlink -m "$IMG")" ] || die "recovery image and output must differ"
  cp --reflink=auto --sparse=always "$RECOVERY_BIN" "$IMG"
  BIN_GOT=$(sha256sum "$IMG" | cut -d' ' -f1)
else
  BIN_GOT=$(py unzip-bin "$ZIP" "$ZIP_MEMBER" "$IMG")
fi
[ "$BIN_GOT" = "$BIN_SHA256" ] || die "extracted image sha256 $BIN_GOT != $BIN_SHA256"
[ "$(stat -c %s "$IMG")" = "$BIN_SIZE" ] || die "extracted image size"
log "  ok   recovery image $BIN_GOT"

log "step 2: GPT layout and boot artefacts"
py layout "$IMG" "$W/layout.env" >/dev/null
# shellcheck disable=SC1091
source "$W/layout.env"
py boot-extract "$IMG" "$W/layout.env" "$W/boot" >/dev/null
for n in kernel.efi vendor_platform.cpio vendor_dlkm.cpio init_boot.cpio vendor_bootconfig.txt \
         vendor_cmdline.txt vbmeta_a.orig.img; do
  check "$n" "$W/boot/$n"
done
read -r ORIG_DIGEST ORIG_VBSIZE < <(py vbmeta-digest "$W/boot/vbmeta_a.orig.img")
[ -n "${ORIG_VBSIZE:-}" ] || die "cannot read the stock vbmeta digest"
BOOT_UUID=$BOOT_GUID

log "step 3: ESP initrd (milestone E: stock vbmeta digest, boot_part_uuid)"
py make-initrd "$W/initrd.partuuid.img" "$W/boot/vendor_bootconfig.txt" \
  "$W/boot/vendor_platform.cpio" "$W/boot/vendor_dlkm.cpio" "$W/boot/init_boot.cpio" -- \
  androidboot.slot_suffix=_a androidboot.force_normal_boot=1 \
  "androidboot.boot_part_uuid=$BOOT_UUID" androidboot.vbmeta.hash_alg=sha256 \
  "androidboot.vbmeta.size=$ORIG_VBSIZE" "androidboot.vbmeta.digest=$ORIG_DIGEST" >/dev/null
check initrd.partuuid.img "$W/initrd.partuuid.img"

log "step 4: ESP (FAT32: milestone E create, G replace loader files, M in-place edit)"
py loader-confs "$W/boot/vendor_cmdline.txt" "$W/loader" strict >/dev/null
ESPIMG="$W/esp.img"
truncate -s "$ESP_SIZE" "$ESPIMG"
SOURCE_DATE_EPOCH=$ESP_EPOCH_E_MKFS mkfs.fat -F 32 -n "$ESP_LABEL" -i "$ESP_SERIAL" "$ESPIMG" >/dev/null
export SOURCE_DATE_EPOCH=$ESP_EPOCH_E
mmd -i "$ESPIMG" ::/EFI ::/EFI/BOOT ::/loader ::/loader/entries
mcopy -i "$ESPIMG" "$SDBOOT" ::/EFI/BOOT/BOOTX64.EFI
mcopy -i "$ESPIMG" "$W/loader/E/loader.conf" ::/loader/loader.conf
mcopy -i "$ESPIMG" "$W/loader/E/lapis.conf" ::/loader/entries/lapis.conf
mcopy -i "$ESPIMG" "$W/boot/kernel.efi" ::/kernel.efi
mcopy -i "$ESPIMG" "$W/initrd.partuuid.img" ::/initrd.img
export SOURCE_DATE_EPOCH=$ESP_EPOCH_G
mcopy -o -i "$ESPIMG" "$W/loader/G/loader.conf" ::/loader/loader.conf
mcopy -o -i "$ESPIMG" "$W/loader/G/lapis.conf" ::/loader/entries/lapis.conf
unset SOURCE_DATE_EPOCH
py esp-rewrite "$ESPIMG" loader/entries/lapis.conf "$W/loader/M/lapis.conf" >/dev/null
# The ESP bytes depend on the host's systemd-boot, mkfs.fat and mtools; with other versions it is a different (but
# equivalent) FAT image, and builder/image.py rewrites the loader files anyway.
if [ "$SDBOOT_MATCH" = 1 ] && [ "$MKFSFAT_V" = 4.2 ] && [ "$MTOOLS_V" = "$MTOOLS_REF_VERSION" ]; then check esp.img "$ESPIMG"
else warn "ESP not compared with kano-real.img (host systemd-boot/mkfs.fat/mtools differ from the reference)"; fi
py splice "$IMG" "$ESP_OFF" "$ESP_SIZE" "$ESPIMG" >/dev/null

log "step 5: logical partitions from super"
py lp-extract "$IMG" "$SUPER_OFF" "$W/lp" >/dev/null
for n in system_a system_b vendor_a product_a system_ext_a system_dlkm_a vendor_dlkm_a; do
  check "$n.img" "$W/lp/$n.img"
done

rebuild_erofs() {  # NAME MOUNT PARTSIZE EDIT-SUBCOMMAND [extra args]
  local name=$1 mount=$2 psize=$3 edit=$4; shift 4
  local src="$W/lp/${name}_a.img" d="$W/$name"
  mkdir -p "$d"
  local fs_size; fs_size=$(py avb-orig-size "$src")
  head -c "$fs_size" "$src" > "$d/orig.erofs"
  py erofs-meta "$d/orig.erofs" "$d/meta.json" >/dev/null
  fsck.erofs --extract="$d/tree" --overwrite --preserve-perms "$d/orig.erofs" > "$d/extract.log" 2>&1
  py "$edit" "$d/tree" "$d/meta.json" "$@" "$d/file_contexts" "$d/fs_config"
  local uuid; uuid=$(py erofs-uuid "$d/orig.erofs")
  rm -f "$W/$name.erofs"
  mkfs.erofs -z lz4hc,level=9 -b 4096 -T 1230768000 -U "$uuid" --mount-point "$mount" \
    --fs-config-file "$d/fs_config" --file-contexts "$d/file_contexts" \
    "$W/$name.erofs" "$d/tree" > "$d/mkfs.log" 2>&1 || { tail -20 "$d/mkfs.log"; die "mkfs.erofs $name"; }
  check "$name.erofs" "$W/$name.erofs"
  cp "$W/$name.erofs" "$W/$name.img"
  local props=()
  while IFS= read -r p; do props+=(--prop "$p"); done < <(py footer-props "$OTATOOLS/bin/avbtool" "$src")
  avbtool add_hashtree_footer --image "$W/$name.img" --partition_name "$name" \
    --partition_size "$psize" --hash_algorithm sha256 --salt "$SALT" --block_size 4096 \
    --fec_num_roots 2 "${props[@]}"
  check "$name.img" "$W/$name.img"
}
SALT=b14f17c0007cfc878a5cdac13f0514ac3b7d22996d4db3d254082387c6705124

log "step 6: vendor (software KeyMint/Gatekeeper, milestone L)"
rebuild_erofs vendor /vendor "$VENDOR_PART_SIZE" edit-vendor "$KEYMINT" "$GATEKEEPER"

log "step 7: system_ext (framework manifest, milestone L)"
rebuild_erofs system_ext /system_ext "$SYSEXT_PART_SIZE" edit-system-ext

log "step 8: re-sign vbmeta_a (vendor, then system_ext descriptor)"
avbtool update_partition_descriptor --image "$W/boot/vbmeta_a.orig.img" \
  --partition_image "$W/vendor.img" --output "$W/vbmeta.step1.img" \
  --algorithm SHA256_RSA4096 --key "$KEY" --rollback_index "$VBMETA_ROLLBACK_INDEX"
avbtool update_partition_descriptor --image "$W/vbmeta.step1.img" \
  --partition_image "$W/system_ext.img" --output "$W/vbmeta.new.img" \
  --algorithm SHA256_RSA4096 --key "$KEY" --rollback_index "$VBMETA_ROLLBACK_INDEX"
read -r NEW_DIGEST NEW_VBSIZE < <(py vbmeta-digest "$W/vbmeta.new.img")
[ "${NEW_VBSIZE:-}" = "${VBMETA_STRUCT_SIZE:-6720}" ] || die "unexpected vbmeta size $NEW_VBSIZE"
if [ $REFKEY = 1 ]; then
  EXPECT[vbmeta.new.img]=${EXPECT_REFKEY[vbmeta.new.img]}; check vbmeta.new.img "$W/vbmeta.new.img"
fi
log "  vbmeta digest $NEW_DIGEST"

log "step 9: super (lpmake, same LP metadata as the stock super)"
py lp-build "$W/lp/lp.json" "$W/lp" "$W/super.img" \
  "vendor_a=$W/vendor.img" "system_ext_a=$W/system_ext.img" > "$W/lpmake.log" 2>&1 \
  || { tail -20 "$W/lpmake.log"; die "lpmake"; }
check super.img "$W/super.img"
py splice "$IMG" "$SUPER_OFF" "$SUPER_SIZE" "$W/super.img" >/dev/null
py splice "$IMG" "$VBMETA_OFF" "$VBMETA_SIZE" "$W/vbmeta.new.img" >/dev/null

log "step 10: standalone initrd.img (new digest + software KeyMint selectors)"
py make-initrd "$W/initrd.img" "$W/boot/vendor_bootconfig.txt" \
  "$W/boot/vendor_platform.cpio" "$W/boot/vendor_dlkm.cpio" "$W/boot/init_boot.cpio" -- \
  androidboot.slot_suffix=_a androidboot.force_normal_boot=1 \
  "androidboot.boot_part_uuid=$BOOT_UUID" androidboot.vbmeta.hash_alg=sha256 \
  "androidboot.vbmeta.size=$NEW_VBSIZE" "androidboot.vbmeta.digest=$NEW_DIGEST" \
  androidboot.hypervisor.protected_vm.supported=0 \
  androidboot.vendor.apex.com.android.hardware.keymint=com.android.hardware.keymint.rust_nonsecure \
  androidboot.vendor.apex.com.android.hardware.gatekeeper=com.android.hardware.gatekeeper.nonsecure >/dev/null
if [ $REFKEY = 1 ]; then
  EXPECT[initrd.img]=${EXPECT_REFKEY[initrd.img]}; check initrd.img "$W/initrd.img"
fi

log "step 11: final hashes"
IMG_SHA=$(sha256sum "$IMG" | cut -d' ' -f1)
if [ $REFKEY = 1 ]; then
  EXPECT[kano-real.img]=${EXPECT_REFKEY[kano-real.img]}
  [ "$IMG_SHA" = "${EXPECT[kano-real.img]}" ] && log "  ok   kano-real.img $IMG_SHA (matches the Lapis $LAPIS_BUILD reference)" \
    || warn "kano-real.img $IMG_SHA differs from the reference ${EXPECT[kano-real.img]}"
fi
INITRD_SHA=$(sha256sum "$W/initrd.img" | cut -d' ' -f1)
# checksums and build-info first, then the final names, then BUILD-DONE: a finished-looking name always has its sums
( cd "$OUT" && printf '%s  %s\n' "$IMG_SHA" kano-real.img "$INITRD_SHA" initrd.img > SHA256SUMS )
{
  echo "INTERMEDIATE IMAGE: kano-real.img is only an input for builder/build.sh. Never write it to a disk or boot it:"
  echo "its ESP entry still has the stock options (spi-intel.writeable=1, no EC/TPM/SPI blocks, editor on) and its"
  echo "vendor still ships fwupd, flashrom, futility, ectool and ap-ec-fw.zip."
  echo
  echo "build-base.sh run $(date -Iseconds)"
  echo "Lapis build    $LAPIS_BUILD"
  [ -z "$ZIP" ] || echo "zip            recovery.zip  sha256 $ZIP_GOT"
  echo "recovery .bin  sha256 $BIN_GOT"
  echo "otatools       pinned ($AVB_V, mkfs.erofs $EROFS_V)"
  echo "keymint apex   sha256 $KEYMINT_SHA"
  echo "gatekeeper apex sha256 $GATEKEEPER_SHA"
  echo "systemd-boot   sha256 $SDBOOT_GOT"
  echo "mkfs.fat $MKFSFAT_V, mtools $MTOOLS_V"
  echo "AVB key        public key sha1 $PUBKEY_SHA1, reference key: $REFKEY"
  echo "vbmeta_a       digest $NEW_DIGEST size $NEW_VBSIZE"
  echo "boot_a GUID    $BOOT_UUID"
  echo "kano-real.img  sha256 $IMG_SHA"
  echo "initrd.img     sha256 $INITRD_SHA"
  echo "warnings       ${#WARNINGS[@]}"
  for w in "${WARNINGS[@]}"; do echo "  - $w"; done
  echo
  echo "NOTE: the ESP /initrd.img inside kano-real.img still carries the stock"
  echo "vbmeta digest $ORIG_DIGEST (milestone E); it does not match vbmeta_a."
  echo "Booting from the ESP needs the standalone initrd.img (or kano-safe2's fix)."
} > "$OUT/build-info.txt"
mv "$W/initrd.img" "$INITRD_FINAL"
mv "$IMG" "$IMG_FINAL"
if [ "$KEEP_WORK" = 0 ]; then
  chmod -R u+rwX "$W" && rm -rf "$W"
fi
touch "$OUT/BUILD-DONE"
log "done: $IMG_FINAL"
log "      $INITRD_FINAL"
[ ${#WARNINGS[@]} -eq 0 ] || log "${#WARNINGS[@]} warning(s), see $OUT/build-info.txt"
