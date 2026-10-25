"""GBOS_TEST_AUTOOFF: developer-only TEST image auto-off.

vendor.sh calls install() only when GBOS_TEST_AUTOOFF is an integer 60..7200.
install() draws a fresh 128-bit nonce (32 lowercase hex from os.urandom, plus
LF), stores it read-only as /vendor/etc/kano-autooff.nonce, and writes the
same bytes to $GBOS_OUT/image/autooff.nonce for the session. Without the
switch neither file is created and the service is absent.

The generated script arms only when that vendor file and /data/kano-autooff.nonce
are both regular (not symlinks), readable, and the same 33 raw bytes: 32
[0-9a-f] and a final LF. NUL, CR, any other size, a failed read, or a mismatch
logs "staying on" and exits 0. After N seconds, confirmed with /proc/uptime so
a sleep that returns early does not count, it reads both files again and only
then runs reboot -p once. /proc/uptime is CLOCK_BOOTTIME (it includes time
spent in s2idle / S0ix). sleep uses CLOCK_MONOTONIC, which pauses across
suspend. After a real sleep of N seconds, boottime has advanced at least as
far as monotonic time, so the gate still opens.

KANO_AUTOOFF_ROOT is a test hook and is unset on the device. A relative value
stays on. An absolute value is not a sandbox: ".." still reaches outside that
directory, and reboot -p is this machine's reboot, not a path under the root.

The session (the same side that writes /data/kano-wifi.conf) copies
image/autooff.nonce onto the identity-checked USB stick's userdata
(/dev/disk/by-id/usb-..., the write-usb.sh target) as a regular file
/data/kano-autooff.nonce immediately before the TEST boot, and deletes it
when the session is over. This repo does not contain that session writer.
It is not written to the internal NVMe. Power-off can arm only when that
/data nonce is present. The only path that copies userdata onto the internal
disk is kano-nvme-install.sh. The safety guarantee on that path is removing
kano-autooff.nonce (a regular file, a symlink, or a directory) from the copied
userdata before unmount. The root directory is listed. A real directory is
removed inside python3: lstat must show a directory and not a symlink,
shutil.rmtree.avoids_symlink_attacks must be true, and an rmtree error exits
non-zero. No filename is handed back to the shell. Only an lstat errno of
ENOENT means the nonce name is absent; any other error stops. After unmount
the userdata is mounted read-only again, the name must lstat as ENOENT, and
it is unmounted again. If the mount, the directory read, the lstat, the
delete, the remount, the recheck, or either unmount fails, the script exits
non-zero and does not resize.f2fs, fsck.f2fs, or write the ESP. Any stop
between dd and that strip (kill, power loss, ssh drop) can still leave the
file; a failed strip does not continue. The vendor scan is defense in depth:
it keeps a TEST service off the daily disk, and it is not the guarantee.
Before blkdiscard, /dev/sda1 must be PARTLABEL userdata and is then mounted
-o ro. An f2fs read-only mount may still replay a previous fsync.
kano-nvme-update.sh also refuses before it pushes super, and it does not
touch userdata. The standalone NVMe delta writer refuses a changed
userdata block and does not scan vendor.
gbos-update.py apply keeps userdata out of range via guard_ranges; rollback
rewrites ESP loader entries only. A fresh stick install creates a new empty
userdata. Those paths cannot create the /data nonce, so a TEST vendor there
stays on. install.sh and docker-install.sh do not write the internal disk;
they refuse the switch while building a USB image. update.sh calls install.sh
and, with --disk, execs write-usb.sh --keep-data onto a USB stick. QEMU has
no /data nonce, so the self-test stays on.
"""
from __future__ import annotations

import os
from pathlib import Path

FILES = (
    "etc/kano-autooff.sh",
    "etc/init/kano-autooff.rc",
    "etc/kano-autooff.nonce",
)

