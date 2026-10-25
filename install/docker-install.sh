#!/usr/bin/env bash
# install/install.sh inside a Linux container: for macOS, Windows (Docker Desktop, run from WSL) or any machine with
# Docker but without the packages. Same result as install.sh; the work directory lives on the host.
#   install/docker-install.sh --work DIR [install.sh options: --zip FILE, --key FILE, --adb-pubkey FILE,
#                                         --skip-space-check, --no-selftest]
# With /dev/kvm usable on the host (Linux), the container gets it (and its group) and the QEMU self-test runs; otherwise
# (macOS, Windows) or with --no-selftest the image is built but not booted in QEMU. Docker needs about 80 GB of disk for
# the work directory. Paths may contain spaces but not ':' (Docker's -v syntax cannot carry it).
# Works with the bash 3.2 that macOS ships.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER=${GBOS_DOCKER:-docker}
TAG=googlebook-anywhere-builder:1
die() { echo "$*"; exit 2; }
# Developer-only. Refused before Docker: a set value must not be dropped and then built as a normal image.
if [ -n "${GBOS_TEST_AUTOOFF:-}" ]; then
    die "GBOS_TEST_AUTOOFF is developer-only (TEST images); docker-install.sh does not accept it"
fi
WORK=''; ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --work) [ $# -ge 2 ] || die "--work needs a directory"; WORK=$2; shift 2 ;;
        --zip|--key|--adb-pubkey) [ $# -ge 2 ] && [ -f "$2" ] || die "not a file: ${2:-} (for $1)"; ARGS+=("$1" "$2"); shift 2 ;;
        --skip-space-check|--no-selftest) ARGS+=("$1"); shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) die "unknown argument $1 (see the header of $0)" ;;
    esac
done
[ -n "$WORK" ] || die "--work DIR is required"
USERID=${GBOS_DOCKER_USER:-$(id -u):$(id -g)}
[[ $USERID =~ ^[0-9]+:[0-9]+$ ]] || die "GBOS_DOCKER_USER must be uid:gid, not '$USERID'"
[ "${USERID%%:*}" != 0 ] || die "run as a normal user (or set GBOS_DOCKER_USER=uid:gid)"
command -v "${DOCKER%% *}" > /dev/null || die "Docker is not installed (https://docs.docker.com/get-docker/)"
case "$ROOT$WORK" in *:*) die "the repository and --work paths must not contain ':' (it breaks docker -v): $ROOT, $WORK" ;; esac
case "$WORK" in /*) ;; *) WORK="$(pwd)/$WORK" ;; esac   # a relative --work under a cwd with ':' must be refused too
case "$WORK" in *:*) die "the --work path must not contain ':' (it breaks docker -v): $WORK" ;; esac
mkdir -p "$WORK"; WORK="$(cd "$WORK" && pwd)"

# files given on the command line are mounted read-only into the container under /in
MOUNTS=(-v "$ROOT:/repo:ro" -v "$WORK:/work")
CARGS=(); i=0; SELFTEST=1
while [ $i -lt ${#ARGS[@]} ]; do
    a=${ARGS[$i]}
    case "$a" in
        --zip|--key|--adb-pubkey)
            f="$(cd "$(dirname "${ARGS[$((i + 1))]}")" && pwd)/$(basename "${ARGS[$((i + 1))]}")"
            case "$f" in *:*) die "file paths must not contain ':' (it breaks docker -v): $f" ;; esac
            MOUNTS+=(-v "$f:/in/${a#--}:ro"); CARGS+=("$a" "/in/${a#--}"); i=$((i + 2)) ;;
        --no-selftest) SELFTEST=0; CARGS+=("$a"); i=$((i + 1)) ;;
        *) CARGS+=("$a"); i=$((i + 1)) ;;
    esac
done
if [ "$SELFTEST" = 1 ]; then
    if [ -r /dev/kvm ] && [ -w /dev/kvm ] && [ "$(uname -s)" = Linux ]; then
        # the container user is only uid:gid; give it the group that lets this user open /dev/kvm on the host
        MOUNTS+=(--device /dev/kvm --group-add "$(stat -c %g /dev/kvm)")
    else
        echo "no usable /dev/kvm on this host: building with --no-selftest (the image is not booted in QEMU)"
        CARGS+=(--no-selftest)
    fi
fi
echo "== building the container image $TAG"
$DOCKER build --platform linux/amd64 -t "$TAG" "$ROOT/install/docker"
echo "== running install.sh in the container (work dir $WORK)"
$DOCKER run --rm --platform linux/amd64 --user "$USERID" -e HOME=/work/.home "${MOUNTS[@]}" "$TAG" \
    bash -c 'mkdir -p "$HOME" && exec bash /repo/install/install.sh --work /work "$@"' install ${CARGS[@]+"${CARGS[@]}"}
echo
echo "The image is $WORK/build/image/googlebook-kano.img (checksum in googlebook-kano.img.sha256)."
echo "Write it with balenaEtcher or Rufus (see README), or on Linux: sudo install/write-usb.sh <image> /dev/disk/by-id/usb-..."
