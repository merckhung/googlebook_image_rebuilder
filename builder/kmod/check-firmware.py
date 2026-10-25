#!/usr/bin/env python3
"""Offline MODULE_FIRMWARE gate over the trees actually shipped by vendor.sh.

Read ELF .modinfo directly (the firmware fields modinfo -F firmware prints), so
Mac fixture tests need neither a target kernel nor host kmod. Never search host
/lib/firmware or the input download cache. Numeric versions are NOT assumed to
be interchangeable: a profile must document the driver's accepted alternatives.
"""
import argparse
import fnmatch
import gzip
import json
import lzma
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import devprofile

MODULE_FILE = re.compile(r"(.+)\.ko(?:\.(?:xz|gz|zst))?$")


def matches(name, pattern):
    # A filename wildcard cannot satisfy a request from another directory.
    parts, globs = name.split("/"), pattern.split("/")
    return len(parts) == len(globs) and all(fnmatch.fnmatchcase(p, g) for p, g in zip(parts, globs))


def firmware_names(data, filename):
    """Read every firmware= record, including repeats; malformed metadata fails."""
    if filename.endswith(".xz"):
        data = lzma.decompress(data)
    elif filename.endswith(".gz"):
        data = gzip.decompress(data)
    elif filename.endswith(".zst"):
        data = subprocess.run(["zstd", "-d", "-q", "-c"], input=data,
                              capture_output=True, check=True).stdout
    if (len(data) < 64 or data[:6] != b"\x7fELF\x02\x01"
            or struct.unpack_from("<HH", data, 16) != (1, 62)):
        raise ValueError("not an x86-64 little-endian relocatable ELF module")
    shoff, = struct.unpack_from("<Q", data, 40)
    entsize, count, names_index = struct.unpack_from("<HHH", data, 58)
    if (entsize != 64 or not count or names_index >= count
            or shoff < 64 or shoff + count * entsize > len(data)):
        raise ValueError("invalid ELF section table")
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * entsize) for i in range(count)]

    def body(section):
        offset, size = section[4:6]
        if offset + size > len(data):
            raise ValueError("truncated ELF section")
        return data[offset:offset + size]

    names = body(sections[names_index])
    info = []
    for section in sections:
        start = section[0]
        end = names.find(b"\0", start)
        if start >= len(names) or end < 0:
            raise ValueError("invalid ELF section name")
        if names[start:end] == b".modinfo":
            if section[1] != 1:
                raise ValueError(".modinfo is not PROGBITS")
            info.append(body(section))
    if len(info) != 1 or not info[0] or not info[0].endswith(b"\0"):
        raise ValueError("missing or malformed .modinfo")
    firmware = set()
    for record in info[0].split(b"\0"):
        if record.startswith(b"firmware="):
            name = record[len(b"firmware="):].decode("ascii")
            if (not re.fullmatch(r"[A-Za-z0-9._+*?\[\]/-]+", name)
                    or any(part in ("", ".", "..") for part in name.split("/"))):
                raise ValueError(f"invalid firmware name {name!r}")
            firmware.add(name)
    return sorted(firmware)


def initrd_modules(path):
    """Iterate regular module files in lapis' concatenated newc archives, in memory."""
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing initrd: {path.name}")
    data = path.read_bytes()
    if data.endswith(b"#BOOTCONFIG\n"):
        if len(data) < 20:
            raise ValueError("truncated initrd bootconfig")
        size, checksum = struct.unpack_from("<II", data, len(data) - 20)
        start = len(data) - 20 - size
        if start < 0 or sum(data[start:-20]) & 0xffffffff != checksum:
            raise ValueError("invalid initrd bootconfig")
        data = data[:start]
    offset = 0
    trailers = 0
    while offset < len(data):
        if data[offset] == 0:
            offset += 1
            continue
        if data[offset:offset + 6] != b"070701" or offset + 110 > len(data):
            raise ValueError("expected an uncompressed newc initrd")
        header = data[offset:offset + 110]
        mode = int(header[14:22], 16)
        size = int(header[54:62], 16)
        namesize = int(header[94:102], 16)
        endname = offset + 110 + namesize
        start = (endname + 3) & ~3
        end = start + size
        if namesize < 1 or endname > len(data) or data[endname - 1] != 0 or end > len(data):
            raise ValueError("truncated initrd entry")
        name = data[offset + 110:endname - 1].decode("ascii")
        if name == "TRAILER!!!":
            trailers += 1
        elif MODULE_FILE.fullmatch(Path(name).name):
            if not stat.S_ISREG(mode):
                raise ValueError(f"initrd module {name} is not a regular file")
            yield name, data[start:end]
        offset = (end + 3) & ~3
    if not trailers:
        raise ValueError("initrd has no newc trailer")


