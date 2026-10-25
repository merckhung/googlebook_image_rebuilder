#!/usr/bin/env bash
# Build inputs for the maintenance system, without root.
# This step checks arguments, the committed pin list, and a local cache, and
# prints where XBOOTLDR and kano-maint go on a stick.
# fetch without --cache only checks the committed pin list and does not
# download. fetch --cache downloads those files and keeps one only when its
# size and sha256 match. The committed list is a dated Arch snapshot.
# This script does not download core.db or extra.db. Regenerating the pin
# list is not one of the commands below.
# Downloading it does not assemble the maintenance root.
# The pinned linux package does not contain boot/initramfs-linux.img.
# assemble builds that file with the root's own mkinitcpio when it is missing.
# It still refuses, and leaves no directory, when that file cannot be built
# or the rest of the maintenance contract fails.
# pack writes kano-maint.img and xbootldr.img only when the process is root
# or FAKEROOTKEY is set and mke2fs -d, mkfs.vfat, and mcopy exist.
# When the process is root, including fakeroot, it records uid 0 on the
# assembled root without following a symlink before mke2fs -d stores that owner.
# Archive setuid, setgid, and sticky bits are kept. A regular file with no
# owner-read bit is given one so mke2fs can copy it; uid 0 makes that bit moot.
#
#   make-maint.sh layout --image IMAGE [--disk-sectors N]
#   make-maint.sh check-pins [--file PINS]
#   make-maint.sh verify-cache --cache DIR [--file PINS]
#   make-maint.sh fetch [--cache DIR]
#   make-maint.sh assemble --cache DIR --out DIR [--file PINS]
#   make-maint.sh pack --out DIR
#   make-maint.sh build --cache DIR --out DIR
#
# fetch and build read only the pin list next to this script. fetch never
# takes --file. build still refuses while the cache does not contain those
# files. verify-cache --file and assemble --file are the offline checkers
# (tests pass a temporary list); they
# are not a way to build from a different list. pack refuses, and writes
# nothing, while the files would be owned by the builder's own uid or the
# image tools are missing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PINS="$HERE/maint.pins"
export PYTHONDONTWRITEBYTECODE=1

refuse() { echo "make-maint: $*" >&2; exit 2; }
usage() {
    echo "make-maint: usage: make-maint.sh layout --image IMAGE [--disk-sectors N]" >&2
    echo "make-maint:        make-maint.sh check-pins [--file PINS]" >&2
    echo "make-maint:        make-maint.sh verify-cache --cache DIR [--file PINS]" >&2
    echo "make-maint:        make-maint.sh fetch [--cache DIR]" >&2
    echo "make-maint:        make-maint.sh assemble --cache DIR --out DIR [--file PINS]" >&2
    echo "make-maint:        make-maint.sh pack --out DIR" >&2
    echo "make-maint:        make-maint.sh build --cache DIR --out DIR" >&2
    exit 2
}

