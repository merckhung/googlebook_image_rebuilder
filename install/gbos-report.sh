#!/usr/bin/env bash
# Read-only report from a running Googlebook (adb) and/or a gbos-probe directory (docs/NEW-DEVICE.md).
# The device sees only the fixed allowlist below. Nothing is installed, no setting is changed, and no file is pushed.
# The report is one fixed list of fields. A value is an integer, a fixed hex id, an enumeration,
# or a version string this project ships. Free text is not written. Anything else is "unknown".
# Raw logs are not written. If a raw log is needed, ask the person for it directly.
# Set GBOS_DEV_KERNEL_DIR to a kernel tree. When uname's release equals that tree's
# include/config/kernel.release, the report says dev-kernel. When .config is a regular
# file, config pass means its sha256 equals the pinned GKI config; otherwise config fail.
# usage: install/gbos-report.sh [--adb] [--serial SERIAL] [--probe DIR] [--fixture DIR] [--out DIR] [--date YYYYMMDD]
#        install/gbos-report.sh --list
#        install/gbos-report.sh --classify COMMAND
#   --out DIR         new or empty directory (default ./gbos-report); a trailing slash is not followed
#   --probe DIR       gbos-probe output (probe.json). Without --adb this does not talk to a device.
#   --fixture DIR     saved allowlist output, one <id>.txt per command (tests). Does not talk to adb.
#   --serial SERIAL   passed to adb -s. Implies --adb.
#   --date YYYYMMDD   report date (default UTC today)
#   --list            print the allowlist, one command per line
#   --classify CMD    print "allow" or "refused" (refused exits 2)
#
set -euo pipefail

die() { printf 'gbos-report: %s\n' "$*" >&2; exit 2; }

RAW=
OUT=

cleanup() {
    status=$?
    if [ -n "$RAW" ]; then
        rm -rf "$RAW"
    fi
    exit "$status"
}
trap cleanup EXIT

IDS=()
CMDS=()
while IFS='|' read -r id cmd; do
    [ -n "$id" ] || continue
    case "$id" in \#*) continue ;; esac
    IDS+=("$id")
    CMDS+=("$cmd")
done << 'EOF'
getprop|getprop
dumpsys-input|dumpsys input
dumpsys-display|dumpsys display
dumpsys-audio|dumpsys audio
dumpsys-power|dumpsys power
dumpsys-battery|dumpsys battery
dumpsys-wifi|dumpsys wifi
dumpsys-bluetooth|dumpsys bluetooth_manager
ls-dev-dri|ls /dev/dri
ls-bluetooth|ls /sys/class/bluetooth
uname-a|uname -a
logcat|logcat -d -b crash,main -s AndroidRuntime:E DEBUG:E ActivityManager:E SurfaceFlinger:E AudioFlinger:E
cat-proc-cpuinfo|cat /proc/cpuinfo
cat-proc-version|cat /proc/version
cat-proc-asound-cards|cat /proc/asound/cards
cat-proc-bus-input-devices|cat /proc/bus/input/devices
cat-dmi-sys-vendor|cat /sys/class/dmi/id/sys_vendor
cat-dmi-product-name|cat /sys/class/dmi/id/product_name
cat-dmi-product-version|cat /sys/class/dmi/id/product_version
cat-dmi-board-vendor|cat /sys/class/dmi/id/board_vendor
cat-dmi-board-name|cat /sys/class/dmi/id/board_name
cat-dmi-bios-vendor|cat /sys/class/dmi/id/bios_vendor
cat-dmi-bios-version|cat /sys/class/dmi/id/bios_version
cat-dmi-bios-date|cat /sys/class/dmi/id/bios_date
cat-drm-vendor|cat /sys/class/drm/card0/device/vendor
cat-drm-device|cat /sys/class/drm/card0/device/device
cat-drm0-uevent|cat /sys/class/drm/card0/device/uevent
cat-drm1-vendor|cat /sys/class/drm/card1/device/vendor
cat-drm1-device|cat /sys/class/drm/card1/device/device
cat-drm1-uevent|cat /sys/class/drm/card1/device/uevent
cat-drm2-vendor|cat /sys/class/drm/card2/device/vendor
cat-drm2-device|cat /sys/class/drm/card2/device/device
cat-drm2-uevent|cat /sys/class/drm/card2/device/uevent
cat-drm3-vendor|cat /sys/class/drm/card3/device/vendor
cat-drm3-device|cat /sys/class/drm/card3/device/device
cat-drm3-uevent|cat /sys/class/drm/card3/device/uevent
cat-suspend-success|cat /sys/power/suspend_stats/success
cat-suspend-fail|cat /sys/power/suspend_stats/fail
cat-proc-bus-pci-devices|cat /proc/bus/pci/devices
cat-wlan-operstate|cat /sys/class/net/wlan0/operstate
cat-wlan-uevent|cat /sys/class/net/wlan0/device/uevent
cat-bt-uevent|cat /sys/class/bluetooth/hci0/device/uevent
EOF

