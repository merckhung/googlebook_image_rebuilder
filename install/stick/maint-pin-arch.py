#!/usr/bin/env python3
"""Maintainer-only. Pin one dated Arch Linux Archive snapshot.

fetch and assemble do not run this script. They read the committed maint.pins
and never download a repository database.

The seeds are the packages that provide the tools and kernel files named by
gbos-install.sh, setup-stick.sh, and maint-assemble.py. Before core.db or
extra.db is parsed, the downloaded bytes must match the sha256 and size in
maint-db.pins. A date with no pin is refused before any download. The script
then closes dependencies and downloads each package with maint-fetch.py. A
package pin file is written only after those bytes match. Offline tests do
not run this against the network.
"""
import gzip
import hashlib
import os
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PINS = os.path.join(HERE, "maint-db.pins")
DB_URL_RE = re.compile(
    r"^https://archive\.archlinux\.org/repos/"
    r"([0-9]{4}/[0-9]{2}/[0-9]{2})/(core|extra)/os/x86_64/(core|extra)\.db$"
)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SIZE_RE = re.compile(r"^[1-9][0-9]{0,12}$")
# gbos-install.sh:110 and :401, setup-stick.sh:55 and :78 and :153,
# maint-assemble.py REQUIRED_TOOLS. linux and mkinitcpio supply the kernel
# and the initramfs tool those scripts require by name.
SEEDS = (
    "base",
    "bash",
    "coreutils",
    "diffutils",
    "filesystem",
    "gawk",
    "gptfdisk",
    "grep",
    "kmod",
    "linux",
    "lz4",
    "mkinitcpio",
    "mtools",
    "python",
    "sed",
    "systemd",
    "util-linux",
    "zstd",
)


def refuse(msg):
    print(f"make-maint: {msg}", file=sys.stderr)
    raise SystemExit(2)


def archive_url(date, repo, name):
    return f"https://archive.archlinux.org/repos/{date}/{repo}/os/x86_64/{name}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_bytes(url):
    opener = urllib.request.build_opener(NoRedirect())
    try:
        resp = opener.open(url, timeout=120)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            refuse(f"refusing redirect for {url}")
        refuse(f"download failed for {url}: HTTP {e.code}")
    except OSError as e:
        refuse(f"download failed for {url}: {e.strerror or e}")
    try:
        if resp.geturl() != url:
            refuse(f"refusing redirect for {url}")
        return resp.read()
    finally:
        resp.close()


def open_db(blob):
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    try:
        return tarfile.open(fileobj=__import__("io").BytesIO(blob), mode="r:")
    except tarfile.TarError:
        refuse("repository database is not a tar archive")


def parse_desc(text):
    fields = {}
    tag = None
    values = []

    def flush():
        nonlocal tag, values
        if tag:
            fields[tag] = values[:]
        tag = None
        values = []

    for line in text.split("\n"):
        if len(line) > 2 and line.startswith("%") and line.endswith("%"):
            flush()
            tag = line[1:-1]
            values = []
        elif line == "":
            flush()
        elif tag is not None:
            values.append(line)
    flush()
    return fields


def dep_base(dep):
    for op in (">=", "<=", "=", ">", "<"):
        if op in dep:
            return dep.split(op, 1)[0]
    return dep


