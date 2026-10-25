#!/usr/bin/env bash
# Read-only check of a Chromebook before it gets UEFI firmware for Googlebook Anywhere (Acer Chromebook Spin 714, KANO).
# Run it on the Chromebook in a ChromeOS shell (developer mode, VT2 as chronos) or in any Linux booted on it:
#   curl -LO <repo>/install/kano-check.sh && bash kano-check.sh
# It only reads (crossystem, /proc, /sys, and `gsctool -a -w` when sudo works without a password); it never writes
# firmware, settings or files. The result is one of:
#   GO       (exit 0) no known blocker was found. Not a guarantee: the official MrChromebox script checks the device
#            again and is what you follow (docs/FIRMWARE.md).
#   STOP     (exit 1) a known blocker: do not flash.
#   UNKNOWN  (exit 2) something this check relies on (CPU, firmware) could not be identified: do not flash on the
#            strength of this check; compare the machine by hand with the official MrChromebox documentation.
set -u
R=${KANO_CHECK_ROOT:-}          # tests point this at a fake /proc and /sys tree; empty on a real machine
stop=0; warn=0; unknown=0
say()  { printf '%-6s %s\n' "$1" "$2"; }
ok()   { say OK "$1"; }
wn()   { say WARN "$1"; warn=$((warn + 1)); }
no()   { say STOP "$1"; stop=$((stop + 1)); }
unk()  { say "???" "$1"; unknown=$((unknown + 1)); }
rd()   { cat "$R$1" 2>/dev/null; }
have() { command -v "$1" >/dev/null 2>&1; }
cs()   { have crossystem && crossystem "$1" 2>/dev/null; }

echo "Googlebook Anywhere: firmware readiness check (read-only)"
echo

# 1. model: KANO only (other Chromebooks need their own profile; see docs/NEW-DEVICE.md)
hwid=$(cs hwid); product=$(rd /sys/class/dmi/id/product_name); board=$(sed -n 's/^CHROMEOS_RELEASE_BOARD=//p' "$R/etc/lsb-release" 2>/dev/null)
model="hwid='${hwid:-?}' product='${product:-?}' board='${board:-?}'"
if printf '%s %s %s' "$hwid" "$product" "$board" | grep -qiw kano; then ok "model is KANO ($model)"
else no "this is not a KANO ($model): only a KANO board is supported"; fi

# 2. CPU: retail Alder Lake-P is family 6 model 154 stepping 3 or 4 (the steppings Intel publishes microcode for).
#    Steppings 0-2 are pre-production samples whose microcode is not in public firmware (this project's own KANO is
#    stepping 2 and runs a MrChromebox ROM with that microcode added by hand). Any other stepping, or a CPU this check
#    cannot read, is UNKNOWN.
fam=$(awk -F': ' '/^cpu family/ {print $2; exit}' "$R/proc/cpuinfo" 2>/dev/null)
mod=$(awk -F': ' '/^model\t/ || /^model[[:space:]]+:/ {print $2; exit}' "$R/proc/cpuinfo" 2>/dev/null)
step=$(awk -F': ' '/^stepping/ {print $2; exit}' "$R/proc/cpuinfo" 2>/dev/null)
name=$(awk -F': ' '/^model name/ {print $2; exit}' "$R/proc/cpuinfo" 2>/dev/null)
if [ "$fam" = 6 ] && [ "$mod" = 154 ]; then
    case "$step" in
        3|4) ok "CPU $name (06-9a-0$step, production stepping)" ;;
        0|1|2) no "CPU $name is stepping $step (06-9a-0$step): a pre-production engineering sample. Public firmware has no microcode for it; do NOT flash" ;;
        *) unk "CPU $name reports stepping '${step:-?}', which this check does not know (retail is 3 or 4)" ;;
    esac
else unk "CPU is family '${fam:-?}' model '${mod:-?}', not the Alder Lake-P (6/154) of the KANO, or it could not be read"; fi
case "$hwid" in *TEST*|*test*) no "HWID '$hwid' marks an engineering/test unit: do NOT flash" ;; esac

# 3. current firmware
vendor=$(rd /sys/class/dmi/id/bios_vendor); bver=$(rd /sys/class/dmi/id/bios_version); fwid=$(cs fwid)
if printf '%s' "$bver" | grep -qi mrchromebox; then ok "already on MrChromebox UEFI firmware ($bver): nothing to flash"
elif [ -n "$fwid" ]; then ok "stock ChromeOS firmware $fwid"
else unk "firmware not identified (vendor '${vendor:-?}', version '${bver:-?}'; no crossystem fwid)"; fi

# 4. developer mode and write protection (only matter while still on stock firmware)
if [ -n "$fwid" ]; then
    [ "$(cs devsw_boot)" = 1 ] && ok "developer mode is on" || no "developer mode is off (step 1 of docs/FIRMWARE.md)"
    wp=$(cs wpsw_cur)
    if [ "$wp" = 0 ]; then ok "hardware write protection is off"
    else wn "hardware write protection is on (wpsw_cur=${wp:-?}): disable it with a SuzyQ cable (docs/FIRMWARE.md step 3)"; fi
    if have gsctool && sudo -n true 2>/dev/null; then
        gw=$(sudo -n gsctool -a -w 2>/dev/null | tr '\n' ' ')
        [ -n "$gw" ] && say INFO "gsctool: $gw"
    fi
fi

# 5. power: never flash on a low battery or without the charger
cap=$(rd /sys/class/power_supply/BAT0/capacity)
ac=0; for f in "$R"/sys/class/power_supply/*/online; do [ -f "$f" ] && [ "$(cat "$f")" = 1 ] && ac=1; done
if [ -n "$cap" ] && [ "$cap" -ge 50 ]; then ok "battery $cap %"; else wn "battery ${cap:-?} %: charge to 50 % or more first"; fi
[ "$ac" = 1 ] && ok "charger connected" || wn "charger not detected: keep it plugged in while flashing"

echo
if [ "$stop" -gt 0 ]; then echo "RESULT: STOP ($stop blocking problem(s)). Do not flash this machine."; exit 1; fi
if [ "$unknown" -gt 0 ]; then
    echo "RESULT: UNKNOWN ($unknown item(s) marked ??? could not be identified). Do not flash on the strength of this check;"
    echo "        compare the machine by hand with the official MrChromebox documentation first."
    exit 2
fi
echo "RESULT: GO with $warn warning(s): no known blocker found. This is not a guarantee: follow docs/FIRMWARE.md and the"
echo "        official MrChromebox script (it checks the device again), and back up the stock firmware to USB in it."
exit 0
