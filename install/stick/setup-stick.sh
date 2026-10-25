#!/usr/bin/env bash
# Turn the KANO maintenance stick into an installer stick. Run as root on the stick's maintenance Linux, after the
# stick's Googlebook partitions hold the image that <pristine dir> was made from (make-pristine.py):
#   setup-stick.sh <pristine dir>
# 1. checks the pristine data with gbos-install's own verifier (manifest, partition table, every partition file) and the
#    stick's super against the manifest;
# 2. installs gbos-install and its service, copies the pristine data to /var/lib/gbos-install/pristine (checked again);
# 3. writes the XBOOTLDR entry "Install Googlebook to the internal disk" from gbos-install.entry.template, after checking
#    that the entry blocks every module the image's own entries block and that every blocked module built into the
#    maintenance kernel (module_blacklist cannot stop those) is covered by builtin-initcalls.txt, whose initcalls go into
#    the entry's initcall_blacklist; the mapping and the entry's whole module_blacklist (module-blacklist.txt) are
#    installed for gbos-install's runtime check of the kernel command line;
# 3b. installs gbos-update.py (next to this script or in install/) and writes the entry "Update Googlebook on the
#    internal disk (keep data)": the installer entry with gbos.update=1 added (gbos-install runs gbos-update.py then);
# 4. the entries for the internal disk (gbos-nvme*.conf) are rewritten from this image's entries only when the stick's
#    copy of the internal disk's kernel and initrd (/gbos-nvme, written by gbos-install) is this image's (the initrd
#    with its own boot_a GUID); otherwise they stay as they are. The stick's menu starts the internal disk by default
#    only when gbos-nvme.conf exists; otherwise the default stays. loader.conf always gets editor no and auto-firmware no.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PR=${1:?usage: setup-stick.sh <pristine dir>}
M="$PR/manifest.txt"
TPL="$HERE/gbos-install.entry.template"
MAP="$HERE/builtin-initcalls.txt"
MAPDST=/usr/local/share/gbos-install/builtin-initcalls.txt
MODDST=/usr/local/share/gbos-install/module-blacklist.txt
MNT=/run/gbos-setup
export MTOOLS_SKIP_CHECK=1 LC_ALL=C
W=""
fail() { echo "setup-stick: $*" >&2; exit 1; }
cleanup() {
    local m
    for m in "$MNT/xb" "$MNT/esp"; do
        if mountpoint -q "$m" 2>/dev/null; then umount "$m" || true; fi
        rmdir "$m" 2>/dev/null || true
    done
    if [ -n "$W" ] && [ -d "$W" ]; then rm -rf "$W"; fi
}
trap 'echo "setup-stick: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
trap cleanup EXIT
pdev() { case "$1" in *[0-9]) echo "${1}p$2" ;; *) echo "$1$2" ;; esac; }
blacklist() { { grep '^options ' "$1" || true; } | tr ' ' '\n' | sed -n 's/^module_blacklist=//p' | tr ',' '\n' | tr '-' '_' | sort -u; }
# esp_entries <FAT image>: the names in its loader/entries, sorted, space-separated (empty on any error); same as gbos-install
esp_entries() { { mdir -b -i "$1" ::/loader/entries || true; } | tr -d '\r' | sed -n 's|^::/loader/entries/\([^/]*\)$|\1|p' |
    LC_ALL=C sort | tr '\n' ' '; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 2; }
SRC=/dev/$(lsblk -no PKNAME "$(findmnt -nvo SOURCE /)")
[ "$(lsblk -dno TRAN "$SRC" | tr -d ' ')" = usb ] || { echo "not running from a USB stick"; exit 2; }
[ "$(cat "/sys/block/$(basename "$SRC")/queue/logical_block_size")" = 512 ] || fail "the stick does not use 512-byte sectors"
[ -f "$M" ] && [ -f "$TPL" ] && [ -f "$MAP" ] || fail "need $M, $TPL and $MAP"
ENG=""
for c in "$HERE/gbos-update.py" "$HERE/../gbos-update.py"; do if [ -f "$c" ]; then ENG=$c; break; fi; done
[ -n "$ENG" ] || fail "need gbos-update.py (install/gbos-update.py) next to setup-stick.sh or in its parent directory"
ENGDST=/usr/local/share/gbos-install/gbos-update.py
for c in lz4 mdir mmd mdel minfo; do command -v "$c" > /dev/null || fail "the maintenance system needs $c for updates (pacman -S lz4 mtools)"; done
W=$(mktemp -d)
# gbos-install's own verifier (the Python block it writes to $W/gbos.py), so both check the data the same way
awk '/^cat > "\$W\/gbos\.py" << .PY.$/ {f = 1; next} /^PY$/ {f = 0} f' "$HERE/gbos-install.sh" > "$W/gbos.py"
grep -q '^def verify_data' "$W/gbos.py" || fail "cannot find the verifier in gbos-install.sh"

