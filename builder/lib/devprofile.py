#!/usr/bin/env python3
"""Device profiles: profiles/<model>/profile.toml, checked against profiles/_safety.toml (the project-wide rules).
Every value the builder needs for one model lives in its profile directory; builder/image.py, builder/vendor.sh,
builder/build.sh and builder/fetch-inputs.sh read it from here. Strict: unknown keys, missing keys and wrong types are
refused, and a profile can only add to the safety rules, never remove from them.
usage: devprofile.py check <profile dir>                     validate (exit 0 = ok)
       devprofile.py check-kconfig <profile dir> <kdir>      validate own-kernel .config against safety rules
       devprofile.py get <profile dir> <a.b>                 one string or boolean value, e.g. model.image_name
       devprofile.py firmware <profile dir>                  "<path> <from> <src> <sha256>" per firmware file
       devprofile.py kmods <profile dir>                     the extra kernel modules the profile ships, one per line
       devprofile.py json <profile dir>                      the validated profile as JSON"""
import bz2, gzip, hashlib, json, lzma, re, shutil, struct, subprocess, sys, zlib
from pathlib import Path

try:
    from compression import zstd as _zstd
except ImportError:
    _zstd = None

try:
    import tomllib
except ModuleNotFoundError:   # Python < 3.11
    sys.exit("devprofile.py: Python 3.11 or newer is needed (tomllib)")

SAFETY = Path(__file__).resolve().parents[2] / "profiles" / "_safety.toml"
LSTR = ("list", str)
SCHEMA = {
    "schema": int,
    "model": {"id": str, "name": str, "status": str, "image_name": str, "dmi": ("map", str)},
    "entries": {"quiet": str, "debug": str},
    "cmdline": {"stock_drop": LSTR, "add": LSTR, "tail": LSTR, "debug_drop": LSTR, "quiet_drop": LSTR,
                "pci_stub_ids": LSTR, "first_stage_drop": LSTR},
    "blacklist": {"modules": LSTR, "debug_add": LSTR, "quiet_add": LSTR, "allow": LSTR},
    "firmware_sources": {"linux_firmware_tag": str, "sof_bin_url": str, "sof_bin_sha256": str},
    "firmware": ("tables", {"path": str, "from": str, "src": str, "sha256": str}),
    "firmware_check": {
        "optional": ("tables", {"module": str, "firmware": str, "reason": str}),
        "alternatives": ("tables", {"module": str, "firmware": str, "files": LSTR, "reason": str}),
    },
    "audio": {"card": str, "config_dir": str, "sources": str},
    "wifi": {"modprobe": LSTR},
    # optional: setdefault in load(). unblock/load are module names, checked against _safety.toml [modules].
    "modules": {"unblock": LSTR, "load": LSTR},
    "display": {"stack": str, "fallback": str, "hwc_backend": str, "build_prop_set": ("list", ("list", str))},
    "input": {"kbd_backlight_leds": str, "power_button": str},
    "hooks": {"vendor": str},
    "kmods": LSTR,
    "camera": {"libcamera": bool},
}
# extra kernel modules the builder can build from pinned sources (builder/kmod/build-<name>.sh) and ship in vendor
KMODS = {"gbos_isa_emu": "ISA compatibility for lapis userspace on Alder Lake and KVM hosts",
         "ac": "ACPI AC adapter (ACPI0003): Android sees the charger; reads _PSR only, no EC host command",
         "gbos_efifb": "the UEFI GOP framebuffer as a DRM device (display.stack = gop-swiftshader), from the base's kheaders",
         "gbos_wakefilter": "drops the ACPI power button's resume KEY_WAKEUP when the button did not wake the system "
                            "(input.power_button = acpi): dark resumes stay dark"}
SAFETY_SCHEMA = {
    "schema": int,
    "cmdline": {"stock_drop": LSTR, "tail": LSTR, "forbidden_prefixes": LSTR},
    "blacklist": {"required": LSTR},
    # "<profile-id> <module> <vendor|dev>" allowlist. A profile's [modules] lists may only use its rows.
    "modules": {"unblock": LSTR, "load": LSTR},
}


class ProfileError(ValueError):
    pass


def _typed(value, spec, where):
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise ProfileError(f"{where}: expected a table")
        unknown = sorted(set(value) - set(spec))
        if unknown:
            raise ProfileError(f"{where}: unknown key(s) {', '.join(unknown)}")
        for k, s in spec.items():
            if k not in value:
                raise ProfileError(f"{where}: missing key {k}")
            _typed(value[k], s, f"{where}.{k}" if where else k)
    elif isinstance(spec, tuple) and spec[0] == "list":
        if not isinstance(value, list):
            raise ProfileError(f"{where}: expected a list")
        for i, x in enumerate(value):
            _typed(x, spec[1], f"{where}[{i}]")
    elif isinstance(spec, tuple) and spec[0] == "map":
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, spec[1]) for k, v in value.items()):
            raise ProfileError(f"{where}: expected a table of {spec[1].__name__}")
    elif isinstance(spec, tuple) and spec[0] == "tables":
        if not isinstance(value, list):
            raise ProfileError(f"{where}: expected an array of tables ([[{where}]])")
        for i, x in enumerate(value):
            _typed(x, spec[1], f"{where}[{i}]")
    elif spec is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProfileError(f"{where}: expected an integer")
    elif not isinstance(value, spec):
        raise ProfileError(f"{where}: expected {spec.__name__}")


TOKEN = re.compile(r"[\x21-\x7e]+")
MODULE = re.compile(r"[a-z0-9_]+")
# profiles/_safety.toml [modules] rows: "<profile-id> <module> <vendor|dev>"
# The module token matches MODULE ([a-z0-9_]+). A wider class cannot be selected:
# _check_module_choice still requires MODULE.fullmatch, so that row never matches a profile name.
MOD_ALLOW = re.compile(r"^([a-z0-9][a-z0-9-]*) ([a-z0-9_]+) (vendor|dev)$")
MODULE_DIR = {"vendor": "/vendor/lib/modules", "dev": "/vendor/lib/dev-modules"}
# Stock lapis vendor_dlkm lib/modules/modules.blocklist. The builder does not rewrite that
# partition; the file in the image every profile boots is the one line "blocklist snd_hda_intel"
# (the shipped vendor_dlkm, not a source file in this repo). unblock may name only these
# modules. load may not. This is the only copy of that list.
STOCK_MODULES_BLOCKLIST = frozenset({"snd_hda_intel"})


def _tokens(lst, where):
    for t in lst:
        if not TOKEN.fullmatch(t):
            raise ProfileError(f"{where}: {t!r} is not one kernel command line option")
    if len(set(lst)) != len(lst):
        raise ProfileError(f"{where}: duplicate items")


def _index_module_allow(s):
    """profile id -> ordered [(module, dir)] for modules.unblock and modules.load. Dir is vendor or dev."""
    parsed = {"unblock": {}, "load": {}}
    seen = set()
    required = set(s["blacklist"]["required"])
    storage = _storage_firmware_module_names()
    for kind in ("unblock", "load"):
        for item in s["modules"][kind]:
            m = MOD_ALLOW.fullmatch(item)
            if not m:
                raise ProfileError(f"_safety modules.{kind}: {item!r} is not 'profile module vendor|dev'")
            pid, name, dirname = m.group(1), m.group(2), m.group(3)
            # Profiles live beside the project _safety.toml, not beside a caller-supplied copy.
            if not (SAFETY.parent / pid / "profile.toml").is_file():
                raise ProfileError(
                    f"_safety modules.{kind}: {pid} has no profiles/{pid}/profile.toml")
            if kind == "unblock" and dirname != "vendor":
                raise ProfileError(
                    f"_safety modules.unblock: {name} must use the vendor dir "
                    "(modules.blocklist lives in vendor_dlkm)")
            if name in required:
                raise ProfileError(f"_safety modules.{kind}: {name} is a required blacklist module")
            if name in _NEVER_SHIP_KO:
                raise ProfileError(f"_safety modules.{kind}: {name} is in the never-ship set")
            if name in storage:
                raise ProfileError(f"_safety modules.{kind}: {name} is a storage or firmware-update module")
            if kind == "unblock" and name not in STOCK_MODULES_BLOCKLIST:
                raise ProfileError(
                    f"_safety modules.unblock: {name} is not on the stock modules.blocklist")
            if kind == "load" and name in STOCK_MODULES_BLOCKLIST:
                raise ProfileError(
                    f"_safety modules.load: {name} is on the stock modules.blocklist; use unblock")
            key = (kind, pid, name)
            if key in seen:
                raise ProfileError(f"_safety modules.{kind}: {pid} {name} listed twice")
            seen.add(key)
            parsed[kind].setdefault(pid, []).append((name, dirname))
    # Cannot fire: unblock names must be in STOCK_MODULES_BLOCKLIST and load names must
    # not, so one module cannot be on both lists.
    for pid, rows in parsed["unblock"].items():
        load_names = {n for n, _d in parsed["load"].get(pid, [])}
        for n, _d in rows:
            if n in load_names:
                raise ProfileError(f"_safety modules: {pid} {n} is both unblock and load")
    return parsed


def _check_module_choice(names, allowed_rows, blocked, where, pid):
    """names must be a subsequence of the allowlist. Every earlier row must already be chosen,
    whether that row is vendor or dev (bluetooth before btusb). Returns {module: dir}."""
    if not all(MODULE.fullmatch(x) for x in names):
        raise ProfileError(f"{where}: expected module names")
    if len(set(names)) != len(names):
        raise ProfileError(f"{where}: duplicate items")
    pos = {n: i for i, (n, _d) in enumerate(allowed_rows)}
    last = -1
    chosen = set()
    dirs = {}
    for n in names:
        if n not in pos:
            raise ProfileError(f"{where}: {n} is not allowlisted for {pid}")
        if n in blocked:
            raise ProfileError(f"{where}: {n} is on the module blacklist")
        i = pos[n]
        # Cannot fire: duplicate names are refused above, and every earlier allowlist row
        # must already be chosen, so this index is strictly increasing.
        if i <= last:
            raise ProfileError(f"{where}: {n} is out of allowlist order for {pid}")
        for pred, _pred_dir in allowed_rows[:i]:
            if pred not in chosen:
                raise ProfileError(f"{where}: {n} requires {pred} earlier in the list")
        last = i
        chosen.add(n)
        dirs[n] = allowed_rows[i][1]
    return dirs


def load_safety(path=SAFETY):
    with open(path, "rb") as f:
        s = tomllib.load(f)
    _typed(s, SAFETY_SCHEMA, "")
    if s["schema"] != 1:
        raise ProfileError(f"{path}: schema {s['schema']} is not supported")
    for k in ("stock_drop", "tail"):
        _tokens(s["cmdline"][k], f"_safety cmdline.{k}")
    if "efi=noruntime" not in s["cmdline"]["tail"] or not any(t.startswith("initcall_blacklist=") for t in s["cmdline"]["tail"]):
        raise ProfileError("_safety cmdline.tail must carry efi=noruntime and an initcall_blacklist=")
    if not all(MODULE.fullmatch(m) for m in s["blacklist"]["required"]):
        raise ProfileError("_safety blacklist.required: not module names")
    s["_modules"] = _index_module_allow(s)
    return s