def files(root):
    # Ignore directory symlinks (e.g. /vendor/lib/modules -> /vendor_dlkm/lib/modules).
    # Those partitions are checked separately; never follow a host-absolute link.
    def fail(error):
        raise error

    for parent, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        dirs.sort()
        for name in sorted(names):
            yield Path(parent) / name


def installed_path(root, path):
    """Resolve links inside the image tree root, never against the host root."""
    pending = list(path.relative_to(root).parts)
    current = root
    links = 0
    while pending:
        part = pending.pop(0)
        if part == ".":
            continue
        if part == "..":
            if current == root:
                return None
            current = current.parent
            continue
        target = current / part
        if target.is_symlink():
            link = Path(os.readlink(target))
            links += 1
            if links > 40:
                return None
            if link.is_absolute():
                # An absolute target resolves from the device root, never against the
                # host or the tree root. Only the one mount point that really contains
                # this tree is remapped; everything else stays outside the tree.
                if root.name == "firmware":
                    if not link.is_relative_to("/vendor/firmware"):
                        return None
                    link = link.relative_to("/vendor/firmware")
                elif link.is_relative_to("/vendor"):
                    link = link.relative_to("/vendor")
                else:
                    return None
                current = root
            pending = list(link.parts) + pending
        else:
            current = target
            if pending and not current.is_dir():
                return None
    return current


def firmware_files(root):
    """Include in-image directory aliases without following escapes or cycles."""
    def walk(relative, ancestors):
        actual = installed_path(root, root / relative)
        if actual is None:
            return
        if actual.is_dir():
            if actual in ancestors:
                return
            for child in sorted(actual.iterdir()):
                yield from walk(relative / child.name, ancestors | {actual})
        elif actual.is_file() and actual.stat().st_size:
            yield relative.as_posix()

    return set(walk(Path(), set()))