echo "== checking the pristine data against its manifest and this stick"
python3 "$W/gbos.py" verify-data "$PR" "$W/head.img" || fail "the pristine data is damaged"
read -r _ SN _ SSTART SLEN _ _ SSHA _ <<< "$(awk '$1 == "part" && $3 == "super"' "$M")"
SP=$(basename "$(pdev "$SRC" "$SN")")
[ "$(cat "/sys/class/block/$SP/start")" = $((SSTART / 512)) ] && [ "$(cat "/sys/class/block/$SP/size")" = $((SLEN / 512)) ] \
    || fail "stick layout differs from the manifest"
echo "   reading the stick's super ($((SLEN / 1000000000)) GB), several minutes"
[ "$(dd if="$(pdev "$SRC" "$SN")" bs=4M status=progress | sha256sum | cut -c1-64)" = "$SSHA" ] \
    || fail "the stick's super is not the manifest's image; write that image first"

echo "== checking the boot entry against the image and the maintenance kernel"
XN=$(sgdisk -p "$SRC" | awk '$NF == "XBOOTLDR" {print $1}'); EN=$(sgdisk -p "$SRC" | awk '$NF == "EFI-SYSTEM" {print $1}')
[[ "$XN" =~ ^[0-9]+$ ]] && [[ "$EN" =~ ^[0-9]+$ ]] || fail "stick has no single XBOOTLDR and EFI-SYSTEM partition"
ROOTUUID=$(blkid -s PARTUUID -o value "$(findmnt -nvo SOURCE /)")
[[ "$ROOTUUID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || fail "root PARTUUID: $ROOTUUID"
mkdir -p "$MNT/xb" "$MNT/esp"
mount "$(pdev "$SRC" "$XN")" "$MNT/xb"
[ -f "$MNT/xb/vmlinuz-linux" ] && [ -f "$MNT/xb/initramfs-linux.img" ] || fail "no maintenance kernel in XBOOTLDR"
KVER=""   # Arch keeps a copy of each kernel as /usr/lib/modules/<version>/vmlinuz; else read the bzImage header
for d in /usr/lib/modules/*/; do
    if [ -f "${d}vmlinuz" ] && cmp -s "${d}vmlinuz" "$MNT/xb/vmlinuz-linux"; then KVER=$(basename "$d"); fi
done
if [ -z "$KVER" ]; then
    KVER=$(python3 - "$MNT/xb/vmlinuz-linux" << 'PY'
import struct, sys
d = open(sys.argv[1], "rb").read(1 << 16)
if d[0x202:0x206] == b"HdrS":
    o = struct.unpack_from("<H", d, 0x20E)[0] + 0x200
    print(d[o:d.index(b"\0", o)].split(b" ")[0].decode())
PY
)
fi
[ -n "$KVER" ] && [ -f "/usr/lib/modules/$KVER/modules.builtin" ] || fail "no /usr/lib/modules/<version>/modules.builtin for the XBOOTLDR kernel (${KVER:-version unknown})"
blacklist "$TPL" > "$W/bl"
[ -s "$W/bl" ] || fail "no module_blacklist in $TPL"
# built-in blocked modules must be covered by builtin-initcalls.txt: "<module> <initcall>" or "<module> - <initcalls>"
awk '{sub(/#.*/, "")} NF' "$MAP" > "$W/map"
awk 'NF == 2 && $2 ~ /^[a-z0-9_]+$/ {next} NF == 3 && $2 == "-" && $3 ~ /^[a-z0-9_]+(,[a-z0-9_]+)*$/ {next}
     {print "bad line: " $0; bad = 1} END {exit bad}' "$W/map" || fail "$MAP has a bad line"
INITCALLS=$(awk '$2 != "-" {print $2}' "$W/map" | paste -sd, -)
[ -n "$INITCALLS" ] || fail "no initcalls in $MAP"
while read -r mod _ need; do
    for i in ${need//,/ }; do
        case ",$INITCALLS," in *",$i,"*) ;; *) fail "$MAP: $mod is accepted only with $i blocked, which it does not list" ;; esac
    done
