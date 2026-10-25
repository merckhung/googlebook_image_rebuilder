#!/usr/bin/env python3
"""Check install/stick/maint.pins and, when asked, the local cache it names.

No network. A missing cache file is a refusal, not a download. Symlinks are
refused so a pin name cannot point outside the cache.
"""
import hashlib
import os
import re
import stat
import sys
import urllib.parse

NAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,200}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SIZE_RE = re.compile(r"^[1-9][0-9]{0,12}$")


def refuse(msg):
    print(f"make-maint: {msg}", file=sys.stderr)
    raise SystemExit(2)


def check_url(url, where):
    if not url.startswith("https://") or any(c in url for c in " \t\r\n\\#?"):
        refuse(f"{where}: url must be https, without a query or fragment")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        refuse(f"{where}: url must be https without userinfo")
    if not parts.hostname or parts.hostname.startswith(".") or ".." in parts.hostname:
        refuse(f"{where}: url has no usable host")
    if not parts.path or parts.path == "/" or parts.path.endswith("/"):
        refuse(f"{where}: url must name a file")


def parse(path):
    try:
        st = os.lstat(path)
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    if stat.S_ISLNK(st.st_mode):
        refuse(f"{path} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        refuse(f"{path} is not a regular file")
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror}")
    if b"\0" in raw:
        refuse(f"{path}: pin list contains a NUL")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        refuse(f"{path}: pin list is not UTF-8")
    if "\r" in text:
        refuse(f"{path}: pin list must be LF line endings")
    rows = []
    names = set()
    urls = set()
    for lineno, line in enumerate(text.split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.isascii():
            refuse(f"{path}:{lineno}: pin line must be ASCII")
        fields = line.split()
        where = f"{path}:{lineno}"
        if len(fields) != 4:
            refuse(f"{where}: want sha256, size, name, url")
        sha, size_s, name, url = fields
        if not SHA_RE.fullmatch(sha):
            refuse(f"{where}: sha256 must be 64 lowercase hex digits")
        if not SIZE_RE.fullmatch(size_s):
            refuse(f"{where}: size must be a positive integer with no leading zero")
        if name.startswith(".") or not NAME_RE.fullmatch(name):
            refuse(f"{where}: name must be a single cache file name")
        check_url(url, where)
        if name in names:
            refuse(f"{where}: duplicate name {name}")
        if url in urls:
            refuse(f"{where}: duplicate url")
        names.add(name)
        urls.add(url)
        rows.append({"sha": sha, "size": int(size_s), "name": name, "url": url, "where": where})
    return rows


def sha256_file(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as e:
        refuse(f"{path} cannot be read ({e.strerror})")
    h = hashlib.sha256()
    try:
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            h.update(block)
    finally:
        os.close(fd)
    return h.hexdigest()


def verify(rows, cache):
    try:
        st = os.lstat(cache)
    except OSError as e:
        refuse(f"cannot read cache {cache}: {e.strerror}")
    if stat.S_ISLNK(st.st_mode):
        refuse(f"{cache} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        refuse(f"{cache} is not a directory")
    for row in rows:
        fp = os.path.join(cache, row["name"])
        if not os.path.lexists(fp):
            refuse(f"{row['name']} is not in the cache")
        try:
            fst = os.lstat(fp)
        except OSError as e:
            refuse(f"cannot read {row['name']}: {e.strerror}")
        if stat.S_ISLNK(fst.st_mode) or not stat.S_ISREG(fst.st_mode):
            refuse(f"{row['name']} is not a regular file")
        if fst.st_size != row["size"]:
            refuse(f"size mismatch for {row['name']}: file has {fst.st_size}, pin says {row['size']}")
        got = sha256_file(fp)
        if got != row["sha"]:
            refuse(f"sha256 mismatch for {row['name']}")


def take(argv, flag):
    """Return (value or None, remaining). flag may appear once."""
    value = None
    out = []
    i = 0
    while i < len(argv):
        if argv[i] == flag:
            if value is not None:
                refuse(f"{flag} given twice")
            if i + 1 >= len(argv) or not argv[i + 1] or argv[i + 1].startswith("--"):
                refuse(f"{flag} needs a path")
            value = argv[i + 1]
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return value, out


def main(argv):
    if not argv or argv[0] not in ("check", "verify"):
        refuse("usage: maint-pins.py check --file PINS | verify --file PINS --cache DIR")
    cmd = argv[0]
    pins, rest = take(argv[1:], "--file")
    cache, rest = take(rest, "--cache")
    if rest:
        refuse(f"unknown argument {rest[0]}")
    if pins is None:
        refuse("--file is required")
    rows = parse(pins)
    if cmd == "check":
        if cache is not None:
            refuse("check does not take --cache")
        print("pins ok")
        print(f"pins {len(rows)}")
        return
    if cache is None:
        refuse("--cache is required")
    verify(rows, cache)
    print("cache ok")
    print(f"pins {len(rows)}")


if __name__ == "__main__":
    main(sys.argv[1:])