check_allowlist() {
    local i c id path meta_bt meta_bs seen_id seen_cmd nl
    [ "${#IDS[@]}" -eq "${#CMDS[@]}" ] || die "internal: allowlist length"
    [ "${#CMDS[@]}" -gt 0 ] || die "internal: empty allowlist"
    # a raw backtick in a case pattern would start command substitution, so build the characters
    meta_bt=$(printf '\140')
    meta_bs=$(printf '\\')
    nl=$(printf '\n')
    seen_id="$nl"
    seen_cmd="$nl"
    i=0
    while [ "$i" -lt "${#CMDS[@]}" ]; do
        id=${IDS[$i]}
        c=${CMDS[$i]}
        case "$id" in
            ''|*[!A-Za-z0-9-]*) die "internal: bad command id" ;;
        esac
        case "$seen_id" in
            *"$nl$id$nl"*) die "internal: duplicate id" ;;
        esac
        case "$seen_cmd" in
            *"$nl$c$nl"*) die "internal: duplicate command" ;;
        esac
        seen_id="${seen_id}${id}${nl}"
        seen_cmd="${seen_cmd}${c}${nl}"
        case "$c" in
            *';'*|*'|'*|*'&'*|*'<'*|*'>'*|*'$'*|*"${meta_bt}"*|*"${meta_bs}"*|*$'\n'*|*$'\r'*)
                die "internal: metacharacter in allowlist" ;;
        esac
        case "$c" in
            getprop|uname\ -a|ls\ /dev/dri|ls\ /sys/class/bluetooth) ;;
            dumpsys\ input|dumpsys\ display|dumpsys\ audio|dumpsys\ power|dumpsys\ battery|dumpsys\ wifi|dumpsys\ bluetooth_manager) ;;
            logcat\ -d\ -b\ crash,main\ -s\ *:*)
                case "$c" in
                    *'*:'*) die "internal: logcat must name tags, not a wildcard" ;;
                esac
                ;;
            cat\ /proc/*|cat\ /sys/*)
                path=${c#cat }
                case "$path" in
                    *[[:space:]]*|*'..'*) die "internal: cat must be exactly one /proc or /sys file" ;;
                    /proc/*|/sys/*) ;;
                    *) die "internal: cat path is not /proc or /sys" ;;
                esac
                case "$path" in
                    /proc/net*|/proc/cmdline|/proc/kallsyms|/proc/kmsg|/proc/modules) die "internal: cat path is not allowed" ;;
                    *address*|*serial*|*uuid*|*wpa*|*pstore*|*WifiConfig*) die "internal: cat path is not allowed" ;;
                esac
                ;;
            *) die "internal: command is outside the read-only shapes" ;;
        esac
        i=$((i + 1))
    done
}
check_allowlist

allow() {
    local c
    for c in "${CMDS[@]}"; do
        [ "$c" = "$1" ] && return 0
    done
    return 1
}

# Per-command watchdog. macOS has no timeout(1); bash 3.2 has no wait -n. Kill only this adb pid.
run_allow_cmd() {
    local _out=$1 _err=$2 _rcf=$3 _pid _waited _limit _rc
    shift 3
    "$@" < /dev/null > "$_out" 2> "$_err" &
    _pid=$!
    _waited=0
    _limit=${GBOS_ADB_TIMEOUT:-20}
    case "$_limit" in
        ''|*[!0-9]*) _limit=20 ;;
    esac
    # Five checks a second, so a command that has already exited is not held for a full second.
    _limit_ticks=$((_limit * 5))
    while kill -0 "$_pid" 2>/dev/null; do
        if [ "$_waited" -ge "$_limit_ticks" ]; then
            kill "$_pid" 2>/dev/null || true
            _grace=0
            while kill -0 "$_pid" 2>/dev/null; do
                if [ "$_grace" -ge 5 ]; then
                    kill -9 "$_pid" 2>/dev/null || true
                    break
                fi
                sleep 0.2
                _grace=$((_grace + 1))
            done
            wait "$_pid" 2>/dev/null || true
            printf 'gbos-report: command timed out\n' >> "$_err"
            printf 'gbos-report: command timed out\n' >&2
            printf '124\n' > "$_rcf"
            return 0
        fi
        sleep 0.2
        _waited=$((_waited + 1))
    done
    _rc=0
    wait "$_pid" || _rc=$?
    printf '%s\n' "$_rc" > "$_rcf"
}

MODE=run
CLASSIFY=
SERIAL=
PROBE=
FIXTURE=
OUT=./gbos-report
DATE=
WANT_ADB=0
OUT_SET=0
PROBE_SET=0
FIXTURE_SET=0
SOURCE=

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        --list) MODE=list; shift ;;
        --classify)
            [ $# -ge 2 ] || die "--classify needs the command to judge"
            MODE=classify
            CLASSIFY=$2
            shift 2
            ;;
        --serial)
            [ $# -ge 2 ] || die "--serial needs a device serial"
            SERIAL=$2
            [ -n "$SERIAL" ] || die "--serial needs a device serial"
            WANT_ADB=1
            shift 2
            ;;
        --probe)
            [ $# -ge 2 ] || die "--probe needs a directory"
            PROBE=$2
            PROBE_SET=1
            shift 2
            ;;
        --fixture)
            [ $# -ge 2 ] || die "--fixture needs a directory"
            FIXTURE=$2
            FIXTURE_SET=1
            shift 2
            ;;
        --adb) WANT_ADB=1; shift ;;
        --out)
            [ $# -ge 2 ] || die "--out needs a directory"
            OUT=$2
            OUT_SET=1
            shift 2
            ;;
        --date)
            [ $# -ge 2 ] || die "--date needs YYYYMMDD"
            DATE=$2
            shift 2
            ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
done

if [ "$MODE" != run ]; then
    [ -z "$SERIAL$PROBE$FIXTURE$DATE" ] && [ "$WANT_ADB" = 0 ] && [ "$OUT_SET" = 0 ] \
        || die "--$MODE does not take report options"
fi

if [ "$MODE" = list ]; then
    printf '%s\n' "${CMDS[@]}"
    exit 0
fi
if [ "$MODE" = classify ]; then
    if allow "$CLASSIFY"; then
        printf 'allow\n'
        exit 0
    fi
    printf 'refused\n'
    exit 2
fi

command -v python3 >/dev/null || die "python3 is needed"

case "$OUT$PROBE$FIXTURE" in
    *$'\n'*|*$'\r'*) die "paths must not contain a newline" ;;
esac
if [ -n "$SERIAL" ]; then
    [ "${#SERIAL}" -le 80 ] || die "serial is too long"
    case "$SERIAL" in
        -*) die "serial must not start with -" ;;
        *[!A-Za-z0-9:._-]*) die "serial has characters this script will not pass to adb" ;;
    esac
fi
if [ "$FIXTURE_SET" = 1 ] && { [ -n "$SERIAL" ] || [ "$WANT_ADB" = 1 ]; }; then
    die "--fixture does not use adb; remove --serial or --adb"
fi
if [ -n "$DATE" ]; then
    case "$DATE" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
        *) die "--date must be YYYYMMDD" ;;
    esac
else
    DATE=$(date -u +%Y%m%d)
fi
# A trailing slash makes -L follow the link on Linux. Strip it, but keep the root.
strip_slashes() {
    _s=$1
    while :; do
        case "$_s" in
            /) break ;;
            */) _s=${_s%/} ;;
            *) break ;;
        esac
    done
    printf '%s\n' "$_s"
}
# "." and ".." components (link/., link/./new, link/../x) follow a symlink that -L misses.
# A single leading ./ is the default --out and is kept. Later components are not followed.
# A system alias may be followed: the link and its parent are both root-owned and not
# group- or other-writable. Linux reports every symlink as mode 777; that is not writable.
# A user-owned symlink is refused.
path_has_dotdot() {
    _p=$1
    case "$_p" in
        ./*) _p=${_p#./} ;;
    esac
    case "$_p" in
        .|..|*/.|*/..|*/./*|*/../*|./*|../*) return 0 ;;
    esac
    return 1
}
if [ "$FIXTURE_SET" = 1 ]; then
    FIXTURE=$(strip_slashes "$FIXTURE")
    [ -n "$FIXTURE" ] || die "--fixture needs a directory"
    [ "$FIXTURE" != / ] || die "--fixture must not be /"
    path_has_dotdot "$FIXTURE" && die "--fixture path is not allowed"
fi
if [ "$PROBE_SET" = 1 ]; then
    PROBE=$(strip_slashes "$PROBE")
    [ -n "$PROBE" ] || die "--probe needs a directory"
    [ "$PROBE" != / ] || die "--probe must not be /"
    path_has_dotdot "$PROBE" && die "--probe path is not allowed"
fi
OUT=$(strip_slashes "$OUT")
[ -n "$OUT" ] || die "--out needs a directory"
[ "$OUT" != / ] || die "--out must not be /"
path_has_dotdot "$OUT" && die "--out path is not allowed"
# An explicit offline source that is empty or invalid must not fall through to adb.
if [ "$FIXTURE_SET" = 1 ]; then
    [ -d "$FIXTURE" ] && [ ! -L "$FIXTURE" ] || die "--fixture must be a directory, not a symlink"
    SOURCE=fixture
elif [ "$WANT_ADB" = 1 ] || [ "$PROBE_SET" = 0 ]; then
    WANT_ADB=1
    SOURCE=adb
else
    SOURCE=
fi
if [ "$PROBE_SET" = 1 ]; then
    [ -d "$PROBE" ] && [ ! -L "$PROBE" ] || die "--probe must be a directory, not a symlink"
    [ -f "$PROBE/probe.json" ] && [ ! -L "$PROBE/probe.json" ] || die "--probe directory has no probe.json"
fi
if [ -z "$SOURCE" ] && [ "$PROBE_SET" = 0 ]; then
    die "nothing to read (see --help)"
fi
if [ "$SOURCE" = adb ]; then
    command -v adb >/dev/null || die "adb not found (pass --probe DIR, or --fixture DIR for a saved capture)"
fi
# lstat uid and permission bits. GNU stat, then BSD stat (macOS).
lstat_uid_mode() {
    _s=$(stat -c '%u %a' -- "$1" 2>/dev/null) || _s=$(stat -f '%u %Lp' "$1" 2>/dev/null) || return 1
    printf '%s\n' "$_s"
}
# Directory metadata. Follow a system alias so the directory itself is what is checked.
stat_uid_mode() {
    _s=$(stat -L -c '%u %a' -- "$1" 2>/dev/null) || _s=$(stat -L -f '%u %Lp' "$1" 2>/dev/null) || return 1
    printf '%s\n' "$_s"
}
# A system alias is a symlink the administrator installed: the link and its parent directory
# are both owned by root and not group- or other-writable. That covers macOS /tmp, /var
# (so $TMPDIR under /var/folders works), and /etc. Linux reports every symlink as mode 777,
# so that dummy mode is not treated as writable there. A user-owned symlink is refused.
mode_is_private() {
    _bits=$((8#$1))
    [ $((_bits & 18)) -eq 0 ]
}
meta_is_system() {
    _uid=$1
    _mode=$2
    _kind=$3
    [ "$_uid" = 0 ] || return 1
    if [ "$_kind" = link ] && [ "$_mode" = 777 ]; then
        [ "$(uname -s)" = Linux ] && return 0
    fi
    mode_is_private "$_mode"
}
system_alias_ok() {
    _link=$1
    _parent=$(dirname -- "$_link")
    [ -n "$_parent" ] || return 1
    [ -L "$_link" ] || return 1
    _lm=$(lstat_uid_mode "$_link") || return 1
    _lu=${_lm%% *}
    _lmode=${_lm#* }
    _pm=$(stat_uid_mode "$_parent") || return 1
    _pu=${_pm%% *}
    _pmode=${_pm#* }
    meta_is_system "$_lu" "$_lmode" link || return 1
    meta_is_system "$_pu" "$_pmode" dir
}
# Reject a symlink that is not a system alias. Missing directories are created later
# by the Python walk, which holds the parent directory fd (no shell mkdir).
secure_prepare_out() {
    _path=$1
    _cur=
    _rest=$_path
    _missing=0
    while [ -n "$_rest" ]; do
        case "$_rest" in
            /*) _rest=${_rest#/} ;;
        esac
        [ -n "$_rest" ] || break
        _last=0
        case "$_rest" in
            */*)
                _part=${_rest%%/*}
                _rest=${_rest#*/}
                ;;
            *)
                _part=$_rest
                _rest=
                _last=1
                ;;
        esac
        [ -n "$_part" ] || continue
        if [ "$_missing" = 1 ]; then
            continue
        fi
        if [ -z "$_cur" ]; then
            case "$_path" in
                /*) _next=/$_part ;;
                *) _next=$_part ;;
            esac
        else
            _next=$_cur/$_part
        fi
        if [ -L "$_next" ]; then
            if [ "$_last" = 1 ] || ! system_alias_ok "$_next"; then
                die "--out must not follow a symlink"
            fi
        elif [ -d "$_next" ]; then
            :
        elif [ -e "$_next" ]; then
            die "--out $_path must be a new or empty directory"
        else
            _missing=1
        fi
        _cur=$_next
    done
    if [ "$_missing" = 0 ]; then
        [ -d "$OUT" ] && [ ! -L "$OUT" ] && [ -z "$(ls -A "$OUT")" ] \
            || die "--out $OUT must be a new or empty directory"
    fi
}
secure_prepare_out "$OUT"
RAW=$(mktemp -d "${TMPDIR:-/tmp}/gbos-report.XXXXXX")
: > "$RAW/manifest.tsv"
if [ -n "$SOURCE" ]; then
    i=0
    while [ "$i" -lt "${#CMDS[@]}" ]; do
        id=${IDS[$i]}
        cmd=${CMDS[$i]}
        allow "$cmd" || die "internal: refusing a command that is not on the read-only allowlist"
        # Every capture starts empty. A sidecar from this run cannot survive into the next
        # command, and a saved .rc is not a capture.
        rm -f "$RAW/$id.txt" "$RAW/$id.err" "$RAW/$id.rc"
        : > "$RAW/$id.err"
        if [ -n "$FIXTURE" ]; then
            got_txt=0
            if [ -L "$FIXTURE/$id.txt" ]; then
                die "refusing symlink fixture $id.txt"
            elif [ -e "$FIXTURE/$id.txt" ]; then
                [ -f "$FIXTURE/$id.txt" ] || die "refusing fixture $id.txt: not a regular file"
                cat < "$FIXTURE/$id.txt" > "$RAW/$id.txt"
                got_txt=1
            else
                : > "$RAW/$id.txt"
            fi
            if [ -L "$FIXTURE/$id.err" ]; then
                die "refusing symlink fixture $id.err"
            elif [ -e "$FIXTURE/$id.err" ]; then
                [ -f "$FIXTURE/$id.err" ] || die "refusing fixture $id.err: not a regular file"
                cat < "$FIXTURE/$id.err" > "$RAW/$id.err"
            fi
            if [ "$got_txt" != 1 ]; then
                # Missing stdout is a failed capture. A leftover success .rc must not invent an empty bus.
                printf '1\n' > "$RAW/$id.rc"
                if [ ! -s "$RAW/$id.err" ]; then
                    printf 'not captured\n' > "$RAW/$id.err"
                fi
            elif [ -L "$FIXTURE/$id.rc" ]; then
                die "refusing symlink fixture $id.rc"
            elif [ -e "$FIXTURE/$id.rc" ]; then
                [ -f "$FIXTURE/$id.rc" ] || die "refusing fixture $id.rc: not a regular file"
                cat < "$FIXTURE/$id.rc" > "$RAW/$id.rc"
            else
                printf '0\n' > "$RAW/$id.rc"
            fi
        else
            if [ -n "$SERIAL" ]; then
                run_allow_cmd "$RAW/$id.txt" "$RAW/$id.err" "$RAW/$id.rc" \
                    command adb -s "$SERIAL" shell "$cmd"
            else
                run_allow_cmd "$RAW/$id.txt" "$RAW/$id.err" "$RAW/$id.rc" \
                    command adb shell "$cmd"
            fi
        fi
        printf '%s\t%s\n' "$id" "$cmd" >> "$RAW/manifest.tsv"
        i=$((i + 1))
    done
fi
set -- render "$OUT" "$DATE" "$RAW" "${PROBE:-}" "${SOURCE:-}" "${SERIAL:-}"

set +e
python3 -I - "$@" << 'PY'
import errno, hashlib, json, os, re, stat, sys
from pathlib import Path

# One schema. Each field below has one checker. A value that is empty or fails is "unknown".
# Free text (product names, BIOS strings, CPU marketing names, device names) is never written.
# Raw command output is never written. Ask the person directly if a raw log is needed.
PRIVACY = (
    "privacy: the default report contains only allowlisted fields and no free text. "
    "Values are integers, fixed hex ids (PCI, USB vid:pid), enumerations "
    "(a known driver, or other plus that PCI or USB id; a known vendor or other; CPU vendor; "
    "wlan0-wlan9, eth0-eth9, hci0-hci99, card0-card99, or renderD0-renderD999, with no leading zero; "
    "on/off/connected/disconnected/unknown), and version strings this project ships "
    "(Googlebook profile id and status, the build id this project ships, "
    "a kernel release only when it exactly equals a pinned image kernel). "
    "Set GBOS_DEV_KERNEL_DIR to a kernel tree. When the running release equals that tree's "
    "include/config/kernel.release, the report says dev-kernel. When .config is a regular file, "
    "config pass means its sha256 equals the pinned GKI config, and config fail means it does not. "
    "The dev release string is never printed. "
    "Model names, BIOS versions, and any other interface name are not included; type those in the issue. "
    "Raw dumpsys/logcat output is not included. A missing value or a value that fails its check is \"unknown\". "
    "This script does not attach raw logs; ask the person directly if one is needed."
)
PRIVACY_ZH = (
    "預設報告沒有自由文字，只有整數、固定格式的十六進位識別碼、列舉"
    "（含 card0–card99、renderD0–renderD999，沒有前導零），以及這個專案自己的版本字串。"
    "只有與映像釘選完全相同的核心版本才原樣印出。"
    "設 GBOS_DEV_KERNEL_DIR 為核心樹，且執行中的 release 等於該樹的 include/config/kernel.release 時，寫 dev-kernel。"
    "該樹的 .config 是一般檔時，其 sha256 等於釘選的 GKI 設定寫 config pass，否則寫 config fail。"
    "開發核心不印 release。"
    "機型名稱、BIOS 版本和其他描述請在 issue 裡自行填寫。不含 dumpsys／logcat 原文；"
    "缺值或檢查不過的值寫成「未知」。辨識到硬體不代表聲音、觸控、Wi-Fi 或休眠已經驗證過。"
    "這個腳本不附原始 log；若需要請直接向使用者索取。"
)
UNK_EN, UNK_ZH = "unknown", "未知"
REAL_GPU = {
    "i915", "xe", "amdgpu", "nouveau", "radeon", "nvidia", "nvidia_drm", "nvidia-drm",
    "msm", "panfrost", "panthor", "v3d", "vc4", "etnaviv", "lima", "ast", "mgag200",
    "qxl", "vmwgfx", "hyperv_drm",
}
SHIM_GPU = {
    "simpledrm", "efifb", "gbos_efifb", "vkms", "virtio_gpu", "virtio-gpu", "virtio",
    "bochs", "bochs-drm", "cirrus",
}
WIFI_DRIVERS = {
    "iwlwifi", "iwlmvm", "ath9k", "ath10k_pci", "ath11k_pci", "ath12k", "brcmfmac",
    "brcmsmac", "mwifiex_pcie", "mt7921e", "mt7921s", "mt7925e", "rtw89_pci", "rtw88_pci",
}
BT_DRIVERS = {"btusb", "btintel", "btrtl", "btbcm", "btqca", "hci_uart", "btnxpuart"}
INPUT_DRIVERS = {
    "i2c_hid", "i2c_hid_acpi", "hid_multitouch", "hid-multitouch", "usbhid", "hid_generic",
    "elan_i2c", "hid_elan", "synaptics_i2c", "synaptics_usb",
}
CPU_VENDORS = {
    "GenuineIntel", "AuthenticAMD", "HygonGenuine", "CentaurHauls", "GenuineTMx86", "AMDisbetter!",
}
BUSNAME = {
    "0018": "i2c", "0003": "usb", "0011": "i8042", "0019": "host", "001e": "spi",
    "0005": "bluetooth", "001f": "platform",
}
CLASS_TOUCH = 0x00000004
CLASS_TOUCH_MT = 0x00000010
CLASS_TOUCHPAD = 0x00100000
CLASS_STYLUS = 0x00000800
# Backstop shapes only: "@", a MAC with one repeated separator, IPv4, IPv6.
# PCI xxxx:xxxx is two groups of four and does not match. No fragment or serial-run rules.
MAC_PAIR_RE = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{2}([:._+\-, \t])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}(?![0-9a-f])")
MAC_QUAD_RE = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{4}([:._+\-, \t])(?:[0-9a-f]{4}\1){2}(?![0-9a-f])")
IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
IPV6_RE = re.compile(
    r"(?i)(?<![0-9a-f:])(?:(?:[0-9a-f]{1,4}:){2,}[0-9a-f:]*|:(?::[0-9a-f]{1,4}){1,7})(?:/\d{1,3})?(?![0-9a-f:])")
PCI_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")
# Exact releases this tree ships. No free hex grammar. prepare.py / gbos_efifb.c / vendor.sh pin the GKI string.
PINNED_KERNELS = (
    "6.18.21-android17-5-g3938c8d97142-ab16302624",
    "6.18.32-android17-5-g837ceedd23a2-ab16614688",
)
LAPIS_BUILDS = {"CL3B.260622.271.R1", "16452207", "CL3B.260622.276.R4", "16658739"}
PROFILES = {"kano": "kano", "generic-x86": "generic-x86"}
PROFILE_STATUS = {"verified": "verified", "experimental": "experimental", "probe-only": "probe-only"}
# Output field names. tests/report/report-test.sh reads this tuple. A new add() key must be listed here.
SCHEMA = (
    "running profile",
    "draft profile",
    "model",
    "board",
    "firmware",
    "firmware flags",
    "build",
    "kernel",
    "display/gpu",
    "gpu pci",
    "drm cards",
    "cpu",
    "sound card",
    "wifi",
    "bluetooth",
    "input",
    "sleep/resume",
    "dri nodes",
    "live linux gpu",
    "live linux sound",
    "live linux wifi",
    "live linux bluetooth",
    "live linux input",
    "compare",
)
# field<TAB>leaf. tests/report/report-test.sh plants every canary into every leaf.
# constant: no device bytes. derived: computed from other leaves. ignored: read, never copied.
SOURCE_MAP_TEXT = """
running profile	constant
model	dmi-sys-vendor
model	probe-dmi-sys-vendor
model	ignored-dmi-product
model	ignored-getprop-rest
model	ignored-probe-dmi-rest
board	dmi-board-vendor
board	probe-dmi-board-vendor
board	ignored-dmi-board-name
firmware	dmi-bios-vendor
firmware	probe-dmi-bios-vendor
firmware	ignored-bios-version
firmware	ignored-bios-date
firmware	ignored-dmi-product-version
firmware flags	probe-firmware
firmware flags	probe-firmware-uefi
firmware flags	probe-report
firmware flags	ignored-probe-coverage
build	prop-display-id
build	prop-build-id
kernel	uname
kernel	proc-version
kernel	ignored-logcat
display/gpu	drm-driver
display/gpu	drm-card1
display/gpu	drm-card2
display/gpu	drm-card3
display/gpu	egl
display/gpu	vulkan
display/gpu	ignored-dumpsys-display
gpu pci	drm-pci
gpu pci	drm-sysfs-vendor
gpu pci	drm-sysfs-device
gpu pci	pci-bus
drm cards	drm-uevent
drm cards	drm-card1
drm cards	drm-card2
drm cards	drm-card3
cpu	cpuinfo
cpu	cpuinfo-family
cpu	cpuinfo-model
cpu	cpuinfo-stepping
cpu	probe-cpu
cpu	probe-cpu-family
cpu	probe-cpu-model
cpu	probe-cpu-stepping
cpu	ignored-cpu-model
cpu	ignored-probe-cpu-model
sound card	ignored-asound
sound card	ignored-probe-audio
sound card	ignored-dumpsys-audio
wifi	dumpsys-wifi
wifi	wifi-curstate
wifi	wifi-supplicant
wifi	wlan-uevent
wifi	wlan-pci
wifi	wlan-usb
wifi	wlan-usb-vid
wifi	wlan-usb-pid
wifi	wlan-usb-bcd
wifi	wlan-operstate
bluetooth	dumpsys-bluetooth
bluetooth	bt-enabled
bluetooth	bt-radio-state
bluetooth	bt-uevent
bluetooth	bt-pci
bluetooth	bt-usb
bluetooth	bt-usb-vid
bluetooth	bt-usb-pid
bluetooth	bt-usb-bcd
bluetooth	ls-bluetooth
bluetooth	prop-bt-service
bluetooth	ignored-probe-bt-device
input	dumpsys-input
input	dumpsys-input-id
input	dumpsys-input-bus
input	dumpsys-input-product
input	dumpsys-input-classes
input	dumpsys-input-sources
input	proc-input
input	proc-input-vendor
input	proc-input-product
input	proc-input-bus
input	ignored-probe-input
input	probe-input-vendor
input	probe-input-product
input	probe-input-bus
input	probe-input-driver
input	probe-input-kind
input	probe-input-busname
sleep/resume	dumpsys-power
sleep/resume	power-success
sleep/resume	power-fail
sleep/resume	suspend-sysfs
sleep/resume	suspend-success
sleep/resume	suspend-fail
sleep/resume	ignored-dumpsys-battery
dri nodes	ls-dri
live linux gpu	probe-pci
live linux gpu	probe-pci-id
live linux gpu	probe-pci-class
live linux gpu	ignored-probe-pci-slot
live linux sound	ignored-probe-audio
live linux wifi	probe-net
live linux wifi	probe-net-wireless
live linux wifi	ignored-probe-net-address
live linux bluetooth	probe-bt
live linux input	ignored-probe-input
draft profile	probe-draft-id
draft profile	probe-draft-status
draft profile	ignored-draft-rest
compare	derived
compare	probe-pci-unread
"""
OEM = {
    "google": "Google",
    "dell": "Dell",
    "dell inc.": "Dell",
    "dell inc": "Dell",
    "lenovo": "Lenovo",
    "hp": "HP",
    "hewlett-packard": "HP",
    "asus": "ASUS",
    "asustek computer inc.": "ASUS",
    "acer": "Acer",
    "framework": "Framework",
    "samsung": "Samsung",
    "intel": "Intel",
    "amd": "AMD",
    "msi": "MSI",
    "micro-star international co., ltd.": "MSI",
    "gigabyte technology co., ltd.": "Gigabyte",
    "microsoft": "Microsoft",
    "microsoft corporation": "Microsoft",
    "apple": "Apple",
    "qemu": "QEMU",
    "innotek gmbh": "innotek",
    "system76": "System76",
    "star labs": "Star Labs",
    "toshiba": "Toshiba",
    "sony": "Sony",
    "huawei": "Huawei",
    "honor": "Honor",
    "xiaomi": "Xiaomi",
    "chuwi": "Chuwi",
    "minisforum": "Minisforum",
    "beelink": "Beelink",
    "gpd": "GPD",
    "supermicro": "Supermicro",
    "red hat": "Red Hat",
    "amazon": "Amazon",
    "amazon ec2": "Amazon",
}
FW_VENDOR = {
    "coreboot": "coreboot",
    "ami": "AMI",
    "american megatrends international, llc.": "AMI",
    "american megatrends inc.": "AMI",
    "american megatrends": "AMI",
    "insyde": "Insyde",
    "insyde corp.": "Insyde",
    "phoenix": "Phoenix",
    "phoenix technologies ltd.": "Phoenix",
    "edk ii": "EDK2",
}
SOUND_FAMILIES = {
    "sof-nau8825", "sofna8825", "sof-hda-dsp", "sof-rt5682", "sof-essx8336",
    "HDA-Intel", "hda-intel", "HDMI", "hdmi",
    "googlebook-sound", "live-linux-only-sound",
}
GPU_DRIVERS = REAL_GPU | SHIM_GPU | {"vboxvideo"}
# Hostnames our own images and fixtures use. A user-set hostname is an identifier only when it is not one of these.
DEFAULT_HOSTS = {
    "localhost", "localhost.localdomain", "localdomain", "(none)", "none",
    "googlebook", "gbos", "kano", "generic-x86",
}
DMI_PLACEHOLDERS = {
    "", "unknown", "to be filled by o.e.m.", "default string", "not specified", "none",
}
HEADER_RE = re.compile(r"^(?:Device\s+)?(-?\d+):\s+(\S.*?)\s*$", re.I)
CARD_SLOTS = (
    (0, "cat-drm-vendor", "cat-drm-device", "cat-drm0-uevent"),
    (1, "cat-drm1-vendor", "cat-drm1-device", "cat-drm1-uevent"),
    (2, "cat-drm2-vendor", "cat-drm2-device", "cat-drm2-uevent"),
    (3, "cat-drm3-vendor", "cat-drm3-device", "cat-drm3-uevent"),
)


# Fixed phrases and tables. render() prints these objects, and the vocabulary is derived from them.
NO_MARKER_EN = "unknown (no image marker)"
NO_MARKER_ZH = "未知（沒有映像標記）"
OMIT_EN = "omitted (probe is not the same machine)"
OMIT_ZH = "已省略（probe 與這台裝置不是同一台）"
AGREE_EN = "agree: DMI, CPU, PCI"
AGREE_ZH = "一致：DMI、CPU、PCI"
PROBE_SAW_EN = "probe saw"
PROBE_SAW_ZH = "探測到"
NO_LINK_EN = "(no link state)"
NO_LINK_ZH = "（沒有連線狀態）"
NO_POWER_EN = "(no power state)"
NO_POWER_ZH = "（沒有開關狀態）"
PROBE_ONLY_EN = "probe only:"
PROBE_ONLY_ZH = "僅 probe："
NOT_GB_EN = "(not Googlebook state)"
NOT_GB_ZH = "（不是 Googlebook 狀態）"
GPU_BOUND_EN = "bound"
GPU_BOUND_ZH = "已綁定"
GPU_NONE_EN = "no native GPU driver"
GPU_NONE_ZH = "沒有原生 GPU 驅動"
SWIFTSHADER_LABEL = "SwiftShader"
DRM_REAL = "real-gpu"
DRM_SHIM = "shim"
DRM_UNREAD = "driver unread"
DRIVER_WORD = "driver"
DRIVER_WORD_ZH = "驅動"
OTHER = "other"
DEV_KERNEL = "dev-kernel"
CONFIG_GATE = "config"
GATE_PASS = "pass"
GATE_FAIL = "fail"
# Pinned GKI .config sha256 (builder/kmod/build-ac.sh). The dev release string is never a report field.
GKI_CONFIG_SHA = "046971f3fb011e5ec31b4ddad2610ee4a459d2000aba4520b10faafe6a27be40"
CPU_LABELS = ("family", "model", "stepping")
KIND_EN = ("touchpad", "touchscreen", "stylus")
KIND_ZH = {"touchpad": "觸控板", "touchscreen": "觸控螢幕", "stylus": "觸控筆"}
SLEEP_EN = ("success", "fail")
SLEEP_ZH = ("成功", "失敗")
# pastel is vulkan.pastel.so, the SwiftShader ICD this tree ships. The report says SwiftShader.
SWIFT_TOKENS = ("swiftshader", "pastel")
PROBE_INPUT_KINDS = {
    "touchpad": "touchpad",
    "touchscreen": "touchscreen",
    "stylus": "stylus",
    "touchpad (i2c, by handler)": "touchpad",
}
WIFI_WORDS = {
    "connected": ("connected", "已連線"),
    "enabled": ("enabled, not connected", "已開啟、未連線"),
    "link-unknown": ("enabled, connection unknown", "已開啟，連線狀態未知"),
    "disabled": ("disabled", "已關閉"),
    "up": ("interface up", "介面已起來"),
    "down": ("interface down", "介面已關閉"),
    "unknown": ("unknown", "未知"),
}
BT_WORDS = {
    "up": ("on", "已開啟"),
    "down": ("off", "已關閉"),
    "present": ("adapter present, power unknown", "介面存在，開關未知"),
    "service": ("service running, power unknown", "服務執行中，開關未確認"),
    "stopped": ("service stopped, power unknown", "服務已停止，開關未確認"),
    "turning-on": ("turning on", "開啟中"),
    "turning-off": ("turning off", "關閉中"),
    "unknown": ("unknown", "未知"),
}
PCI_ATOM = re.compile(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}")
_REPORT_WORDS = None
# English and Chinese row labels, in the order render() writes them. Vocabulary is these
# strings plus the enumeration values, not a second hand list and not anything read at runtime.
ROW_LABELS = (
    ("running profile", "執行中的 profile"),
    ("draft profile", "profile 草稿"),
    ("model", "機型"),
    ("board", "板名"),
    ("firmware", "韌體"),
    ("firmware flags", "韌體旗標"),
    ("build", "建置版本"),
    ("kernel", "核心"),
    ("display/gpu", "顯示／GPU 驅動"),
    ("gpu pci", "GPU PCI"),
    ("drm cards", "DRM 卡"),
    ("cpu", "CPU"),
    ("sound card", "音效卡"),
    ("wifi", "Wi-Fi"),
    ("bluetooth", "藍牙"),
    ("input", "觸控／觸控板"),
    ("sleep/resume", "休眠／喚醒"),
    ("dri nodes", "DRM 節點"),
    ("live linux gpu", "live Linux GPU"),
    ("live linux sound", "live Linux 音效卡"),
    ("live linux wifi", "live Linux Wi-Fi"),
    ("live linux bluetooth", "live Linux 藍牙"),
    ("live linux input", "live Linux 觸控／觸控板"),
    ("compare", "比對"),
)


def _word_pieces(value):
    """Alnum words, including . _ + - inside a word, and maximal CJK runs.
    1-6 digit counters are not words. The whole string is not a word when it
    merely joins several words; each word is."""
    text = str(value).strip()
    low = text.lower()
    out = set()
    for m in re.finditer(r"[a-z0-9]+(?:[._+\-_][a-z0-9]+)*", low):
        tok = m.group(0)
        if re.fullmatch(r"\d{1,6}", tok):
            continue
        out.add(tok)
        for part in re.split(r"[^a-z0-9]+", tok):
            if part and not re.fullmatch(r"\d{1,6}", part):
                out.add(part)
    for m in re.finditer(r"[\u4e00-\u9fff]+", text):
        if m.group(0):
            out.add(m.group(0))
    return out


def report_vocabulary():
    """Field names, enumeration values, units, and fixed labels compiled into this script.
    Nothing read at runtime (a kernel release file, a build id, device data) is included."""
    words = set()

    def take(value):
        words.update(_word_pieces(value))

    for en, zh in ROW_LABELS:
        take(en)
        take(zh)
    for name in SCHEMA:
        take(name)
    for bag in (OEM, FW_VENDOR, PROFILES, PROFILE_STATUS, KIND_ZH):
        for val in bag.values():
            take(val)
    for val in BUSNAME.values():
        take(val)
    for val in PROBE_INPUT_KINDS.values():
        take(val)
    for bag in (
        ("GenuineIntel", "AuthenticAMD"),
        REAL_GPU, SHIM_GPU, WIFI_DRIVERS, BT_DRIVERS, INPUT_DRIVERS,
        GPU_DRIVERS, LAPIS_BUILDS, PINNED_KERNELS, KIND_EN, CPU_LABELS,
        SLEEP_EN, SLEEP_ZH,
    ):
        for val in bag:
            take(val)
    for table in (WIFI_WORDS, BT_WORDS):
        for en, zh in table.values():
            take(en)
            take(zh)
    for val in (
        NO_MARKER_EN, NO_MARKER_ZH, OMIT_EN, OMIT_ZH, AGREE_EN, AGREE_ZH,
        PROBE_SAW_EN, PROBE_SAW_ZH, NO_LINK_EN, NO_LINK_ZH, NO_POWER_EN, NO_POWER_ZH,
        PROBE_ONLY_EN, PROBE_ONLY_ZH, NOT_GB_EN, NOT_GB_ZH,
        GPU_BOUND_EN, GPU_BOUND_ZH, GPU_NONE_EN, GPU_NONE_ZH, SWIFTSHADER_LABEL,
        DRM_REAL, DRM_SHIM, DRM_UNREAD, DRIVER_WORD, DRIVER_WORD_ZH, OTHER,
        DEV_KERNEL, CONFIG_GATE, GATE_PASS, GATE_FAIL,
        UNK_EN, UNK_ZH,
        "pci", "usb", "yes", "no", "on", "off", "UEFI", "Secure Boot",
        "gbos-report", "redactions", "sources", "fixture", "adb", "probe", "none",
        "fixture+probe", "adb+probe",
        "tool: install/gbos-report.sh (read-only allowlist; no install, no settings change, no file push)",
        "note: a recognized device is not proof that audio, touch, Wi-Fi, or suspend works. Read both files before pasting.",
        "Googlebook 機器回報", "日期", "工具", "唯讀", "輸出防火牆", "遮罩", "行",
        "來源", "項目", "結果", "沒有",
        PRIVACY, PRIVACY_ZH,
    ):
        take(val)
    for name in report_node_names():
        take(name)
    return words


def report_node_names():
    """Interface and DRM node names this report can print. No leading zeros."""
    names = []
    for i in range(10):
        names.append("wlan%d" % i)
        names.append("eth%d" % i)
    for i in range(100):
        names.append("hci%d" % i)
        names.append("card%d" % i)
    for i in range(1000):
        names.append("renderD%d" % i)
    return names


def is_report_word(token):
    """True when this whole identifier is a word the report itself can print."""
    global _REPORT_WORDS
    if _REPORT_WORDS is None:
        _REPORT_WORDS = report_vocabulary()
    low = (token or "").strip().lower()
    if not low:
        return False
    return low in _REPORT_WORDS


def fail(msg):
    sys.stderr.write(f"gbos-report: {msg}\n")
    raise SystemExit(2)


def norm(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def read_text(path):
    try:
        return norm(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        fail(f"cannot read {path}: {e}")


# Directories this process created for --out. Each entry is (parent_fd, name).
# On failure they are removed through the parent fd, so a swapped path is not followed.
_CREATED = []
_HELD_OUT = None


def _dir_flags():
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _close_fd(fd):
    if fd is None or fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _open_real_dir(path):
    """Open an absolute directory. Every component must already be a real directory."""
    parts = [p for p in path.split("/") if p]
    flags = _dir_flags()
    root_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        root_flags |= os.O_DIRECTORY
    fd = os.open("/", root_flags)
    try:
        for part in parts:
            st = os.lstat(part, dir_fd=fd)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                _close_fd(fd)
                fail("--out must not follow a symlink")
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except SystemExit:
        _close_fd(fd)
        raise
    except OSError as e:
        _close_fd(fd)
        fail(f"--out cannot be opened ({e.strerror})")


def _mode_private(mode):
    """Not group-writable and not other-writable. The owner bit is not part of this check."""
    return (stat.S_IMODE(mode) & 0o022) == 0


def _linux_dummy_link_mode(st):
    """Linux lstat reports every symlink as 0777. That mode is not a permission."""
    return sys.platform.startswith("linux") and stat.S_IMODE(st.st_mode) == 0o777


def _system_alias(parent_fd, link_st):
    """Root-owned symlink whose parent directory is root-owned and not group/other-writable.
    Covers macOS /tmp, /var (including $TMPDIR under /var/folders), and /etc.
    A user-owned symlink at any ancestor is refused."""
    if link_st.st_uid != 0:
        return False
    if not _linux_dummy_link_mode(link_st) and not _mode_private(link_st.st_mode):
        return False
    try:
        parent = os.fstat(parent_fd)
    except OSError:
        return False
    if not stat.S_ISDIR(parent.st_mode):
        return False
    if parent.st_uid != 0 or not _mode_private(parent.st_mode):
        return False
    return True


def _resolve_alias(parent_fd, part, built):
    try:
        link = os.readlink(part, dir_fd=parent_fd)
    except OSError:
        return ""
    if not link.startswith("/"):
        parent = built[: built.rfind("/")] or "/"
        link = os.path.normpath(os.path.join(parent, link))
    if not link.startswith("/") or ".." in link.split("/"):
        return ""
    return link


def _release_created(delete):
    """Drop directories this process created. delete removes them through the parent fd."""
    global _CREATED
    items = _CREATED
    _CREATED = []
    if delete:
        for parent_fd, name in reversed(items):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            _close_fd(parent_fd)
    else:
        for parent_fd, _name in items:
            _close_fd(parent_fd)


def _fail_walk(fd, msg):
    _close_fd(fd)
    fail(msg)


def open_out_dir(path):
    """Open --out. Follow only a system alias. Create missing components with the parent fd."""
    global _CREATED
    path = os.path.abspath(path)
    parts = [p for p in path.split("/") if p]
    if not parts:
        fail("--out must not be /")
    flags = _dir_flags()
    root_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        root_flags |= os.O_DIRECTORY
    fd = os.open("/", root_flags)
    built = ""
    created_final = False
    try:
        for index, part in enumerate(parts):
            built = built + "/" + part
            last = index == len(parts) - 1
            try:
                st = os.lstat(part, dir_fd=fd)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    _fail_walk(fd, f"--out cannot be opened ({e.strerror})")
                parent_dup = os.dup(fd)
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except OSError as e:
                    _close_fd(parent_dup)
                    _fail_walk(fd, f"--out cannot be created ({e.strerror})")
                _CREATED.append((parent_dup, part))
                if last:
                    created_final = True
                try:
                    nxt = os.open(part, flags, dir_fd=fd)
                except OSError as e:
                    _fail_walk(fd, "--out must not follow a symlink" if e.errno == errno.ELOOP
                               else f"--out cannot be opened ({e.strerror})")
                os.close(fd)
                fd = nxt
                continue
            if stat.S_ISLNK(st.st_mode):
                if last or not _system_alias(fd, st):
                    _fail_walk(fd, "--out must not follow a symlink")
                target = _resolve_alias(fd, part, built)
                if not target:
                    _fail_walk(fd, "--out must not follow a symlink")
                held = fd
                fd = -1
                _close_fd(held)
                fd = _open_real_dir(target)
                built = target.rstrip("/")
                continue
            if not stat.S_ISDIR(st.st_mode):
                _fail_walk(fd, "--out must be a directory")
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except OSError as e:
                _fail_walk(fd, "--out must not follow a symlink" if e.errno == errno.ELOOP
                           else f"--out cannot be opened ({e.strerror})")
            os.close(fd)
            fd = nxt
        if not created_final:
            try:
                names = os.listdir(fd)
            except OSError as e:
                _fail_walk(fd, f"--out cannot be opened ({e.strerror})")
            if names:
                _fail_walk(fd, "--out must be a new or empty directory")
        return fd
    except SystemExit:
        _close_fd(fd)
        raise


def close_out_dir():
    global _HELD_OUT
    if _HELD_OUT is not None:
        _close_fd(_HELD_OUT[1])
        _HELD_OUT = None


def write_excl(path, text):
    """Create one report file in the directory opened with O_NOFOLLOW. Both files share that fd."""
    global _HELD_OUT
    parent = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    if name != "summary.md" and not re.fullmatch(r"gbos-report-[0-9]{8}\.txt", name):
        fail("internal: report file name")
    if _HELD_OUT is None or _HELD_OUT[0] != parent:
        if _HELD_OUT is not None:
            _close_fd(_HELD_OUT[1])
            _HELD_OUT = None
        _HELD_OUT = (parent, open_out_dir(parent))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, 0o644, dir_fd=_HELD_OUT[1])
    except OSError as e:
        fail(f"cannot create {name} ({e.strerror})")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def cell(value):
    return (value or "").replace("|", "/").replace("\n", " ").strip()


def prop_get(text, key):
    m = re.search(rf"(?m)^\[{re.escape(key)}\]: \[(.*)\]\s*$", text or "")
    return m.group(1) if m else ""


def raw_scalar(value):
    """One value, exactly. A single trailing newline is the file ending. Anything else is empty."""
    if not isinstance(value, str):
        return ""
    v = value.replace("\r\n", "\n").replace("\r", "\n")
    if v.endswith("\n"):
        v = v[:-1]
    if "\n" in v or v == "" or v != v.strip():
        return ""
    return v


def field_raw(value):
    """The bytes after a ': ' delimiter. A second space, or any other extra byte, is empty."""
    if not isinstance(value, str):
        return ""
    v = value.replace("\r\n", "\n").replace("\r", "\n")
    if v.startswith(" ") and not v.startswith("  "):
        v = v[1:]
    return raw_scalar(v)


def ipv4_hit(value):
    for m in IPV4_RE.finditer(value or ""):
        parts = m.group(0).split(".")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            continue
        if len(nums) == 4 and all(0 <= n <= 255 for n in nums):
            return True
    return False


def secret_line(value):
    """@, MAC, IPv4, or IPv6 anywhere in the raw value."""
    v = value or ""
    if "@" in v or MAC_PAIR_RE.search(v) or MAC_QUAD_RE.search(v):
        return True
    if ipv4_hit(v) or IPV6_RE.search(v):
        return True
    return False


def ident_exact(value, banned):
    """The whole value equals a device-unique identifier. Not a fragment or a report word."""
    hay = (value or "").lower()
    if not hay or is_report_word(hay):
        return False
    for b in banned or []:
        needle = (b or "").lower()
        if not needle or is_report_word(needle):
            continue
        if hay == needle:
            return True
    return False


def hex12_forms(value):
    """A whole 12-hex serial, or a MAC whose separators are one repeated character. Not a fragment."""
    raw = raw_scalar(value) if isinstance(value, str) else ""
    if not raw:
        return set()
    low = raw.lower()
    forms = set()
    if re.fullmatch(r"[0-9a-f]{12}", low):
        forms.add(low)
    if re.fullmatch(r"[0-9a-f]{2}([:.\-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}", low):
        forms.add(re.sub(r"[^0-9a-f]", "", low))
    return forms


def shipped_hex12():
    """12-hex groups of a pinned image kernel. A dev-tree release is not one of these."""
    found = set()
    for rel in PINNED_KERNELS:
        found.update(re.findall(r"[0-9a-f]{12}", rel.lower()))
    return found


def kernel_embeds_banned(raw, banned):
    """A complete 12-hex group inside a kernel release equals a collected MAC or serial.
    A group that is part of a release this tree ships is the public build id, not that serial."""
    parts = set(re.findall(r"[0-9a-f]{12}", (raw or "").lower())) - shipped_hex12()
    if not parts:
        return False
    found = set()
    for b in banned or []:
        found |= hex12_forms(b)
    return bool(parts & found)


def open_regular(path):
    """A regular file, opened without following a symlink or blocking on a fifo.
    None when the path is missing, a symlink, a directory, or any other non-regular file."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def kernel_release_text():
    """The dev tree's kernel.release, only when it is a regular file. Never vocabulary.
    A fifo, symlink, or other non-regular file is not a release, so the kernel stays other."""
    dev = os.environ.get("GBOS_DEV_KERNEL_DIR", "")
    if not dev:
        return ""
    fd = open_regular(Path(dev) / "include" / "config" / "kernel.release")
    if fd is None:
        return ""
    try:
        blob = os.read(fd, 4096)
    finally:
        os.close(fd)
    rel = raw_scalar(blob.decode("utf-8", "replace"))
    if rel and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", rel) and not secret_line(rel):
        return rel
    return ""


def kernel_config_gate():
    """pass or fail when .config is a regular file. Empty when it is missing, a symlink, or a fifo.
    pass is the pinned GKI config hash. The file bytes are not a report field."""
    dev = os.environ.get("GBOS_DEV_KERNEL_DIR", "")
    if not dev:
        return ""
    fd = open_regular(Path(dev) / ".config")
    if fd is None:
        return ""
    try:
        chunks = []
        total = 0
        while True:
            blob = os.read(fd, 1024 * 1024)
            if not blob:
                break
            chunks.append(blob)
            total += len(blob)
            if total > 8 * 1024 * 1024:
                return GATE_FAIL
    finally:
        os.close(fd)
    digest = hashlib.sha256(b"".join(chunks)).hexdigest()
    if digest == GKI_CONFIG_SHA:
        return GATE_PASS
    return GATE_FAIL


def dev_kernel_value():
    """dev-kernel, plus the config enum when .config could be read. Never the release text."""
    gate = kernel_config_gate()
    if gate == GATE_PASS:
        return DEV_KERNEL + ", " + CONFIG_GATE + " " + GATE_PASS
    if gate == GATE_FAIL:
        return DEV_KERNEL + ", " + CONFIG_GATE + " " + GATE_FAIL
    return DEV_KERNEL


def vendor_enum(value, banned):
    """Known OEM, or other. The product string is never this function's input."""
    raw = raw_scalar(value)
    if not raw or raw.lower() in DMI_PLACEHOLDERS:
        return ""
    if secret_line(raw) or ident_exact(raw, banned):
        return ""
    hit = OEM.get(raw.lower())
    if hit:
        return hit
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,&'+-]{0,80}", raw):
        return OTHER
    return ""


def firmware_vendor(value, banned):
    """Firmware vendor enumeration. Version and date are not accepted."""
    raw = raw_scalar(value)
    if not raw or raw.lower() in DMI_PLACEHOLDERS:
        return ""
    if secret_line(raw) or ident_exact(raw, banned):
        return ""
    hit = FW_VENDOR.get(raw.lower())
    if hit:
        return hit
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,&'+-]{0,80}", raw):
        return OTHER
    return ""


def valid_build(value, banned):
    raw = raw_scalar(value)
    if raw in LAPIS_BUILDS and not ident_exact(raw, banned) and not secret_line(raw):
        return raw
    return ""


def valid_kernel(value, banned):
    """Print a release only when it exactly equals one this tree ships.
    A dev-tree release (uname equal to kernel.release) is dev-kernel.
    The release text itself is not printed."""
    raw = raw_scalar(value)
    if not raw or secret_line(raw):
        return ""
    shaped = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", raw)
    if raw in PINNED_KERNELS:
        if ident_exact(raw, banned) or kernel_embeds_banned(raw, banned):
            return OTHER if shaped else ""
        return raw
    dev = kernel_release_text()
    if dev and raw == dev:
        return dev_kernel_value() if shaped else ""
    if ident_exact(raw, banned) or kernel_embeds_banned(raw, banned):
        return OTHER if shaped else ""
    if shaped:
        return OTHER
    return ""


def valid_iface(value, banned):
    """wlan0-wlan9 or eth0-eth9. Any other interface name is not reported."""
    raw = raw_scalar(value)
    if not raw or secret_line(raw) or ident_exact(raw, banned):
        return ""
    if re.fullmatch(r"wlan[0-9]", raw) or re.fullmatch(r"eth[0-9]", raw):
        return raw
    return ""


def valid_profile(value, banned):
    raw = raw_scalar(value)
    if not raw or secret_line(raw) or ident_exact(raw, banned):
        return ""
    return PROFILES.get(raw.lower(), "")


def valid_status(value, banned):
    raw = raw_scalar(value)
    if not raw or secret_line(raw) or ident_exact(raw, banned):
        return ""
    return PROFILE_STATUS.get(raw.lower(), "")


def valid_pci(value):
    raw = raw_scalar(value)
    if not raw:
        return ""
    low = raw.lower()
    if not PCI_RE.fullmatch(low):
        return ""
    return low


def canon_name(value, allowed):
    raw = raw_scalar(value)
    if not raw:
        return ""
    for name in allowed:
        if raw == name or raw.lower() == name.lower():
            return name
    return ""


def enum_driver(value, allowed, banned):
    """Known driver, or other. A secret is dropped. The raw name is never kept."""
    raw = raw_scalar(value)
    if not raw:
        return ""
    if secret_line(raw) or ident_exact(raw, banned):
        return ""
    name = canon_name(raw, allowed)
    if name:
        return name
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", raw):
        return OTHER
    return ""


def valid_num(value):
    """A 1-6 digit counter or CPU field. Those digits are not firewall words, so a serial of the same digits stays beside the number."""
    raw = raw_scalar(value) if isinstance(value, str) else ""
    if not re.fullmatch(r"\d{1,6}", raw):
        return ""
    if secret_line(raw):
        return ""
    return raw


def cpu_vendor_enum(value, banned):
    raw = raw_scalar(value)
    if not raw or secret_line(raw) or ident_exact(raw, banned):
        return ""
    if raw == "GenuineIntel":
        return "GenuineIntel"
    if raw == "AuthenticAMD":
        return "AuthenticAMD"
    if re.fullmatch(r"[A-Za-z0-9!]{1,16}", raw):
        return OTHER
    return ""


def hex4(value):
    """Exactly 1-4 hex digits, or 0x plus those digits. A longer token is not a prefix."""
    raw = raw_scalar(value) if isinstance(value, str) else ""
    if not raw:
        return ""
    if re.fullmatch(r"0[xX][0-9a-fA-F]{1,4}", raw):
        raw = raw[2:]
    elif not re.fullmatch(r"[0-9a-fA-F]{1,4}", raw):
        return ""
    return raw.lower().zfill(4)


def add_token(out, value):
    """Keep the exact identifier. Do not strip it into a different token."""
    if not isinstance(value, str):
        return
    v = value.replace("\r\n", "\n").replace("\r", "\n")
    if v.endswith("\n"):
        v = v[:-1]
    if v == "" or "\n" in v or v in out:
        return
    low = v.lower()
    if low in ("unknown", "0", "none", "null", "n/a") or low in DEFAULT_HOSTS:
        return
    out.append(v)


def ident_key(key):
    return re.search(
        r"(?i)serial|sno|android_id|hostname|macaddr|wifimac|imei|meid|(^|[._])host$", key or "")


def collect_banned(getprop, uname, version, cpu_text, serial, env_serial, probe_data=None):
    banned = []
    add_token(banned, serial)
    add_token(banned, env_serial)
    for m in re.finditer(r"(?m)^\[([^\]]+)\]: \[(.*)\]\s*$", getprop or ""):
        key, val = m.group(1), m.group(2)
        if ident_key(key) or key in ("ro.build.host", "net.hostname"):
            add_token(banned, val)
    parts = (uname or "").split()
    if len(parts) >= 2 and parts[0] == "Linux":
        add_token(banned, parts[1])
    um = re.search(r"\(([^)@\s]+)@([^)\s]+)\)", version or "")
    if um:
        add_token(banned, um.group(2))
    for key, val in first_cpu_block(cpu_text).items():
        if ident_key(key):
            add_token(banned, val)

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and ident_key(str(k)):
                    add_token(banned, v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(probe_data)
    return banned


def first_cpu_block(text):
    vals = {}
    started = False
    for line in (text or "").splitlines():
        if not line.strip():
            if started:
                break
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        vals.setdefault(k.strip(), v)
        started = True
    return vals


def format_cpu_fields(vendor, family, model, stepping, banned):
    """Vendor enum plus family, model, and stepping. The marketing name is not used."""
    ven = cpu_vendor_enum(field_raw(vendor), banned)
    fam = valid_num(field_raw(family))
    mod = valid_num(field_raw(model))
    step = valid_num(field_raw(stepping))
    bits = []
    labels = CPU_LABELS
    if ven:
        bits.append(ven)
    if fam:
        bits.append(labels[0] + " " + fam)
    if mod:
        bits.append(labels[1] + " " + mod)
    if step:
        bits.append(labels[2] + " " + step)
    return " ".join(bits), (ven, fam, mod, step)


def cpu_from_text(text, banned):
    vals = first_cpu_block(text)
    if not vals:
        return "", ("", "", "", "")
    return format_cpu_fields(
        vals.get("vendor_id", ""), vals.get("cpu family", ""), vals.get("model", ""),
        vals.get("stepping", ""), banned)


def json_text(value):
    return value if isinstance(value, str) else ""


def json_num(value):
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    return ""


def cpu_from_probe(cpu, banned):
    if not isinstance(cpu, dict):
        return "", ("", "", "", "")
    return format_cpu_fields(
        json_text(cpu.get("vendor_id")), json_num(cpu.get("cpu family")),
        json_num(cpu.get("model")), json_num(cpu.get("stepping")), banned)


def parse_kernel(uname, version, banned):
    rel = ""
    parts = (uname or "").split()
    if len(parts) >= 3 and parts[0] == "Linux":
        rel = parts[2]
    if not rel:
        m = re.search(r"Linux version (\S+)", version or "")
        if m:
            rel = m.group(1)
    return valid_kernel(rel, banned)


def pci_hex(text):
    raw = raw_scalar(text)
    m = re.fullmatch(r"(?i)(?:0x)?([0-9a-f]{4})", raw)
    return m.group(1).lower() if m else ""


def sysfs_value(text):
    val = raw_scalar(text)
    if not val:
        return ""
    if re.search(r"(?i)permission denied|no such file|not found|timed out|not a directory", val):
        return ""
    return val


def read_rc(raw_dir, id_):
    p = Path(raw_dir) / f"{id_}.rc"
    if not p.is_file():
        return 1
    v = raw_scalar(read_text(p))
    if not re.fullmatch(r"-?\d+", v):
        return 1
    return int(v)


def capture_status(rc, stdout, stderr):
    """ok, unreadable, or unknown. A missing capture is unknown, never absent."""
    if rc == 0:
        return "ok"
    blob = f"{stderr or ''}\n{stdout or ''}"
    if re.search(r"(?i)not captured", blob):
        return "unknown"
    if re.search(r"(?i)device '.+' not found|device offline|unauthorized|timed out|command timed out", blob):
        return "unreadable"
    if re.search(r"(?i)permission denied", blob):
        return "unreadable"
    if re.search(r"(?i)no such file|does not exist|not a directory|not found", blob):
        return "unknown"
    return "unreadable"


def uevent_map(text):
    """KEY=value with the value exactly as written. Spaces are not removed before the validator."""
    out = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if not k or k != k.strip() or re.search(r"\s", k):
            continue
        out[k] = v
    return out


def indexed_name(prefix, text, width):
    """prefix plus a decimal with no leading zero. card00, hci00, and renderD001 are not names."""
    if not isinstance(text, str):
        return False
    m = re.fullmatch(re.escape(prefix) + r"([0-9]{1,%d})" % width, text)
    if not m:
        return False
    num = m.group(1)
    return len(num) == 1 or not num.startswith("0")


def dri_nodes(text):
    """Every cardN and renderD node. A token that only looks like one is unread, and is not printed."""
    seen = []
    bad = False
    for line in (text or "").splitlines():
        for n in line.split():
            if indexed_name("card", n, 2) or indexed_name("renderD", n, 3):
                if n not in seen:
                    seen.append(n)
            elif re.match(r"(?i)(?:card|renderD)", n):
                bad = True
    return seen, bad


def listed_card_indexes(nodes):
    """cardN names from a successfully read DRM list. renderD nodes are not cards."""
    found = set()
    for name in nodes or []:
        if indexed_name("card", name, 2):
            found.add(int(name[4:]))
    return found


def load_cards(texts, stderrs, rcs, banned, only_indexes):
    """Card sysfs counts only for indexes named by a successfully captured DRM list.
    Any other capture state passes an empty set, so no card row is built."""
    cards = []
    unreadable = False
    for index, ven_id, dev_id, ue_id in CARD_SLOTS:
        if index not in only_indexes:
            continue
        ids = (ven_id, dev_id, ue_id)
        if not any(cid in rcs or cid in texts for cid in ids):
            continue
        statuses = [capture_status(rcs.get(cid, 0), texts.get(cid, ""), stderrs.get(cid, "")) for cid in ids]
        # Nothing was collected for this slot. The DRM listing decides if it was required.
        if all(s == "unknown" for s in statuses):
            continue
        # A missing uevent on a listed card is unknown on the merge key. Permission denied is too.
        # Vendor and device may be absent when the uevent that was read is a platform shim.
        if statuses[2] != "ok" or any(s == "unreadable" for s in statuses):
            unreadable = True
        ue = uevent_map(texts.get(ue_id, "")) if statuses[2] == "ok" else {}
        raw_driver = ue.get("DRIVER", "") if statuses[2] == "ok" else ""
        driver = enum_driver(raw_driver, GPU_DRIVERS, banned)
        pci = valid_pci(ue.get("PCI_ID") or "")
        if not pci and statuses[0] == "ok" and statuses[1] == "ok":
            ven_raw = sysfs_value(texts.get(ven_id, ""))
            dev_raw = sysfs_value(texts.get(dev_id, ""))
            v, d = pci_hex(ven_raw), pci_hex(dev_raw)
            pci = valid_pci(f"{v}:{d}") if v and d else ""
        if not driver and not pci:
            continue
        if driver in REAL_GPU:
            kind = "real"
        elif driver in SHIM_GPU or driver == "vboxvideo":
            kind = "shim"
        else:
            kind = "unknown"
        cards.append({"index": index, "driver": driver, "pci": pci, "kind": kind})
    return cards, unreadable


def gpu_verdict(cards, swift, unreadable, dri_status):
    real = [c for c in cards if c["kind"] == "real" and c["driver"]]
    shims = [c for c in cards if c["kind"] == "shim" and c["driver"]]
    named_unknown = [c for c in cards if c["kind"] == "unknown" and c["driver"]]
    pci_only = [c for c in cards if not c["driver"] and c["pci"]]
    # An unread card, or a DRM list that was not successfully read, is unknown.
    # A successfully read empty list stays missing here so the caller can still merge.
    if unreadable or dri_status in ("unreadable", "unknown"):
        return "unknown", "未知", "unknown"
    if real:
        names = []
        for c in real:
            if c["driver"] not in names:
                names.append(c["driver"])
        label = ",".join(names)
        return f"{GPU_BOUND_EN} ({label})", f"{GPU_BOUND_ZH}（{label}）", "known"
    if pci_only:
        label = ",".join(c["pci"] for c in pci_only)
        return f"unknown ({label})", f"未知（{label}）", "unknown"
    if named_unknown:
        bits = []
        for c in named_unknown:
            if c["pci"] and c["pci"] not in bits:
                bits.append(c["pci"])
        for c in named_unknown:
            if c["driver"] and c["driver"] not in bits:
                bits.append(c["driver"])
        label = ", ".join(bits) if bits else OTHER
        return f"unknown ({label})", f"未知（{label}）", "unknown"
    # SwiftShader alone, or an empty card list, is not "no native driver".
    if not cards:
        return "", "", "missing"
    if shims:
        # egl and vulkan are inputs only on the shim path. A real GPU never reads them.
        bits = []
        if swift:
            bits.append(SWIFTSHADER_LABEL)
        for c in shims:
            if c["driver"] not in bits:
                bits.append(c["driver"])
        extra = ", ".join(bits) if bits else DRM_SHIM
        return f"{GPU_NONE_EN} ({extra})", f"{GPU_NONE_ZH}（{extra}）", "shim"
    return "", "", "missing"


def drm_unread(cards, card_unreadable, dri_status):
    """An unread card is incomplete. A read platform shim may have no PCI id."""
    if card_unreadable or dri_status in ("unreadable", "unknown"):
        return True
    for c in cards:
        if c.get("pci"):
            continue
        drv = c.get("driver") or ""
        if drv in SHIM_GPU or drv == "vboxvideo":
            continue
        return True
    return False


def input_kind_name(name):
    low = (name or "").lower()
    if "touchpad" in low or "trackpad" in low:
        return "touchpad"
    if "stylus" in low or re.search(r"\bpen\b", low):
        return "stylus"
    if "touchscreen" in low or re.search(r"\btouch\b", low):
        return "touchscreen"
    return ""


CLASS_HEX_RE = re.compile(r"0x[0-9A-Fa-f]{1,8}\Z")
CLASS_SYM_RE = re.compile(r"[A-Z][A-Z0-9_]*(?: \| [A-Z][A-Z0-9_]*)*\Z")
SOURCE_HEX_RE = re.compile(r"0x[0-9A-Fa-f]+(?:[ \t]+0x[0-9A-Fa-f]+)*\Z")


def colon_token(raw):
    """The bytes after ':'. One leading space is the delimiter. Extra space or a suffix is empty."""
    if not isinstance(raw, str) or not raw.startswith(" ") or raw.startswith("  "):
        return None
    value = raw[1:]
    if value == "" or value != value.strip() or "\n" in value:
        return None
    return value


def class_bits_from_value(value):
    """A whole Classes token: 0xHEX, or SYMBOL (| SYMBOL)*. None when the raw value is not that."""
    if value is None:
        return None
    if CLASS_HEX_RE.fullmatch(value):
        return int(value, 16)
    if not CLASS_SYM_RE.fullmatch(value):
        return None
    bits = 0
    for tok in value.split(" | "):
        if tok == "TOUCHPAD":
            bits |= CLASS_TOUCHPAD
        elif tok == "TOUCH_MT":
            bits |= CLASS_TOUCH_MT
        elif tok == "TOUCH":
            bits |= CLASS_TOUCH
        elif tok in ("STYLUS", "EXTERNAL_STYLUS"):
            bits |= CLASS_STYLUS
    return bits


def sources_from_value(value):
    """A whole Sources token: one or more 0xHEX fields. A suffix is not a source."""
    if value is None or not SOURCE_HEX_RE.fullmatch(value):
        return None
    return [int(part, 16) for part in value.split()]


def kind_from_bits(class_bits, sources):
    if class_bits & CLASS_TOUCHPAD:
        return "touchpad"
    if class_bits & CLASS_STYLUS:
        return "stylus"
    if class_bits & (CLASS_TOUCH | CLASS_TOUCH_MT):
        return "touchscreen"
    for src in sources:
        if (src & 0x00100008) == 0x00100008:
            return "touchpad"
        if (src & 0x00004002) == 0x00004002 or (src & 0x00008002) == 0x00008002:
            return "stylus"
        if (src & 0x00001002) == 0x00001002:
            return "touchscreen"
    return ""


def bus_label(bus):
    raw = raw_scalar(bus) if isinstance(bus, str) else ""
    if not raw:
        return ""
    b = raw.lower()
    if re.fullmatch(r"0x[0-9a-f]{1,4}", b):
        b = b[2:]
    if re.fullmatch(r"[0-9a-f]{1,4}", b):
        return BUSNAME.get(b.zfill(4), "")
    if b in BUSNAME.values():
        return b
    return ""


def parse_input_dumpsys(text):
    found = []
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        m = HEADER_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        name = m.group(2).strip()
        if re.match(r"(?i)^(classes|sources|generation|descriptor|uniq|path|identifier)\b", name):
            i += 1
            continue
        body = []
        i += 1
        while i < len(lines) and not HEADER_RE.match(lines[i].strip()):
            body.append(lines[i])
            i += 1
        blob = "\n".join(body)
        class_lines = re.findall(r"(?m)^[ \t]*Classes:(.*)$", blob)
        class_bits = 0
        saw_class = False
        for rest in class_lines:
            raw_class = colon_token(rest)
            got = class_bits_from_value(raw_class)
            if got is None:
                continue
            saw_class = True
            class_bits |= got
        source_lines = re.findall(r"(?m)^[ \t]*Sources:(.*)$", blob)
        sources = []
        saw_sources = False
        for rest in source_lines:
            raw_src = colon_token(rest)
            got = sources_from_value(raw_src)
            if got is None:
                continue
            saw_sources = True
            sources.extend(got)
        # An illegal token contributes nothing. A valid sibling line still does.
        from_bits = ""
        if saw_class or saw_sources:
            from_bits = kind_from_bits(class_bits, sources)
        kind = from_bits or input_kind_name(name)
        rank = 3 if from_bits else (2 if input_kind_name(name) else 0)
        if not kind:
            continue
        vid = pid = bus = ""
        im = re.search(
            r"(?i)bus=0x([0-9a-f]{1,4})(?=$|[, \t\n])[, \t]+vendor=0x([0-9a-f]{1,4})(?=$|[, \t\n])[, \t]+product=0x([0-9a-f]{1,4})(?=$|[, \t\n])",
            blob)
        if im:
            bus = im.group(1).lower()
            vid = hex4(im.group(2))
            pid = hex4(im.group(3))
        found.append({"kind": kind, "bus": bus, "vid": vid, "pid": pid, "rank": rank, "driver": "", "name": name})
    return found


def abs_bit(hexstr, bit):
    try:
        return (int(hexstr, 16) >> bit) & 1 == 1
    except ValueError:
        return False


def parse_input_proc(text):
    found = []
    for block in re.split(r"\n\s*\n", text or ""):
        nm = re.search(r'(?m)^N: Name="([^"]*)"', block)
        if not nm:
            continue
        name = nm.group(1)
        if name != name.strip():
            name = ""
        else:
            name = name.strip()
        bus = vid = pid = ""
        im = re.search(r"(?m)^I: (.*)$", block)
        if im:
            raw_i = im.group(1)
            if raw_i == raw_i.strip() and "  " not in raw_i:
                for tok in raw_i.split(" "):
                    if tok.count("=") != 1:
                        continue
                    k, v = tok.split("=", 1)
                    if k == "Bus":
                        if re.fullmatch(r"[0-9A-Fa-f]{1,4}", v):
                            bus = v.lower()
                    elif k == "Vendor":
                        if re.fullmatch(r"[0-9A-Fa-f]{1,4}", v):
                            vid = hex4(v)
                    elif k == "Product":
                        if re.fullmatch(r"[0-9A-Fa-f]{1,4}", v):
                            pid = hex4(v)
        handlers = ""
        hm = re.search(r"(?m)^H: Handlers=(.*)$", block)
        if hm:
            handlers = hm.group(1)
        kind = input_kind_name(name)
        rank = 2 if kind else 0
        if not kind and bus_label(bus) == "i2c" and re.search(r"\bmouse\d*", handlers):
            kind, rank = "touchpad", 1
        if not kind:
            am = re.search(r"(?m)^B: ABS=([0-9A-Fa-f]+)[ \t]*$", block)
            if am and (abs_bit(am.group(1), 0x35) or abs_bit(am.group(1), 0x2F)) and not re.search(r"\bmouse\d*", handlers):
                kind, rank = "touchscreen", 1
        if not kind and not (vid and pid):
            continue
        found.append({"kind": kind, "bus": bus, "vid": vid, "pid": pid, "rank": rank, "driver": "", "name": name})
    return found


def attach_named_ids(items):
    """Same device name, ids from the record that actually has them. The name itself is not parsed for an id."""
    known = {}
    for item in items:
        name = item.get("name") or ""
        vid, pid = item.get("vid") or "", item.get("pid") or ""
        if name and vid and pid and name not in known:
            known[name] = (vid, pid, item.get("bus") or "")
    out = []
    for item in items:
        name = item.get("name") or ""
        if name and not (item.get("vid") and item.get("pid")) and name in known:
            vid, pid, bus = known[name]
            item = dict(item)
            item["vid"], item["pid"] = vid, pid
            if bus and not item.get("bus"):
                item["bus"] = bus
        out.append(item)
    return out


def merge_inputs(items):
    """Same vid:pid and type merge. A different type stays. An empty bus is filled, not used as a new row."""
    groups = []
    pending = []

    def fill(cur, item, bus):
        b1 = cur.get("bus") or ""
        if b1 and bus and b1 != bus:
            return False
        if bus and not b1:
            cur["bus"] = bus
        if item.get("driver") and not cur.get("driver"):
            cur["driver"] = item["driver"]
        if item.get("kind") and item.get("rank", 0) >= cur.get("rank", 0):
            cur["rank"] = item.get("rank", 0)
        return True

    for item in items:
        vid, pid = hex4(item.get("vid") or ""), hex4(item.get("pid") or "")
        kind = item.get("kind") or ""
        bus = item.get("bus") or ""
        if vid and pid and not kind:
            pending.append(dict(item, vid=vid, pid=pid, bus=bus))
            continue
        if not kind:
            continue
        placed = False
        if vid and pid:
            for cur in groups:
                if cur.get("vid") != vid or cur.get("pid") != pid or cur.get("kind") != kind:
                    continue
                if fill(cur, item, bus):
                    placed = True
                    break
        if not placed:
            groups.append(dict(item, vid=vid, pid=pid, kind=kind, bus=bus))
    for item in pending:
        for cur in groups:
            if cur.get("vid") == item["vid"] and cur.get("pid") == item["pid"]:
                fill(cur, item, item.get("bus") or "")
    return groups


def format_inputs(items, banned):
    seen = set()
    en_parts = []
    zh_parts = []
    for kind in KIND_EN:
        for item in items:
            if item.get("kind") != kind:
                continue
            vid, pid = hex4(item.get("vid") or ""), hex4(item.get("pid") or "")
            label = f"{vid}:{pid}" if vid and pid else "unknown"
            if label != "unknown" and (secret_line(label) or ident_exact(label, banned) or not PCI_RE.fullmatch(label)):
                label = "unknown"
            bus = bus_label(item.get("bus") or "")
            drv = enum_driver(item.get("driver") or "", INPUT_DRIVERS, banned)
            key = (kind, label, bus, drv)
            if key in seen:
                continue
            seen.add(key)
            bus_en = f" ({bus})" if bus else ""
            bus_zh = f"（{bus}）" if bus else ""
            drv_en = f" {DRIVER_WORD} {drv}" if drv else ""
            drv_zh = f" {DRIVER_WORD_ZH} {drv}" if drv else ""
            en_parts.append(f"{kind}: {label}{bus_en}{drv_en}")
            zh_parts.append(f"{KIND_ZH[kind]}：{label}{bus_zh}{drv_zh}")
    return "; ".join(en_parts), "；".join(zh_parts)


def wifi_current(text):
    """Cut at history. A colon is not a word boundary, so History: is matched on its own."""
    m = re.search(
        r"(?im)^[ \t]*(?:(?:mNetworkHistory|NetworkHistory|Latest scan|Scan Results|mScanResults|WifiScoreCard)\b|History:[ \t]*$)",
        text or "")
    if not m:
        return text or ""
    return text[:m.start()]


def wifi_state(dumpsys, operstate):
    """Current-window fields only. Any disagreement is unknown. History is already cut off."""
    raw = dumpsys or ""
    text = wifi_current(raw)
    op = (operstate or "").lower()
    if op not in ("", "up", "down"):
        op = "bad" if op else ""
    if not raw.strip() and not op:
        return ""
    idx = text.find("mWifiInfo")
    radio_zone = text[:idx] if idx >= 0 else text
    radios = [r.lower() for r in re.findall(r"(?i)(?<!\S)Wi-Fi is (enabled|disabled)(?!\S)", radio_zone)]
    if re.search(r"(?i)(?<!\S)Wi-Fi is (enabled|disabled)\S", radio_zone):
        return "unknown"
    if len(set(radios)) > 1:
        return "unknown"
    radio = radios[0] if radios else ""
    curs = []
    for m in re.finditer(r"curState=", text):
        line_rest = text[m.end():].split("\n", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9_]+", line_rest):
            return "unknown"
        curs.append(line_rest)
    if len(set(curs)) > 1:
        return "unknown"
    cur = curs[0] if curs else ""
    supps = []
    for m in re.finditer(r"Supplicant state: ", text):
        mm = re.match(r"([A-Za-z0-9_]+)(?=$|[, \t\n])", text[m.end():])
        if not mm:
            return "unknown"
        supps.append(mm.group(1).upper())
    if len(set(supps)) > 1:
        return "unknown"
    supp = supps[0] if supps else ""
    down_supp = {"DISCONNECTED", "INACTIVE", "SCANNING", "DORMANT", "UNINITIALIZED", "INVALID", "INTERFACE_DISABLED"}
    supp_up = supp == "COMPLETED"
    supp_down = supp in down_supp
    cur_up = cur == "ConnectedState"
    cur_down = cur in ("DisconnectedState", "DisabledState")
    link_up = supp_up or cur_up
    hard_down = radio == "disabled" or cur == "DisabledState" or supp == "INTERFACE_DISABLED"
    if (supp_up and cur_down) or (supp_down and cur_up) or (supp_up and radio == "disabled") or (cur_up and radio == "disabled"):
        return "unknown"
    if radio == "enabled" and (cur == "DisabledState" or supp == "INTERFACE_DISABLED"):
        return "unknown"
    if op == "down" and link_up:
        return "unknown"
    if op == "up" and hard_down:
        return "unknown"
    if op == "bad":
        return "unknown"
    if supp_up or (cur_up and not supp):
        return "connected"
    if radio == "disabled" or supp == "INTERFACE_DISABLED" or cur == "DisabledState":
        return "disabled"
    if supp_down or cur_down:
        return "enabled"
    if radio == "enabled":
        return "link-unknown"
    if op == "up":
        return "up"
    if op == "down":
        return "down"
    if text.strip() or op:
        return "unknown"
    return ""


def bt_status_block(text):
    lines = (text or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"(?i)^Bluetooth Status\s*$", line.strip()):
            start = i + 1
            break
    if start is None:
        return ""
    block = []
    for line in lines[start:]:
        if line.strip() and not line[:1].isspace():
            break
        block.append(line)
    return "\n".join(block)


def bt_state(dumpsys_bt, getprop, hci_ls):
    block = bt_status_block(dumpsys_bt or "")
    enabled = []
    states = []
    for line in block.splitlines():
        m = re.fullmatch(r"([ \t]*)enabled: (.*)", line, re.I)
        if m:
            if m.group(2).lower() in ("true", "false"):
                enabled.append((len(m.group(1).expandtabs(4)), m.group(2).lower()))
        sm = re.fullmatch(r"([ \t]*)state: (.*)", line, re.I)
        if sm:
            if sm.group(2).upper() not in ("ON", "OFF", "TURNING_ON", "TURNING_OFF"):
                continue
            sm = re.fullmatch(r"([ \t]*)state: (ON|OFF|TURNING_ON|TURNING_OFF)", line, re.I)
        if sm:
            states.append((len(sm.group(1).expandtabs(4)), sm.group(2).upper()))
    power = ""
    if enabled:
        shallow = min(ind for ind, _val in enabled)
        vals = [val for ind, val in enabled if ind == shallow]
        if len(set(vals)) > 1:
            return "unknown"
        power = "up" if vals[0] == "true" else "down"
    state = ""
    if states:
        shallow = min(ind for ind, _val in states)
        vals = [val for ind, val in states if ind == shallow]
        if len(set(vals)) > 1:
            return "unknown"
        state = vals[0]
    if state == "TURNING_ON":
        return "turning-on"
    if state == "TURNING_OFF":
        return "turning-off"
    stable = "up" if state == "ON" else ("down" if state == "OFF" else "")
    if power and stable and power != stable:
        return "unknown"
    if power or stable:
        return power or stable
    if re.search(r"(?m)^hci\d+\s*$", hci_ls or ""):
        return "present"
    m = re.search(r"(?m)^\[init\.svc\.bluetooth\]: \[(running|stopped)\]$", getprop or "")
    if m:
        return "service" if m.group(1) == "running" else "stopped"
    return ""


def sleep_counts(power, success, fail, power_rc, success_rc, fail_rc):
    from_power = None
    if power_rc == 0:
        sm = re.search(r"(?m)^Suspend Success Count:(.*)$", power or "")
        fm = re.search(r"(?m)^Suspend Failure Count:(.*)$", power or "")
        if sm and fm:
            s = valid_num(field_raw(sm.group(1)))
            f = valid_num(field_raw(fm.group(1)))
            if s and f:
                from_power = (s, f)
    from_sys = None
    if success_rc == 0 and fail_rc == 0:
        s, f = valid_num(sysfs_value(success)), valid_num(sysfs_value(fail))
        if s and f:
            from_sys = (s, f)
    if from_power and from_sys and from_power != from_sys:
        return None
    return from_power or from_sys


def draft_fields(text):
    def grab(key):
        m = re.search(rf'(?m)^{key} = "([^"]*)"', text or "")
        return m.group(1) if m else ""
    return grab("id"), grab("status")


def soft_dict(data, key):
    if not isinstance(data, dict):
        return {}
    val = data.get(key)
    return val if isinstance(val, dict) else {}


def soft_list(data, key):
    if not isinstance(data, dict):
        return []
    val = data.get(key)
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, dict)]


def load_probe(path):
    if not path:
        return None
    root = Path(path)
    raw = read_text(root / "probe.json")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"probe.json is not JSON ({e.msg})")
    if not isinstance(data, dict):
        fail("probe.json must be a JSON object")
    report = ""
    report_path = root / "report.txt"
    if report_path.exists() or report_path.is_symlink():
        if report_path.is_symlink():
            fail("refusing symlink in probe dir: report.txt")
        if report_path.is_file():
            report = read_text(report_path)
    draft = ""
    draft_path = root / "profile.draft.toml"
    if draft_path.exists() or draft_path.is_symlink():
        if draft_path.is_symlink():
            fail("refusing symlink in probe dir: profile.draft.toml")
        if draft_path.is_file():
            draft = read_text(draft_path)
    return data, report, draft


def flags_from_report(text):
    m = re.search(r"(?m)^firmware:\s*(.{0,300})$", text or "")
    if not m:
        return ""
    line = m.group(1)
    parts = []
    # The token must end at a separator. A suffix such as yes@example.test is not yes.
    u = re.search(r"(?:^|[;,]\s*)UEFI (yes|no)(?=\s*(?:,|;|$))", line, re.I)
    s = re.search(r"(?:^|[;,]\s*)Secure Boot (on|off|enabled|disabled)(?=\s*(?:,|;|$))", line, re.I)
    if u:
        parts.append("UEFI " + u.group(1).lower())
    if s:
        parts.append("Secure Boot " + s.group(1).lower())
    return ", ".join(parts)


def firmware_flags(data, report_txt):
    fw = data.get("firmware") if isinstance(data, dict) else None
    if isinstance(fw, dict):
        parts = []
        uefi = fw.get("uefi")
        if isinstance(uefi, bool):
            parts.append("UEFI yes" if uefi else "UEFI no")
        sb = fw.get("secure_boot")
        if isinstance(sb, str) and sb.lower() in ("on", "off", "enabled", "disabled"):
            parts.append("Secure Boot " + sb.lower())
        if parts:
            return ", ".join(parts)
    return flags_from_report(report_txt)


def dmi_map(raw_get, banned):
    """Vendor enumerations only. Product, board, BIOS, and EC strings are not report fields."""
    return {
        "sys_vendor": vendor_enum(raw_get("sys_vendor"), banned),
        "board_vendor": vendor_enum(raw_get("board_vendor"), banned),
        "bios_vendor": firmware_vendor(raw_get("bios_vendor"), banned),
    }


def parse_pci_devices(text):
    """/proc/bus/pci/devices: 4 hex, tab, 8 hex vendor+device. No strip. One bad line is an unread bus."""
    ids = []
    for line in (text or "").splitlines():
        if line == "":
            continue
        fields = line.split("\t")
        if len(fields) < 2 or not re.fullmatch(r"[0-9a-fA-F]{4}", fields[0]) or not re.fullmatch(r"[0-9a-fA-F]{8}", fields[1]):
            return None
        ids.append(fields[1][:4].lower() + ":" + fields[1][4:].lower())
    return ids


def probe_pci_ids(data):
    """Every probe PCI id, duplicates kept.
    A missing key, a null, an unread slot, or an invalid entry is an incomplete bus.
    An empty list is a bus that was read and had no devices."""
    if not isinstance(data, dict) or "pci" not in data:
        return None
    items = data.get("pci")
    if not isinstance(items, list):
        return None
    ids = []
    for item in items:
        if not isinstance(item, dict) or item.get("unread") is True or "id" not in item or "class" not in item:
            return None
        pid = valid_pci(json_text(item.get("id")))
        cls = item.get("class")
        if not pid or not isinstance(cls, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", cls):
            return None
        ids.append(pid)
    return ids


def bus_id_from_uevent(ue):
    """PCI_ID, or a USB PRODUCT vid/pid. The value is the whole token."""
    pci = valid_pci(ue.get("PCI_ID") or "")
    if pci:
        return "pci " + pci
    prod = ue.get("PRODUCT") or ""
    # The bcd field is not a report token. A missing third field, an empty bcd,
    # a blank bcd, or any other bcd must not hide a valid vid:pid.
    m = re.fullmatch(r"([0-9A-Fa-f]{1,4})/([0-9A-Fa-f]{1,4})(?:/(.*))?", prod)
    if not m:
        return ""
    vid, pid = hex4(m.group(1)), hex4(m.group(2))
    if not (vid and pid):
        return ""
    return "usb " + vid + ":" + pid


def radio_driver(ue, allowed, banned):
    """A known driver stays a name. other keeps the PCI or USB id when that id was actually read."""
    name = enum_driver(ue.get("DRIVER", ""), allowed, banned)
    if name != "other":
        return name
    extra = bus_id_from_uevent(ue)
    if extra:
        return OTHER + ", " + extra
    return OTHER


def machine_key(vendor, parts, pcis, complete):
    """DMI vendor enum, CPU family/model/stepping, and the full PCI id list. Duplicates stay."""
    if not complete or not vendor or pcis is None:
        return ""
    ven, fam, mod, step = parts
    if not (ven and fam and mod and step):
        return ""
    return vendor + "\n" + "|".join((ven, fam, mod, step)) + "\n" + ",".join(sorted(pcis))


def render(out_dir, date, raw_dir, probe_dir, source, serial):
    texts, stderrs, rcs = {}, {}, {}
    manifest = Path(raw_dir) / "manifest.tsv"
    if manifest.is_file():
        for line in read_text(manifest).splitlines():
            if not line.strip():
                continue
            id_, _cmd = line.split("\t", 1)
            stdout_path = Path(raw_dir) / f"{id_}.txt"
            err_path = Path(raw_dir) / f"{id_}.err"
            texts[id_] = read_text(stdout_path) if stdout_path.is_file() else ""
            stderrs[id_] = read_text(err_path) if err_path.is_file() else ""
            rcs[id_] = read_rc(raw_dir, id_)
    probe = load_probe(probe_dir) if probe_dir else None
    data = report_txt = draft = None
    if probe:
        data, report_txt, draft = probe
    data = data or {}

    def ok_text(cid):
        if rcs.get(cid, 0) != 0:
            return ""
        return texts.get(cid, "")

    adb_source = source in ("adb", "fixture")
    here = "fixture" if source == "fixture" else ("adb" if source == "adb" else "")
    getprop = ok_text("getprop")
    uname = ok_text("uname-a")
    version = ok_text("cat-proc-version")
    cpu_text = ok_text("cat-proc-cpuinfo")
    # Vocabulary words are this report's own text. What remains is device-unique.
    banned = [token for token in collect_banned(
        getprop, uname, version, cpu_text, serial, os.environ.get("ANDROID_SERIAL", ""), data)
        if not is_report_word(token)]

    def raw_dmi(key, cid):
        return ok_text(cid)

    dmi_adb = dmi_map(lambda key: raw_dmi(key, {
        "sys_vendor": "cat-dmi-sys-vendor",
        "product_name": "cat-dmi-product-name",
        "board_vendor": "cat-dmi-board-vendor",
        "board_name": "cat-dmi-board-name",
        "bios_vendor": "cat-dmi-bios-vendor",
        "bios_version": "cat-dmi-bios-version",
        "bios_date": "cat-dmi-bios-date",
    }[key]), banned) if adb_source else {}
    dmi_probe_raw = soft_dict(data, "dmi")
    dmi_probe = dmi_map(lambda key: json_text(dmi_probe_raw.get(key)), banned) if data else {}

    chosen = dmi_adb if adb_source else dmi_probe
    model = chosen.get("sys_vendor") or ""
    board = chosen.get("board_vendor") or ""
    firmware = chosen.get("bios_vendor") or ""
    if adb_source:
        model_origin = board_origin = fw_origin = here
    else:
        model_origin = board_origin = fw_origin = "probe" if data else ""

    omit_en, omit_zh = OMIT_EN, OMIT_ZH

    build = valid_build(prop_get(getprop, "ro.build.display.id"), banned) or valid_build(prop_get(getprop, "ro.build.id"), banned)
    kernel = parse_kernel(uname, version, banned) if adb_source else ""
    empty_cpu = ("", ("", "", "", ""))
    cpu, adb_parts = cpu_from_text(cpu_text, banned) if adb_source else empty_cpu
    cpu_probe, cpu_probe_parts = cpu_from_probe(soft_dict(data, "cpu"), banned) if data else empty_cpu
    if not adb_source and data:
        cpu = cpu_probe
    cpu_origin = here if adb_source else ("probe" if data else "")
    sound = ""
    probe_sound = ""
    gb_inputs = merge_inputs(attach_named_ids(
        parse_input_dumpsys(ok_text("dumpsys-input")) + parse_input_proc(ok_text("cat-proc-bus-input-devices"))))
    input_en, input_zh = format_inputs(gb_inputs, banned)
    probe_items = []
    for item in soft_list(data, "input"):
        kind_raw = json_text(item.get("kind"))
        kind = PROBE_INPUT_KINDS.get(kind_raw, "")
        if not kind:
            continue
        vid, pid = hex4(json_text(item.get("vendor"))), hex4(json_text(item.get("product")))
        probe_items.append({
            "kind": kind, "bus": json_text(item.get("bus")) or json_text(item.get("busname")),
            "vid": vid, "pid": pid, "rank": 2,
            "driver": json_text(item.get("driver")),
        })
    probe_input_en, probe_input_zh = format_inputs(merge_inputs(probe_items), banned)

    egl = prop_get(getprop, "ro.hardware.egl")
    vulkan = prop_get(getprop, "ro.hardware.vulkan")
    # pastel is this project's SwiftShader package (vulkan.pastel.so), not a second renderer.
    swift = raw_scalar(egl).lower() == "swiftshader" or raw_scalar(vulkan).lower() in SWIFT_TOKENS
    dri_status = "unknown"
    if "ls-dev-dri" in rcs:
        dri_status = capture_status(rcs.get("ls-dev-dri", 0), texts.get("ls-dev-dri", ""), stderrs.get("ls-dev-dri", ""))
    nodes, nodes_bad = ([], False)
    if dri_status == "ok":
        nodes, nodes_bad = dri_nodes(ok_text("ls-dev-dri"))
    # Per-card sysfs and uevent count only when a successfully captured list names that card.
    # A failed capture (missing, ENOENT, denied, or any other non-zero status) names nothing.
    only_indexes = listed_card_indexes(nodes) if dri_status == "ok" else set()
    cards, card_unreadable = load_cards(texts, stderrs, rcs, banned, only_indexes)
    if nodes_bad:
        card_unreadable = True
    parsed = {c["index"] for c in cards}
    for n in nodes:
        if indexed_name("card", n, 2) and int(n[4:]) not in parsed:
            card_unreadable = True
    dri_blocked = dri_status in ("unreadable", "unknown")
    gpu_en, gpu_zh, gpu_kind = gpu_verdict(
        cards, swift, card_unreadable or dri_blocked, dri_status)
    read_pcis = []
    for c in cards:
        if c["pci"] and c["pci"] not in read_pcis:
            read_pcis.append(c["pci"])
    gpu_blocked = drm_unread(cards, card_unreadable, dri_status)
    adb_pcis = None
    adb_pci_complete = False
    if adb_source and rcs.get("cat-proc-bus-pci-devices", 1) == 0:
        adb_pcis = parse_pci_devices(texts.get("cat-proc-bus-pci-devices", ""))
        adb_pci_complete = adb_pcis is not None
    drm_bits = []
    for c in cards:
        bits = [f"card{c['index']}"]
        if c["driver"]:
            bits.append("driver " + c["driver"])
        if c["pci"]:
            bits.append("pci " + c["pci"])
        if c["kind"] == "real" and not gpu_blocked:
            bits.append(DRM_REAL)
        elif c["kind"] == "shim":
            bits.append(DRM_SHIM)
        elif c["kind"] != "real" and c["driver"] != "other":
            bits.append(DRM_UNREAD)
        drm_bits.append(" ".join(bits))

    wifi = wifi_state(ok_text("dumpsys-wifi"), sysfs_value(ok_text("cat-wlan-operstate")))
    wifi_driver = radio_driver(uevent_map(ok_text("cat-wlan-uevent")), WIFI_DRIVERS, banned)
    bt = bt_state(ok_text("dumpsys-bluetooth"), getprop, ok_text("ls-bluetooth"))
    bt_driver = radio_driver(uevent_map(ok_text("cat-bt-uevent")), BT_DRIVERS, banned)
    slept = None
    if adb_source:
        slept = sleep_counts(
            ok_text("dumpsys-power"), texts.get("cat-suspend-success", ""), texts.get("cat-suspend-fail", ""),
            rcs.get("dumpsys-power", 1), rcs.get("cat-suspend-success", 1), rcs.get("cat-suspend-fail", 1))

    draft_id = draft_status = ""
    if draft:
        raw_id, raw_status = draft_fields(draft)
        draft_id = valid_profile(raw_id, banned)
        draft_status = valid_status(raw_status, banned)

    probe_pcis = probe_pci_ids(data) if probe_dir else None
    probe_pci_complete = probe_pcis is not None
    probe_gpus = []
    if probe_pci_complete:
        for p in soft_list(data, "pci"):
            cls = str(p.get("class") or "")
            if not cls.startswith("03"):
                continue
            pid = valid_pci(json_text(p.get("id")))
            drv = enum_driver(json_text(p.get("driver")), GPU_DRIVERS, banned)
            if pid or drv:
                probe_gpus.append((pid, drv))
    wl = []
    for n in soft_list(data, "net"):
        if n.get("wireless") is True:
            name = valid_iface(json_text(n.get("name")), banned)
            if name and name not in wl:
                wl.append(name)
    bt_names = []
    for b in soft_list(data, "bluetooth"):
        name = json_text(b.get("name"))
        if indexed_name("hci", name, 2) and name not in bt_names:
            bt_names.append(name)

    def radio_words(state, driver, table):
        """State and a validated driver stay independent. No driver does not invent a link."""
        if not state and not driver:
            return "", ""
        if state in table:
            en, zh = table[state]
        else:
            en, zh = "unknown", "未知"
        if driver:
            en += f", {DRIVER_WORD} {driver}"
            zh += f"，{DRIVER_WORD_ZH} {driver}"
        return en, zh

    def wifi_words(state, driver):
        return radio_words(state, driver, WIFI_WORDS)

    def bt_words(state, driver):
        return radio_words(state, driver, BT_WORDS)

    wifi_en, wifi_zh = wifi_words(wifi, wifi_driver)
    bt_en, bt_zh = bt_words(bt, bt_driver)
    if slept:
        sleep_en = f"{SLEEP_EN[0]} {slept[0]}, {SLEEP_EN[1]} {slept[1]}"
        sleep_zh = f"{SLEEP_ZH[0]} {slept[0]}、{SLEEP_ZH[1]} {slept[1]}"
    else:
        sleep_en = sleep_zh = ""

    merge_ok = True
    if probe_dir and adb_source:
        left = machine_key(dmi_adb.get("sys_vendor"), adb_parts, adb_pcis, adb_pci_complete and not gpu_blocked)
        right = machine_key(dmi_probe.get("sys_vendor"), cpu_probe_parts, probe_pcis, probe_pci_complete)
        merge_ok = bool(left) and left == right
    flags_en = flags_zh = ""
    flags_origin = ""
    if probe_dir:
        flags_origin = "probe"
        if merge_ok:
            flags = firmware_flags(data, report_txt or "")
            flags_en = flags_zh = flags
        else:
            flags_en, flags_zh = omit_en, omit_zh
    live_omit = bool(probe_dir and adb_source and not merge_ok)
    if live_omit:
        probe_gpus = []
        probe_sound = ""
        probe_input_en = probe_input_zh = ""
        wl = []
        bt_names = []
    compare_en = compare_zh = ""
    if data and adb_source:
        if not merge_ok:
            compare_en, compare_zh = omit_en, omit_zh
        else:
            compare_en, compare_zh = AGREE_EN, AGREE_ZH

    src = []
    if source == "fixture":
        src.append("fixture")
    elif source == "adb":
        src.append("adb")
    if probe_dir:
        src.append("probe")
    sources = ", ".join(src) if src else "none"
    sources_zh = "、".join(src) if src else "沒有"

    rows = []

    def add(key_en, key_zh, val_en, val_zh, origin):
        rows.append((key_en, key_zh, val_en or UNK_EN, val_zh or UNK_ZH, origin or "—"))

    add("running profile", "執行中的 profile", NO_MARKER_EN, NO_MARKER_ZH, "—")
    if probe_dir:
        if live_omit:
            add("draft profile", "profile 草稿", omit_en, omit_zh, "probe")
        elif draft_id and draft_status:
            add("draft profile", "profile 草稿", f"{draft_id} ({draft_status})", f"{draft_id}（{draft_status}）", "probe")
        else:
            add("draft profile", "profile 草稿", "", "", "probe")
    add("model", "機型", model, model, model_origin)
    add("board", "板名", board, board, board_origin)
    add("firmware", "韌體", firmware, firmware, fw_origin)
    if probe_dir:
        add("firmware flags", "韌體旗標", flags_en, flags_zh, flags_origin)
    add("build", "建置版本", build, build, here if adb_source else "")
    add("kernel", "核心", kernel, kernel, here if adb_source else "")
    add("display/gpu", "顯示／GPU 驅動", gpu_en, gpu_zh, here if adb_source else "")
    if read_pcis and not gpu_blocked:
        add("gpu pci", "GPU PCI", ", ".join(read_pcis), ", ".join(read_pcis), here)
    elif gpu_kind == "unknown" or adb_source:
        add("gpu pci", "GPU PCI", UNK_EN if (gpu_kind == "unknown" or adb_source) else "", UNK_ZH if (gpu_kind == "unknown" or adb_source) else "", here or "—")
    else:
        add("gpu pci", "GPU PCI", "", "", "")
    if drm_bits:
        add("drm cards", "DRM 卡", "; ".join(drm_bits), "; ".join(drm_bits), here)
    elif adb_source:
        add("drm cards", "DRM 卡", "", "", here)
    add("cpu", "CPU", cpu, cpu, cpu_origin)
    add("sound card", "音效卡", sound, sound, here if adb_source else "")
    if wifi_en:
        add("wifi", "Wi-Fi", wifi_en, wifi_zh, here)
    else:
        add("wifi", "Wi-Fi", "", "", here if adb_source else "")
    if bt_en:
        add("bluetooth", "藍牙", bt_en, bt_zh, here)
    else:
        add("bluetooth", "藍牙", "", "", here if adb_source else "")
    add("input", "觸控／觸控板", input_en, input_zh, here if input_en else (here if adb_source else ""))
    if sleep_en:
        add("sleep/resume", "休眠／喚醒", sleep_en, sleep_zh, here)
    else:
        add("sleep/resume", "休眠／喚醒", "", "", here if adb_source else "")
    if nodes:
        add("dri nodes", "DRM 節點", " ".join(nodes), " ".join(nodes), here)
    elif adb_source:
        add("dri nodes", "DRM 節點", "", "", here)
    if probe_dir:
        if probe_gpus:
            bits = []
            for pid, drv in probe_gpus:
                bits.append(" ".join(x for x in (pid, drv) if x))
            body = ", ".join(bits)
            if not adb_source:
                add("live linux gpu", "live Linux GPU",
                    f"{PROBE_ONLY_EN} {body} {NOT_GB_EN}",
                    f"{PROBE_ONLY_ZH}{body}{NOT_GB_ZH}", "probe")
            else:
                add("live linux gpu", "live Linux GPU", body, body, "probe")
        elif live_omit:
            add("live linux gpu", "live Linux GPU", omit_en, omit_zh, "probe")
        else:
            add("live linux gpu", "live Linux GPU", "", "", "probe")
        if probe_sound and (probe_sound != sound or not sound):
            add("live linux sound", "live Linux 音效卡", probe_sound, probe_sound, "probe")
        elif live_omit:
            add("live linux sound", "live Linux 音效卡", omit_en, omit_zh, "probe")
        elif probe_dir:
            add("live linux sound", "live Linux 音效卡", probe_sound, probe_sound, "probe")
        if wl:
            add("live linux wifi", "live Linux Wi-Fi",
                PROBE_SAW_EN + " " + ", ".join(wl) + " " + NO_LINK_EN,
                PROBE_SAW_ZH + " " + "、".join(wl) + NO_LINK_ZH, "probe")
        elif live_omit:
            add("live linux wifi", "live Linux Wi-Fi", omit_en, omit_zh, "probe")
        else:
            add("live linux wifi", "live Linux Wi-Fi", "", "", "probe")
        if bt_names:
            add("live linux bluetooth", "live Linux 藍牙",
                PROBE_SAW_EN + " " + ", ".join(bt_names) + " " + NO_POWER_EN,
                PROBE_SAW_ZH + " " + "、".join(bt_names) + NO_POWER_ZH, "probe")
        elif live_omit:
            add("live linux bluetooth", "live Linux 藍牙", omit_en, omit_zh, "probe")
        else:
            add("live linux bluetooth", "live Linux 藍牙", "", "", "probe")
        if live_omit and not probe_input_en:
            add("live linux input", "live Linux 觸控／觸控板", omit_en, omit_zh, "probe")
        else:
            add("live linux input", "live Linux 觸控／觸控板", probe_input_en, probe_input_zh, "probe")
        if compare_en:
            origin = "fixture+probe" if source == "fixture" else "adb+probe"
            add("compare", "比對", compare_en, compare_zh, origin)

    rows, n_redact = redact_rows(rows, banned)
    lines = [
        f"gbos-report {date}",
        "tool: install/gbos-report.sh (read-only allowlist; no install, no settings change, no file push)",
        PRIVACY,
        f"redactions: {n_redact}",
        "note: a recognized device is not proof that audio, touch, Wi-Fi, or suspend works. Read both files before pasting.",
        f"sources: {sources}",
        "",
    ]
    for key_en, _key_zh, val_en, _val_zh, origin in rows:
        lines.append(f"{key_en}: {val_en} [{origin}]")
    report = "\n".join(lines).rstrip("\n") + "\n"
    shown_date = f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
    summary = [
        "# Googlebook 機器回報",
        "",
        f"日期：{shown_date}",
        f"工具：install/gbos-report.sh（唯讀。{PRIVACY_ZH}）",
        f"輸出防火牆：遮罩 {n_redact} 行",
        f"來源：{sources_zh}",
        "",
        "| 項目 | 結果 | 來源 |",
        "|---|---|---|",
    ]
    for _key_en, key_zh, _val_en, val_zh, origin in rows:
        origin_zh = {
            "fixture": "fixture", "adb": "adb", "probe": "probe", "—": "—",
            "fixture+probe": "fixture、probe", "adb+probe": "adb、probe",
        }.get(origin, origin)
        summary.append(f"| {key_zh} | {cell(val_zh)} | {origin_zh} |")
    summary.append("")
    for key_en, _key_zh, _val_en, _val_zh, _origin in rows:
        if key_en not in SCHEMA:
            fail(f"internal: {key_en} is not in SCHEMA")
    wrote = False
    try:
        write_excl(str(Path(out_dir) / f"gbos-report-{date}.txt"), report)
        write_excl(str(Path(out_dir) / "summary.md"), "\n".join(summary))
        sys.stdout.write(f"gbos-report: wrote {out_dir}/gbos-report-{date}.txt and {out_dir}/summary.md\n")
        wrote = True
    finally:
        close_out_dir()
        _release_created(not wrote)


def shield_atoms(value):
    """Hide PCI/USB ids and shipped version strings so a fragment is not its own word."""
    text = value or ""
    if not text:
        return text
    spans = [(m.start(), m.end()) for m in PCI_ATOM.finditer(text)]
    low = text.lower()
    for rel in list(LAPIS_BUILDS) + list(PINNED_KERNELS):
        rel_l = rel.lower()
        start = 0
        while rel_l:
            i = low.find(rel_l, start)
            if i < 0:
                break
            spans.append((i, i + len(rel_l)))
            start = i + len(rel_l)
    if not spans:
        return text
    chars = list(text)
    for a, b in spans:
        for i in range(max(0, a), min(b, len(chars))):
            chars[i] = "\x00"
    return "".join(chars)


def leaks_value(value, tokens):
    """A device identifier in this value. PCI ids and shipped versions are one token each.
    A needle that is itself a whole PCI id is sought in the unshielded value, so a serial
    equal to a printed id still redacts those rows. Fragments stay shielded.
    CJK counts as a word character, so a shorter needle cannot hit inside a longer phrase."""
    if not value:
        return False
    if "@" in value or MAC_PAIR_RE.search(value) or MAC_QUAD_RE.search(value):
        return True
    if ipv4_hit(value) or IPV6_RE.search(value):
        return True
    if kernel_embeds_banned(value, tokens):
        return True
    shielded = shield_atoms(value)
    edge = r"A-Za-z0-9\u4e00-\u9fff"
    for raw in tokens or []:
        needle = raw or ""
        if not needle or is_report_word(needle) or re.fullmatch(r"\d{1,6}", needle):
            continue
        if PCI_ATOM.fullmatch(needle):
            if re.search(re.escape(needle), value, re.I):
                return True
            continue
        if re.search(
            r"(?<![%s])" % edge + re.escape(needle) + r"(?![%s])" % edge, shielded, re.I
        ):
            return True
    return False


def redact_rows(rows, tokens):
    """Both languages come from one row. A leak replaces that fact in both files."""
    out = []
    n = 0
    for key_en, key_zh, val_en, val_zh, origin in rows:
        if leaks_value(val_en, tokens) or leaks_value(val_zh, tokens):
            n += 1
            out.append((key_en, key_zh, UNK_EN, UNK_ZH, origin))
        else:
            out.append((key_en, key_zh, val_en, val_zh, origin))
    return out, n


def main():
    if len(sys.argv) != 8 or sys.argv[1] != "render":
        fail("internal: unknown mode")
    out_dir, date, raw_dir, probe_dir, source, serial = sys.argv[2:8]
    render(out_dir, date, raw_dir, probe_dir, source, serial)


main()
PY
rc=$?
set -e
exit "$rc"