command -v python3 >/dev/null 2>&1 || refuse "python3 is required"
[ $# -ge 1 ] || usage
cmd=$1
shift

case "$cmd" in
    layout)
        image=""
        disk=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --image)
                    [ -z "$image" ] || refuse "--image given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--image needs a path"
                    image=$2
                    shift 2
                    ;;
                --disk-sectors)
                    [ -z "$disk" ] || refuse "--disk-sectors given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--disk-sectors needs a number"
                    disk=$2
                    shift 2
                    ;;
                *) refuse "unknown argument $1" ;;
            esac
        done
        [ -n "$image" ] || refuse "--image is required"
        if [ -n "$disk" ]; then
            exec python3 -I "$HERE/maint-layout.py" --image "$image" --disk-sectors "$disk"
        fi
        exec python3 -I "$HERE/maint-layout.py" --image "$image"
        ;;
    check-pins)
        file=$PINS
        given=0
        while [ $# -gt 0 ]; do
            case "$1" in
                --file)
                    [ "$given" = 0 ] || refuse "--file given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--file needs a path"
                    file=$2
                    given=1
                    shift 2
                    ;;
                *) refuse "unknown argument $1" ;;
            esac
        done
        exec python3 -I "$HERE/maint-pins.py" check --file "$file"
        ;;
    verify-cache)
        file=$PINS
        cache=""
        given=0
        while [ $# -gt 0 ]; do
            case "$1" in
                --file)
                    [ "$given" = 0 ] || refuse "--file given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--file needs a path"
                    file=$2
                    given=1
                    shift 2
                    ;;
                --cache)
                    [ -z "$cache" ] || refuse "--cache given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--cache needs a path"
                    cache=$2
                    shift 2
                    ;;
                *) refuse "unknown argument $1" ;;
            esac
        done
        [ -n "$cache" ] || refuse "--cache is required"
        exec python3 -I "$HERE/maint-pins.py" verify --file "$file" --cache "$cache"
        ;;
    fetch)
        # Committed list only. --cache downloads; without it, print the check.
        cache=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --cache)
                    [ -z "$cache" ] || refuse "--cache given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--cache needs a path"
                    cache=$2
                    shift 2
                    ;;
                --file|--from-dir)
                    refuse "fetch reads only the committed pin list and takes no $1"
                    ;;
                *) refuse "unknown argument $1" ;;
            esac
        done
        out=$(python3 -I "$HERE/maint-pins.py" check --file "$PINS") || exit $?
        count=$(printf '%s\n' "$out" | awk '$1 == "pins" && $2 ~ /^[0-9]+$/ {n = $2} END {print n + 0}')
        [ "$count" -gt 0 ] || refuse "committed pin list has no pinned files"
        if [ -z "$cache" ]; then
            printf '%s\n' "$out"
            exit 0
        fi
        exec python3 -I "$HERE/maint-fetch.py" --file "$PINS" --cache "$cache"
        ;;
    assemble)
        file=$PINS
        cache=""
        dest=""
        given=0
        while [ $# -gt 0 ]; do
            case "$1" in
                --file)
                    [ "$given" = 0 ] || refuse "--file given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--file needs a path"
                    file=$2
                    given=1
                    shift 2
                    ;;
                --cache)
                    [ -z "$cache" ] || refuse "--cache given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--cache needs a path"
                    cache=$2
                    shift 2
                    ;;
                --out)
                    [ -z "$dest" ] || refuse "--out given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--out needs a path"
                    dest=$2
                    shift 2
                    ;;
                *) refuse "unknown argument $1" ;;
            esac
        done
        [ -n "$cache" ] || refuse "--cache is required"
        [ -n "$dest" ] || refuse "--out is required"
        exec python3 -I "$HERE/maint-assemble.py" assemble --file "$file" --cache "$cache" --out "$dest"
        ;;
    pack)
        dest=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --out)
                    [ -z "$dest" ] || refuse "--out given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--out needs a path"
                    dest=$2
                    shift 2
                    ;;
                *) refuse "unknown argument $1" ;;
            esac
        done
        [ -n "$dest" ] || refuse "--out is required"
        exec python3 -I "$HERE/maint-assemble.py" pack --out "$dest"
        ;;
    build)
        cache=""
        dest=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --cache)
                    [ -z "$cache" ] || refuse "--cache given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--cache needs a path"
                    cache=$2
                    shift 2
                    ;;
                --out)
                    [ -z "$dest" ] || refuse "--out given twice"
                    [ $# -ge 2 ] && [ -n "$2" ] || refuse "--out needs a path"
                    dest=$2
                    shift 2
                    ;;
                --file)
                    refuse "build reads only the committed pin list and takes no --file"
                    ;;
                *) refuse "unknown argument $1" ;;
            esac
        done
        [ -n "$cache" ] || refuse "--cache is required"
        [ -n "$dest" ] || refuse "--out is required"
        python3 -I "$HERE/maint-assemble.py" assemble --file "$PINS" --cache "$cache" --out "$dest"
        exec python3 -I "$HERE/maint-assemble.py" pack --out "$dest"
        ;;
    *) refuse "unknown command $cmd" ;;
esac