def load(profile_dir, safety_path=SAFETY):
    d = Path(profile_dir)
    with open(d / "profile.toml", "rb") as f:
        p = tomllib.load(f)
    p.setdefault("firmware", [])       # [[firmware]] tables: a profile without device firmware simply has none
    p.setdefault("firmware_check", {})
    if isinstance(p["firmware_check"], dict):
        p["firmware_check"].setdefault("optional", [])
        p["firmware_check"].setdefault("alternatives", [])
    if isinstance(p.get("display"), dict):
        p["display"].setdefault("fallback", "")
    p.setdefault("kmods", [])          # extra kernel modules (KMODS): none unless the profile lists them
    p.setdefault("camera", {"libcamera": False})   # the libcamera camera HAL overlay (hooks.vendor camera()): off
    if "modules" not in p:
        p["modules"] = {"unblock": [], "load": []}
    elif isinstance(p.get("modules"), dict):
        p["modules"].setdefault("unblock", [])
        p["modules"].setdefault("load", [])
    if isinstance(p.get("cmdline"), dict):
        p["cmdline"].setdefault("quiet_drop", [])
    if isinstance(p.get("blacklist"), dict):
        p["blacklist"].setdefault("quiet_add", [])
    # [gralloc] is optional. A profile that asks (amd-hx370) pins libminigbm_gralloc.so.
    # kano and generic-x86 leave the key absent, so the stock file stays.
    gralloc = p.pop("gralloc", None)
    _typed(p, SCHEMA, "")
    if gralloc is not None:
        _typed({"gralloc": gralloc}, {"gralloc": {"sha256": str}}, "")
        if not re.fullmatch(r"[0-9a-f]{64}", gralloc["sha256"]):
            raise ProfileError("gralloc.sha256: expected 64 lower-case hex digits")
        p["gralloc"] = {"sha256": gralloc["sha256"]}
    s = load_safety(safety_path)
    if p["schema"] != 1:
        raise ProfileError(f"schema {p['schema']} is not supported")
    m = p["model"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", m["id"]) or m["id"] != d.resolve().name:
        raise ProfileError(f"model.id {m['id']!r} must be lower-case and equal to the directory name {d.resolve().name!r}")
    if m["status"] not in ("verified", "experimental", "probe-only"):
        raise ProfileError("model.status must be verified, experimental or probe-only")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.img", m["image_name"]):
        raise ProfileError("model.image_name must be a plain file name ending in .img")
    for k, t in p["entries"].items():
        if not t or not t.isprintable():
            raise ProfileError(f"entries.{k}: empty or not printable")
    c = p["cmdline"]
    for k in ("stock_drop", "add", "tail", "debug_drop", "quiet_drop"):
        _tokens(c[k], f"cmdline.{k}")
    # the builder (module_blacklist=, the safety tail's initcall_blacklist= and efi=) owns these; a profile option could
    # override them later on the line, so no profile option may start with one of them
    for k in ("add", "tail"):
        for t in c[k]:
            for pre in s["cmdline"]["forbidden_prefixes"]:
                if t.startswith(pre):
                    raise ProfileError(f"cmdline.{k}: {t} is not allowed in a profile (prefix {pre})")
    if (set(c["stock_drop"]) | set(c["debug_drop"]) | set(c["quiet_drop"])) & set(s["cmdline"]["tail"]):
        raise ProfileError("cmdline.stock_drop/debug_drop/quiet_drop must not remove a safety option")
    if not all(re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", x) for x in c["pci_stub_ids"]):
        raise ProfileError("cmdline.pci_stub_ids: expected vvvv:dddd (lower-case hex)")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+\.ko", x) for x in c["first_stage_drop"]):
        raise ProfileError("cmdline.first_stage_drop: expected module file names (name.ko)")
    b = p["blacklist"]
    for k in ("modules", "debug_add", "quiet_add", "allow"):
        if not all(MODULE.fullmatch(x) for x in b[k]) or len(set(b[k])) != len(b[k]):
            raise ProfileError(f"blacklist.{k}: expected distinct module names")
    missing = [x for x in s["blacklist"]["required"] if x not in b["modules"]]
    if missing:
        raise ProfileError(f"blacklist.modules lacks required module(s): {', '.join(missing)}")
    if set(b["allow"]) & (set(b["modules"]) | set(b["debug_add"]) | set(b["quiet_add"]) | set(s["blacklist"]["required"])):
        raise ProfileError("blacklist.allow overlaps the blacklist or the required modules")
    blocked = set(b["modules"]) | set(b["debug_add"]) | set(b["quiet_add"]) | set(s["blacklist"]["required"])
    chosen_dirs = {}
    for kind in ("unblock", "load"):
        chosen_dirs.update(_check_module_choice(
            p["modules"][kind], s["_modules"][kind].get(m["id"], []), blocked, f"modules.{kind}", m["id"]))
    # Cannot fire: _index_module_allow already rejects a name on both lists, and a profile
    # name that is not allowlisted for that list fails in _check_module_choice.
    overlap = set(p["modules"]["unblock"]) & set(p["modules"]["load"])
    if overlap:
        raise ProfileError("modules: listed in both unblock and load: " + ", ".join(sorted(overlap)))
    p["_module_dirs"] = chosen_dirs
    fs = p["firmware_sources"]
    seen_rules = set()
    for kind, rules in p["firmware_check"].items():
        for i, rule in enumerate(rules):
            where = f"firmware_check.{kind}[{i}]"
            if not MODULE.fullmatch(rule["module"]):
                raise ProfileError(f"{where}.module: expected an exact module name (use underscores)")
            if not rule["reason"].strip() or any(ord(c) < 32 for c in rule["reason"]):
                raise ProfileError(f"{where}.reason: expected a non-empty, single-line reason")
            patterns = [rule["firmware"]] + rule.get("files", [])
            for pattern in patterns:
                if (not re.fullmatch(r"[A-Za-z0-9._+*?\[\]/-]+", pattern)
                        or any(part in ("", ".", "..") for part in pattern.split("/"))):
                    raise ProfileError(f"{where}: expected relative firmware paths/globs")
            if kind == "alternatives" and not rule["files"]:
                raise ProfileError(f"{where}.files: expected at least one accepted alternative")
            key = (rule["module"], rule["firmware"])
            if key in seen_rules:
                raise ProfileError(f"{where}: duplicate module/firmware rule")
            seen_rules.add(key)
    paths = set()
    for i, fw in enumerate(p["firmware"]):
        if fw["from"] not in ("linux-firmware", "sof-bin"):
            raise ProfileError(f"firmware[{i}].from must be linux-firmware or sof-bin")
        for k in ("path", "src"):
            v = fw[k]
            if not re.fullmatch(r"[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*", v) or ".." in v.split("/"):
                raise ProfileError(f"firmware[{i}].{k}: not a plain relative path")
        if not re.fullmatch(r"[0-9a-f]{64}", fw["sha256"]):
            raise ProfileError(f"firmware[{i}].sha256: expected 64 lower-case hex digits")
        if fw["path"] in paths:
            raise ProfileError(f"firmware: {fw['path']} listed twice")
        paths.add(fw["path"])
    if any(fw["from"] == "linux-firmware" for fw in p["firmware"]) and not re.fullmatch(r"[0-9]{8}", fs["linux_firmware_tag"]):
        raise ProfileError("firmware_sources.linux_firmware_tag: expected a linux-firmware release tag (YYYYMMDD)")
    if any(fw["from"] == "sof-bin" for fw in p["firmware"]) and not (
            fs["sof_bin_url"].startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", fs["sof_bin_sha256"])):
        raise ProfileError("firmware_sources: sof-bin files need an https sof_bin_url and its sof_bin_sha256")
    a = p["audio"]
    for k in ("card", "config_dir"):
        if a[k] and not re.fullmatch(r"[A-Za-z0-9._-]+", a[k]):
            raise ProfileError(f"audio.{k}: not a plain name")
    if a["sources"] and not (d / a["sources"]).is_file():
        raise ProfileError(f"audio.sources: {a['sources']} is not a file in {d}")
    if not all(MODULE.fullmatch(x) for x in p["wifi"]["modprobe"]):
        raise ProfileError("wifi.modprobe: expected module names")
    disp = p["display"]
    # native: the image's own GPU stack (KANO: xe + Mesa ANV); gop-swiftshader: machines whose GPU has no driver in this
    # kernel (for example AMD, QEMU; CPU: Intel 12th gen / AMD Zen 5 or newer, see docs/COMPATIBILITY.en.md "Minimum CPU"): the firmware's GOP framebuffer (gbos_efifb.ko) and SwiftShader from Cuttlefish
    if disp["stack"] not in ("native", "gop-swiftshader"):
        raise ProfileError("display.stack must be native or gop-swiftshader")
    if disp["fallback"] not in ("", "gop-swiftshader") or (disp["fallback"] and disp["stack"] != "native"):
        raise ProfileError("display.fallback is empty or gop-swiftshader for a native stack")
    if (disp["stack"] == "gop-swiftshader" or bool(disp["fallback"])) != ("gbos_efifb" in p["kmods"]):
        raise ProfileError("display.stack = gop-swiftshader and kmods = [\"gbos_efifb\"] go together")
    if disp["hwc_backend"] not in ("", "client"):
        raise ProfileError("display.hwc_backend must be empty or client")
    for i, kv in enumerate(disp["build_prop_set"]):
        if len(kv) != 3 or not re.fullmatch(r"[A-Za-z0-9_.]+", kv[0]) or kv[0].startswith(("ro.adb.", "service.adb.", "persist.adb.", "ro.debuggable")):
            raise ProfileError(f"display.build_prop_set[{i}]: expected [property, old value, new value] (not an adb/debuggable property)")
    inp = p["input"]
    if inp["kbd_backlight_leds"] and not re.fullmatch(r"/sys/devices/[A-Za-z0-9:._/-]+/leds", inp["kbd_backlight_leds"]):
        raise ProfileError("input.kbd_backlight_leds: expected empty or /sys/devices/.../leds")
    if inp["power_button"] not in ("acpi", "stock"):
        raise ProfileError("input.power_button must be acpi or stock")
    if "gbos_wakefilter" in p["kmods"] and inp["power_button"] != "acpi":
        raise ProfileError("kmods gbos_wakefilter filters the ACPI power button: it needs input.power_button = acpi")
    unknown = [k for k in p["kmods"] if k not in KMODS]
    if unknown or len(set(p["kmods"])) != len(p["kmods"]):
        raise ProfileError(f"kmods: expected distinct names from {', '.join(sorted(KMODS))}")
    h = p["hooks"]["vendor"]
    if h and not (re.fullmatch(r"[a-z0-9_]+\.py", h) and (d / h).is_file()):
        raise ProfileError(f"hooks.vendor: {h} is not a Python file in {d}")
    needs_hook = [k for k, v in (("audio.config_dir", a["config_dir"]), ("wifi.modprobe", p["wifi"]["modprobe"]),
                                 ("display.hwc_backend", disp["hwc_backend"]), ("display.build_prop_set", disp["build_prop_set"]),
                                 ("input.kbd_backlight_leds", inp["kbd_backlight_leds"]),
                                 ("input.power_button = acpi", inp["power_button"] == "acpi"),
                                 ("camera.libcamera", p["camera"]["libcamera"])) if v]
    if needs_hook and not h:
        raise ProfileError(f"{', '.join(needs_hook)} need a hooks.vendor file (see profiles/kano/vendor.py)")
    p["_dir"] = str(d.resolve())
    p["_safety"] = s
    return p


def module_specs(prof):
    """(module, dir) for this profile: [modules].load, then [modules].unblock."""
    dirs = prof["_module_dirs"]
    return [(n, dirs[n]) for n in prof["modules"]["load"] + prof["modules"]["unblock"]]


def module_boot_lines(prof):
    """Indented init `exec` lines. Empty when the profile lists no modules.

    modprobe is invoked without -b. ueventd passes -b and therefore honors the stock
    vendor_dlkm modules.blocklist; this command does not. Only STOCK_MODULES_BLOCKLIST
    names may be unblock (snd_hda_intel today), so that file is not applied and the
    partition is not rewritten. load must not name those modules. vendor_dlkm is a
    logical partition inside super. The builder rebuilds only vendor_a (mkfs.erofs,
    avbtool add_hashtree_footer, update_partition_descriptor) and leaves every other
    logical partition byte-identical. `exec` waits, so an earlier row (bluetooth before
    btusb, vendor or dev) is in the kernel before the next modprobe. Output order is
    load, then unblock, so a codec load runs before snd_hda_intel.
    """
    specs = module_specs(prof)
    if not specs:
        return ""
    return "".join(
        "    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname="
        + MODULE_DIR[kind] + " " + name + "\n"
        for name, kind in specs)


def append_module_boot(hw_head, prof):
    """Append module_boot_lines to an early-boot rc head. Returns hw_head unchanged when there is nothing to add."""
    lines = module_boot_lines(prof)
    if not lines:
        return hw_head
    if not hw_head:
        return "on early-boot\n" + lines + "\n"
    return hw_head.rstrip("\n") + "\n" + lines + "\n"


def missing_dev_modules(prof, kdir):
    """Dev-dir module names whose .ko is not a top-level file in <kdir>/modules. Empty when none are requested."""
    need = [n for n, kind in module_specs(prof) if kind == "dev"]
    if not need:
        return []
    root = Path(kdir) / "modules" if kdir else None
    missing = []
    for n in need:
        fns = (f"{n}.ko", f"{n.replace('_', '-')}.ko")
        if root is None or not any((root / fn).is_file() for fn in fns):
            missing.append(n)
    return missing


def stock_tokens(vendor_cmdline, prof):
    """The stock vendor_boot options that every entry starts from: minus the safety and profile drop lists and the stock
    module_blacklist=/initcall_blacklist= items, which the builder replaces."""
    drop = set(prof["_safety"]["cmdline"]["stock_drop"]) | set(prof["cmdline"]["stock_drop"])
    return [o for o in vendor_cmdline.split() if o not in drop and not o.startswith(("module_blacklist=", "initcall_blacklist="))]


SERIAL = ("console=", "earlycon=")
DEBUG_ONLY = ("keep_bootcon", "printk.devkmsg=on")
# Lapis kernel 6.18.21 (research/amd/lapis-kernel.config:1-3,521-523), commit
# 3938c8d97142d5cf31543aa96f864a2cf832dda0. arch/x86/include/asm/setup.h:7 sets
# COMMAND_LINE_SIZE to 2048, including the terminating NUL. arch/x86/kernel/setup.c:916-924,
# when CONFIG_CMDLINE_OVERRIDE is unset, does strlcat(builtin_cmdline, " ", COMMAND_LINE_SIZE)
# and then strlcat of the bootloader cmdline, so the bootloader string may occupy
# 2048 - 1 - len(CONFIG_CMDLINE) - 1 bytes. The last 1 is the separating space; without it
# the budget would be 1974, and the kernel would truncate one byte earlier.
COMMAND_LINE_SIZE = 2048
KERNEL_CONFIG_CMDLINE = "console=ttynull stack_depot_disable=on cgroup_disable=pressure bootconfig"
X86_BOOT_CMDLINE_MAX = COMMAND_LINE_SIZE - 1 - len(KERNEL_CONFIG_CMDLINE) - 1  # 1973
# kano-boot.sh can type extra debug parameters onto a serial-debug entry. Keep this many
# bytes free under the kernel budget so that append is not what gets truncated.
CMDLINE_HEADROOM = 64
# systemd-boot prepends this to the entry's options= line before the kernel sees it (image.py counts it).
BOOT_CMDLINE_PREFIX = "initrd=\\initrd.img "


def boot_cmdline_len(opts):
    """Bytes of the bootloader command line, including the systemd-boot initrd= prefix."""
    return len(" ".join(opts)) + len(BOOT_CMDLINE_PREFIX)


def cmdline_within_limit(n):
    """True when n bytes fit in the lapis x86 command line with CMDLINE_HEADROOM still free."""
    return n <= X86_BOOT_CMDLINE_MAX - CMDLINE_HEADROOM


def make_entries(prof, vendor_cmdline):
    """The options of the serial-debug and quiet entries, and the "normal" options (no pci-stub and no serial-debug
    extras; no entry uses them since the plain entry was dropped, guards() reads its module_blacklist), as token lists."""
    c, b = prof["cmdline"], prof["blacklist"]
    opts = stock_tokens(vendor_cmdline, prof) + c["add"]
    tail = prof["_safety"]["cmdline"]["tail"] + c["tail"]
    normal = opts + ["module_blacklist=" + ",".join(b["modules"])] + tail
    debug = [o for o in opts if o not in c["debug_drop"]] + \
        ["module_blacklist=" + ",".join(b["modules"] + b["debug_add"])] + tail + list(DEBUG_ONLY) + \
        (["pci-stub.ids=" + ",".join(c["pci_stub_ids"])] if c["pci_stub_ids"] else [])
    # quiet: the debug entry's hardware set without any serial console (the 115200-baud console is synchronous),
    # plus any profile-specific quiet_drop / quiet_add (e.g. amd-hx370's default safe entry blacklists amdgpu and
    # drops amdgpu.* cmdline options, while the debug entry is the experimental native amdgpu entry).
    quiet_drop = set(c.get("quiet_drop", ()))
    quiet_add = b.get("quiet_add", [])
    quiet = []
    for o in debug:
        if o.startswith(SERIAL) or o in DEBUG_ONLY + ("ignore_loglevel", "loglevel=8") or o in quiet_drop:
            continue
        if o.startswith("module_blacklist=") and quiet_add:
            mods = o.split("=", 1)[1].split(",")
            for m in quiet_add:
                if m not in mods:
                    mods.append(m)
            o = "module_blacklist=" + ",".join(mods)
        quiet.append(o)
    quiet.append("loglevel=4")
    return {"normal": normal, "debug": debug, "quiet": quiet}


def guards(prof):
    """(modules, initcalls, plain options) that every entry of this profile carries: the normal module_blacklist, the
    initcall_blacklist= and the efi= items of the tail."""
    tail = prof["_safety"]["cmdline"]["tail"] + prof["cmdline"]["tail"]
    ic = [x.split("=", 1)[1].split(",") for x in tail if x.startswith("initcall_blacklist=")]
    if len(ic) != 1:
        raise ProfileError("the tail must have exactly one initcall_blacklist=")
    return set(prof["blacklist"]["modules"]), ic[0], [x for x in tail if x.startswith("efi=")]


# Own-kernel (.config, bzImage, System.map) safety rules when GBOS_DEV_KERNEL_DIR overrides the stock GKI kernel.
# All Kconfig symbols, Makefile targets (.ko module names), and initcall C symbol names below are derived from the
# pinned Android GKI kernel source tree kernel/common @ 3938c8d97142d5cf31543aa96f864a2cf832dda0 (build 16302624).
#
# Every driver that profiles/_safety.toml (57 required modules) or a profile's blacklist.modules relies on blocking via
# module_blacklist= must NOT be built into vmlinux (=y), because module_blacklist= only stops loadable modules (or
# initcalls whose C function name happens to equal the module name, which is false for e.g. intel_spi_pci_driver_init).
#
# Coverage of the 57 modules in profiles/_safety.toml blacklist.required (the v0.1 hard blacklist, pinned so the
# KANO cmdline stays token-for-token with the former builder). 55 of them match an in-tree symbol in
# KCONFIG_NEVER_BUILTIN / KCONFIG_BUILTIN_MODULE (or the SATA names libata / ahci / ahci_platform). The other two,
# cros_ec_usb and cros_ec_pwrmon, are Google out-of-tree vendor_dlkm modules from stock lapis
# (6.12.23-android16-5-ga3bbc4527b7b) and have no Kconfig symbol in kernel/common 3938c8d97142.
# _safety.toml does not list intel_thc_hid. CONFIG_INTEL_THC_HID (drivers/hid/intel-thc-hid/Kconfig:7, Makefile:8)
# builds intel-thc.o, module intel_thc; =y is refused and =m requires intel_thc in blacklist.modules.
# libahci is co-built with ahci / ahci_platform (drivers/ata/Makefile:6). Existing profiles do not list ahci, so
# CONFIG_SATA_AHCI=m is refused for that reason. A profile that adds ahci must also add libahci.
# CONFIG_CROS_EC_UART and CONFIG_CROS_EC_RPMSG are not in the v0.1 required list (adding them would change the
# pinned KANO cmdline). The gate still refuses =y, and refuses =m unless that profile's blacklist names the
# module. EC variants also append both names via builder/image.py EC_EXTRA_BLOCK.
#
# Raw memory / port / MSR posture (verified against stock lapis-kernel.config and the Track 2 dev kernel .config):
#   - CONFIG_DEVMEM, CONFIG_DEVPORT, CONFIG_PROC_KCORE, CONFIG_X86_MSR, CONFIG_ACPI_CUSTOM_METHOD,
#     CONFIG_ACPI_EC_DEBUGFS, CONFIG_ACPI_DEBUGGER_USER, CONFIG_ACPI_CONFIGFS, CONFIG_ACPI_CUSTOM_DSDT are all unset in
#     stock lapis, so /dev/mem, /dev/port, /proc/kcore, /dev/cpu/*/msr and ACPI debugfs/configfs injection nodes do not
#     exist at all. (In lib/Kconfig.debug:1878,1898, CONFIG_STRICT_DEVMEM depends on DEVMEM and CONFIG_IO_STRICT_DEVMEM
#     depends on STRICT_DEVMEM, so Kconfig writes '# CONFIG_STRICT_DEVMEM is not set' when CONFIG_DEVMEM is unset.)
#   - CONFIG_HAS_IOPORT=y and CONFIG_X86_IOPL_IOPERM=y remain enabled in stock lapis and the Track 2 dev kernel because
#     standard x86_64 PCI/legacy drivers select HAS_IOPORT; privileged root processes holding CAP_SYS_RAWIO are part of
#     the documented root-equivalency boundary in docs/SAFETY.md.
#   - Internal SD/eMMC over PCI/ACPI is blocked via CONFIG_MMC_SDHCI_PCI / CONFIG_MMC_SDHCI_ACPI (sdhci_pci, sdhci_acpi);
#     other non-PCI/ACPI MMC host drivers remain a documented limitation (HX 370 uses NVMe).

# Built-in (=y) drivers in stock GKI 3938c8d97142 that the safety tail blocks at boot via initcall_blacklist=
# (init/main.c:1270 initcall_blacklist(), :1292 initcall_blacklisted(), :1302 sprint_symbol_no_offset()).
# Mapping: Kconfig_symbol -> (initcall_c_symbol, module_name_when_m, kernel_source_citation)
KCONFIG_INITCALL_BLACKLIST = {
    "CONFIG_BLK_DEV_NVME": (
        "nvme_init",
        "nvme",
        "drivers/nvme/host/Kconfig:5, drivers/nvme/host/Makefile:6 (nvme.o), drivers/nvme/host/pci.c:4013,4033 module_init(nvme_init)",
    ),
    "CONFIG_INTEL_MEI_ME": (
        "mei_me_driver_init",
        "mei_me",
        "drivers/misc/mei/Kconfig:17, drivers/misc/mei/Makefile:17 (mei-me.o), drivers/misc/mei/pci-me.c:523,533 module_pci_driver(mei_me_driver)",
    ),
}

KCONFIG_NEVER_YM = (
    # fs/efivarfs/Kconfig:2, fs/efivarfs/Makefile:6 (efivarfs.o), fs/efivarfs/super.c:539,555 module_init(efivarfs_init)
    ("CONFIG_EFIVAR_FS", "exposes UEFI NVRAM variables for writing via efivarfs (fs/efivarfs/Kconfig:2, super.c:555)"),
    # drivers/firmware/efi/Kconfig:118, drivers/firmware/efi/Makefile:41 (capsule-loader.o), capsule-loader.c:320,333 module_init(efi_capsule_loader_init)
    ("CONFIG_EFI_CAPSULE_LOADER", "exposes /dev/efi_capsule_loader for UEFI firmware capsule updates (drivers/firmware/efi/Kconfig:118, capsule-loader.c:333)"),
    # drivers/firmware/efi/Kconfig:170 bool, depends on EFI_STUB. libstub/x86-stub.c:912 calls
    # efi_enable_reset_attack_mitigation() before exit_boot() at :926. libstub/tpm.c:42 writes
    # NV MemoryOverwriteRequestControl while boot services are still alive, so efi=noruntime
    # (which only disables runtime services) does not stop it. Lapis leaves this unset
    # (research/amd/lapis-kernel.config:2118). The only other libstub set_efi_var is
    # random.c:142, which deletes RandomSeed and is not a Kconfig option.
    ("CONFIG_RESET_ATTACK_MITIGATION", "EFI stub writes NV MemoryOverwriteRequestControl at every boot before efi=noruntime applies (drivers/firmware/efi/Kconfig:170, libstub/x86-stub.c:912, libstub/tpm.c:42)"),
    # Firmware update via ACPI, not an EFI runtime service, so efi=noruntime does not cover it.
    # Same class as CONFIG_EFI_CAPSULE_LOADER. Lapis leaves it unset. The pinned driver was not re-read this round.
    ("CONFIG_ACPI_PFRUT", "ACPI Platform Firmware Runtime Update is a firmware-update interface; efi=noruntime does not cover it"),
    # drivers/char/Kconfig:311, drivers/char/Makefile:6 (mem.o), drivers/char/mem.c:691,746,775 fs_initcall(chr_dev_init)
    ("CONFIG_DEVMEM", "exposes /dev/mem raw physical memory and MMIO access (drivers/char/Kconfig:311, mem.c:691)"),
    # drivers/char/Kconfig:343, drivers/char/Makefile:6 (mem.o), drivers/char/mem.c:368,695,775 fs_initcall(chr_dev_init)
    ("CONFIG_DEVPORT", "exposes /dev/port raw I/O port access (0xCF8/0xCFC PCI config, 0x62/0x66/0x70/0x71 EC/CMOS; drivers/char/Kconfig:343, mem.c:695)"),
    # fs/proc/Kconfig:32, fs/proc/Makefile:32 (kcore.o), fs/proc/kcore.c:723,742 fs_initcall(proc_kcore_init)
    ("CONFIG_PROC_KCORE", "exposes /proc/kcore raw kernel virtual memory image disclosure interface (fs/proc/Kconfig:32, kcore.c:742)"),
    # arch/x86/Kconfig:1390, arch/x86/kernel/Makefile:90 (msr.o), arch/x86/kernel/msr.c:262,287 module_init(msr_init)
    ("CONFIG_X86_MSR", "exposes /dev/cpu/*/msr raw model-specific register read/write access (arch/x86/Kconfig:1390, msr.c:287)"),
    # Legacy debugfs acpi/custom_method AML injection interface (removed upstream prior to 3938c8d97142; kept fail-closed)
    ("CONFIG_ACPI_CUSTOM_METHOD", "exposes debugfs acpi/custom_method for arbitrary AML method injection"),
    # drivers/acpi/Kconfig:143, drivers/acpi/Makefile:104 (ec_sys.o), drivers/acpi/ec_sys.c:143 module_init(acpi_ec_sys_init)
    ("CONFIG_ACPI_EC_DEBUGFS", "exposes /sys/kernel/debug/ec direct Embedded Controller read/write access (drivers/acpi/Kconfig:143, Makefile:104, ec_sys.c:143)"),
    # drivers/acpi/Kconfig:79, drivers/acpi/Makefile:108 (acpi_dbg.o), drivers/acpi/acpi_dbg.c:780 module_init(acpi_aml_init)
    ("CONFIG_ACPI_DEBUGGER_USER", "exposes /sys/kernel/debug/acpi/acpidbg userspace AML debugger interface (drivers/acpi/Kconfig:79, Makefile:108, acpi_dbg.c:780)"),
    # drivers/acpi/Kconfig:516, drivers/acpi/Makefile:124 (acpi_configfs.o), drivers/acpi/acpi_configfs.c:275 module_init(acpi_configfs_init)
    ("CONFIG_ACPI_CONFIGFS", "exposes configfs ACPI table injection interface (drivers/acpi/Kconfig:516, Makefile:124, acpi_configfs.c:275)"),
    # drivers/acpi/Kconfig:368, drivers/acpi/Makefile:15 (tables.o)
    ("CONFIG_ACPI_CUSTOM_DSDT", "links a custom DSDT AML table into the kernel (drivers/acpi/Kconfig:368)"),
)

KCONFIG_NEVER_BUILTIN = (
    # TPM / Cr50 / Ti50 / AMD fTPM (blacklist: tpm, tpm_tis, tpm_tis_core, tpm_tis_spi, tpm_tis_i2c_cr50, tpm_crb)
    # drivers/char/tpm/Kconfig:6, drivers/char/tpm/Makefile:5 (tpm.o), drivers/char/tpm/tpm-interface.c:558 subsys_initcall(tpm_init)
    ("CONFIG_TCG_TPM", "TPM core built-in (=y) bypasses module_blacklist=tpm and can alter PCRs / trigger BitLocker recovery (drivers/char/tpm/Kconfig:6, tpm-interface.c:558)"),
    # drivers/char/tpm/Kconfig:62, drivers/char/tpm/Makefile:25 (tpm_tis.o), drivers/char/tpm/tpm_tis.c:430 module_init(init_tis)
    ("CONFIG_TCG_TIS", "TPM TIS built-in (=y) bypasses module_blacklist=tpm_tis (drivers/char/tpm/Kconfig:62, tpm_tis.c:430)"),
    # drivers/char/tpm/Kconfig:56, drivers/char/tpm/Makefile:24 (tpm_tis_core.o). Library objects, no module_init of their own.
    ("CONFIG_TCG_TIS_CORE", "TPM TIS core built-in (=y) bypasses module_blacklist=tpm_tis_core (drivers/char/tpm/Kconfig:56, Makefile:24)"),
    # drivers/char/tpm/Kconfig:73, drivers/char/tpm/Makefile:28 (tpm_tis_spi.o), drivers/char/tpm/tpm_tis_spi_main.c:359 module_spi_driver(tpm_tis_spi_driver)
    ("CONFIG_TCG_TIS_SPI", "TPM TIS SPI built-in (=y) bypasses module_blacklist=tpm_tis_spi (drivers/char/tpm/Kconfig:73, tpm_tis_spi_main.c:359)"),
    # CONFIG_TCG_TIS_SPI_CR50 is a bool folded into tpm_tis_spi.o (Kconfig:85-86, Makefile:30). It is not in
    # KCONFIG_NEVER_BUILTIN; check_kconfig allows =y only when CONFIG_TCG_TIS_SPI=m and tpm_tis_spi is blacklisted.
    # drivers/char/tpm/Kconfig:116, drivers/char/tpm/Makefile:32 (tpm_tis_i2c_cr50.o), drivers/char/tpm/tpm_tis_i2c_cr50.c:834 module_i2c_driver(cr50_i2c_driver)
    ("CONFIG_TCG_TIS_I2C_CR50", "Cr50 I2C TPM built-in (=y) bypasses module_blacklist=tpm_tis_i2c_cr50 (drivers/char/tpm/Kconfig:116, tpm_tis_i2c_cr50.c:834)"),
    # drivers/char/tpm/Kconfig:214, drivers/char/tpm/Makefile:44 (tpm_crb.o), drivers/char/tpm/tpm_crb.c:932 module_acpi_driver(crb_acpi_driver)
    ("CONFIG_TCG_CRB", "AMD fTPM / PTP CRB built-in (=y) bypasses module_blacklist=tpm_crb (drivers/char/tpm/Kconfig:214, tpm_crb.c:932)"),
    # SPI NOR flash & controllers (blacklist: mtd, spi_nor, spi_intel, spi_intel_pci, spi_intel_platform, spi_amd, spi_amd_pci, spidev, intel_thc_hid, intel_quicki2c, intel_quickspi)
    # drivers/mtd/Kconfig:1, drivers/mtd/Makefile:7 (mtd.o), drivers/mtd/mtdcore.c:2584,2634 module_init(init_mtd)
    ("CONFIG_MTD", "MTD subsystem built-in (=y) bypasses module_blacklist=mtd (drivers/mtd/Kconfig:1, mtdcore.c:2634)"),
    # drivers/mtd/spi-nor/Kconfig:2, drivers/mtd/spi-nor/Makefile:18 (spi-nor.o), drivers/mtd/spi-nor/core.c:3877 module_init(spi_nor_module_init)
    ("CONFIG_MTD_SPI_NOR", "SPI-NOR flash built-in (=y) bypasses module_blacklist=spi_nor (drivers/mtd/spi-nor/Kconfig:2, core.c:3877)"),
    # drivers/spi/Kconfig:521, drivers/spi/Makefile:75 (spi-intel.o). The driver init is the module built from that object.
    ("CONFIG_SPI_INTEL", "Intel SPI flash core built-in (=y) bypasses module_blacklist=spi_intel (drivers/spi/Kconfig:521, Makefile:75)"),
    # drivers/spi/Kconfig:524, drivers/spi/Makefile:76 (spi-intel-pci.o), drivers/spi/spi-intel-pci.c:113 module_pci_driver(intel_spi_pci_driver)
    ("CONFIG_SPI_INTEL_PCI", "Intel PCI SPI flash controller built-in (=y) bypasses module_blacklist=spi_intel_pci (drivers/spi/Kconfig:524, spi-intel-pci.c:113)"),
    # drivers/spi/Kconfig:542, drivers/spi/Makefile:77 (spi-intel-platform.o), drivers/spi/spi-intel-platform.c:38 module_platform_driver(intel_spi_platform_driver)
    ("CONFIG_SPI_INTEL_PLATFORM", "Intel platform SPI flash controller built-in (=y) bypasses module_blacklist=spi_intel_platform (drivers/spi/Kconfig:542, spi-intel-platform.c:38)"),
    # drivers/spi/Kconfig:1307, drivers/spi/Makefile:169 (builds BOTH spi-amd.o and spi-amd-pci.o), drivers/spi/spi-amd.c:894 & spi-amd-pci.c:66
    ("CONFIG_SPI_AMD", "AMD SPI flash controller built-in (=y) bypasses module_blacklist=spi_amd,spi_amd_pci (drivers/spi/Kconfig:1307, Makefile:169)"),
    # Legacy/alias Kconfig name (in 3938c8d97142 spi-amd-pci.o is built under CONFIG_SPI_AMD at drivers/spi/Makefile:169)
    ("CONFIG_SPI_AMD_PCI", "AMD PCI SPI flash controller built-in (=y) bypasses module_blacklist=spi_amd_pci (drivers/spi/Makefile:169, spi-amd-pci.c:66)"),
    # drivers/spi/Kconfig:1336, drivers/spi/Makefile:14 (spidev.o), drivers/spi/spidev.c:884 module_init(spidev_init)
    ("CONFIG_SPI_SPIDEV", "raw /dev/spidev* built-in (=y) bypasses module_blacklist=spidev (drivers/spi/Kconfig:1336, spidev.c:884)"),
    # drivers/hid/intel-thc-hid/Kconfig:7, Makefile:8-10 (intel-thc.o from intel-thc/intel-thc-dev.o).
    # intel-thc/intel-thc-dev.c has no module_init; MODULE_LICENSE is at :1719. The .ko name is intel_thc.
    ("CONFIG_INTEL_THC_HID", "Intel THC HID hardware transport built-in (=y) bypasses module_blacklist=intel_thc (drivers/hid/intel-thc-hid/Kconfig:7, Makefile:8)"),
    # drivers/hid/intel-thc-hid/Kconfig:31, drivers/hid/intel-thc-hid/Makefile:18 (intel-quicki2c.o), pci-quicki2c.c:1039 module_pci_driver(quicki2c_driver)
    ("CONFIG_INTEL_QUICKI2C", "Intel THC QuickI2C built-in (=y) bypasses module_blacklist=intel_quicki2c (drivers/hid/intel-thc-hid/Kconfig:31, pci-quicki2c.c:1039)"),
    # drivers/hid/intel-thc-hid/Kconfig:20, drivers/hid/intel-thc-hid/Makefile:13 (intel-quickspi.o), pci-quickspi.c:1001 module_pci_driver(quickspi_driver)
    ("CONFIG_INTEL_QUICKSPI", "Intel THC QuickSPI built-in (=y) bypasses module_blacklist=intel_quickspi (drivers/hid/intel-thc-hid/Kconfig:20, pci-quickspi.c:1001)"),
    # Legacy/alias Kconfig names for Intel THC QuickI2C / QuickSPI
    ("CONFIG_INTEL_THC_QUICKI2C", "Intel THC QuickI2C alias built-in (=y) bypasses module_blacklist=intel_quicki2c"),
    ("CONFIG_INTEL_THC_QUICKSPI", "Intel THC QuickSPI alias built-in (=y) bypasses module_blacklist=intel_quickspi"),
    # Raw I2C + SPD EEPROM (blacklist: i2c_dev, at24, ee1004)
    # drivers/i2c/Kconfig:43, drivers/i2c/Makefile:15 (i2c-dev.o), drivers/i2c/i2c-dev.c:805 module_init(i2c_dev_init)
    ("CONFIG_I2C_CHARDEV", "raw /dev/i2c-* chardev built-in (=y) bypasses module_blacklist=i2c_dev (drivers/i2c/Kconfig:43, i2c-dev.c:805)"),
    # drivers/misc/eeprom/Kconfig:4, drivers/misc/eeprom/Makefile:2 (at24.o), drivers/misc/eeprom/at24.c:868 module_init(at24_init)
    ("CONFIG_EEPROM_AT24", "AT24 I2C EEPROM driver built-in (=y) bypasses module_blacklist=at24 (drivers/misc/eeprom/Kconfig:4, at24.c:868)"),
    # drivers/misc/eeprom/Kconfig:110, drivers/misc/eeprom/Makefile:9 (ee1004.o), drivers/misc/eeprom/ee1004.c:348 module_i2c_driver(ee1004_driver)
    ("CONFIG_EEPROM_EE1004", "EE1004 SPD EEPROM driver built-in (=y) exposes DIMM SPD EEPROMs (drivers/misc/eeprom/Kconfig:110, ee1004.c:348)"),
    # Thunderbolt / USB4 (blacklist: thunderbolt)
    # drivers/thunderbolt/Kconfig:2 (USB4), drivers/thunderbolt/Makefile:3 (thunderbolt.o), drivers/thunderbolt/nhi.c:1565,1584 rootfs_initcall(nhi_init)
    ("CONFIG_USB4", "Thunderbolt/USB4 driver built-in (=y) bypasses module_blacklist=thunderbolt (drivers/thunderbolt/Kconfig:2, nhi.c:1584)"),
    ("CONFIG_THUNDERBOLT", "Thunderbolt legacy alias built-in (=y) bypasses module_blacklist=thunderbolt"),
    # Internal SD/eMMC (blacklist: sdhci_pci, sdhci_acpi)
    # drivers/mmc/host/Kconfig:110, drivers/mmc/host/Makefile:15 (sdhci-pci.o), drivers/mmc/host/sdhci-pci-core.c:2398 module_pci_driver(sdhci_driver)
    ("CONFIG_MMC_SDHCI_PCI", "PCI SDHCI controller built-in (=y) bypasses module_blacklist=sdhci_pci and exposes internal SD/eMMC storage (drivers/mmc/host/Kconfig:110, sdhci-pci-core.c:2398)"),
    # drivers/mmc/host/Kconfig:139, drivers/mmc/host/Makefile:18 (sdhci-acpi.o), drivers/mmc/host/sdhci-acpi.c:1057 module_platform_driver(sdhci_acpi_driver)
    ("CONFIG_MMC_SDHCI_ACPI", "ACPI SDHCI controller built-in (=y) bypasses module_blacklist=sdhci_acpi and exposes internal SD/eMMC storage (drivers/mmc/host/Kconfig:139, sdhci-acpi.c:1057)"),
    # ChromeOS EC / HPS / Wilco EC / GSMI / NVRAM (all in-tree symbols for _safety.toml blacklist.required)
    # drivers/platform/chrome/Kconfig:75, drivers/platform/chrome/Makefile:13 (cros_ec.o)
    ("CONFIG_CROS_EC", "ChromeOS EC core built-in (=y) bypasses module_blacklist=cros_ec (drivers/platform/chrome/Kconfig:75, Makefile:13)"),
    # drivers/platform/chrome/Kconfig:88, drivers/platform/chrome/Makefile:14 (cros_ec_i2c.o), drivers/platform/chrome/cros_ec_i2c.c:376 module_i2c_driver(cros_ec_driver)
    ("CONFIG_CROS_EC_I2C", "ChromeOS EC I2C transport built-in (=y) bypasses module_blacklist=cros_ec_i2c (drivers/platform/chrome/Kconfig:88, cros_ec_i2c.c:376)"),
    # drivers/platform/chrome/Kconfig:110, drivers/platform/chrome/Makefile:15 (cros_ec_ishtp.o), drivers/platform/chrome/cros_ec_ishtp.c:745 module_init(cros_ec_ishtp_mod_init)
    ("CONFIG_CROS_EC_ISHTP", "ChromeOS EC ISHTP transport built-in (=y) bypasses module_blacklist=cros_ec_ishtp (drivers/platform/chrome/Kconfig:110, cros_ec_ishtp.c:745)"),
    # drivers/platform/chrome/Kconfig:98, drivers/platform/chrome/Makefile:17 (cros_ec_rpmsg.o)
    ("CONFIG_CROS_EC_RPMSG", "ChromeOS EC RPMSG transport built-in (=y) bypasses module_blacklist=cros_ec_rpmsg (drivers/platform/chrome/Kconfig:98, Makefile:17)"),
    # drivers/platform/chrome/Kconfig:123, drivers/platform/chrome/Makefile:18 (cros_ec_spi.o), drivers/platform/chrome/cros_ec_spi.c:833 module_spi_driver(cros_ec_driver_spi)
    ("CONFIG_CROS_EC_SPI", "ChromeOS EC SPI transport built-in (=y) bypasses module_blacklist=cros_ec_spi (drivers/platform/chrome/Kconfig:123, cros_ec_spi.c:833)"),
    # drivers/platform/chrome/Kconfig:133, drivers/platform/chrome/Makefile:19 (cros_ec_uart.o)
    ("CONFIG_CROS_EC_UART", "ChromeOS EC UART transport built-in (=y) bypasses module_blacklist=cros_ec_uart (drivers/platform/chrome/Kconfig:133, Makefile:19)"),
    # drivers/platform/chrome/Kconfig:143, drivers/platform/chrome/Makefile:27 (cros_ec_lpcs.o), drivers/platform/chrome/cros_ec_lpc.c:909 module_init(cros_ec_lpc_init)
    ("CONFIG_CROS_EC_LPC", "ChromeOS EC LPC transport built-in (=y) bypasses module_blacklist=cros_ec_lpcs (drivers/platform/chrome/Kconfig:143, cros_ec_lpc.c:909)"),
    # drivers/platform/chrome/Kconfig:157, drivers/platform/chrome/Makefile:29 (cros-ec-proto.o). Protocol library.
    ("CONFIG_CROS_EC_PROTO", "ChromeOS EC protocol built-in (=y) bypasses module_blacklist=cros_ec_proto (drivers/platform/chrome/Kconfig:157, Makefile:29)"),
    # drivers/platform/chrome/Kconfig:162, drivers/platform/chrome/Makefile:30 (cros_kbd_led_backlight.o), drivers/platform/chrome/cros_kbd_led_backlight.c:251 module_platform_driver(keyboard_led_driver)
    ("CONFIG_CROS_KBD_LED_BACKLIGHT", "ChromeOS keyboard LED backlight built-in (=y) bypasses module_blacklist=cros_kbd_led_backlight (drivers/platform/chrome/Kconfig:162, cros_kbd_led_backlight.c:251)"),
    # drivers/platform/chrome/Kconfig:173, drivers/platform/chrome/Makefile:31 (cros_ec_chardev.o), drivers/platform/chrome/cros_ec_chardev.c:416 module_platform_driver(cros_ec_chardev_driver)
    ("CONFIG_CROS_EC_CHARDEV", "ChromeOS EC /dev/cros_ec chardev built-in (=y) bypasses module_blacklist=cros_ec_chardev (drivers/platform/chrome/Kconfig:173, cros_ec_chardev.c:416)"),
    # drivers/platform/chrome/Kconfig:184, drivers/platform/chrome/Makefile:32 (cros_ec_lightbar.o), drivers/platform/chrome/cros_ec_lightbar.c:692 module_platform_driver(cros_ec_lightbar_driver)
    ("CONFIG_CROS_EC_LIGHTBAR", "ChromeOS EC lightbar built-in (=y) bypasses module_blacklist=cros_ec_lightbar (drivers/platform/chrome/Kconfig:184, cros_ec_lightbar.c:692)"),
    # drivers/platform/chrome/Kconfig:195, drivers/platform/chrome/Makefile:33 (cros_ec_vbc.o), drivers/platform/chrome/cros_ec_vbc.c:152 module_platform_driver(cros_ec_vbc_driver)
    ("CONFIG_CROS_EC_VBC", "ChromeOS EC VBC NVRAM built-in (=y) bypasses module_blacklist=cros_ec_vbc (drivers/platform/chrome/Kconfig:195, cros_ec_vbc.c:152)"),
    # drivers/platform/chrome/Kconfig:206, drivers/platform/chrome/Makefile:34 (cros_ec_debugfs.o), drivers/platform/chrome/cros_ec_debugfs.c:577 module_platform_driver(cros_ec_debugfs_driver)
    ("CONFIG_CROS_EC_DEBUGFS", "ChromeOS EC debugfs built-in (=y) bypasses module_blacklist=cros_ec_debugfs (drivers/platform/chrome/Kconfig:206, cros_ec_debugfs.c:577)"),
    # drivers/platform/chrome/Kconfig:217, drivers/platform/chrome/Makefile:36 (cros-ec-sensorhub.o), drivers/platform/chrome/cros_ec_sensorhub.c:316 module_platform_driver(cros_ec_sensorhub_driver)
    ("CONFIG_CROS_EC_SENSORHUB", "ChromeOS EC sensorhub built-in (=y) bypasses module_blacklist=cros_ec_sensorhub (drivers/platform/chrome/Kconfig:217, cros_ec_sensorhub.c:316)"),
    # drivers/platform/chrome/Kconfig:230, drivers/platform/chrome/Makefile:37 (cros_ec_sysfs.o), drivers/platform/chrome/cros_ec_sysfs.c:437 module_platform_driver(cros_ec_sysfs_driver)
    ("CONFIG_CROS_EC_SYSFS", "ChromeOS EC sysfs (reboot/flash) built-in (=y) bypasses module_blacklist=cros_ec_sysfs (drivers/platform/chrome/Kconfig:230, cros_ec_sysfs.c:437)"),
    # drivers/platform/chrome/Kconfig:246, drivers/platform/chrome/Makefile:25 (cros-ec-typec.o), drivers/platform/chrome/cros_ec_typec.c:1464 module_platform_driver(cros_typec_driver)
    ("CONFIG_CROS_EC_TYPEC", "ChromeOS EC Type-C built-in (=y) bypasses module_blacklist=cros_ec_typec (drivers/platform/chrome/Kconfig:246, cros_ec_typec.c:1464)"),
    # drivers/platform/chrome/Kconfig:261 (depends on HID && I2C && PM, not CROS_EC!), drivers/platform/chrome/Makefile:38 (cros_hps_i2c.o), cros_hps_i2c.c:155 module_i2c_driver(hps_i2c_driver)
    ("CONFIG_CROS_HPS_I2C", "ChromeOS HPS I2C presence sensor built-in (=y) bypasses module_blacklist=cros_hps_i2c (drivers/platform/chrome/Kconfig:261, cros_hps_i2c.c:155)"),
    # drivers/platform/chrome/Kconfig:271, drivers/platform/chrome/Makefile:39 (cros_usbpd_logger.o), drivers/platform/chrome/cros_usbpd_logger.c:270 module_platform_driver(cros_usbpd_logger_driver)
    ("CONFIG_CROS_USBPD_LOGGER", "ChromeOS USB-PD logger built-in (=y) bypasses module_blacklist=cros_usbpd_logger (drivers/platform/chrome/Kconfig:271, cros_usbpd_logger.c:270)"),
    # drivers/platform/chrome/Kconfig:283, drivers/platform/chrome/Makefile:40 (cros_usbpd_notify.o), drivers/platform/chrome/cros_usbpd_notify.c:276 module_init(cros_usbpd_notify_init)
    ("CONFIG_CROS_USBPD_NOTIFY", "ChromeOS USB-PD notify built-in (=y) bypasses module_blacklist=cros_usbpd_notify (drivers/platform/chrome/Kconfig:283, cros_usbpd_notify.c:276)"),
    # drivers/platform/chrome/Kconfig:308, drivers/platform/chrome/Makefile:16 (cros_typec_switch.o), drivers/platform/chrome/cros_typec_switch.c:324 module_platform_driver(cros_typec_switch_driver)
    ("CONFIG_CROS_TYPEC_SWITCH", "ChromeOS Type-C switch built-in (=y) bypasses module_blacklist=cros_typec_switch (drivers/platform/chrome/Kconfig:308, cros_typec_switch.c:324)"),
    # drivers/mfd/Kconfig:252, drivers/mfd/Makefile:18 (cros_ec_dev.o), drivers/mfd/cros_ec_dev.c:403 module_init(cros_ec_dev_init)
    ("CONFIG_MFD_CROS_EC_DEV", "ChromeOS EC MFD cell creator built-in (=y) bypasses module_blacklist=cros_ec_dev (drivers/mfd/Kconfig:252, cros_ec_dev.c:403)"),
    # drivers/power/supply/Kconfig:934, drivers/power/supply/Makefile:113 (cros_usbpd-charger.o), cros_usbpd-charger.c:724 module_platform_driver(cros_usbpd_charger_driver)
    ("CONFIG_CHARGER_CROS_USBPD", "ChromeOS USB-PD charger built-in (=y) bypasses module_blacklist=cros_usbpd_charger (drivers/power/supply/Kconfig:934, cros_usbpd-charger.c:724)"),
    # drivers/power/supply/Kconfig:943, drivers/power/supply/Makefile:114 (cros_peripheral_charger.o), cros_peripheral_charger.c:386 module_platform_driver(cros_pchg_driver)
    ("CONFIG_CHARGER_CROS_PCHG", "ChromeOS peripheral charger built-in (=y) bypasses module_blacklist=cros_peripheral_charger (drivers/power/supply/Kconfig:943, cros_peripheral_charger.c:386)"),
    # drivers/power/supply/Kconfig:953, drivers/power/supply/Makefile:112 (cros_charge-control.o), cros_charge-control.c:332 module_platform_driver(cros_chctl_driver)
    ("CONFIG_CHARGER_CROS_CONTROL", "ChromeOS EC charge control built-in (=y) bypasses module_blacklist=cros_charge_control (drivers/power/supply/Kconfig:953, cros_charge-control.c:332)"),
    # drivers/usb/typec/ucsi/Kconfig:72, drivers/usb/typec/ucsi/Makefile:28 (cros_ec_ucsi.o), drivers/usb/typec/ucsi/cros_ec_ucsi.c:361 module_platform_driver(cros_ucsi_driver)
    ("CONFIG_CROS_EC_UCSI", "ChromeOS EC UCSI built-in (=y) bypasses module_blacklist=cros_ec_ucsi (drivers/usb/typec/ucsi/Kconfig:72, cros_ec_ucsi.c:361)"),
    ("CONFIG_UCSI_CROS_EC", "ChromeOS EC UCSI legacy alias built-in (=y) bypasses module_blacklist=cros_ec_ucsi"),
    # drivers/input/keyboard/Kconfig:730, drivers/input/keyboard/Makefile:19 (cros_ec_keyb.o), drivers/input/keyboard/cros_ec_keyb.c:795 module_platform_driver(cros_ec_keyb_driver)
    ("CONFIG_KEYBOARD_CROS_EC", "ChromeOS EC keyboard built-in (=y) bypasses module_blacklist=cros_ec_keyb (drivers/input/keyboard/Kconfig:730, cros_ec_keyb.c:795)"),
    # drivers/i2c/busses/Kconfig:1534, drivers/i2c/busses/Makefile:152 (i2c-cros-ec-tunnel.o), i2c-cros-ec-tunnel.c:318 module_platform_driver(ec_i2c_tunnel_driver)
    ("CONFIG_I2C_CROS_EC_TUNNEL", "ChromeOS EC I2C tunnel built-in (=y) bypasses module_blacklist=i2c_cros_ec_tunnel (drivers/i2c/busses/Kconfig:1534, i2c-cros-ec-tunnel.c:318)"),
    # drivers/iio/common/cros_ec_sensors/Kconfig:5, Makefile:7 (cros-ec-sensors-core.o), cros_ec_sensors_core.c:888
    ("CONFIG_IIO_CROS_EC_SENSORS_CORE", "ChromeOS EC sensors core built-in (=y) bypasses module_blacklist=cros_ec_sensors_core (drivers/iio/common/cros_ec_sensors/Kconfig:5, Makefile:7)"),
    # drivers/iio/common/cros_ec_sensors/Kconfig:16, Makefile:8 (cros_ec_sensors.o), cros_ec_sensors.c:326 module_platform_driver(cros_ec_sensors_platform_driver)
    ("CONFIG_IIO_CROS_EC_SENSORS", "ChromeOS EC sensors built-in (=y) bypasses module_blacklist=cros_ec_sensors (drivers/iio/common/cros_ec_sensors/Kconfig:16, cros_ec_sensors.c:326)"),
    # drivers/iio/accel/Kconfig:401, drivers/iio/accel/Makefile:79 (cros_ec_accel_legacy.o), cros_ec_accel_legacy.c:247 module_platform_driver(cros_ec_accel_platform_driver)
    ("CONFIG_IIO_CROS_EC_ACCEL_LEGACY", "ChromeOS EC legacy accelerometer built-in (=y) bypasses module_blacklist=cros_ec_accel_legacy (drivers/iio/accel/Kconfig:401, cros_ec_accel_legacy.c:247)"),
    # drivers/iio/pressure/Kconfig:60, drivers/iio/pressure/Makefile:15 (cros_ec_baro.o), cros_ec_baro.c:207 module_platform_driver(cros_ec_baro_platform_driver)
    ("CONFIG_IIO_CROS_EC_BARO", "ChromeOS EC barometer built-in (=y) bypasses module_blacklist=cros_ec_baro (drivers/iio/pressure/Kconfig:60, cros_ec_baro.c:207)"),
    # drivers/iio/common/cros_ec_sensors/Kconfig:25, Makefile:9 (cros_ec_lid_angle.o), cros_ec_lid_angle.c:135 module_platform_driver(cros_ec_lid_angle_platform_driver)
    ("CONFIG_IIO_CROS_EC_SENSORS_LID_ANGLE", "ChromeOS EC lid angle sensor built-in (=y) bypasses module_blacklist=cros_ec_lid_angle (drivers/iio/common/cros_ec_sensors/Kconfig:25, cros_ec_lid_angle.c:135)"),
    # drivers/iio/light/Kconfig:223, drivers/iio/light/Makefile:26 (cros_ec_light_prox.o), cros_ec_light_prox.c:264 module_platform_driver(cros_ec_light_prox_platform_driver)
    ("CONFIG_IIO_CROS_EC_LIGHT_PROX", "ChromeOS EC light/proximity sensor built-in (=y) bypasses module_blacklist=cros_ec_light_prox (drivers/iio/light/Kconfig:223, cros_ec_light_prox.c:264)"),
    # drivers/media/cec/platform/Kconfig:5, drivers/media/cec/platform/cros-ec/Makefile:2 (cros-ec-cec.o), cros-ec-cec.c:598 module_platform_driver(cros_ec_cec_driver)
    ("CONFIG_CEC_CROS_EC", "ChromeOS EC CEC built-in (=y) bypasses module_blacklist=cros_ec_cec (drivers/media/cec/platform/Kconfig:5, cros-ec-cec.c:598)"),
    # drivers/leds/Kconfig:189, drivers/leds/Makefile:30 (leds-cros_ec.o), drivers/leds/leds-cros_ec.c:258 module_platform_driver(cros_ec_led_driver)
    ("CONFIG_LEDS_CROS_EC", "ChromeOS EC LEDs built-in (=y) bypasses module_blacklist=leds_cros_ec (drivers/leds/Kconfig:189, leds-cros_ec.c:258)"),
    # drivers/hid/Kconfig:455, drivers/hid/Makefile:61 (hid-google-hammer.o), drivers/hid/hid-google-hammer.c:613 module_init(hammer_init)
    ("CONFIG_HID_GOOGLE_HAMMER", "Google Hammer EC HID built-in (=y) bypasses module_blacklist=hid_google_hammer (drivers/hid/Kconfig:455, hid-google-hammer.c:613)"),
    # drivers/platform/chrome/wilco_ec/Kconfig:2, drivers/platform/chrome/wilco_ec/Makefile:5 (wilco_ec.o), drivers/platform/chrome/wilco_ec/core.c:170 module_platform_driver(wilco_ec_driver)
    ("CONFIG_WILCO_EC", "Wilco EC built-in (=y) bypasses module_blacklist=wilco_ec (drivers/platform/chrome/wilco_ec/Kconfig:2, core.c:170)"),
    # drivers/power/supply/Kconfig:1008, drivers/power/supply/Makefile:120 (wilco-charger.o), drivers/power/supply/wilco-charger.c:187 module_platform_driver(wilco_charge_driver)
    ("CONFIG_CHARGER_WILCO", "Wilco EC charger built-in (=y) bypasses module_blacklist=wilco_charger (drivers/power/supply/Kconfig:1008, wilco-charger.c:187)"),
    # drivers/platform/chrome/wilco_ec/Kconfig:15, drivers/platform/chrome/wilco_ec/Makefile:7 (wilco_ec_debugfs.o), debugfs.c:283 module_platform_driver(wilco_ec_debugfs_driver)
    ("CONFIG_WILCO_EC_DEBUGFS", "Wilco EC debugfs built-in (=y) bypasses module_blacklist=wilco_ec_debugfs (drivers/platform/chrome/wilco_ec/Kconfig:15, debugfs.c:283)"),
    # drivers/platform/chrome/wilco_ec/Kconfig:34, drivers/platform/chrome/wilco_ec/Makefile:11 (wilco_ec_telem.o), telemetry.c:470 module_init(telem_module_init)
    ("CONFIG_WILCO_EC_TELEMETRY", "Wilco EC telemetry built-in (=y) bypasses module_blacklist=wilco_ec_telem (drivers/platform/chrome/wilco_ec/Kconfig:34, telemetry.c:470)"),
    # drivers/rtc/Kconfig:2066, drivers/rtc/Makefile:194 (rtc-wilco-ec.o), drivers/rtc/rtc-wilco-ec.c:189 module_platform_driver(wilco_ec_rtc_driver)
    ("CONFIG_RTC_DRV_WILCO_EC", "Wilco EC RTC built-in (=y) bypasses module_blacklist=rtc_wilco_ec (drivers/rtc/Kconfig:2066, rtc-wilco-ec.c:189)"),
    # drivers/firmware/google/Kconfig:12, drivers/firmware/google/Makefile:3 (gsmi.o), drivers/firmware/google/gsmi.c:1091 module_init(gsmi_init)
    ("CONFIG_GOOGLE_SMI", "Google GSMI firmware log writer built-in (=y) bypasses module_blacklist=gsmi (drivers/firmware/google/Kconfig:12, gsmi.c:1091)"),
    # drivers/platform/chrome/Kconfig:18, drivers/platform/chrome/Makefile:7 (chromeos_acpi.o), drivers/platform/chrome/chromeos_acpi.c:282 module_platform_driver(chromeos_acpi_device_driver)
    ("CONFIG_CHROMEOS_ACPI", "ChromeOS ACPI built-in (=y) bypasses module_blacklist=chromeos_acpi (drivers/platform/chrome/Kconfig:18, chromeos_acpi.c:282)"),
    ("CONFIG_ACPI_CHROMEOS", "ChromeOS ACPI legacy alias built-in (=y) bypasses module_blacklist=chromeos_acpi"),
    # drivers/char/Kconfig:320, drivers/char/Makefile:24 (nvram.o), drivers/char/nvram.c:540 module_init(nvram_module_init)
    ("CONFIG_NVRAM", "/dev/nvram CMOS writer built-in (=y) bypasses module_blacklist=nvram (drivers/char/Kconfig:320, nvram.c:540)"),
    # AMD PSP / TEE / SFH / watchdog. amd-hx370 blacklist.modules includes ccp, amdtee, amd_sfh, sp5100_tco.
    # ccp_crypto (CONFIG_CRYPTO_DEV_CCP_CRYPTO) and ucsi_acpi (CONFIG_UCSI_ACPI) are in-tree modules that are not on
    # that blacklist; =m is refused until a profile lists the module name. The pinned step-1 .config leaves both unset.
    # CONFIG_AMD_PMC is not gated: drivers/platform/x86/amd/pmc/Kconfig:6 and Makefile:7 build amd-pmc.o
    # (pmc.c:844 module_platform_driver(amd_pmc_driver) -> initcall amd_pmc_driver_init), and the step-1 kernel sets
    # CONFIG_AMD_PMC=m for s2idle. amd_pmc is not in profiles/_safety.toml or the profile blacklist, so refusing =m
    # would reject that kernel, and adding it to module_blacklist= would stop the SMU driver the image needs.
    # Note: CONFIG_CRYPTO_DEV_CCP (drivers/crypto/Kconfig:515, bool menu gate at drivers/crypto/Makefile:11) and
    # CONFIG_CRYPTO_DEV_SP_PSP (drivers/crypto/ccp/Kconfig:38, bool feature object linked into ccp.o at
    # drivers/crypto/ccp/Makefile:11) are bool options whose linkage unit is CONFIG_CRYPTO_DEV_CCP_DD (ccp.o); they are
    # checked separately in check_kconfig() so CONFIG_CRYPTO_DEV_CCP=y + CONFIG_CRYPTO_DEV_CCP_DD=m is permitted when
    # 'ccp' is in blacklist.modules.
    # drivers/crypto/ccp/Kconfig:2, drivers/crypto/ccp/Makefile:2 (ccp.o), drivers/crypto/ccp/sp-dev.c:325 module_init(sp_mod_init)
    ("CONFIG_CRYPTO_DEV_CCP_DD", "AMD PSP CCP device driver built-in (=y) bypasses module_blacklist=ccp (drivers/crypto/ccp/Kconfig:2, sp-dev.c:325)"),
    # drivers/crypto/ccp/Kconfig:23, drivers/crypto/ccp/Makefile:19 (ccp-crypto.o), drivers/crypto/ccp/ccp-crypto-main.c:425 module_init(ccp_crypto_init)
    ("CONFIG_CRYPTO_DEV_CCP_CRYPTO", "AMD CCP crypto offload driver built-in (=y) bypasses module_blacklist=ccp_crypto (drivers/crypto/ccp/Kconfig:23, ccp-crypto-main.c:425)"),
    # drivers/tee/amdtee/Kconfig:3, drivers/tee/amdtee/Makefile:2 (amdtee.o), drivers/tee/amdtee/core.c:518 module_init(amdtee_driver_init)
    ("CONFIG_AMDTEE", "AMD-TEE driver built-in (=y) bypasses module_blacklist=amdtee (drivers/tee/amdtee/Kconfig:3, core.c:518)"),
    # drivers/tee/Kconfig:3, drivers/tee/Makefile:2 (tee.o -> module tee, NOT amdtee), drivers/tee/tee_core.c:1447 subsys_initcall(tee_init)
    ("CONFIG_TEE", "TEE subsystem built-in (=y) bypasses module_blacklist=tee (drivers/tee/Kconfig:3, tee_core.c:1447)"),
    # drivers/hid/amd-sfh-hid/Kconfig:6, drivers/hid/amd-sfh-hid/Makefile:7 (amd_sfh.o), drivers/hid/amd-sfh-hid/amd_sfh_pcie.c:557 module_pci_driver(amd_mp2_pci_driver)
    ("CONFIG_AMD_SFH_HID", "AMD Sensor Fusion Hub built-in (=y) bypasses module_blacklist=amd_sfh (drivers/hid/amd-sfh-hid/Kconfig:6, amd_sfh_pcie.c:557)"),
    # drivers/watchdog/Kconfig:1239, drivers/watchdog/Makefile:116 (sp5100_tco.o), drivers/watchdog/sp5100_tco.c:630 module_init(sp5100_tco_init)
    ("CONFIG_SP5100_TCO", "AMD SP5100 TCO watchdog built-in (=y) bypasses module_blacklist=sp5100_tco (drivers/watchdog/Kconfig:1239, sp5100_tco.c:630)"),
    # drivers/usb/typec/ucsi/Kconfig:40, drivers/usb/typec/ucsi/Makefile:24 (ucsi_acpi.o), drivers/usb/typec/ucsi/ucsi_acpi.c:273 module_platform_driver(ucsi_acpi_platform_driver)
    ("CONFIG_UCSI_ACPI", "UCSI ACPI driver built-in (=y) bypasses module_blacklist=ucsi_acpi (drivers/usb/typec/ucsi/Kconfig:40, ucsi_acpi.c:273)"),
)

# Module name required in blacklist.modules whenever a KCONFIG_NEVER_BUILTIN symbol is built as =m.
# Note: in drivers/spi/Makefile:169, obj-$(CONFIG_SPI_AMD) += spi-amd.o spi-amd-pci.o builds BOTH spi_amd and spi_amd_pci,
# which is enforced in check_kconfig() below.
KCONFIG_BUILTIN_MODULE = {
    "CONFIG_TCG_TPM": "tpm",
    "CONFIG_TCG_TIS": "tpm_tis",
    "CONFIG_TCG_TIS_CORE": "tpm_tis_core",
    "CONFIG_TCG_TIS_SPI": "tpm_tis_spi",
    "CONFIG_TCG_TIS_I2C_CR50": "tpm_tis_i2c_cr50",
    "CONFIG_TCG_CRB": "tpm_crb",
    "CONFIG_MTD": "mtd",
    "CONFIG_MTD_SPI_NOR": "spi_nor",
    "CONFIG_SPI_INTEL": "spi_intel",
    "CONFIG_SPI_INTEL_PCI": "spi_intel_pci",
    "CONFIG_SPI_INTEL_PLATFORM": "spi_intel_platform",
    "CONFIG_SPI_AMD": "spi_amd",
    "CONFIG_SPI_AMD_PCI": "spi_amd_pci",
    "CONFIG_SPI_SPIDEV": "spidev",
    "CONFIG_INTEL_THC_HID": "intel_thc",
    "CONFIG_INTEL_QUICKI2C": "intel_quicki2c",
    "CONFIG_INTEL_QUICKSPI": "intel_quickspi",
    "CONFIG_INTEL_THC_QUICKI2C": "intel_quicki2c",
    "CONFIG_INTEL_THC_QUICKSPI": "intel_quickspi",
    "CONFIG_I2C_CHARDEV": "i2c_dev",
    "CONFIG_EEPROM_AT24": "at24",
    "CONFIG_EEPROM_EE1004": "ee1004",
    "CONFIG_USB4": "thunderbolt",
    "CONFIG_THUNDERBOLT": "thunderbolt",
    "CONFIG_MMC_SDHCI_PCI": "sdhci_pci",
    "CONFIG_MMC_SDHCI_ACPI": "sdhci_acpi",
    "CONFIG_CROS_EC": "cros_ec",
    "CONFIG_CROS_EC_I2C": "cros_ec_i2c",
    "CONFIG_CROS_EC_ISHTP": "cros_ec_ishtp",
    "CONFIG_CROS_EC_SPI": "cros_ec_spi",
    "CONFIG_CROS_EC_RPMSG": "cros_ec_rpmsg",
    "CONFIG_CROS_EC_UART": "cros_ec_uart",
    "CONFIG_CROS_EC_LPC": "cros_ec_lpcs",
    "CONFIG_CROS_EC_PROTO": "cros_ec_proto",
    "CONFIG_CROS_KBD_LED_BACKLIGHT": "cros_kbd_led_backlight",
    "CONFIG_CROS_EC_CHARDEV": "cros_ec_chardev",
    "CONFIG_CROS_EC_LIGHTBAR": "cros_ec_lightbar",
    "CONFIG_CROS_EC_VBC": "cros_ec_vbc",
    "CONFIG_CROS_EC_DEBUGFS": "cros_ec_debugfs",
    "CONFIG_CROS_EC_SENSORHUB": "cros_ec_sensorhub",
    "CONFIG_CROS_EC_SYSFS": "cros_ec_sysfs",
    "CONFIG_CROS_EC_TYPEC": "cros_ec_typec",
    "CONFIG_CROS_HPS_I2C": "cros_hps_i2c",
    "CONFIG_CROS_USBPD_LOGGER": "cros_usbpd_logger",
    "CONFIG_CROS_USBPD_NOTIFY": "cros_usbpd_notify",
    "CONFIG_CROS_TYPEC_SWITCH": "cros_typec_switch",
    "CONFIG_MFD_CROS_EC_DEV": "cros_ec_dev",
    "CONFIG_CHARGER_CROS_USBPD": "cros_usbpd_charger",
    "CONFIG_CHARGER_CROS_PCHG": "cros_peripheral_charger",
    "CONFIG_CHARGER_CROS_CONTROL": "cros_charge_control",
    "CONFIG_CROS_EC_UCSI": "cros_ec_ucsi",
    "CONFIG_UCSI_CROS_EC": "cros_ec_ucsi",
    "CONFIG_KEYBOARD_CROS_EC": "cros_ec_keyb",
    "CONFIG_I2C_CROS_EC_TUNNEL": "i2c_cros_ec_tunnel",
    "CONFIG_IIO_CROS_EC_SENSORS_CORE": "cros_ec_sensors_core",
    "CONFIG_IIO_CROS_EC_SENSORS": "cros_ec_sensors",
    "CONFIG_IIO_CROS_EC_ACCEL_LEGACY": "cros_ec_accel_legacy",
    "CONFIG_IIO_CROS_EC_BARO": "cros_ec_baro",
    "CONFIG_IIO_CROS_EC_SENSORS_LID_ANGLE": "cros_ec_lid_angle",
    "CONFIG_IIO_CROS_EC_LIGHT_PROX": "cros_ec_light_prox",
    "CONFIG_CEC_CROS_EC": "cros_ec_cec",
    "CONFIG_LEDS_CROS_EC": "leds_cros_ec",
    "CONFIG_HID_GOOGLE_HAMMER": "hid_google_hammer",
    "CONFIG_WILCO_EC": "wilco_ec",
    "CONFIG_CHARGER_WILCO": "wilco_charger",
    "CONFIG_WILCO_EC_DEBUGFS": "wilco_ec_debugfs",
    "CONFIG_WILCO_EC_TELEMETRY": "wilco_ec_telem",
    "CONFIG_RTC_DRV_WILCO_EC": "rtc_wilco_ec",
    "CONFIG_GOOGLE_SMI": "gsmi",
    "CONFIG_CHROMEOS_ACPI": "chromeos_acpi",
    "CONFIG_ACPI_CHROMEOS": "chromeos_acpi",
    "CONFIG_NVRAM": "nvram",
    "CONFIG_CRYPTO_DEV_CCP_DD": "ccp",
    "CONFIG_CRYPTO_DEV_CCP_CRYPTO": "ccp_crypto",
    "CONFIG_AMDTEE": "amdtee",
    "CONFIG_TEE": "tee",
    "CONFIG_AMD_SFH_HID": "amd_sfh",
    "CONFIG_SP5100_TCO": "sp5100_tco",
    "CONFIG_UCSI_ACPI": "ucsi_acpi",
}

# SATA names check_kconfig already walks. libahci is the object drivers/ata/Makefile:6
# builds together with ahci / ahci_platform, not a separate Kconfig symbol.
_SATA_KCONFIG = (
    ("CONFIG_ATA", "libata"),
    ("CONFIG_SATA_AHCI", "ahci"),
    ("CONFIG_SATA_AHCI_PLATFORM", "ahci_platform"),
)
_SATA_LIBAHCI = "libahci"

# Kconfig symbols whose module names [modules] reuses as the storage / firmware-update
# class. The names are values of KCONFIG_BUILTIN_MODULE. NVMe and MEI come from
# KCONFIG_INITCALL_BLACKLIST. SATA names come from _SATA_KCONFIG plus _SATA_LIBAHCI.
# No glob: a name that is not already in those lists is not added here.
_STORAGE_FW_BUILTIN = (
    "CONFIG_MMC_SDHCI_PCI",
    "CONFIG_MMC_SDHCI_ACPI",
    "CONFIG_MTD",
    "CONFIG_MTD_SPI_NOR",
    "CONFIG_SPI_INTEL",
    "CONFIG_SPI_INTEL_PCI",
    "CONFIG_SPI_INTEL_PLATFORM",
    "CONFIG_SPI_AMD",
    "CONFIG_SPI_AMD_PCI",
    "CONFIG_SPI_SPIDEV",
    "CONFIG_USB4",
    "CONFIG_THUNDERBOLT",
    "CONFIG_TCG_TPM",
    "CONFIG_TCG_TIS",
    "CONFIG_TCG_TIS_CORE",
    "CONFIG_TCG_TIS_SPI",
    "CONFIG_TCG_TIS_I2C_CR50",
    "CONFIG_TCG_CRB",
    "CONFIG_CRYPTO_DEV_CCP_DD",
    "CONFIG_CRYPTO_DEV_CCP_CRYPTO",
)


def _storage_firmware_module_names():
    """Module names [modules] refuses as storage or firmware-update.

    Union of names the kconfig gate already maps: KCONFIG_INITCALL_BLACKLIST
    (nvme, mei_me), KCONFIG_BUILTIN_MODULE for _STORAGE_FW_BUILTIN (sdhci, mtd,
    spi-nor, thunderbolt, tpm, ccp, …), and the SATA names including libahci.
    """
    names = {row[1] for row in KCONFIG_INITCALL_BLACKLIST.values()}
    for sym in _STORAGE_FW_BUILTIN:
        names.add(KCONFIG_BUILTIN_MODULE[sym])
    names.update(mod for _sym, mod in _SATA_KCONFIG)
    names.add(_SATA_LIBAHCI)
    return frozenset(names)

KCONFIG_REQUIRED_Y = (
    # kernel/module/Kconfig:2, Makefile:2141, kernel/Makefile:58
    ("CONFIG_MODULES", "loadable module support is required for out-of-tree / vendor_dlkm modules (kernel/module/Kconfig:2)"),
    # kernel/module/Kconfig:132
    ("CONFIG_MODULE_UNLOAD", "module unload support is required (kernel/module/Kconfig:132)"),
    # kernel/module/Kconfig:161, kernel/module/Makefile:23 (version.o)
    ("CONFIG_MODVERSIONS", "MODVERSIONS ABI checking is required (kernel/module/Kconfig:161)"),
    # init/Kconfig:760, kernel/Makefile:93 (configs.o)
    ("CONFIG_IKCONFIG", "embedded IKCFG in vmlinux/bzImage is required for the build-time .config cross-check (init/Kconfig:760)"),
    # init/Kconfig:772
    ("CONFIG_IKCONFIG_PROC", "/proc/config.gz support is required for runtime/build verification (init/Kconfig:772)"),
)


def parse_kconfig(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(CONFIG_[A-Za-z0-9_]+)=(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _lz4_decompress_block(src: bytes) -> bytes:
    """Decompress a single LZ4 raw block (used by LZ4 legacy frame format \\x02!L\\x18)."""
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if i >= n:
                    raise ValueError("truncated literal length")
                b = src[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        if i + lit_len > n:
            raise ValueError("truncated literals")
        out.extend(src[i:i + lit_len])
        i += lit_len
        if i >= n:
            break
        if i + 2 > n:
            raise ValueError("truncated match offset")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise ValueError("invalid match offset")
        match_len = (token & 0x0F) + 4
        if match_len == 19:
            while True:
                if i >= n:
                    raise ValueError("truncated match length")
                b = src[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        if offset >= match_len:
            start = len(out) - offset
            out.extend(out[start:start + match_len])
        else:
            for _ in range(match_len):
                out.append(out[-offset])
    return bytes(out)


def _lz4_decompress_legacy(buf: bytes) -> bytes:
    """Decompress an LZ4 legacy stream (magic 0x184C2102 followed by <u32 block_size><block>...) in pure Python."""
    if not buf.startswith(b"\x02!L\x18"):
        raise ValueError("missing LZ4 legacy magic")
    pos = 4
    chunks = []
    while pos + 4 <= len(buf):
        if pos + 4 == len(buf):
            break  # 4-byte uncompressed size trailer appended by Linux cmd_lz4_with_size
        bsz = int.from_bytes(buf[pos:pos + 4], "little")
        if bsz == 0 or bsz == 0x184C2102 or bsz > 8 * 1024 * 1024 or pos + 4 + bsz > len(buf):
            raise ValueError("invalid or truncated LZ4 legacy block size")
        chunks.append(_lz4_decompress_block(buf[pos + 4:pos + 4 + bsz]))
        pos += 4 + bsz
    if not chunks or len(buf) - pos not in (0, 4):
        raise ValueError("no valid LZ4 legacy blocks decoded")
    return b"".join(chunks)


def _hdrs_payload_bounds(raw: bytes, bzimage: Path) -> tuple[int, int]:
    """Return (start, plen) for the HdrS-declared payload slice."""
    if len(raw) < 0x250 or raw[0x202:0x206] != b"HdrS":
        raise ProfileError(
            f"{bzimage}: unsupported or undecodable bzImage payload format (missing x86 HdrS setup header)"
        )
    setup_sects = raw[0x1f1] or 4
    poff = int.from_bytes(raw[0x248:0x24c], "little")
    plen = int.from_bytes(raw[0x24c:0x250], "little")
    start = (setup_sects + 1) * 512 + poff
    if plen <= 0 or start < 512 or start + plen > len(raw):
        raise ProfileError(
            f"{bzimage}: invalid HdrS payload slice (offset={poff}, len={plen}, file_size={len(raw)})"
        )
    return start, plen


# Tests add a name here to pretend that decompressor is not installed.
_hidden_decompressors: set[str] = set()

# lzop's on-disk header. scripts/extract-ikconfig only searches for \x89LZ; the
# missing-tool path requires this longer magic so a chance 3-byte hit inside a
# gzip member is not treated as a second payload.
_LZO_FILE_MAGIC = b"\x89LZO\x00\x0d\x0a\x1a\x0a"


def _tool_ready(kind: str) -> bool:
    if kind in _hidden_decompressors:
        return False
    if kind == "lzo":
        return shutil.which("lzop") is not None
    if kind == "zstd":
        return _zstd is not None or shutil.which("zstd") is not None
    if kind == "lz4frame":
        return shutil.which("lz4") is not None
    return True


def _gzip_header_end(data: bytes) -> int | None:
    """Offset of the deflate stream inside a gzip member, or None."""
    if len(data) < 10 or data[:3] != b"\x1f\x8b\x08":
        return None
    flg = data[3]
    pos = 10
    if flg & 0x04:
        if pos + 2 > len(data):
            return None
        pos += 2 + int.from_bytes(data[pos:pos + 2], "little")
    if flg & 0x08:
        z = data.find(b"\x00", pos)
        if z < 0:
            return None
        pos = z + 1
    if flg & 0x10:
        z = data.find(b"\x00", pos)
        if z < 0:
            return None
        pos = z + 1
    if flg & 0x02:
        pos += 2
    if pos > len(data):
        return None
    return pos


def _gzip_lenient(data: bytes) -> bytes | None:
    """Inflate a gzip member without trusting the CRC.

    scripts/extract-ikconfig pipes the member to zcat and keeps whatever was
    written when the exit status is not 1 (gunzip writes the bytes, then
    reports a bad CRC). lib/decompress_inflate.c:133 uses raw inflate
    (-MAX_WBITS) and :181-182 skips the 8-byte trailer, so a bad CRC still boots.
    """
    start = _gzip_header_end(data)
    if start is None:
        return None
    dec = zlib.decompressobj(-15)
    out = bytearray()
    step = 1 << 16
    i = 0
    blob = data[start:]
    try:
        while i < len(blob):
            out += dec.decompress(blob[i:i + step])
            i += step
            if dec.eof:
                break
        out += dec.flush()
    except zlib.error:
        pass
    return bytes(out) if out else None


def _drain_decompressor(
    dec, data: bytes, max_out: int, *, then_max_out: int | None = None, then_after: int = 0,
) -> tuple[bytes | None, bool, bool]:
    """Pull decompressed output in bounded pieces.

    LZMADecompressor and BZ2Decompressor drop the bytes a decompress() call
    already produced when that same call raises. max_out bounds that loss.
    then_max_out, once `then_after` bytes are kept, is the max_out for the
    rest of this decompressor (replay a known prefix, then read the tail).
    Returns (output or None, eof, raised).
    """
    out = bytearray()
    pos = 0
    limit = 512 * 1024 * 1024
    eof = False
    raised = False
    cur_max = max_out
    try:
        while len(out) <= limit:
            if then_max_out is not None and cur_max != then_max_out and len(out) >= then_after:
                cur_max = then_max_out
            if getattr(dec, "eof", False):
                eof = True
                break
            needs = getattr(dec, "needs_input", True)
            if needs:
                if pos >= len(data):
                    piece = dec.decompress(b"", cur_max)
                    if not piece:
                        break
                else:
                    chunk = data[pos:pos + (1 << 16)]
                    pos += len(chunk)
                    piece = dec.decompress(chunk, cur_max)
            else:
                piece = dec.decompress(b"", cur_max)
                if not piece:
                    break
            if piece:
                out += piece
    except Exception:
        raised = True
    if getattr(dec, "eof", False):
        eof = True
    return (bytes(out) if out else None), eof, raised


def _lenient_stream(factory, data: bytes, *, recover_tail: bool = False) -> bytes | None:
    """Decompress, keeping bytes produced before an error.

    extract-ikconfig's try_decompress does not check unxz/unlzma/bunzip2's
    exit status before dump_config. A finished member is returned as-is.
    bzip2 (recover_tail false) keeps the 4096-byte pieces and, when that
    pass produced nothing, retries at 64 bytes: bunzip2 often writes less
    than that on a short failure, and this path stays the one the bzip2
    fixtures locked. xz and lzma set recover_tail. The raising decompress()
    drops that call's output, so a second pass replays the kept prefix in
    4096-byte steps and then reads one byte at a time. At most the byte in
    flight when the error is raised is missing. unxz and unlzma still write
    that byte; dump_config does not need it when it is the last byte of
    IKCFG_ED.
    """
    try:
        dec = factory()
    except Exception:
        return None
    got, eof, raised = _drain_decompressor(dec, data, 4096)
    if not recover_tail:
        if got:
            return got
        try:
            dec = factory()
        except Exception:
            return None
        fine, _, _ = _drain_decompressor(dec, data, 64)
        return fine
    if eof or not raised:
        return got
    try:
        dec = factory()
    except Exception:
        return got
    if not got:
        fine, _, _ = _drain_decompressor(dec, data, 1)
        return fine
    recovered, _, _ = _drain_decompressor(
        dec, data, 4096, then_max_out=1, then_after=len(got),
    )
    if recovered is not None and len(recovered) >= len(got):
        return recovered
    return got


def _zstd_first_frame_size(buf: bytes) -> int | None:
    """Byte length of the first zstd frame, or None if the header is not one.

    RFC 8878 frame header plus block headers. Used so the zstd CLI path can
    tell one frame from two: `zstd -dc` succeeds on concatenated frames, while
    the Python decompressor reports the second frame as unused_data.
    """
    if len(buf) < 6 or buf[:4] != b"\x28\xb5\x2f\xfd":
        return None
    desc = buf[4]
    fcs_flag = (desc >> 6) & 3
    single = (desc >> 5) & 1
    if (desc >> 3) & 1:
        return None
    checksum = (desc >> 2) & 1
    did_flag = desc & 3
    pos = 5
    if not single:
        if pos >= len(buf):
            return None
        pos += 1
    pos += (0, 1, 2, 4)[did_flag]
    pos += ((1 if single else 0), 2, 4, 8)[fcs_flag]
    if pos > len(buf):
        return None
    while pos + 3 <= len(buf):
        hdr = int.from_bytes(buf[pos:pos + 3], "little")
        pos += 3
        last = hdr & 1
        btype = (hdr >> 1) & 3
        bsize = hdr >> 3
        if btype == 3:
            return None
        pos += 1 if btype == 1 else bsize
        if pos > len(buf):
            return None
        if last:
            if checksum:
                pos += 4
            if pos > len(buf):
                return None
            return pos
    return None


def _zstd_cli_decode(frame: bytes) -> bytes | None:
    if not frame or not shutil.which("zstd"):
        return None
    proc = subprocess.run(["zstd", "-dc"], input=frame, capture_output=True)
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout
    return None


def _gzip_member_ok(data: bytes) -> bool:
    """True when dump_config would keep this gzip member.

    scripts/extract-ikconfig keeps zcat output only when the status is not 1:
    0 is a clean member, 2 is trailing garbage. A bad CRC is status 1, so
    dump_config discards that output and try_decompress runs instead. The
    scan path (try_decompress) is the one that keeps pre-error bytes.
    """
    if len(data) < 10 or data[:2] != b"\x1f\x8b":
        return False
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        dec.decompress(data)
        dec.flush()
    except zlib.error:
        return False
    return bool(dec.eof)


def _script_dump_config_hit(blob: bytes) -> bool:
    """True when dump_config would keep a config from this decompressed member.

    scripts/extract-ikconfig finds IKCFG_ST plus a gzip header and keeps zcat
    output when the status is not 1. It does not look for IKCFG_ED. A member
    whose last byte was dropped still counts when that byte is only the end
    marker and the gzip member itself finished.
    """
    start = 0
    marker = b"IKCFG_ST\x1f\x8b\x08"
    while True:
        i = blob.find(marker, start)
        if i < 0:
            return False
        if _gzip_member_ok(blob[i + 8:]):
            return True
        start = i + 1


def _scan_member_ikcfg(blob: bytes) -> bool:
    """IKCFG hit for the try_decompress scan, including a tail missing IKCFG_ED."""
    return _has_decoded_ikcfg(blob) or _script_dump_config_hit(blob)


def _has_decoded_ikcfg(blob: bytes) -> bool:
    """True when blob contains at least one IKCFG_ST/IKCFG_ED pair whose gzip member decodes."""
    start = 0
    while True:
        i = blob.find(b"IKCFG_ST", start)
        if i < 0:
            return False
        if i + 10 <= len(blob) and blob[i + 8:i + 10] == b"\x1f\x8b":
            j = blob.find(b"IKCFG_ED", i + 8)
            if j > i:
                try:
                    gzip.decompress(blob[i + 8:j])
                    return True
                except Exception:
                    pass
        start = i + 1


def _lz4_member_allow_trailing(buf: bytes) -> bytes | None:
    """Decode an LZ4 legacy stream. Bytes after the last valid block are ignored
    (extract-ikconfig keeps lz4's output even when the rest of the file follows)."""
    if not buf.startswith(b"\x02!L\x18"):
        return None
    pos = 4
    chunks: list[bytes] = []
    while pos + 4 < len(buf):
        bsz = int.from_bytes(buf[pos:pos + 4], "little")
        if bsz == 0 or bsz == 0x184C2102 or bsz > 8 * 1024 * 1024 or pos + 4 + bsz > len(buf):
            break
        try:
            chunks.append(_lz4_decompress_block(buf[pos + 4:pos + 4 + bsz]))
        except ValueError:
            break
        pos += 4 + bsz
        if sum(len(c) for c in chunks) > 512 * 1024 * 1024:
            return None
    if not chunks:
        return None
    return b"".join(chunks)


def _decompress_scanned_member(kind: str, buf: bytes) -> bytes | None:
    """Decompress one kernel compression member at the start of buf.
    Trailing bytes after the member are allowed. None means this offset is not a member.
    LZO is attempted only when lzop is installed (scripts/extract-ikconfig pipes to lzop -d)."""
    try:
        if kind == "gzip":
            if not buf.startswith(b"\x1f\x8b"):
                return None
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                out = dec.decompress(buf) + dec.flush()
                if dec.eof:
                    return out
            except zlib.error:
                pass
            # Bad CRC: keep the inflated bytes, the way gunzip and
            # lib/decompress_inflate.c do. extract-ikconfig still sees the IKCFG.
            return _gzip_lenient(buf)
        if kind == "xz":
            return _lenient_stream(
                lambda: lzma.LZMADecompressor(format=lzma.FORMAT_XZ), buf, recover_tail=True,
            )
        if kind == "lzma":
            return _lenient_stream(
                lambda: lzma.LZMADecompressor(format=lzma.FORMAT_ALONE), buf, recover_tail=True,
            )
        if kind == "bzip2":
            return _lenient_stream(bz2.BZ2Decompressor, buf)
        if kind == "lz4":
            return _lz4_member_allow_trailing(buf)
        if kind == "lz4frame":
            if not _tool_ready("lz4frame"):
                return None
            proc = subprocess.run(["lz4", "-dc"], input=buf, capture_output=True)
            if proc.stdout and (proc.returncode == 0 or _has_decoded_ikcfg(proc.stdout)):
                return proc.stdout
            return None
        if kind == "zstd":
            if not _tool_ready("zstd"):
                return None
            if _zstd is not None and "zstd" not in _hidden_decompressors:
                dec = _zstd.ZstdDecompressor()
                out = dec.decompress(buf, max_length=512 * 1024 * 1024) if hasattr(dec, "decompress") else dec.decompress(buf)
                # compression.zstd raises if the frame does not finish; trailing bytes land in unused_data
                if getattr(dec, "eof", True) is False:
                    return None
                return out
            end = _zstd_first_frame_size(buf)
            if end is None or len(buf) - end not in (0, 4):
                # A later frame is not this member. Callers that need "one member"
                # refuse on the declared slice; a scan still uses the first frame
                # when it alone carries an IKCFG.
                if end is None:
                    return None
            return _zstd_cli_decode(buf[:end] if end else buf)
        if kind == "lzo":
            if not _tool_ready("lzo"):
                return None
            proc = subprocess.run(["lzop", "-dc"], input=buf, capture_output=True)
            if proc.stdout and (proc.returncode in (0, 2) or _has_decoded_ikcfg(proc.stdout)):
                return proc.stdout
            return None
    except Exception:
        return None
    return None


# scripts/extract-ikconfig (kernel/common 3938c8d97142, same sequence as Linux v6.18
# scripts/extract-ikconfig): dump_config searches the raw image for the first
# IKCFG_ST + gzip header (\037\213\010) and zcats it; on failure it does not try a later
# raw match. try_decompress then walks gzip, xz, bzip2, lzma, lzo, lz4 legacy, zstd.
_EXTRACT_IKCONFIG_MAGICS = (
    ("gzip", b"\x1f\x8b\x08"),
    ("xz", b"\xfd7zXZ\x00"),
    ("bzip2", b"BZh"),
    ("lzma", b"\x5d\x00\x00\x00"),
    ("lzo", b"\x89LZ"),
    ("lz4", b"\x02!L\x18"),
    ("zstd", b"\x28\xb5\x2f\xfd"),
)


def _extract_ikconfig_hit(raw: bytes) -> tuple[str, int] | None:
    """The (kind, file offset) scripts/extract-ikconfig would use, or None if it would fail."""
    marker = b"IKCFG_ST\x1f\x8b\x08"
    raw_at = raw.find(marker)
    if raw_at >= 0 and _gzip_member_ok(raw[raw_at + 8:]):
        return ("raw-ikcfg", raw_at)
    for kind, magic in _EXTRACT_IKCONFIG_MAGICS:
        pos = 0
        while True:
            found = raw.find(magic, pos)
            if found < 0:
                break
            decoded = _decompress_scanned_member(kind, raw[found:])
            if decoded is not None and _scan_member_ikcfg(decoded):
                return (kind, found)
            pos = found + 1
    return None


def _outside_ikcfg_candidates(raw: bytes, start: int, end: int) -> list[str]:
    """Plausible payload / IKCFG hits whose bytes are not the single HdrS slice.
    Offsets inside [start, end) belong to the declared payload (including an IKCFG gzip
    stored as LZ4 literals) and are not a second candidate."""
    found: list[str] = []
    pos = 0
    marker = b"IKCFG_ST\x1f\x8b\x08"
    while True:
        at = raw.find(marker, pos)
        if at < 0:
            break
        if not (start <= at < end) and _gzip_member_ok(raw[at + 8:]):
            found.append(f"raw IKCFG at {at}")
        pos = at + 1
        if len(found) > 8:
            return found
    extra = _EXTRACT_IKCONFIG_MAGICS + (("lz4frame", b'\x04"M\x18'),)
    for kind, magic in extra:
        pos = 0
        while True:
            at = raw.find(magic, pos)
            if at < 0:
                break
            if not (start <= at < end):
                # Without the decompressor, a magic outside the HdrS slice is still
                # a second candidate. Ignoring it (the old lzop-missing path) let a
                # gzip image with an LZO trailer pass. LZO's script magic is only
                # three bytes; require the full lzop header so a chance \x89LZ
                # inside another member is not a false refuse.
                if kind in ("lzo", "zstd", "lz4frame") and not _tool_ready(kind):
                    if kind == "lzo" and not raw.startswith(_LZO_FILE_MAGIC, at):
                        pos = at + 1
                        continue
                    found.append(f"{kind} magic at {at} (decompressor unavailable)")
                    if len(found) > 8:
                        return found
                else:
                    decoded = _decompress_scanned_member(kind, raw[at:])
                    if decoded is not None and _scan_member_ikcfg(decoded):
                        found.append(f"{kind} payload at {at}")
                        if len(found) > 8:
                            return found
            pos = at + 1
    # Uncompressed ELF is not a try_decompress magic. extract-ikconfig sees it via IKCFG_ST.
    # An ELF that starts outside the HdrS slice and carries its own IKCFG is still a second payload.
    pos = 0
    while True:
        at = raw.find(b"\x7fELF", pos)
        if at < 0:
            break
        if not (start <= at < end) and at + 64 <= len(raw) and raw[at + 4] == 2 and raw[at + 5] == 1:
            try:
                e_phoff, e_shoff = struct.unpack_from("<QQ", raw, at + 32)
                e_phentsize, e_phnum, e_shentsize, e_shnum = struct.unpack_from("<HHHH", raw, at + 54)
            except struct.error:
                e_phoff = e_shoff = e_phnum = e_shnum = 0
                e_phentsize = e_shentsize = 0
            span_end = at + 64
            if 0 < e_phoff and e_phnum and e_phentsize >= 56 and at + e_phoff + e_phnum * e_phentsize <= len(raw):
                for i in range(e_phnum):
                    p_type, _, p_offset, _, _, p_filesz, _, _ = struct.unpack_from(
                        "<IIQQQQQQ", raw, at + e_phoff + i * e_phentsize
                    )
                    if p_type == 1 and p_filesz:
                        span_end = max(span_end, at + p_offset + p_filesz)
            if 0 < e_shoff and e_shnum and e_shentsize >= 64 and at + e_shoff + e_shnum * e_shentsize <= len(raw):
                span_end = max(span_end, at + e_shoff + e_shnum * e_shentsize)
            if span_end > at + 64 and _has_decoded_ikcfg(raw[at:span_end]):
                found.append(f"ELF payload at {at}")
                if len(found) > 8:
                    return found
        pos = at + 1
    return found


def _refuse_ambiguous_bzimage(raw: bytes, start: int, plen: int, bzimage: Path, vmlinux: bytes) -> None:
    """Refuse when the boot image does not name exactly one payload.

    scripts/extract-ikconfig's first successful hit must be the HdrS slice (a raw IKCFG
    that sits inside that slice counts: LZ4 often stores the gzipped config as literals).
    Any other payload or IKCFG outside the slice is a second candidate and is refused.
    A declared payload whose own IKCFG gzip is corrupt or missing is left for
    _decode_ikcfg_blob, which reports that more specific error.
    """
    end = start + plen
    others = _outside_ikcfg_candidates(raw, start, end)
    hit = _extract_ikconfig_hit(raw)
    reasons: list[str] = []
    if others:
        reasons.append(
            "more than one plausible payload or IKCFG candidate (" + "; ".join(others) + ")"
        )
    if hit is None:
        if _has_decoded_ikcfg(vmlinux):
            reasons.append(
                "HdrS payload is not the payload scripts/extract-ikconfig would use "
                "(scripts/extract-ikconfig finds no IKCFG)"
            )
    else:
        kind, off = hit
        inside = start <= off < end
        named = (kind == "raw-ikcfg" and inside) or (kind != "raw-ikcfg" and off == start)
        if not named:
            reasons.append(
                "HdrS payload is not the payload scripts/extract-ikconfig would use "
                f"(script hit {kind} at {off}, HdrS slice [{start}, {end}))"
            )
    if reasons:
        raise ProfileError(f"{bzimage}: " + "; ".join(reasons))


def _decode_ikcfg_blob(blob: bytes, bzimage: Path) -> str | None:
    """Return the one embedded IKCFG text. Zero markers → None. Two decodable pairs → refuse."""
    if blob.find(b"IKCFG_ST") < 0:
        return None
    texts: list[str] = []
    start = 0
    while True:
        i = blob.find(b"IKCFG_ST", start)
        if i < 0:
            break
        if i + 10 > len(blob) or blob[i + 8:i + 10] != b"\x1f\x8b":
            if not texts:
                raise ProfileError(f"{bzimage}: corrupted embedded IKCFG header after IKCFG_ST")
            start = i + 1
            continue
        j = blob.find(b"IKCFG_ED", i + 8)
        if j <= i:
            raise ProfileError(f"{bzimage}: truncated embedded IKCFG (missing IKCFG_ED)")
        try:
            texts.append(gzip.decompress(blob[i + 8:j]).decode("utf-8", "replace"))
        except Exception as e:
            raise ProfileError(f"{bzimage}: corrupted embedded IKCFG gzip payload ({e})") from e
        start = j + len(b"IKCFG_ED")
    if len(texts) > 1:
        raise ProfileError(
            f"{bzimage}: more than one plausible payload or IKCFG candidate "
            f"({len(texts)} IKCFG_ST/IKCFG_ED pairs in the decompressed payload)"
        )
    return texts[0]


def _elf64_le_span(blob: bytes) -> int | None:
    """End offset of an ELF64 LE image inside blob, from program and section headers.

    Returns None when the headers are truncated. payload_length must end at this
    span. x86 make bzImage strips with objcopy -S before compression
    (arch/x86/boot/compressed/Makefile:119) and has no uncompressed payload
    recipe; this span is not an objcopy -O binary of vmlinux.
    """
    if len(blob) < 64 or blob[:4] != b"\x7fELF" or blob[4] != 2 or blob[5] != 1:
        return None
    try:
        e_phoff, e_shoff = struct.unpack_from("<QQ", blob, 32)
        e_phentsize, e_phnum, e_shentsize, e_shnum = struct.unpack_from("<HHHH", blob, 54)
    except struct.error:
        return None
    span = 64
    if e_phoff and e_phnum and e_phentsize >= 56:
        table_end = e_phoff + e_phnum * e_phentsize
        if table_end > len(blob):
            return None
        span = max(span, table_end)
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            p_type, _, p_offset, _, _, p_filesz, _, _ = struct.unpack_from("<IIQQQQQQ", blob, off)
            if p_type == 1 and p_filesz:
                span = max(span, p_offset + p_filesz)
    if e_shoff and e_shnum and e_shentsize >= 64:
        sh_end = e_shoff + e_shnum * e_shentsize
        if sh_end > len(blob):
            return None
        span = max(span, sh_end)
    if span > len(blob):
        return None
    return span


def _decompress_declared_payload(bzimage: Path, force_pure_python_lz4: bool = False, raw: bytes | None = None) -> bytes:
    """Decompress the HdrS-declared slice only.
    kbuild formats: gzip, LZ4 legacy (`lz4 -l`), xz, lzma-alone, zstd, bzip2, uncompressed ELF
    (arch/x86/boot/compressed/Makefile gzip :136, bzip2 :138, lzma :140, xzkern :142,
    lz4 :146, zstd :148). An LZ4 frame is not an extract-ikconfig magic and is not a kbuild
    output. CONFIG_KERNEL_LZO (Makefile :154, lzo recipes :143-144) is not decoded and is refused.
    The declared slice must be one member plus an optional 4-byte uncompressed-size trailer."""
    if raw is None:
        raw = bzimage.read_bytes()
    start, plen = _hdrs_payload_bounds(raw, bzimage)
    payload = raw[start:start + plen]

    # 1. LZ4 legacy frame (\x02!L\x18, arch/x86/boot/compressed/Makefile:146)
    if payload.startswith(b"\x02!L\x18"):
        pos = 4
        n_blocks = 0
        while pos + 4 <= len(payload):
            if pos + 4 == len(payload):
                break  # 4-byte uncompressed size trailer from cmd_lz4_with_size
            bsz = int.from_bytes(payload[pos:pos + 4], "little")
            if bsz == 0 or bsz == 0x184C2102 or bsz > 8 * 1024 * 1024 or pos + 4 + bsz > len(payload):
                raise ProfileError(f"{bzimage}: corrupt or truncated LZ4 legacy payload block")
            pos += 4 + bsz
            n_blocks += 1
        if n_blocks == 0 or len(payload) - pos not in (0, 4):
            raise ProfileError(f"{bzimage}: corrupt or truncated LZ4 legacy payload")
        lz4_stream = payload[:pos]
        if shutil.which("lz4") and not force_pure_python_lz4:
            r = subprocess.run(["lz4", "-dc"], input=lz4_stream, capture_output=True)
            i_st = r.stdout.find(b"IKCFG_ST\x1f\x8b")
            i_ed = r.stdout.find(b"IKCFG_ED", i_st + 10) if i_st >= 0 else -1
            if r.returncode == 0 and i_st >= 0 and i_ed > i_st:
                try:
                    if _decode_ikcfg_blob(r.stdout, bzimage) is not None:
                        return r.stdout
                except ProfileError:
                    pass
        try:
            return _lz4_decompress_legacy(lz4_stream)
        except ValueError as e:
            raise ProfileError(f"{bzimage}: corrupt LZ4 legacy payload ({e})") from e

    # 2. LZ4 standard frame (\x04"M\x18)
    if payload.startswith(b'\x04"M\x18'):
        if not shutil.which("lz4") or force_pure_python_lz4:
            raise ProfileError(f"{bzimage}: cannot decode LZ4 frame payload without lz4 CLI")
        for candidate in (payload, payload[:-4] if len(payload) > 8 else payload):
            r = subprocess.run(["lz4", "-dc"], input=candidate, capture_output=True)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        raise ProfileError(f"{bzimage}: corrupt LZ4 frame payload")

    # 3. gzip (\x1f\x8b, arch/x86/boot/compressed/Makefile:136)
    if payload.startswith(b"\x1f\x8b"):
        try:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out = d.decompress(payload) + d.flush()
        except Exception as e:
            raise ProfileError(f"{bzimage}: corrupt gzip payload ({e})") from e
        if not d.eof or len(d.unused_data) not in (0, 4):
            raise ProfileError(f"{bzimage}: corrupt or trailing-garbage gzip payload")
        return out

    # 4. xz (\xfd7zXZ\x00, arch/x86/boot/compressed/Makefile:142 xzkern)
    if payload.startswith(b"\xfd7zXZ\x00"):
        try:
            d = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
            out = d.decompress(payload)
        except Exception as e:
            raise ProfileError(f"{bzimage}: corrupt xz payload ({e})") from e
        if not d.eof or len(d.unused_data) not in (0, 4):
            raise ProfileError(f"{bzimage}: corrupt or trailing-garbage xz payload")
        return out

    # 5. lzma alone (\x5d\x00\x00, arch/x86/boot/compressed/Makefile:140)
    if payload.startswith(b"\x5d\x00\x00"):
        try:
            d = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
            out = d.decompress(payload)
        except Exception as e:
            raise ProfileError(f"{bzimage}: corrupt lzma payload ({e})") from e
        if not d.eof or len(d.unused_data) not in (0, 4):
            raise ProfileError(f"{bzimage}: corrupt or trailing-garbage lzma payload")
        return out

    # 6. zstd (\x28\xb5\x2f\xfd, arch/x86/boot/compressed/Makefile:148)
    if payload.startswith(b"\x28\xb5\x2f\xfd"):
        if _zstd is not None:
            try:
                d = _zstd.ZstdDecompressor()
                out = d.decompress(payload)
            except Exception as e:
                raise ProfileError(f"{bzimage}: corrupt zstd payload ({e})") from e
            if not d.eof or len(d.unused_data) not in (0, 4):
                raise ProfileError(f"{bzimage}: corrupt or trailing-garbage zstd payload")
            return out
        if shutil.which("zstd") and "zstd" not in _hidden_decompressors:
            # zstd -dc returns 0 for concatenated frames, so a successful CLI
            # decode of the whole slice does not prove there is one member.
            # Accept only the first frame, or that frame plus the 4-byte size
            # trailer cmd_zstd_with_size appends (Makefile:148).
            end = _zstd_first_frame_size(payload)
            if end is not None and len(payload) - end in (0, 4):
                out = _zstd_cli_decode(payload[:end])
                if out is not None:
                    return out
            raise ProfileError(f"{bzimage}: corrupt or trailing-garbage zstd payload")
        raise ProfileError(f"{bzimage}: cannot decode zstd payload")

    # 7. bzip2 (BZh, arch/x86/boot/compressed/Makefile:138)
    if payload.startswith(b"BZh"):
        try:
            d = bz2.BZ2Decompressor()
            out = d.decompress(payload)
        except Exception as e:
            raise ProfileError(f"{bzimage}: corrupt bzip2 payload ({e})") from e
        if not d.eof or len(d.unused_data) not in (0, 4):
            raise ProfileError(f"{bzimage}: corrupt or trailing-garbage bzip2 payload")
        return out

    # 8. Uncompressed ELF vmlinux. payload_length must end at the ELF span.
    # A gzip (or any other bytes) packed into the same plen is a second member;
    # gzip/xz already reject unused_data longer than 4 bytes.
    if payload.startswith(b"\x7fELF"):
        span = _elf64_le_span(payload)
        if span is None:
            raise ProfileError(f"{bzimage}: uncompressed ELF payload is not a complete ELF64")
        if span != len(payload):
            raise ProfileError(
                f"{bzimage}: uncompressed ELF payload has {len(payload) - span} trailing bytes "
                "after the ELF span (payload_length must cover one vmlinux, not a following member)"
            )
        return payload

    raise ProfileError(
        f"{bzimage}: unsupported or undecodable bzImage payload format (magic={payload[:8].hex()})"
    )


def _extract_bzimage_payload(bzimage: Path, force_pure_python_lz4: bool = False, raw: bytes | None = None) -> bytes:
    """Decompress the HdrS payload and refuse an ambiguous bzImage.

    The self-built-kernel gate catches mistakes in a developer's own kernel config
    (a driver left built-in, or an override that ignores the bootloader command line).
    A developer who deliberately crafts a kernel to defeat the gate already controls
    the machine; that case is out of scope. Ambiguity is still refused, because a
    mistake hides in the same place: more than one plausible payload or IKCFG, or a
    payload the HdrS header names that is not the one scripts/extract-ikconfig would
    use (kernel/common 3938c8d97142, scripts/extract-ikconfig: raw IKCFG_ST+gzip first,
    then gzip, xz, bzip2, lzma, lzo, lz4 legacy, zstd). CONFIG_KERNEL_LZO is not decoded.
    """
    if raw is None:
        raw = bzimage.read_bytes()
    vmlinux = _decompress_declared_payload(bzimage, force_pure_python_lz4=force_pure_python_lz4, raw=raw)
    start, plen = _hdrs_payload_bounds(raw, bzimage)
    _refuse_ambiguous_bzimage(raw, start, plen, bzimage, vmlinux)
    return vmlinux


def _extract_bzimage_ikcfg(bzimage: Path, force_pure_python_lz4: bool = False) -> str | None:
    """Extract the embedded IKCFG (.config) from the decompressed bzImage payload."""
    vmlinux = _extract_bzimage_payload(bzimage, force_pure_python_lz4=force_pure_python_lz4)
    return _decode_ikcfg_blob(vmlinux, bzimage)


def build_test_vmlinux_elf(
    ikcfg_text: str | None,
    symbols: list[tuple[int, str, str]] | None = None,
    sysmap_text: str | None = None,
) -> bytes:
    """Build a minimal 64-bit little-endian ELF vmlinux carrying an embedded IKCFG blob and an SHT_SYMTAB
    matching symbols or sysmap_text (used by unit-test fixtures)."""
    prog = bytearray(b"\x90" * 16)
    if ikcfg_text is not None:
        prog += b"IKCFG_ST" + gzip.compress(ikcfg_text.encode("utf-8")) + b"IKCFG_ED"
    while len(prog) % 8:
        prog.append(0)
    strtab = bytearray(b"\x00.text\x00.symtab\x00.strtab\x00")
    symtab = bytearray(24)  # STN_UNDEF
    if symbols is None:
        if sysmap_text is not None:
            symbols = []
            for raw_line in sysmap_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                addr_s, tp, name = line.split()
                symbols.append((int(addr_s, 16), tp, name))
        else:
            symbols = [
                (0xFFFFFFFF81000000, "t", "nvme_init"),
                (0xFFFFFFFF81000100, "t", "mei_me_driver_init"),
            ]
    for addr, tp, name in symbols:
        name_off = len(strtab)
        strtab.extend(name.encode("ascii") + b"\x00")
        bind = 1 if tp.isupper() else 0
        stype = 2 if tp in ("t", "T") else 1
        st_info = (bind << 4) | stype
        # st_other in a real ELF is the visibility byte, not an nm type letter.
        # This fixture stores the System.map letter there so the unit-test .symtab
        # path can round-trip. Production bzImage is stripped (objcopy -S,
        # arch/x86/boot/compressed/Makefile:119) and does not use this convention.
        st_other = ord(tp) & 0xFF
        symtab.extend(struct.pack("<IBBHQQ", name_off, st_info, st_other, 1, addr, 16))
    while len(strtab) % 8:
        strtab.append(0)
    ehdr_sz = 64
    phdr_sz = 56
    prog_off = ehdr_sz + phdr_sz
    sym_off = prog_off + len(prog)
    str_off = sym_off + len(symtab)
    shdr_off = str_off + len(strtab)
    vaddr_base = 0xFFFFFFFF81000000
    e_ident = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    ehdr = struct.pack(
        "<16sHHIQQQIHHHHHH",
        e_ident, 2, 62, 1, vaddr_base, ehdr_sz, shdr_off, 0,
        ehdr_sz, phdr_sz, 1, 64, 4, 3,
    )
    phdr = struct.pack(
        "<IIQQQQQQ",
        1, 5, prog_off, vaddr_base, vaddr_base, len(prog), len(prog), 0x1000,
    )
    sh_null = b"\x00" * 64
    sh_text = struct.pack("<IIQQQQIIQQ", 1, 1, 6, vaddr_base, prog_off, len(prog), 0, 0, 8, 0)
    sh_sym = struct.pack("<IIQQQQIIQQ", 7, 2, 0, 0, sym_off, len(symtab), 3, 1, 8, 24)
    sh_str = struct.pack("<IIQQQQIIQQ", 15, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0)
    return ehdr + phdr + bytes(prog) + bytes(symtab) + bytes(strtab) + sh_null + sh_text + sh_sym + sh_str


def wrap_hdrs_bzimage(payload: bytes, setup_extra: bytes = b"") -> bytes:
    """Wrap a compressed or uncompressed vmlinux payload in a valid x86 HdrS bzImage container."""
    setup_sects = 4
    setup = bytearray((setup_sects + 1) * 512)
    setup[0:2] = b"MZ"
    setup[0x1F1] = setup_sects
    setup[0x1FE:0x200] = b"\x55\xaa"
    setup[0x202:0x206] = b"HdrS"
    struct.pack_into("<II", setup, 0x248, 0, len(payload))
    if setup_extra:
        setup[0x260:0x260 + len(setup_extra)] = setup_extra
    return bytes(setup) + payload


def _parse_sysmap(sysmap_text: str, sysmap_path: Path) -> tuple[list[tuple[int, str, str]], dict[str, list[tuple[int, str]]]]:
    entries: list[tuple[int, str, str]] = []
    by_name: dict[str, list[tuple[int, str]]] = {}
    for lineno, raw_line in enumerate(sysmap_text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        m = re.fullmatch(r"([0-9a-fA-F]{8,16})\s+([A-Za-z])\s+(\S+)", line)
        if not m:
            raise ProfileError(f"{sysmap_path}:{lineno}: malformed System.map line {raw_line!r}")
        addr = int(m.group(1), 16)
        tp = m.group(2)
        name = m.group(3)
        entries.append((addr, tp, name))
        by_name.setdefault(name, []).append((addr, tp))
    if not entries:
        raise ProfileError(f"{sysmap_path}: empty System.map")
    return entries, by_name


def _kallsyms_watch_symbols(required_initcalls: set[str], kallsyms_all: bool) -> set[str]:
    """Symbols the runtime kallsyms table must contain for this config.

    scripts/kallsyms.c symbol_valid() without --all-symbols keeps symbols in
    [_stext, _etext] and [_sinittext, _einittext] (both ends inclusive; the range
    symbols themselves stay, and _einittext is the end_sym the corner case at
    :220-224 does not drop) plus names starting with __start_ or __stop_.
    scripts/link-vmlinux.sh:141-142 passes --all-symbols only for CONFIG_KALLSYMS_ALL=y.

    __init_end is after INIT_DATA_SECTION and PERCPU_SECTION
    (arch/x86/kernel/vmlinux.lds.S:310,414,429), past _einittext, so it is absent
    when KALLSYMS_ALL=n. The initcall names are .init.text functions inside
    _sinittext.._einittext, so they are in the table either way, and those names
    are what initcall_blacklist= matches. Requiring KALLSYMS_ALL=y would refuse
    the Kconfig default even when that check can still be done. __init_begin and
    __init_end are watched only when ALL=y puts them in the runtime table.
    """
    watch = set(required_initcalls) | {"_stext", "_etext", "_sinittext", "_einittext"}
    if kallsyms_all:
        watch |= {"__init_begin", "__init_end"}
    return watch


_RELEASE_FAMILY = re.compile(rb"Linux version 6\.18\.\d+-android17-")


def _require_release_family(banner_raw: bytes, where: str) -> None:
    """Refuse a linux_banner outside the pinned release family.

    kernel/common 3938c8d97142 is the source pin. This check is the release
    family ``Linux version 6.18.<n>-android17-``, not that exact commit. The
    android17 suffix is the build's setlocalversion/Kleaf stamp;
    CONFIG_LOCALVERSION is often empty.
    """
    if not banner_raw.startswith(b"Linux version "):
        raise ProfileError(f"{where}: linux_banner address in System.map does not match the image")
    if _RELEASE_FAMILY.match(banner_raw) is None:
        raise ProfileError(
            f"{where}: linux_banner {banner_raw[:80]!r} is not the pinned release family "
            "'Linux version 6.18.<n>-android17-' (kernel/common 3938c8d97142 is the source pin; "
            "this check is that release family, not the exact commit). "
            "The android17 suffix is the build setlocalversion/Kleaf stamp; "
            "CONFIG_LOCALVERSION is often empty."
        )


def _linux_banner_from_map(vmlinux: bytes, sysmap_by_name: dict, bzimage: Path, sysmap_path: Path) -> bytes | None:
    """The linux_banner bytes System.map names, or None when the map has no such symbol.

    A map that names the symbol but whose address is outside a PT_LOAD is a refuse.
    An image without program headers returns None so a kernel that does not carry
    this table is left to the caller.
    """
    if "linux_banner" not in sysmap_by_name:
        return None
    if len(vmlinux) < 64 or vmlinux[:4] != b"\x7fELF" or vmlinux[4] != 2 or vmlinux[5] != 1:
        return None
    e_phoff = struct.unpack_from("<Q", vmlinux, 32)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", vmlinux, 54)
    if e_phoff == 0 or e_phnum == 0 or e_phentsize < 56 or e_phoff + e_phnum * e_phentsize > len(vmlinux):
        return None
    va = sysmap_by_name["linux_banner"][0][0]
    off = None
    for i in range(e_phnum):
        p_type, _, p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from(
            "<IIQQQQQQ", vmlinux, e_phoff + i * e_phentsize
        )
        if p_type == 1 and p_filesz > 0 and p_vaddr <= va < p_vaddr + p_filesz:
            if p_offset + p_filesz > len(vmlinux):
                raise ProfileError(f"{bzimage}: truncated PT_LOAD segment in vmlinux ELF")
            off = p_offset + (va - p_vaddr)
            break
    if off is None:
        raise ProfileError(f"{sysmap_path}: System.map address 0x{va:x} is outside {bzimage} PT_LOAD segments")
    raw = vmlinux[off:off + 256]
    nul = raw.find(b"\x00")
    if nul >= 0:
        raw = raw[:nul]
    return raw


def _verify_sysmap_against_vmlinux(
    vmlinux: bytes,
    sysmap_entries: list[tuple[int, str, str]],
    sysmap_by_name: dict[str, list[tuple[int, str]]],
    required_initcalls: set[str],
    bzimage: Path,
    sysmap_path: Path,
    kallsyms_all: bool = False,
) -> None:
    """Verify System.map against the decompressed vmlinux.

    .symtab, when present, must match System.map in full. That does not skip the runtime kallsyms
    table: if System.map names kallsyms_num_syms and the other kallsyms anchors, those bytes are
    checked too. A disagreement on a watched symbol is a refuse; the runtime table wins, .symtab
    cannot hide it. The kallsyms watch set is _kallsyms_watch_symbols(): the required initcalls
    plus the text/inittext range symbols kallsyms.c keeps, and __init_begin/__init_end only when
    CONFIG_KALLSYMS_ALL=y. The table located is the one those System.map symbols name (the
    linked addresses the kernel uses), not a second unreferenced copy.
    The .symtab type letter is read from st_other only because the unit-test fixture stores
    the nm letter there. A general unstripped ELF uses st_other for visibility; this gate does
    not claim to accept one. Production bzImages are stripped (arch/x86/boot/compressed/Makefile:119,
    objcopy -S), so step-1 has no .symtab and only takes the kallsyms path.
    """
    if len(vmlinux) < 64 or vmlinux[:4] != b"\x7fELF" or vmlinux[4] != 2 or vmlinux[5] != 1:
        raise ProfileError(
            f"{bzimage}: bzImage payload is not a 64-bit ELF vmlinux (cannot verify System.map symbols {sorted(required_initcalls)})"
        )
    e_phoff, e_shoff = struct.unpack_from("<QQ", vmlinux, 32)
    e_phentsize, e_phnum, e_shentsize, e_shnum = struct.unpack_from("<HHHH", vmlinux, 54)
    symtab_checked = False
    kallsyms_anchors = (
        "linux_banner",
        "kallsyms_num_syms",
        "kallsyms_names",
        "kallsyms_token_table",
        "kallsyms_token_index",
        "kallsyms_offsets",
        "kallsyms_relative_base",
    )

    # Check if ELF SHT_SYMTAB (.symtab) is present (unstripped vmlinux or unit-test ELF fixture)
    if e_shoff > 0 and e_shnum > 0 and e_shentsize >= 64 and e_shoff + e_shnum * e_shentsize <= len(vmlinux):
        shdrs = []
        for i in range(e_shnum):
            shdrs.append(struct.unpack_from("<IIQQQQIIQQ", vmlinux, e_shoff + i * e_shentsize))
        sym_shdrs = [s for s in shdrs if s[1] == 2]  # SHT_SYMTAB
        if sym_shdrs:
            sym_sh = sym_shdrs[0]
            sh_offset, sh_size, sh_link, _, _, sh_entsize = sym_sh[4], sym_sh[5], sym_sh[6], sym_sh[7], sym_sh[8], sym_sh[9]
            if sh_link >= len(shdrs) or sh_entsize < 24 or sh_offset + sh_size > len(vmlinux):
                raise ProfileError(f"{bzimage}: malformed ELF SHT_SYMTAB")
            str_sh = shdrs[sh_link]
            str_off, str_sz = str_sh[4], str_sh[5]
            if str_off + str_sz > len(vmlinux):
                raise ProfileError(f"{bzimage}: malformed ELF SHT_STRTAB")
            strtab = vmlinux[str_off:str_off + str_sz]
            elf_syms: list[tuple[int, str, str]] = []
            elf_by_name: dict[str, list[tuple[int, str]]] = {}
            for off in range(sh_offset + sh_entsize, sh_offset + sh_size, sh_entsize):
                st_name, st_info, st_other, st_shndx, st_value, _ = struct.unpack_from("<IBBHQQ", vmlinux, off)
                if st_name == 0 or st_name >= len(strtab):
                    continue
                end = strtab.find(b"\x00", st_name)
                if end < 0:
                    continue
                sname = strtab[st_name:end].decode("ascii", "replace")
                # Fixture-only: build_test_vmlinux_elf stores the nm letter in st_other.
                # A real ELF keeps visibility there (usually 0); the fallback below is not
                # an nm classification and is not how a production image is checked.
                if chr(st_other) in ("t", "T", "d", "D", "r", "R", "b", "B"):
                    tp = chr(st_other)
                else:
                    is_func = (st_info & 0xF) == 2
                    is_glob = (st_info >> 4) == 1
                    tp = ("T" if is_glob else "t") if is_func else ("D" if is_glob else "d")
                elf_syms.append((st_value, tp, sname))
                elf_by_name.setdefault(sname, []).append((st_value, tp))
            if elf_syms != sysmap_entries:
                raise ProfileError(
                    f"{sysmap_path}: System.map does not match {bzimage} ELF symbol table"
                )
            for ic in sorted(required_initcalls):
                if not any(tp in ("t", "T") for _, tp in elf_by_name.get(ic, ())):
                    raise ProfileError(
                        f"{bzimage}: initcall symbol {ic} is not a text symbol (t/T) in vmlinux ELF"
                    )
            symtab_checked = True
            if not all(k in sysmap_by_name for k in kallsyms_anchors):
                return

    # Stripped vmlinux (production) or an unstripped ELF whose System.map also names kallsyms.
    if e_phoff == 0 or e_phnum == 0 or e_phentsize < 56 or e_phoff + e_phnum * e_phentsize > len(vmlinux):
        if symtab_checked:
            raise ProfileError(
                f"{bzimage}: runtime kallsyms disagrees with .symtab "
                "(System.map names kallsyms anchors but vmlinux has no PT_LOAD to read them)"
            )
        raise ProfileError(f"{bzimage}: vmlinux ELF lacks valid program headers")
    pt_loads = []
    for i in range(e_phnum):
        p_type, _, p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from(
            "<IIQQQQQQ", vmlinux, e_phoff + i * e_phentsize
        )
        if p_type == 1 and p_filesz > 0:
            if p_offset + p_filesz > len(vmlinux):
                raise ProfileError(f"{bzimage}: truncated PT_LOAD segment in vmlinux ELF")
            pt_loads.append((p_vaddr, p_filesz, p_offset))
    if not pt_loads:
        raise ProfileError(f"{bzimage}: vmlinux ELF has no PT_LOAD segments")

    def v2o(va: int) -> int:
        for vaddr, filesz, offset in pt_loads:
            if vaddr <= va < vaddr + filesz:
                return offset + (va - vaddr)
        raise ProfileError(f"{sysmap_path}: System.map address 0x{va:x} is outside {bzimage} PT_LOAD segments")

    for ksym in kallsyms_anchors:
        if ksym not in sysmap_by_name:
            raise ProfileError(
                f"{sysmap_path}: System.map does not match {bzimage} (missing {ksym} required to verify kallsyms)"
            )
    banner_off = v2o(sysmap_by_name["linux_banner"][0][0])
    banner_raw = vmlinux[banner_off:banner_off + 256]
    nul = banner_raw.find(b"\x00")
    if nul >= 0:
        banner_raw = banner_raw[:nul]
    # Release family, not the exact commit 3938c8d97142. Another tree can rename
    # objects the Kconfig tables do not know.
    _require_release_family(banner_raw, str(sysmap_path))

    num_syms_va = sysmap_by_name["kallsyms_num_syms"][0][0]
    names_va = sysmap_by_name["kallsyms_names"][0][0]
    tok_tab_va = sysmap_by_name["kallsyms_token_table"][0][0]
    tok_idx_va = sysmap_by_name["kallsyms_token_index"][0][0]
    offsets_va = sysmap_by_name["kallsyms_offsets"][0][0]
    rel_base_va = sysmap_by_name["kallsyms_relative_base"][0][0]

    # scripts/kallsyms.c writes kallsyms_num_syms as .long (unsigned int). The next label is
    # .balign 8, so the names symbol sits 8 bytes later and the 4 padding bytes are not the count.
    num_syms = struct.unpack_from("<I", vmlinux, v2o(num_syms_va))[0]
    if (
        names_va != num_syms_va + 8
        or offsets_va != tok_idx_va + 512
        or not (1 <= num_syms <= 5_000_000)
        or rel_base_va != ((offsets_va + num_syms * 4 + 7) & ~7)
    ):
        raise ProfileError(f"{sysmap_path}: kallsyms table layout in System.map does not match {bzimage}")

    rel_base = struct.unpack_from("<Q", vmlinux, v2o(rel_base_va))[0]
    tok_tab_off = v2o(tok_tab_va)
    tok_idx_off = v2o(tok_idx_va)
    tok_indices = struct.unpack_from("<256H", vmlinux, tok_idx_off)
    tokens: list[bytes] = []
    for idx in tok_indices:
        end = vmlinux.find(b"\x00", tok_tab_off + idx)
        if end < 0 or end - (tok_tab_off + idx) > 256:
            raise ProfileError(f"{sysmap_path}: corrupt kallsyms_token_table at System.map address")
        tokens.append(vmlinux[tok_tab_off + idx:end])

    names_off = v2o(names_va)
    offsets_off = v2o(offsets_va)
    if offsets_off + num_syms * 4 > len(vmlinux):
        raise ProfileError(f"{sysmap_path}: kallsyms_offsets runs past end of {bzimage}")

    watch = _kallsyms_watch_symbols(required_initcalls, kallsyms_all)
    ks_found: dict[str, list[tuple[int, str]]] = {}
    pos = names_off
    vlen = len(vmlinux)
    for i in range(num_syms):
        if pos >= vlen:
            raise ProfileError(f"{sysmap_path}: kallsyms_names runs past end of {bzimage}")
        length = vmlinux[pos]
        pos += 1
        if length & 0x80:
            if pos >= vlen:
                raise ProfileError(f"{sysmap_path}: truncated kallsyms_names entry in {bzimage}")
            length = (length & 0x7F) | (vmlinux[pos] << 7)
            pos += 1
        if length == 0 or pos + length > vlen:
            raise ProfileError(f"{sysmap_path}: invalid kallsyms_names entry length in {bzimage}")
        raw_bytes = vmlinux[pos:pos + length]
        pos += length
        expanded = b"".join(tokens[b] for b in raw_bytes).decode("ascii", "replace")
        stype, sname = expanded[0], expanded[1:]
        if sname in watch:
            # kernel/kallsyms.c: kallsyms_relative_base + (u32)offset. No signed special case.
            off_u = struct.unpack_from("<I", vmlinux, offsets_off + i * 4)[0]
            va = (rel_base + off_u) & 0xFFFFFFFFFFFFFFFF
            ks_found.setdefault(sname, []).append((va, stype))

    all_label = "y" if kallsyms_all else "n"
    for sym in sorted(watch):
        got = ks_found.get(sym)
        exp = sysmap_by_name.get(sym)
        if exp is None or got != exp:
            prefix = "runtime kallsyms disagrees with .symtab; " if symtab_checked else ""
            if got is None:
                raise ProfileError(
                    f"{sysmap_path}: {prefix}kallsyms has no {sym} "
                    f"(CONFIG_KALLSYMS_ALL={all_label}; scripts/kallsyms.c keeps symbols in "
                    f"_stext.._etext and _sinittext.._einittext, plus __start_ and __stop_ names, "
                    f"unless --all-symbols). System.map entry for {sym} is {exp}"
                )
            raise ProfileError(
                f"{sysmap_path}: {prefix}kallsyms entry for {sym} ({got}) does not match "
                f"System.map entry for {sym} ({exp})"
            )


class CheckedConfig(dict):
    """Config dict returned by check_kconfig. bzimage_sha256 is the image the gate actually read."""

    bzimage_sha256: str


# Module basenames that must never be shipped. Hyphens in the filename are normalized to underscores.
_NEVER_SHIP_KO = frozenset({
    "efivarfs", "capsule_loader", "msr", "ec_sys", "acpi_dbg", "acpi_configfs",
})


def _shipped_module_base(filename: str) -> str:
    base = filename[:-3] if filename.endswith(".ko") else filename
    return base.replace("-", "_")


def _check_shipped_modules(kdir: Path, cfg: dict, bmods: set, cfg_path: Path) -> None:
    """Cross-check modules/*.ko against the .config when that directory exists.

    A basename in the never-load set is refused. A basename that this gate maps from a
    Kconfig symbol must be =m in the config and listed in blacklist.modules. Other names
    (amdgpu, amd_pmc, device drivers this table does not map) are not rejected here.
    """
    moddir = kdir / "modules"
    if not moddir.is_dir():
        return
    reverse: dict[str, list[str]] = {}
    for sym, mod in KCONFIG_BUILTIN_MODULE.items():
        reverse.setdefault(mod, []).append(sym)
    reverse.setdefault("spi_amd_pci", []).append("CONFIG_SPI_AMD")
    for ko in sorted(moddir.glob("*.ko")):
        base = _shipped_module_base(ko.name)
        if base in _NEVER_SHIP_KO:
            raise ProfileError(
                f"{ko}: module file {base} is in the never-load set and must not be shipped"
            )
        syms = reverse.get(base)
        if not syms:
            continue
        if base not in bmods:
            raise ProfileError(f"{ko}: {base} is not in blacklist.modules")
        if not any(cfg.get(s) == "m" for s in syms):
            raise ProfileError(
                f"{cfg_path}: {ko.name} does not correspond to a =m Kconfig object ({', '.join(syms)})"
            )


def _boot_entries_missing(prof, token: str) -> list[str]:
    """Quiet and serial-debug entries that do not carry token.

    make_entries appends the tail after stock options. An empty vendor cmdline
    still shows a drop list removing a tail token from an entry that is written.
    """
    entries = make_entries(prof, "")
    missing = []
    if token not in entries["quiet"]:
        missing.append("quiet entry")
    if token not in entries["debug"]:
        missing.append("serial entry")
    return missing


def _require_on_boot_entries(prof, token: str, cfg_path, option: str, why: str) -> None:
    missing = _boot_entries_missing(prof, token)
    if missing:
        raise ProfileError(
            f"{cfg_path}: {option} is forbidden unless every boot entry carries {token} "
            f"(missing on {', '.join(missing)}). The gate checks the quiet and serial options "
            f"make_entries writes, not only the declared tail. {why}"
        )


def check_kconfig(profile_dir, kernel_dir, safety_path=SAFETY):
    """Validate a developer's own kernel (.config, bzImage, System.map) against the GBOS safety rules.

    The self-built-kernel gate catches mistakes in that developer's config: a driver left
    built-in, or an override that ignores the bootloader command line. A developer who
    deliberately crafts a kernel to defeat the gate already controls the machine; that case
    is out of scope. Ambiguity is still refused, because a mistake hides in the same place:
    more than one plausible payload or IKCFG, an HdrS payload that is not the one
    scripts/extract-ikconfig would use (kernel/common 3938c8d97142), or a .symtab that
    disagrees with runtime kallsyms on the checked symbols. The runtime table is preferred.

    Returns a CheckedConfig (a dict of CONFIG_* values) whose bzimage_sha256 is the SHA-256
    of the bzImage bytes this call verified.
    """
    prof = load(profile_dir, safety_path=safety_path) if not isinstance(profile_dir, dict) else profile_dir
    kdir = Path(kernel_dir)
    cfg_path = kdir / ".config"
    if not cfg_path.is_file():
        raise ProfileError(f"{kdir}: missing .config (GBOS_DEV_KERNEL_DIR requires .config for the safety gate)")
    cfg_text = cfg_path.read_text(encoding="utf-8", errors="replace")
    cfg = parse_kconfig(cfg_text)
    if not cfg:
        raise ProfileError(f"{cfg_path}: no CONFIG_* entries found")
    bz = kdir / "bzImage"
    if not bz.is_file():
        raise ProfileError(
            f"{kdir}: missing bzImage (check-kconfig requires the image whose IKCFG and symbols it checks)"
        )
    raw_bz = bz.read_bytes()
    bz_sha = hashlib.sha256(raw_bz).hexdigest()
    vmlinux_payload = _extract_bzimage_payload(bz, raw=raw_bz)
    embedded = _decode_ikcfg_blob(vmlinux_payload, bz)
    if embedded is None:
        raise ProfileError(f"{bz}: missing embedded IKCFG (CONFIG_IKCONFIG=y is required in bzImage)")
    if parse_kconfig(embedded) != cfg:
        raise ProfileError(f"{bz}: embedded IKCFG does not match {cfg_path}")
    bmods = set(prof["blacklist"]["modules"])
    # 1. CONFIG_KALLSYMS=y (init/Kconfig:1974, kernel/kallsyms.c) is required so initcall_blacklisted() in
    #    init/main.c:1292 initcall_blacklisted() resolves names via sprint_symbol_no_offset() at :1302
    if cfg.get("CONFIG_KALLSYMS") != "y":
        raise ProfileError(
            f"{cfg_path}: CONFIG_KALLSYMS=y is required (initcall_blacklist=nvme_init,mei_me_driver_init uses "
            "sprint_symbol_no_offset() in init/main.c:1302, which is a no-op without CONFIG_KALLSYMS=y)"
        )
    # 1b. Core module/IKCFG invariants (always required; no small-config bypass)
    for k, why in KCONFIG_REQUIRED_Y:
        v = cfg.get(k)
        if v is not None and v != "y":
            raise ProfileError(f"{cfg_path}: {k}=y is required, got {k}={v} ({why})")
        if v is None:
            raise ProfileError(f"{cfg_path}: {k}=y is required ({why})")
    if cfg.get("CONFIG_STRICT_DEVMEM") == "n":
        raise ProfileError(f"{cfg_path}: CONFIG_STRICT_DEVMEM=n is forbidden (weakens /dev/mem restrictions; lib/Kconfig.debug:1878)")
    if cfg.get("CONFIG_IO_STRICT_DEVMEM") == "n":
        raise ProfileError(f"{cfg_path}: CONFIG_IO_STRICT_DEVMEM=n is forbidden (weakens /dev/mem I/O restrictions; lib/Kconfig.debug:1898)")
    # 1c. Built-in kernel command line (CONFIG_CMDLINE_BOOL / CONFIG_CMDLINE / CONFIG_CMDLINE_OVERRIDE / CONFIG_CMDLINE_FORCE)
    #     and embedded bootconfig (CONFIG_BOOT_CONFIG_EMBED / CONFIG_BOOT_CONFIG_FORCE, init/Kconfig:1504,1517).
    #     Note: CONFIG_BOOT_CONFIG=y and the 'bootconfig' token in CONFIG_CMDLINE are permitted (stock lapis uses them
    #     for the verified initrd androidboot.* trailer).
    for over_k in ("CONFIG_CMDLINE_OVERRIDE", "CONFIG_CMDLINE_FORCE"):
        if cfg.get(over_k) not in (None, "n"):
            raise ProfileError(
                f"{cfg_path}: {over_k}={cfg[over_k]} is forbidden (overrides the ESP bootloader command line and "
                "drops module_blacklist=, initcall_blacklist= and efi=noruntime; arch/x86/Kconfig:2314)"
            )
    for bc_k in ("CONFIG_BOOT_CONFIG_EMBED", "CONFIG_BOOT_CONFIG_FORCE"):
        if cfg.get(bc_k) not in (None, "n"):
            raise ProfileError(
                f"{cfg_path}: {bc_k}={cfg[bc_k]} is forbidden (embedded/forced bootconfig is not permitted; "
                "bootconfig must come only from the verified initrd trailer; init/Kconfig:1504,1517)"
            )
    if cfg.get("CONFIG_CMDLINE_BOOL") not in (None, "n", "y"):
        raise ProfileError(f"{cfg_path}: invalid CONFIG_CMDLINE_BOOL={cfg.get('CONFIG_CMDLINE_BOOL')}")
    if cfg.get("CONFIG_CMDLINE_BOOL") == "y" or cfg.get("CONFIG_CMDLINE", '""') != '""':
        raw_cmd = cfg.get("CONFIG_CMDLINE", "")
        if len(raw_cmd) < 2 or not (raw_cmd.startswith('"') and raw_cmd.endswith('"')) or '"' in raw_cmd[1:-1] or "'" in raw_cmd[1:-1]:
            raise ProfileError(f"{cfg_path}: CONFIG_CMDLINE must be a double-quoted string without inner quotes")
        cmd_str = raw_cmd[1:-1]
        # 256 is a backstop for the builtin string alone. builder/image.py checks
        # the bootloader line plus this string against x86 COMMAND_LINE_SIZE
        # (2048, arch/x86/include/asm/setup.h:7) via x86_cmdline_fits(): setup.c
        # strlcat drops the tail, which is where the safety options sit.
        if len(cmd_str) > 256:
            raise ProfileError(f"{cfg_path}: CONFIG_CMDLINE exceeds 256 characters ({len(cmd_str)})")
        # kernel/params.c:parameqn() treats '-' and '_' as equivalent in parameter and module names
        # (e.g. spi_intel.writeable=1 is identical to spi-intel.writeable=1).
        raw_prefixes = tuple(prof["_safety"]["cmdline"]["forbidden_prefixes"]) + ("amdgpu.", "pci-stub.")
        forbidden_pre_norm = tuple(p.replace("_", "-") for p in raw_prefixes)
        forbidden_toks = (
            set(prof["_safety"]["cmdline"]["stock_drop"])
            | set(prof["cmdline"]["stock_drop"])
            | set(prof["cmdline"]["debug_drop"])
            | set(prof["cmdline"].get("quiet_drop", ()))
        )
        forbidden_toks_norm = {
            (t.split("=", 1)[0].replace("_", "-") + "=" + t.split("=", 1)[1] if "=" in t else t.replace("_", "-")): t
            for t in forbidden_toks
        }
        tail_tokens = prof["_safety"]["cmdline"]["tail"] + prof["cmdline"]["tail"]
        tail_kv_norm = {
            t.split("=", 1)[0].replace("_", "-"): (
                t.split("=", 1)[0].replace("_", "-") + "=" + t.split("=", 1)[1],
                t,
            )
            for t in tail_tokens
            if "=" in t
        }
        for tok in cmd_str.split():
            if not TOKEN.fullmatch(tok):
                raise ProfileError(f"{cfg_path}: CONFIG_CMDLINE token {tok!r} is not a valid kernel option")
            if tok == "--" or tok.startswith("--"):
                raise ProfileError(
                    f"{cfg_path}: CONFIG_CMDLINE token {tok!r} is forbidden ('--' stops parse_args() in "
                    "kernel/params.c and skips the appended ESP bootloader command line)"
                )
            key = tok.split("=", 1)[0]
            key_norm = key.replace("_", "-")
            tok_norm = (key_norm + "=" + tok.split("=", 1)[1]) if "=" in tok else key_norm
            if tok_norm.startswith(forbidden_pre_norm):
                raise ProfileError(
                    f"{cfg_path}: CONFIG_CMDLINE token {tok!r} is forbidden (starts with a reserved safety/GPU prefix)"
                )
            if tok in forbidden_toks or tok_norm in forbidden_toks_norm:
                raise ProfileError(
                    f"{cfg_path}: CONFIG_CMDLINE token {tok!r} is forbidden (listed in cmdline stock_drop/debug_drop/quiet_drop)"
                )
            if key_norm in tail_kv_norm and tok_norm != tail_kv_norm[key_norm][0]:
                raise ProfileError(
                    f"{cfg_path}: CONFIG_CMDLINE token {tok!r} conflicts with safety/profile tail option {tail_kv_norm[key_norm][1]!r}"
                )
    # Clang LTO can point initcall entries at generated __initstub__ wrappers (include/linux/init.h).
    # The blacklist names nvme_init / mei_me_driver_init, so those built-ins are refused under LTO.
    # CONFIG_LTO_NONE (the pinned step-1 choice) and the HAS_/ARCH_SUPPORTS_ flags are not this check.
    if cfg.get("CONFIG_BLK_DEV_NVME") == "y" or cfg.get("CONFIG_INTEL_MEI_ME") == "y":
        for lto_k in ("CONFIG_LTO_CLANG", "CONFIG_LTO_CLANG_THIN", "CONFIG_LTO_CLANG_FULL"):
            if cfg.get(lto_k) not in (None, "n"):
                raise ProfileError(
                    f"{cfg_path}: {lto_k}={cfg[lto_k]} is forbidden while CONFIG_BLK_DEV_NVME or "
                    "CONFIG_INTEL_MEI_ME is built-in (Clang LTO initcall wrappers are not the names "
                    "initcall_blacklist= checks; use CONFIG_LTO_NONE)"
                )
    # 2. Built-in drivers blocked by initcall_blacklist= (CONFIG_BLK_DEV_NVME, CONFIG_INTEL_MEI_ME) and SATA isolation
    _, tail_initcalls, _ = guards(prof)
    tail_ic_set = set(tail_initcalls)
    sysmap_path = kdir / "System.map"
    sysmap_text = sysmap_path.read_text(encoding="utf-8", errors="replace") if sysmap_path.is_file() else None
    sysmap_parsed: tuple[list[tuple[int, str, str]], dict[str, list[tuple[int, str]]]] | None = None
    required_initcalls: set[str] = set()
    for kcfg_sym, (ic_sym, mod_name, citation) in KCONFIG_INITCALL_BLACKLIST.items():
        val = cfg.get(kcfg_sym)
        if val not in (None, "n", "m", "y"):
            raise ProfileError(f"{cfg_path}: invalid {kcfg_sym}={val}")
        if val == "m" and mod_name not in bmods:
            if kcfg_sym == "CONFIG_BLK_DEV_NVME":
                raise ProfileError(
                    f"{cfg_path}: CONFIG_BLK_DEV_NVME=m is not blocked by initcall_blacklist={ic_sym} unless "
                    f"{mod_name!r} is in blacklist.modules (use CONFIG_BLK_DEV_NVME=y so initcall_blacklist={ic_sym} "
                    f"blocks it, or unset it; {citation})"
                )
            raise ProfileError(
                f"{cfg_path}: {kcfg_sym}=m without {mod_name!r} in blacklist.modules bypasses "
                f"initcall_blacklist={ic_sym} ({citation})"
            )
        if val == "y":
            if ic_sym not in tail_ic_set:
                raise ProfileError(
                    f"{cfg_path}: {kcfg_sym}=y requires {ic_sym!r} in initcall_blacklist= ({citation})"
                )
            if sysmap_text is None:
                raise ProfileError(
                    f"{kdir}: missing System.map (required when {kcfg_sym}=y to verify initcall symbol {ic_sym}; {citation})"
                )
            if sysmap_parsed is None:
                sysmap_parsed = _parse_sysmap(sysmap_text, sysmap_path)
            _, sysmap_by_name = sysmap_parsed
            if not any(tp in ("t", "T") for _, tp in sysmap_by_name.get(ic_sym, ())):
                raise ProfileError(
                    f"{sysmap_path}: {kcfg_sym}=y but text symbol (t/T) {ic_sym} is missing from System.map ({citation})"
                )
            required_initcalls.add(ic_sym)
    release_checked = False
    if required_initcalls and sysmap_parsed is not None:
        _verify_sysmap_against_vmlinux(
            vmlinux_payload,
            sysmap_parsed[0],
            sysmap_parsed[1],
            required_initcalls,
            bz,
            sysmap_path,
            cfg.get("CONFIG_KALLSYMS_ALL") == "y",
        )
        release_checked = True
    # NVMe/MEI being modular must not skip the release family. No System.map
    # still accepts: that kernel is not required to carry the map.
    if not release_checked and sysmap_path.is_file() and sysmap_text is not None:
        if sysmap_parsed is None:
            sysmap_parsed = _parse_sysmap(sysmap_text, sysmap_path)
        banner = _linux_banner_from_map(vmlinux_payload, sysmap_parsed[1], bz, sysmap_path)
        if banner is not None:
            _require_release_family(banner, str(sysmap_path))
    # drivers/ata/Kconfig:14 (ATA -> libata.o), :109 (SATA_AHCI -> ahci.o + libahci.o),
    # :154 (SATA_AHCI_PLATFORM -> ahci_platform.o + libahci.o). drivers/ata/Makefile:6 builds libahci.o
    # with ahci.o. Existing profiles do not list ahci, so =m is refused below for that reason.
    # A profile that blacklists ahci must also blacklist libahci.
    for k, mod in _SATA_KCONFIG:
        v = cfg.get(k)
        if v == "y":
            raise ProfileError(f"{cfg_path}: {k}=y is forbidden (exposes internal SATA disks; initcall_blacklist only blocks NVMe)")
        if v == "m" and mod not in bmods:
            raise ProfileError(f"{cfg_path}: {k}=m without {mod!r} in blacklist.modules exposes internal SATA disks")
        if v == "m" and k != "CONFIG_ATA" and mod in bmods and _SATA_LIBAHCI not in bmods:
            raise ProfileError(
                f"{cfg_path}: {k}=m lists {mod!r} in blacklist.modules but not {_SATA_LIBAHCI!r} "
                "(drivers/ata/Makefile:6 builds libahci.o together with ahci)"
            )
    # 3. Options forbidden as both =y and =m
    for k, why in KCONFIG_NEVER_YM:
        if cfg.get(k) in ("y", "m"):
            raise ProfileError(f"{cfg_path}: {k}={cfg[k]} is forbidden ({why})")
    # Firmware-persistent writers that a cmdline token can turn off. The token has to be
    # on the quiet entry and the serial entry that make_entries actually writes.
    # A declared tail is not enough: quiet_drop can remove a profile-tail token.
    # erst_disable is amd-hx370 only: _safety.toml is the pinned KANO cmdline.
    if cfg.get("CONFIG_ACPI_APEI") in ("y", "m"):
        _require_on_boot_entries(
            prof, "erst_disable", cfg_path,
            f"CONFIG_ACPI_APEI={cfg['CONFIG_ACPI_APEI']}",
            "drivers/acpi/apei/erst.c:1266 pstore_register; :1199 returns when erst_disable is set; "
            ":998 is the __setup; erst-dbg.c:210 skips the debug device. The pinned KANO cmdline "
            "is unchanged; amd-hx370 carries the token.",
        )
    if cfg.get("CONFIG_EFI_VARS_PSTORE") in ("y", "m"):
        _require_on_boot_entries(
            prof, "efi_pstore.pstore_disable=1", cfg_path,
            f"CONFIG_EFI_VARS_PSTORE={cfg['CONFIG_EFI_VARS_PSTORE']}",
            "requires efi_pstore.pstore_disable=1 (drivers/firmware/efi/efi-pstore.c:51 "
            "module_param_cb, :261 returns before register; the object is efi-pstore.o)",
        )
    # 4. Drivers that must never be built-in (=y), and when modular (=m) must have their module(s) in blacklist.modules
    for k, why in KCONFIG_NEVER_BUILTIN:
        v = cfg.get(k)
        if v == "y":
            raise ProfileError(f"{cfg_path}: {k}=y is forbidden ({why})")
        if v == "m":
            req_mods = ("spi_amd", "spi_amd_pci") if k == "CONFIG_SPI_AMD" else (KCONFIG_BUILTIN_MODULE[k],)
            for mod in req_mods:
                if mod not in bmods:
                    raise ProfileError(
                        f"{cfg_path}: {k}=m without {mod!r} in blacklist.modules is forbidden ({why})"
                    )
    # 5. AMD CCP / PSP bool options (drivers/crypto/Kconfig:515 CONFIG_CRYPTO_DEV_CCP bool menu gate;
    #    drivers/crypto/ccp/Kconfig:38 CONFIG_CRYPTO_DEV_SP_PSP bool feature linked into ccp.o via
    #    drivers/crypto/ccp/Makefile:11). The actual tristate linkage unit is CONFIG_CRYPTO_DEV_CCP_DD (ccp.o).
    #    Enabling either bool is only safe when CONFIG_CRYPTO_DEV_CCP_DD=m and 'ccp' is in blacklist.modules.
    for ccp_bool in ("CONFIG_CRYPTO_DEV_CCP", "CONFIG_CRYPTO_DEV_SP_PSP"):
        if cfg.get(ccp_bool) == "y":
            if cfg.get("CONFIG_CRYPTO_DEV_CCP_DD") != "m" or "ccp" not in bmods:
                raise ProfileError(
                    f"{cfg_path}: {ccp_bool}=y is forbidden unless the CCP linkage unit CONFIG_CRYPTO_DEV_CCP_DD=m "
                    "and 'ccp' is in blacklist.modules (drivers/crypto/ccp/Makefile:2,11)"
                )
    # CONFIG_TCG_TIS_SPI_CR50 is a bool compiled into tpm_tis_spi.o (drivers/char/tpm/Kconfig:85-86,
    # Makefile:30). =y is allowed only when that module is built and blacklisted.
    if cfg.get("CONFIG_TCG_TIS_SPI_CR50") == "y":
        if cfg.get("CONFIG_TCG_TIS_SPI") != "m" or "tpm_tis_spi" not in bmods:
            raise ProfileError(
                f"{cfg_path}: CONFIG_TCG_TIS_SPI_CR50=y is forbidden unless CONFIG_TCG_TIS_SPI=m "
                "and 'tpm_tis_spi' is in blacklist.modules (drivers/char/tpm/Kconfig:85, Makefile:30)"
            )
    # 6. Profile-specific rules for amd-hx370
    if prof["model"]["id"] == "amd-hx370":
        if cfg.get("CONFIG_DRM_AMDGPU") == "y":
            raise ProfileError(
                f"{cfg_path}: CONFIG_DRM_AMDGPU=y is forbidden on amd-hx370 (the default safe boot entry blacklists "
                "amdgpu via module_blacklist=; CONFIG_DRM_AMDGPU must be =m; drivers/gpu/drm/amd/amdgpu/Kconfig:3)"
            )
        if cfg.get("CONFIG_DRM_AMDGPU") != "m":
            raise ProfileError(f"{cfg_path}: profiles/amd-hx370 requires CONFIG_DRM_AMDGPU=m")
        if cfg.get("CONFIG_DRM_AMD_ISP") in ("y", "m"):
            raise ProfileError(
                f"{cfg_path}: CONFIG_DRM_AMD_ISP={cfg['CONFIG_DRM_AMD_ISP']} is forbidden on amd-hx370 "
                "(amdgpu/isp_4_1_1.bin is intentionally omitted from profiles/amd-hx370/profile.toml; "
                "drivers/gpu/drm/amd/amdgpu/Kconfig:77)"
            )
    _check_shipped_modules(kdir, cfg, bmods, cfg_path)
    checked = CheckedConfig(cfg)
    checked.bzimage_sha256 = bz_sha
    return checked


# arch/x86/include/asm/setup.h:7. setup.c strlcat includes the terminating NUL,
# so the visible characters must be strictly shorter than this.
X86_COMMAND_LINE_SIZE = 2048


def builtin_cmdline_text(cfg: dict) -> str:
    """Unquoted CONFIG_CMDLINE when CONFIG_CMDLINE_BOOL=y, otherwise empty.

    arch/x86/kernel/setup.c:916-924 prepends the builtin line to the bootloader
    line only when CMDLINE_BOOL is set and OVERRIDE is not. An empty builtin
    does not add the separating space.
    """
    if not isinstance(cfg, dict) or cfg.get("CONFIG_CMDLINE_BOOL") != "y":
        return ""
    raw = cfg.get("CONFIG_CMDLINE", '""')
    if isinstance(raw, str) and len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return ""


def x86_cmdline_fits(bootloader_len: int, builtin: str) -> bool:
    """True when the bootloader line, and a non-empty builtin plus one space, fit.

    Visible characters must be < COMMAND_LINE_SIZE. Truncation drops the tail.
    """
    if bootloader_len < 0:
        return False
    if not builtin:
        return bootloader_len < X86_COMMAND_LINE_SIZE
    return bootloader_len + len(builtin) + 1 < X86_COMMAND_LINE_SIZE


def require_same_bzimage(raw: bytes, expected_sha256: str, what: str) -> None:
    """Refuse when raw is not the bzImage check_kconfig already verified."""
    got = hashlib.sha256(raw).hexdigest()
    if got != expected_sha256:
        raise ProfileError(f"{what} does not match the bzImage check-kconfig verified")


def _main(argv):
    if len(argv) < 3 or argv[1] not in ("check", "check-kconfig", "get", "firmware", "kmods", "json"):
        sys.exit(__doc__)
    try:
        p = load(argv[2])
        if argv[1] == "check-kconfig":
            if len(argv) != 4:
                sys.exit("usage: devprofile.py check-kconfig <profile dir> <kernel dir>")
            check_kconfig(p, argv[3])
    except (OSError, tomllib.TOMLDecodeError, ProfileError) as e:
        sys.exit(f"profile {argv[2]}: {e}")
    if argv[1] == "check":
        print(f"profile {p['model']['id']} ok")
    elif argv[1] == "check-kconfig":
        print(f"kconfig {Path(argv[3]).resolve()} ok for {p['model']['id']}")
    elif argv[1] == "get":
        v = p
        for k in argv[3].split("."):
            v = v[k]
        if isinstance(v, bool):
            v = "true" if v else "false"
        if not isinstance(v, str):
            sys.exit(f"{argv[3]} is not a string or a boolean")
        print(v)
    elif argv[1] == "firmware":
        for fw in p["firmware"]:
            print(fw["path"], fw["from"], fw["src"], fw["sha256"])
    elif argv[1] == "kmods":
        for k in p["kmods"]:
            print(k)
    else:
        print(json.dumps({k: v for k, v in p.items()}, indent=1, sort_keys=True))


if __name__ == "__main__":
    _main(sys.argv)