done < <(awk '$2 == "-"' "$W/map")
awk '{print $1}' "$W/map" | tr '-' '_' | sort -u > "$W/covered"
MNB=$(comm -23 "$W/covered" "$W/bl")
[ -z "$MNB" ] || fail "$MAP lists modules missing from the entry's module_blacklist: $(echo "$MNB" | tr '\n' ' ')"
sed 's|.*/||; s|\.ko$||; s|-|_|g' "/usr/lib/modules/$KVER/modules.builtin" | sort -u | comm -12 "$W/bl" - > "$W/built"
UNCOVERED=$(comm -23 "$W/built" "$W/covered")
[ -z "$UNCOVERED" ] || fail "kernel $KVER has these built in and $MAP does not block them: $(echo "$UNCOVERED" | tr '\n' ' ')"
EEN=$(awk '$1 == "part" && $3 == "EFI-SYSTEM" {print $2}' "$M")
zstd -q -dc "$PR/p$EEN.img.zst" > "$W/esp.img"
ENTRIES=$(esp_entries "$W/esp.img")
case "$ENTRIES" in
    "lapis-debug.conf lapis-quiet.conf " | "lapis-debug.conf lapis-quiet.conf lapis.conf ") ;;
    *) fail "the image's ESP has unexpected boot entries: ${ENTRIES:-none readable}" ;;
esac
for f in $ENTRIES; do
    mtype -i "$W/esp.img" "::/loader/entries/$f" > "$W/$f.orig"
    blacklist "$W/$f.orig" > "$W/$f.bl"
    [ -s "$W/$f.bl" ] || fail "the image's entry $f has no module_blacklist"
    MISS=$(comm -23 "$W/$f.bl" "$W/bl")
    [ -z "$MISS" ] || fail "the image's $f blocks modules that $TPL does not: $(echo "$MISS" | tr '\n' ' ')"
done
if [ "$(uname -r)" = "$KVER" ]; then
    [ -e /sys/kernel/security/lockdown ] || fail "kernel $KVER has no lockdown support; gbos-install would refuse to run"
else
    echo "   note: running kernel $(uname -r) is not the XBOOTLDR kernel $KVER; lockdown support not checked here (gbos-install checks it)"
fi
sed -e "s/@ROOT_PARTUUID@/$ROOTUUID/" -e "s/@INITCALL_BLACKLIST@/$INITCALLS/" "$TPL" > "$W/gbos-install.conf"
if grep -q '@[A-Z_]*@' "$W/gbos-install.conf"; then fail "template not rendered"; fi
sed -e 's/^title .*/title Update Googlebook on the internal disk (keep data)/' -e '/^options /s/ gbos\.install=1$/ gbos.install=1 gbos.update=1/' \
    "$W/gbos-install.conf" > "$W/gbos-update.conf"
[ "$(grep -c ' gbos\.install=1 gbos\.update=1$' "$W/gbos-update.conf")" = 1 ] || fail "cannot derive the update entry"
[ "$(grep '^options ' "$W/gbos-install.conf" | wc -c)" -lt 1900 ] || fail "kernel command line too long"
echo "   kernel $KVER: built in and blocked by initcall_blacklist ($INITCALLS): $(tr '\n' ' ' < "$W/built")"
echo "   the entry blocks everything the image's entries block"
{ echo "# module_blacklist of the installer entry (from gbos-install.entry.template, by setup-stick.sh): gbos-install"
  echo "# refuses to run when the kernel command line does not block every one of these"
  cat "$W/bl"; } > "$W/module-blacklist.txt"

echo "== installing the installer"
install -D -m 0755 "$HERE/gbos-install.sh" /usr/local/sbin/gbos-install
install -D -m 0644 "$HERE/gbos-installer.service" /etc/systemd/system/gbos-installer.service
install -D -m 0644 "$MAP" "$MAPDST"
install -D -m 0644 "$W/module-blacklist.txt" "$MODDST"
install -D -m 0644 "$ENG" "$ENGDST"
cmp -s "$ENG" "$ENGDST" || fail "installed gbos-update.py differs"
cmp -s "$HERE/gbos-install.sh" /usr/local/sbin/gbos-install && cmp -s "$HERE/gbos-installer.service" /etc/systemd/system/gbos-installer.service \
    && cmp -s "$MAP" "$MAPDST" && cmp -s "$W/module-blacklist.txt" "$MODDST" || fail "installed files differ"
systemctl enable gbos-installer.service
rm -rf /var/lib/gbos-install/pristine.new; mkdir -p /var/lib/gbos-install
cp -a "$PR" /var/lib/gbos-install/pristine.new; sync
python3 "$W/gbos.py" verify-data /var/lib/gbos-install/pristine.new "$W/head2.img" || fail "the copied pristine data is damaged"
rm -rf /var/lib/gbos-install/pristine; mv /var/lib/gbos-install/pristine.new /var/lib/gbos-install/pristine