RC = """# TEST image: power off only when /data/kano-autooff.nonce matches /vendor/etc/kano-autooff.nonce.
# See /vendor/etc/kano-autooff.sh. Images built without GBOS_TEST_AUTOOFF do not include this service.
service kano_autooff /system/bin/sh /vendor/etc/kano-autooff.sh
    oneshot
    disabled
    user root
    seclabel u:r:kano_kmsg_logcat:s0

# The functional QEMU self-test never sets sys.boot_completed. class late_start
# is started by `class_start late_start` in action nonencrypted (init.rc). There
# is no late-start event. Start on that action so the nonce gate runs inside the
# self-test window. boot_completed starts it again on a real boot if the earlier
# oneshot already exited (for example /data was not mounted yet). A second start
# while this oneshot is running does not start another copy.
on nonencrypted
    start kano_autooff

on property:sys.boot_completed=1
    start kano_autooff
"""

# @@WAIT@@ is replaced with the decimal seconds. The script is POSIX sh
# (Android mksh / toybox). External commands: cat, wc, sleep, reboot.
_SCRIPT = r"""#!/system/bin/sh
# TEST image (GBOS_TEST_AUTOOFF). Power off only when both nonce files are
# regular, readable, and the same 33 raw bytes: 32 lowercase hex digits and LF.
# A symlink is refused even when its target has those bytes: /data can point at
# the vendor file or at another disk. A hard link cannot cross the read-only
# vendor erofs and the userdata f2fs, so it cannot alias the vendor nonce.
# NUL is rejected because command substitution drops it and wc -c does not.
# Any other size, CR, a failed read, or a mismatch stays on.
# The wait is @@WAIT@@ seconds. sleep must exit 0 and /proc/uptime must move
# forward by at least that much; then both files are read again. reboot -p once.
# KANO_AUTOOFF_ROOT is unset on the device. A relative value stays on.
LC_ALL=C
export LC_ALL
IFS=' 	
'

_kano_wait=@@WAIT@@
_kano_nl='
'
_kano_cr=$(printf '\r')
_kano_tab=$(printf '\t')
if [ "${#_kano_cr}" -ne 1 ] || [ "${#_kano_tab}" -ne 1 ]; then
  printf '%s\n' "KANO-AUTOOFF: bad root, staying on" >&2
  exit 0
fi
_kano_base=
_kano_body=
_kano_hex=
_kano_vhex=
_kano_canon=
_kano_trim=
_kano_field_cs=
_kano_up=
_kano_before=0
_kano_after=0
_kano_need=0

kano_log() {
  printf '%s\n' "KANO-AUTOOFF: $1" >> "${_kano_base}/dev/kmsg"
}

kano_trim() {
  _kano_t=$1
  while :; do
    case "$_kano_t" in
      " "*) _kano_t=${_kano_t# } ;;
      "$_kano_tab"*) _kano_t=${_kano_t#"$_kano_tab"} ;;
      *) break ;;
    esac
  done
  while :; do
    case "$_kano_t" in
      *" ") _kano_t=${_kano_t% } ;;
      *"$_kano_tab") _kano_t=${_kano_t%"$_kano_tab"} ;;
      *) break ;;
    esac
  done
  _kano_trim=$_kano_t
}

kano_digits() {
  kano_trim "$1"
  case "$_kano_trim" in
    ""|*[!0-9]*) return 1 ;;
  esac
  _kano_c=$_kano_trim
  while :; do
    case "$_kano_c" in
      0) break ;;
      0*) _kano_c=${_kano_c#0} ;;
      *) break ;;
    esac
  done
  _kano_canon=$_kano_c
  return 0
}

kano_read_body() {
  _kano_rf=$1
  _kano_body=
  if [ -L "$_kano_rf" ]; then
    return 1
  fi
  if [ ! -f "$_kano_rf" ]; then
    return 1
  fi
  if [ ! -r "$_kano_rf" ]; then
    return 1
  fi
  if ! _kano_raw=$(cat "$_kano_rf" 2>/dev/null && printf x); then
    return 1
  fi
  case "$_kano_raw" in
    *x) ;;
    *) return 1 ;;
  esac
  _kano_body=${_kano_raw%x}
  if ! _kano_wc=$(wc -c < "$_kano_rf" 2>/dev/null); then
    return 1
  fi
  if ! kano_digits "$_kano_wc"; then
    return 1
  fi
  if [ "$_kano_canon" != "${#_kano_body}" ]; then
    return 1
  fi
  case "$_kano_body" in
    *"$_kano_cr"*) return 1 ;;
  esac
  if [ -L "$_kano_rf" ] || [ ! -f "$_kano_rf" ]; then
    return 1
  fi
  return 0
}

kano_nonce_hex() {
  if ! kano_read_body "$1"; then
    return 1
  fi
  if [ "$_kano_canon" != 33 ]; then
    return 1
  fi
  case "$_kano_body" in
    *"$_kano_nl") ;;
    *) return 1 ;;
  esac
  _kano_hex=${_kano_body%"$_kano_nl"}
  if [ "${#_kano_hex}" -ne 32 ]; then
    return 1
  fi
  case "$_kano_hex" in
    *[!0-9a-f]*) return 1 ;;
  esac
  return 0
}

kano_field_cs() {
  _kano_field=$1
  _kano_frac=
  case "$_kano_field" in
    *.*.*) return 1 ;;
    *.*)
      _kano_sec=${_kano_field%%.*}
      _kano_frac=${_kano_field#*.}
      ;;
    *)
      _kano_sec=$_kano_field
      ;;
  esac
  case "$_kano_sec" in
    ""|*[!0-9]*|0[0-9]*) return 1 ;;
  esac
  if [ "${#_kano_sec}" -gt 8 ]; then
    return 1
  fi
  if [ "$_kano_sec" -gt 20000000 ]; then
    return 1
  fi
  case "$_kano_frac" in
    "") _kano_cs=0 ;;
    [0-9]) _kano_cs=${_kano_frac}0 ;;
    [0-9][0-9]) _kano_cs=$_kano_frac ;;
    *) return 1 ;;
  esac
  case "$_kano_cs" in
    0) ;;
    0*) _kano_cs=${_kano_cs#0} ;;
  esac
  case "$_kano_cs" in
    ""|*[!0-9]*) return 1 ;;
  esac
  _kano_field_cs=$((_kano_sec * 100 + _kano_cs))
  return 0
}

kano_uptime() {
  if ! kano_read_body "${_kano_base}/proc/uptime"; then
    return 1
  fi
  if [ "${#_kano_body}" -lt 4 ] || [ "${#_kano_body}" -gt 64 ]; then
    return 1
  fi
  case "$_kano_body" in
    *"$_kano_nl") ;;
    *) return 1 ;;
  esac
  _kano_line=${_kano_body%"$_kano_nl"}
  case "$_kano_line" in
    *"$_kano_nl"*) return 1 ;;
    *" "*) ;;
    *) return 1 ;;
  esac
  _kano_a=${_kano_line%% *}
  _kano_b=${_kano_line#* }
  case "$_kano_b" in
    *" "*|*"$_kano_tab"*) return 1 ;;
  esac
  if ! kano_field_cs "$_kano_a"; then
    return 1
  fi
  _kano_up=$_kano_field_cs
  if ! kano_field_cs "$_kano_b"; then
    return 1
  fi
  return 0
}

_kano_base=${KANO_AUTOOFF_ROOT-}
case "$_kano_base" in
  ""|/*) ;;
  *)
    printf '%s\n' "KANO-AUTOOFF: bad root, staying on" >&2
    exit 0
    ;;
esac
if ! kano_nonce_hex "${_kano_base}/vendor/etc/kano-autooff.nonce"; then
  kano_log "nonce gate closed, staying on"
  exit 0
fi
_kano_vhex=$_kano_hex
if ! kano_nonce_hex "${_kano_base}/data/kano-autooff.nonce"; then
  kano_log "nonce gate closed, staying on"
  exit 0
fi
if [ "$_kano_vhex" != "$_kano_hex" ]; then
  kano_log "nonce gate closed, staying on"
  exit 0
fi
kano_log "armed"
if ! kano_uptime; then
  kano_log "sleep failed, staying on"
  exit 0
fi
_kano_before=$_kano_up
if [ "$_kano_before" -gt $((2000000099 - _kano_wait * 100)) ]; then
  kano_log "sleep failed, staying on"
  exit 0
fi
if ! sleep "$_kano_wait"; then
  kano_log "sleep failed, staying on"
  exit 0
fi
if ! kano_uptime; then
  kano_log "sleep failed, staying on"
  exit 0
fi
_kano_after=$_kano_up
_kano_need=$((_kano_before + _kano_wait * 100))
if [ "$_kano_after" -lt "$_kano_need" ]; then
  kano_log "sleep failed, staying on"
  exit 0
fi
if ! kano_nonce_hex "${_kano_base}/vendor/etc/kano-autooff.nonce"; then
  kano_log "nonce gate closed, staying on"
  exit 0
fi
_kano_vhex=$_kano_hex
if ! kano_nonce_hex "${_kano_base}/data/kano-autooff.nonce"; then
  kano_log "nonce gate closed, staying on"
  exit 0
fi
if [ "$_kano_vhex" != "$_kano_hex" ]; then
  kano_log "nonce gate closed, staying on"
  exit 0
fi
kano_log "powering off" || exit 0
reboot -p
exit 0
"""