def check(profile, vendor, module_trees, initrd, base_vendor):
    pid = profile["model"]["id"]
    report = {"profile": pid, "modules": [], "optional": [], "alternatives": [], "errors": [], "warnings": []}
    if not base_vendor.is_dir() or base_vendor.is_symlink():
        raise ValueError(f"missing original vendor module tree: {base_vendor.name}")
    fwroot = vendor / "firmware"
    if not fwroot.is_dir() or fwroot.is_symlink():
        raise ValueError("missing final vendor firmware directory: vendor/firmware")
    available = firmware_files(fwroot)

    rules = [(kind, rule) for kind, rows in profile["firmware_check"].items() for rule in rows]
    found = set()
    required = {}
    for field, names in (("profile.kmods", profile["kmods"]),
                         ("profile.wifi.modprobe", profile["wifi"]["modprobe"]),
                         ("profile.modules.load", profile["modules"]["load"]),
                         ("profile.modules.unblock", profile["modules"]["unblock"])):
        for name in names:
            required.setdefault(name, []).append(field)
    if profile["cmdline"]["pci_stub_ids"]:
        # image.py adds this exact vendor_dlkm module to the initrd after stage 1.
        required.setdefault("pci_stub", []).append("builder initrd addition")

    def inspect(source, filename, read, added=False):
        module = MODULE_FILE.fullmatch(filename)[1].replace("-", "_")
        found.add(module)
        reasons = required.get(module, []) + (["builder vendor addition/replacement"] if added else [])
        entry = {"module": module, "source": source, "firmware": [],
                 "scope": "required" if reasons else "report-only", "required_by": reasons}
        report["modules"].append(entry)
        try:
            names = firmware_names(read(), filename)
        except (ValueError, OSError, EOFError, subprocess.SubprocessError, lzma.LZMAError, zlib.error) as exc:
            if isinstance(exc, OSError) and exc.strerror:
                # OSError.filename is the module (already named by the source label) or a
                # failing command such as zstd; name the command, never leak a host path.
                if isinstance(exc.filename, (str, os.PathLike)) and Path(exc.filename).name != filename:
                    msg = f"{exc.strerror}: {Path(exc.filename).name}"
                else:
                    msg = f"{exc.strerror}: {source}"
            else:
                msg = str(exc)
            entry["error"] = msg
            report["errors" if reasons else "warnings"].append(f"profile {pid}: module {module} ({source}): {msg}")
            return
        for name in names:
            matching = [(kind, rule) for kind, rule in rules
                        if reasons and rule["module"] == module and matches(name, rule["firmware"])]
            result = {"firmware": name, "status": "present",
                      "matched": sorted(f for f in available if matches(f, name))}
            entry["firmware"].append(result)
            if len(matching) > 1:
                report["errors"].append(f"profile {pid}: module {module}: overlapping firmware rules for {name}")
                continue
            if matching:
                kind, rule = matching[0]
                result["reason"] = rule["reason"]
                if kind == "optional":
                    result["status"] = "optional-present" if result["matched"] else "optional-missing"
                    report["optional"].append({"module": module, "source": source, **result})
                    continue
                if not result["matched"]:
                    result["matched"] = sorted(f for f in available
                                               if any(matches(f, p) for p in rule["files"]))
                    if result["matched"]:
                        result["status"] = "alternative"
                        report["alternatives"].append({"module": module, "source": source, **result})
            if not result["matched"]:
                result["status"] = "missing"
                if reasons:
                    report["errors"].append(f"profile {pid}: module {module} ({source}): missing required firmware {name}")

    def read_module(path, source):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"module is not a regular file: {source}")
        return path.read_bytes()

    for root in [vendor, *module_trees]:
        label = "vendor" if root == vendor else root.name.removeprefix("firmware-")
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"missing shipped module tree: {label}")
        for path in files(root):
            if MODULE_FILE.fullmatch(path.name):
                rel = path.relative_to(root)
                source = f"{label}:{rel.as_posix()}"
                # Compare the final vendor readback with the untouched original.
                # Builder/kmod and hook additions in vendor are required; the
                # GBOS_DEV_KERNEL_DIR tree (lib/dev-modules) is scoped by the
                # profile like vendor_dlkm/system_dlkm.
                added = False
                if root == vendor and not rel.is_relative_to("lib/dev-modules"):
                    old = installed_path(base_vendor, base_vendor / rel)
                    added = (old is None or not old.is_file() or path.is_symlink()
                             or old.read_bytes() != path.read_bytes())
                inspect(source, path.name, lambda: read_module(path, source), added)
    for name, data in initrd_modules(initrd):
        inspect(f"initrd:{name}", Path(name).name, lambda: data)
    required_modules = set(required) | {m["module"] for m in report["modules"] if m["scope"] == "required"}
    for kind, rule in rules:
        if rule["module"] not in required_modules:
            report["warnings"].append(
                f"profile {pid}: unused {kind} firmware rule for report-only module "
                f"{rule['module']} ({rule['firmware']}): {rule['reason']}"
            )
    for name in sorted(required.keys() - found):
        report["errors"].append(f"profile {pid}: requested module {name} is absent from the shipped module trees/initrd")
    if not found:
        report["errors"].append(f"profile {pid}: no shipped modules found")
    report["summary"] = {}
    for scope in ("required", "report-only"):
        entries = [m for m in report["modules"] if m["scope"] == scope]
        declarations = [f for m in entries for f in m["firmware"]]
        report["summary"][scope] = {
            "modules": len(entries), "wanted": len(declarations),
            "missing": sum(f["status"] == "missing" for f in declarations),
            "metadata_errors": sum("error" in m for m in entries),
        }
    report["ok"] = not report["errors"]
    return report


def logical_roots(args):
    """Absolute build-host roots this run may name, mapped to their logical names.

    Only absolute host paths enter the mapping: a caller's relative spelling (v, i, .)
    is never treated as text to rewrite. A message may echo either the caller's
    absolute spelling or its resolved form, so both are mapped; a root that names
    nothing (only slashes: "/", "//", "///", or an empty value) is skipped, since
    replacing it would rewrite every path in the message. The repo root drops to
    nothing ("<repo>/profiles/_safety.toml" -> "profiles/_safety.toml"); the profile
    directory, each module tree root, the initrd, the report and the untouched base
    vendor tree keep their logical identity. Longest path first, so a nested root is
    replaced before its parent.
    """
    roots = {}

    def add(path, name):
        for candidate in (Path(path), Path(path).resolve()):
            if candidate.is_absolute() and candidate.name:
                roots[str(candidate)] = name

    add(Path(__file__).resolve().parents[2], "")
    add(args.profile, args.profile.name or str(args.profile))
    add(args.vendor, "vendor")
    for root in args.module_tree:
        add(root, root.name.removeprefix("firmware-"))
    add(args.initrd, "initrd")
    add(args.report, args.report.name)
    add(args.base_vendor, args.base_vendor.name)
    return sorted(roots.items(), key=lambda item: len(item[0]), reverse=True)


