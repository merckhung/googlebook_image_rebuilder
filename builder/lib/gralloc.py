#!/usr/bin/env python3
"""Drop-in libminigbm_gralloc.so for a profile that asks with [gralloc].

kano and generic-x86 have no table. install_gralloc returns without reading or
writing the vendor tree, and changed_paths leaves the vendor diff list alone.
The file is installed with vendor.sh place() at lib64/libminigbm_gralloc.so,
mode 0644. place() copies the stock vmap entry, so the file_contexts label
(selinux, uid, gid, caps) stays the one the base image already had.
"""
import hashlib
import os
import tempfile
from pathlib import Path

REL = "lib64/libminigbm_gralloc.so"
MODE = 0o644


def changed_paths(prof, expect_changed):
    """Content-diff paths. The replacement counts only when the profile asks."""
    if not prof.get("gralloc"):
        return expect_changed
    return sorted(set(list(expect_changed) + [REL]))


def install_gralloc(prof, tree, vmap, place, env=None):
    """Replace the stock gralloc with the bytes that were hashed.

    The source is read once. Those bytes are written to a temp file in the
    destination directory, place() records the stock file_contexts, and
    os.replace puts that same temp inode on lib64/libminigbm_gralloc.so.
    A symlink source is refused. place() must not be given the caller's path:
    a second open of that path can see different bytes than the hash.
    """
    g = prof.get("gralloc")
    if not g:
        return False
    rel = REL
    if rel not in vmap or not (tree / rel).is_file() or (tree / rel).is_symlink():
        raise SystemExit("stock libminigbm_gralloc.so is not in the vendor tree")
    if vmap[rel]["mode"] != 0o644:
        raise SystemExit("stock libminigbm_gralloc.so is not mode 0644")
    env = os.environ if env is None else env
    src = env.get("GBOS_MINIGBM", "")
    src_path = Path(src) if src else None
    if not src or src_path.is_symlink() or not src_path.is_file():
        if src and src_path.is_symlink():
            raise SystemExit("GBOS_MINIGBM is a symlink: " + src)
        raise SystemExit(
            "profile gralloc needs libminigbm_gralloc.so at "
            + (src or "GBOS_MINIGBM")
            + " (see the git repo tools/minigbm/build-libminigbm-gralloc.sh)")
    data = src_path.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    want = g["sha256"]
    if got != want:
        raise SystemExit(
            f"libminigbm_gralloc.so sha256 {got} differs from profile gralloc.sha256 {want}")
    kept = {k: vmap[rel][k] for k in ("selinux", "uid", "gid", "caps")}
    fd = None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".libminigbm_gralloc.", dir=tree / "lib64")
        os.write(fd, data)
        os.close(fd)
        fd = None
        os.chmod(tmp_name, MODE)
        (tree / rel).unlink()
        place(rel, Path(tmp_name), rel, MODE)
        os.replace(tmp_name, tree / rel)
        tmp_name = None
        os.chmod(tree / rel, MODE)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    meta = vmap[rel]
    if meta["mode"] != MODE or any(meta[k] != kept[k] for k in kept):
        raise SystemExit("libminigbm_gralloc.so file_contexts drifted")
    installed = hashlib.sha256((tree / rel).read_bytes()).hexdigest()
    if installed != want:
        raise SystemExit(
            f"installed libminigbm_gralloc.so sha256 {installed} differs from profile gralloc.sha256 {want}")
    return True


def readback_mismatch(blob, want):
    """Return an error string when blob is not the pinned gralloc, else None."""
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        return (
            "image vendor libminigbm_gralloc.so sha256 "
            + got
            + " differs from profile gralloc.sha256 "
            + want
        )
    return None