def read_db_pins(path):
    """Pre-pinned core.db and extra.db digests. No download and no parse."""
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        refuse(f"cannot read {path}: {e.strerror or e}")
    if b"\0" in raw:
        refuse(f"{path}: pin list contains a NUL")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        refuse(f"{path}: pin list is not UTF-8")
    if "\r" in text:
        refuse(f"{path}: pin list must be LF line endings")
    pins = {}
    for lineno, line in enumerate(text.split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        where = f"{path}:{lineno}"
        if len(fields) != 4:
            refuse(f"{where}: want sha256, size, name, url")
        sha, size_s, name, url = fields
        if not SHA_RE.fullmatch(sha):
            refuse(f"{where}: sha256 must be 64 lowercase hex digits")
        if not SIZE_RE.fullmatch(size_s):
            refuse(f"{where}: size must be a positive integer with no leading zero")
        match = DB_URL_RE.fullmatch(url)
        if match is None:
            refuse(f"{where}: url must be an Arch archive core.db or extra.db")
        date, repo, url_repo = match.groups()
        if url_repo != repo or name != f"{repo}.db":
            refuse(f"{where}: name does not match the database url")
        key = (date, repo)
        if key in pins:
            refuse(f"{where}: duplicate database {repo}.db {date}")
        pins[key] = {"sha": sha, "size": int(size_s), "name": name, "url": url}
    return pins


def check_db(blob, pin):
    """Refuse before the caller parses. The pin is not taken from the blob."""
    digest = hashlib.sha256(blob).hexdigest()
    if len(blob) != pin["size"] or digest != pin["sha"]:
        refuse("database digest does not match the pre-pinned digest")


def load_repo(date, repo, by_name, by_provide, pins):
    pin = pins.get((date, repo))
    if pin is None:
        refuse(f"no pre-pinned digest for {repo}.db {date}")
    blob = get_bytes(pin["url"])
    check_db(blob, pin)
    tar = open_db(blob)
    try:
        for member in tar:
            if not member.name.endswith("/desc"):
                continue
            raw = tar.extractfile(member)
            if raw is None:
                continue
            fields = parse_desc(raw.read().decode("utf-8"))
            name = fields.get("NAME", [""])[0]
            filename = fields.get("FILENAME", [""])[0]
            sha = fields.get("SHA256SUM", [""])[0].lower()
            size = fields.get("CSIZE", [""])[0]
            if not name or not filename or len(sha) != 64 or not size.isdigit() or size == "0":
                refuse(f"{repo} database has an incomplete package record")
            if name in by_name:
                continue
            pkg = {
                "name": name,
                "filename": filename,
                "sha": sha,
                "size": int(size),
                "repo": repo,
                "depends": fields.get("DEPENDS", []),
            }
            by_name[name] = pkg
            for item in fields.get("PROVIDES", []):
                by_provide.setdefault(dep_base(item), pkg)
                by_provide.setdefault(item, pkg)
    finally:
        tar.close()


def lookup(token, by_name, by_provide):
    base = dep_base(token)
    if base in by_name:
        return by_name[base]
    if token in by_provide:
        return by_provide[token]
    return by_provide.get(base)


def closure(by_name, by_provide):
    order = []
    done = set()
    visiting = set()
    missing = []

    def visit(token):
        pkg = lookup(token, by_name, by_provide)
        if pkg is None:
            missing.append(token)
            return
        name = pkg["name"]
        if name in done or name in visiting:
            return
        visiting.add(name)
        for dep in pkg["depends"]:
            visit(dep)
        visiting.remove(name)
        done.add(name)
        order.append(pkg)

    for seed in SEEDS:
        visit(seed)
    if missing:
        refuse("unresolved dependencies: " + " ".join(sorted(set(missing))))
    return order


def cache_name(filename):
    # Epoch versions use ":" which is not a cache file name this repo accepts.
    name = filename.replace(":", "_")
    if name.startswith(".") or not re.fullmatch(r"[A-Za-z0-9._+-]{1,200}", name):
        refuse(f"cache name is not allowed: {filename}")
    return name


def write_pins(path, date, pkgs):
    lines = [
        "# Pinned inputs for install/stick/make-maint.sh.",
        "# One download per line, LF only, no comments on the same line:",
        "#   <sha256-lowercase> <size-bytes> <cache-name> <https-url>",
        "# cache-name is a single file name (no slash, no leading dot).",
        "# size-bytes is a positive decimal with no leading zero.",
        "# The URL is https, without userinfo, query, or fragment.",
        "# Blank lines and lines whose first non-space character is # are ignored.",
        "# Hashes below were checked against the downloaded bytes.",
        f"# Arch Linux Archive snapshot {date}, core and extra, x86_64.",
        "",
    ]
    seen = set()
    for pkg in pkgs:
        name = cache_name(pkg["filename"])
        if name in seen:
            refuse(f"duplicate cache name {name}")
        seen.add(name)
        url = archive_url(date, pkg["repo"], pkg["filename"])
        lines.append(f"{pkg['sha']} {pkg['size']} {name} {url}")
    text = "\n".join(lines) + "\n"
    tmp = path + ".gbos-partial"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main(argv):
    date = None
    cache = None
    out = None
    db_pins = DB_PINS
    plan = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--plan":
            plan = True
            i += 1
            continue
        if arg in ("--date", "--cache", "--out", "--db-pins"):
            if i + 1 >= len(argv) or not argv[i + 1] or argv[i + 1].startswith("--"):
                refuse(f"{arg} needs a value")
            value = argv[i + 1]
            i += 2
            if arg == "--date":
                date = value
            elif arg == "--cache":
                cache = value
            elif arg == "--db-pins":
                db_pins = value
            else:
                out = value
            continue
        refuse(f"unknown argument {arg}")
    if date is None or len(date) != 10 or date[4] != "/" or date[7] != "/":
        refuse("--date must be YYYY/MM/DD")
    if not plan and (cache is None or out is None):
        refuse("--cache and --out are required unless --plan")
    pins = read_db_pins(db_pins)
    for repo in ("core", "extra"):
        if (date, repo) not in pins:
            refuse(f"no pre-pinned digest for {repo}.db {date}")
    by_name = {}
    by_provide = {}
    # core wins when a name exists in both repositories
    load_repo(date, "core", by_name, by_provide, pins)
    load_repo(date, "extra", by_name, by_provide, pins)
    pkgs = closure(by_name, by_provide)
    total = sum(pkg["size"] for pkg in pkgs)
    print(f"packages {len(pkgs)}")
    print(f"bytes {total}")
    if plan:
        for pkg in pkgs:
            print(f"{pkg['repo']} {pkg['name']} {pkg['filename']}")
        return
    os.makedirs(cache, 0o755, exist_ok=True)
    fd, tmp_pins = tempfile.mkstemp(prefix="maint-pins-", suffix=".pins")
    os.close(fd)
    try:
        write_pins(tmp_pins, date, pkgs)
        rc = __import__("subprocess").run(
            [sys.executable, "-I", os.path.join(HERE, "maint-fetch.py"),
             "--file", tmp_pins, "--cache", cache]
        ).returncode
        if rc != 0:
            refuse(f"package download failed ({rc})")
        write_pins(out, date, pkgs)
    finally:
        try:
            os.unlink(tmp_pins)
        except OSError:
            pass
    print(f"pins {len(pkgs)}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
