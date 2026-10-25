#!/usr/bin/env python3
"""Download pinned https files into a cache and check size and sha256.

make-maint.sh fetch passes the committed pin list and, when asked, a cache
directory. It never passes --from-dir. Tests pass --from-dir so the same
checks run on local bytes. A file is published only after both checks match.
"""
import hashlib
import importlib.util
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_pins():
    path = os.path.join(HERE, "maint-pins.py")
    spec = importlib.util.spec_from_file_location("maint_pins", path)
    if spec is None or spec.loader is None:
        print("make-maint: cannot load the pin checker", file=sys.stderr)
        raise SystemExit(2)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PINS = load_pins()


def real_dir(path):
    try:
        st = os.lstat(path)
    except OSError as e:
        PINS.refuse(f"cannot read {path}: {e.strerror}")
    if stat.S_ISLNK(st.st_mode):
        PINS.refuse(f"{path} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        PINS.refuse(f"{path} is not a directory")


def open_nofollow(path, label):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as e:
        PINS.refuse(f"cannot read {label}: {e.strerror}")


def matching(path, row):
    try:
        st = os.lstat(path)
    except OSError as e:
        PINS.refuse(f"cannot read {row['name']}: {e.strerror}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        PINS.refuse(f"{row['name']} is not a regular file")
    if st.st_size != row["size"]:
        return False
    return PINS.sha256_file(path) == row["sha"]


def write_all(fd, block):
    view = memoryview(block)
    while view:
        wrote = os.write(fd, view)
        if wrote <= 0:
            PINS.refuse("short write")
        view = view[wrote:]


def stream_into(fd, read, size):
    digest = hashlib.sha256()
    got = 0
    while got < size:
        block = read(min(1024 * 1024, size - got))
        if not block:
            break
        write_all(fd, block)
        digest.update(block)
        got += len(block)
    extra = read(1)
    if extra:
        got += len(extra)
    return got, digest.hexdigest()


def take_bytes(row, src):
    if src is None:
        return url_reader(row)
    path = os.path.join(src, row["name"])
    if not os.path.lexists(path):
        PINS.refuse(f"{row['name']} is not in the fixture")
    fd = open_nofollow(path, row["name"])

    def read(n, source=fd):
        return os.read(source, n)

    def close(source=fd):
        os.close(source)

    return close, read


def url_reader(row):
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect())
    try:
        resp = opener.open(row["url"], timeout=120)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            PINS.refuse(f"refusing redirect for {row['name']}")
        PINS.refuse(f"download of {row['name']} failed: HTTP {e.code}")
    except OSError as e:
        PINS.refuse(f"download of {row['name']} failed: {e.strerror or e}")
    length = resp.headers.get("Content-Length")
    if length is not None and (not length.isdigit() or int(length) != row["size"]):
        resp.close()
        PINS.refuse(
            f"size mismatch for {row['name']}: response length {length}, pin says {row['size']}"
        )
    if resp.geturl() != row["url"]:
        resp.close()
        PINS.refuse(f"refusing redirect for {row['name']}")

    def close(response=resp):
        response.close()

    return close, resp.read


def unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def publish(row, cache, src):
    final = os.path.join(cache, row["name"])
    if os.path.lexists(final):
        if matching(final, row):
            print(f"kept {row['name']}")
            return
    partial = final + ".gbos-partial"
    if os.path.lexists(partial):
        try:
            st = os.lstat(partial)
        except OSError as e:
            PINS.refuse(f"cannot read {row['name']}: {e.strerror}")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            PINS.refuse(f"{row['name']} partial is not a regular file")
        unlink_quiet(partial)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        out = os.open(partial, flags, 0o644)
    except OSError as e:
        PINS.refuse(f"cannot write {row['name']}: {e.strerror}")
    close_src = None
    try:
        try:
            close_src, read = take_bytes(row, src)
            got, digest = stream_into(out, read, row["size"])
        finally:
            os.close(out)
            if close_src is not None:
                close_src()
    except BaseException:
        unlink_quiet(partial)
        raise
    if got != row["size"]:
        unlink_quiet(partial)
        PINS.refuse(
            f"size mismatch for {row['name']}: download has {got}, pin says {row['size']}"
        )
    if digest != row["sha"]:
        unlink_quiet(partial)
        PINS.refuse(f"sha256 mismatch for {row['name']}")
    os.replace(partial, final)
    if not matching(final, row):
        unlink_quiet(final)
        PINS.refuse(f"sha256 mismatch for {row['name']}")
    print(f"fetched {row['name']}")


def fetch(rows, cache, src):
    if not rows:
        PINS.refuse("pin list has no pinned files")
    real_dir(cache)
    if src is not None:
        real_dir(src)
    for row in rows:
        publish(row, cache, src)


def main(argv):
    pins, rest = PINS.take(argv, "--file")
    cache, rest = PINS.take(rest, "--cache")
    src, rest = PINS.take(rest, "--from-dir")
    if rest:
        PINS.refuse(f"unknown argument {rest[0]}")
    if pins is None:
        PINS.refuse("--file is required")
    if cache is None:
        PINS.refuse("--cache is required")
    rows = PINS.parse(pins)
    fetch(rows, cache, src)
    print("fetch ok")
    print(f"pins {len(rows)}")


if __name__ == "__main__":
    main(sys.argv[1:])