echo "== boot entries"
mkdir -p "$MNT/xb/loader/entries"
cp "$W/gbos-install.conf" "$MNT/xb/loader/entries/gbos-install.conf"
cp "$W/gbos-update.conf" "$MNT/xb/loader/entries/gbos-update.conf"
# the internal disk's kernel and initrd as gbos-install leaves them for this image: its kernel, its initrd with the
# boot_a GUID that the stick's copy of the internal disk's initrd carries
SAME=0
if [ -f "$MNT/xb/gbos-nvme/initrd.img" ] && [ -f "$MNT/xb/gbos-nvme/kernel.efi" ]; then
    mcopy -n -i "$W/esp.img" ::/kernel.efi "$W/kernel.efi"; mcopy -n -i "$W/esp.img" ::/initrd.img "$W/initrd.img"
    NG=$( { grep -ao 'androidboot\.boot_part_uuid=[0-9a-f-]\{36\}' "$MNT/xb/gbos-nvme/initrd.img" || true; } | tail -n 1 | cut -d= -f2)
    if [ -n "$NG" ] && python3 "$W/gbos.py" bootconfig-check "$MNT/xb/gbos-nvme/initrd.img" "$NG" \
            && python3 "$W/gbos.py" bootconfig-set "$W/initrd.img" "$NG" \
            && cmp -s "$W/initrd.img" "$MNT/xb/gbos-nvme/initrd.img" && cmp -s "$W/kernel.efi" "$MNT/xb/gbos-nvme/kernel.efi"; then
        SAME=1
    fi
fi
if [ "$SAME" = 1 ]; then
    # entries for the current internal installation, from the image's quiet and serial-debug entries
    for f in lapis-quiet.conf lapis-debug.conf; do
        python3 "$W/gbos.py" lift-entry "$W/$f.orig" "$W/$f" || fail "entry $f"
    done
    sed -e 's/^title .*/title Googlebook (internal disk)/' -e 's|^linux .*|linux /gbos-nvme/kernel.efi|' -e 's|^initrd .*|initrd /gbos-nvme/initrd.img|' "$W/lapis-quiet.conf" > "$MNT/xb/loader/entries/gbos-nvme.conf"
    sed -e 's/^title .*/title Googlebook (internal disk) serial-debug/' -e 's|^linux .*|linux /gbos-nvme/kernel.efi|' -e 's|^initrd .*|initrd /gbos-nvme/initrd.img|' "$W/lapis-debug.conf" > "$MNT/xb/loader/entries/gbos-nvme-debug.conf"
    echo "   entries for the internal disk rewritten from this image (its kernel and initrd are this image's)"
elif [ -f "$MNT/xb/gbos-nvme/initrd.img" ] || [ -f "$MNT/xb/loader/entries/gbos-nvme.conf" ]; then
    echo "   note: the internal disk holds another image (or the stick's copy of its kernel/initrd is incomplete);"
    echo "         its entries are left as they are. Run the installer to put this image on the internal disk."
fi
HAVE_NVME=0; if [ -f "$MNT/xb/loader/entries/gbos-nvme.conf" ]; then HAVE_NVME=1; fi
sync; ls "$MNT/xb/loader/entries"; umount "$MNT/xb"
X=$(pdev "$SRC" "$XN"); blockdev --flushbufs "$X"
mtype -i "$X" ::/loader/entries/gbos-install.conf > "$W/rb.entry"
cmp -s "$W/gbos-install.conf" "$W/rb.entry" || fail "XBOOTLDR entry read-back differs"
mtype -i "$X" ::/loader/entries/gbos-update.conf > "$W/rb.uentry"
cmp -s "$W/gbos-update.conf" "$W/rb.uentry" || fail "XBOOTLDR update entry read-back differs"
mount "$(pdev "$SRC" "$EN")" "$MNT/esp"
CUR=$( { sed -n 's/^default[[:space:]]\{1,\}//p' "$MNT/esp/loader/loader.conf" 2>/dev/null || true; } | tail -n 1 | tr -d '\r')
if [ "$HAVE_NVME" = 1 ]; then DEF=gbos-nvme.conf; else DEF=$CUR; fi
{ if [ -n "$DEF" ]; then echo "default $DEF"; fi; printf 'timeout 5\neditor no\nauto-firmware no\n'; } > "$W/loader.conf"
mkdir -p "$MNT/esp/loader"; cp "$W/loader.conf" "$MNT/esp/loader/loader.conf"
sync; umount "$MNT/esp"
S=$(pdev "$SRC" "$EN"); blockdev --flushbufs "$S"
mtype -i "$S" ::/loader/loader.conf > "$W/rb.loader"
cmp -s "$W/loader.conf" "$W/rb.loader" || fail "stick loader.conf read-back differs"
if [ "$HAVE_NVME" = 1 ]; then
    echo "SETUP OK: menu default = internal disk; 'Install Googlebook to the internal disk' starts gbos-install"
else
    echo "SETUP OK: 'Install Googlebook to the internal disk' starts gbos-install; menu default unchanged (${DEF:-none}) until an install"
fi