def script(seconds: int) -> str:
    """POSIX sh installed as /vendor/etc/kano-autooff.sh. seconds is 60..7200."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 60 <= seconds <= 7200:
        raise ValueError(f"GBOS_TEST_AUTOOFF must be an integer 60..7200, not {seconds!r}")
    rendered = str(seconds)
    if rendered != f"{seconds}" or not rendered.isascii() or not rendered.isdigit():
        raise ValueError(f"GBOS_TEST_AUTOOFF must be an integer 60..7200, not {seconds!r}")
    if rendered.startswith("0"):
        raise ValueError(f"GBOS_TEST_AUTOOFF must be an integer 60..7200, not {seconds!r}")
    text = _SCRIPT.replace("@@WAIT@@", rendered)
    if "@@WAIT@@" in text or text.count(f"_kano_wait={rendered}") != 1:
        raise RuntimeError("autooff script did not receive its wait")
    if not text.endswith("\n") or "\r" in text:
        raise RuntimeError("autooff script line endings")
    return text


def _write_nofollow(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        view = data
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise OSError(f"short write: {path}")
            view = view[wrote:]
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def install(tree: Path, vmap: dict, seconds: int) -> None:
    """Write the script, init rc, vendor nonce, and $GBOS_OUT/image/autooff.nonce.

    The nonce is generated here, once per call. The vendor copy is mode 0444.
    The sidecar next to the image is the same 33 bytes, mode 0644, for the session.
    """
    text = script(seconds)
    out = os.environ.get("GBOS_OUT", "")
    if not out:
        raise ValueError("GBOS_OUT is required to write image/autooff.nonce")
    nonce = os.urandom(16).hex() + "\n"
    raw = nonce.encode("ascii")
    if len(raw) != 33 or raw[-1:] != b"\n":
        raise RuntimeError("nonce format")
    if any(byte not in b"0123456789abcdef" for byte in raw[:-1]):
        raise RuntimeError("nonce format")
    like = dict(vmap["etc/init/fwupd.rc"])
    _write_nofollow(tree / "etc/kano-autooff.sh", text.encode("utf-8"), 0o644)
    rc_text = RC if RC.endswith("\n") else RC + "\n"
    _write_nofollow(tree / "etc/init/kano-autooff.rc", rc_text.encode("utf-8"), 0o644)
    _write_nofollow(tree / "etc/kano-autooff.nonce", raw, 0o444)
    vmap["etc/kano-autooff.sh"] = dict(like)
    vmap["etc/init/kano-autooff.rc"] = dict(like)
    nonce_meta = dict(like)
    nonce_meta["mode"] = 0o444
    vmap["etc/kano-autooff.nonce"] = nonce_meta
    _write_nofollow(Path(out) / "image" / "autooff.nonce", raw, 0o644)