def scrub_paths(value, mapping):
    """Replace host roots with their logical names in diagnostic text.

    A root only matches between whole path boundaries, which are an explicit delimiter
    set rather than "not a path character": a left delimiter is the string start or one
    of whitespace ' " ( [ = , : ;; a right delimiter is the string end, "/", or one of
    whitespace ' " ) ] , : ; . So ordinary text that merely contains a root, such as
    "intel+<host>/vendor+suffix" or the sibling tree "<host>/vendor甲", is left intact.
    One left-to-right pass in a single compiled regex, with a named group per root: the
    logical name the pass just wrote is never scanned again, and the key is read from the
    match itself, so nothing has to strip a separator back off. The repo root maps to
    nothing and swallows the one separator that followed it.
    """
    if not mapping:
        return value
    logical = {}
    parts = []
    for index, (raw, name) in enumerate(mapping):
        group = f"root_{index}"
        token = f"(?P<{group}>{re.escape(raw)})"
        if name:
            # The separator after the root is kept; only a path boundary may follow.
            token += r"(?=(?:/|[\s'\")\],;:.]|$))"
        else:
            # The repo root drops, taking its one separator with it.
            token += r"(?:/|(?=[\s'\")\],;:.]|$))"
        logical[group] = name
        parts.append(token)
    pattern = re.compile(r"(?:^|(?<=[\s'\"(\[=,;:]))(?:" + "|".join(parts) + r")")

    def replace(match):
        return logical[match.lastgroup]

    def walk(node):
        if isinstance(node, str):
            return pattern.sub(replace, node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node

    return walk(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--vendor", required=True, type=Path)
    parser.add_argument("--base-vendor", required=True, type=Path,
                        help="untouched vendor extraction, to identify builder additions/replacements")
    parser.add_argument("--module-tree", required=True, action="append", type=Path)
    parser.add_argument("--initrd", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    pname = args.profile.name or str(args.profile)
    try:
        # Resolve the profile path: an error from the loader then carries an absolute
        # path the scrub can map, while a relative "--profile sub/generic-x86" still
        # reports the logical name "generic-x86".
        profile = devprofile.load(args.profile.resolve())
        report = check(profile, args.vendor, args.module_tree, args.initrd, args.base_vendor)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        if isinstance(exc, OSError) and exc.filename and isinstance(exc.filename, (str, os.PathLike)):
            msg = f"{exc.strerror}: {Path(exc.filename).name}"
        else:
            msg = str(exc)
        report = {"profile": pname, "ok": False,
                  "errors": [f"profile {pname}: firmware check failed: {msg}"]}
    # Only diagnostic text crosses the boundary: host build directories are replaced by
    # their logical names in the errors, the warnings, each module's error and everything
    # printed below. Data fields (module, source, firmware, matched, declared_by, path,
    # profile) are never rewritten.
    mapping = logical_roots(args)
    report["errors"] = scrub_paths(report.get("errors", []), mapping)
    if "warnings" in report:
        report["warnings"] = scrub_paths(report["warnings"], mapping)
    for entry in report.get("modules", []):
        if "error" in entry:
            entry["error"] = scrub_paths(entry["error"], mapping)

    def say(message, stream=sys.stdout):
        print(scrub_paths(message, mapping), file=stream)

    try:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        # Report the logical name, never the missing host directory or a traceback.
        say(f"firmware check: cannot write report {args.report.name}: {exc.strerror or exc}",
            sys.stderr)
        return 1
    for item in report.get("optional", []):
        say(f"OPTIONAL firmware: profile {report['profile']}: module {item['module']}: "
            f"{item['firmware']} ({item['status']}): {item['reason']}")
    for warning in report.get("warnings", []):
        if ": unused " in warning:
            say(f"UNUSED firmware rule: WARNING: {warning}")
    for error in report["errors"]:
        say("FAIL " + error, sys.stderr)
    if "summary" in report:
        stock = report["summary"]["report-only"]
        say(f"firmware report-only: {stock['modules']} stock modules; "
            f"{stock['missing']} missing firmware declarations; {stock['metadata_errors']} metadata errors")
    req = report.get("summary", {}).get("required", {"modules": 0, "wanted": 0})
    say(f"firmware check: {'PASS' if report['ok'] else 'FAIL'}; "
        f"required: {req['modules']} modules, {req['wanted']} firmware declarations; "
        f"{len(report.get('modules', []))} modules; report: {args.report.name}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
