#!/usr/bin/env bash
# Read-only hardware probe for a new Googlebook device profile (docs/NEW-DEVICE.md). Run it on a live Linux (e.g. a USB
# stick) on the target machine; root is optional. It only reads files under /sys and /proc (it starts no other program,
# loads no module, mounts nothing, never reads disks, serial numbers or MAC addresses) and writes only into --out:
#   report.txt          what was found, and what lapis (Googlebook) has a driver for
#   coverage.txt        every PCI, USB and ACPI device against the lapis kernel modules (install/probe/lapis-modules.tsv)
#                       and the drivers built into the lapis kernel
#   profile.draft.toml  a starting point for profiles/<model>/profile.toml (status probe-only; check every value)
#   probe.json          the raw facts
# usage: install/gbos-probe.sh [--out DIR] [--modules FILE] [--root DIR]
#   --out DIR       new or empty directory for the results (default ./gbos-probe)
#   --modules FILE  lapis module list (default install/probe/lapis-modules.tsv next to this script)
#   --root DIR      read <DIR>/sys and <DIR>/proc instead of /sys and /proc (tests)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT=./gbos-probe; MODS="$HERE/probe/lapis-modules.tsv"; ROOT=/
die() { echo "gbos-probe: $*" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case "$1" in
        --out) [ $# -ge 2 ] || die "--out needs a directory"; OUT=$2; shift 2 ;;
        --modules) [ $# -ge 2 ] || die "--modules needs a file"; MODS=$2; shift 2 ;;
        --root) [ $# -ge 2 ] || die "--root needs a directory"; ROOT=$2; shift 2 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
done
command -v python3 > /dev/null || die "python3 is needed"
[ -f "$MODS" ] || die "module list $MODS not found"
[ -d "$ROOT/sys" ] && [ -d "$ROOT/proc" ] || die "$ROOT has no sys/ and proc/"
if [ -e "$OUT" ] || [ -L "$OUT" ]; then
    [ -d "$OUT" ] && [ ! -L "$OUT" ] && [ -z "$(ls -A "$OUT")" ] || die "--out $OUT must be a new or empty directory"
fi
mkdir -p "$OUT"
exec python3 - "$ROOT" "$OUT" "$MODS" << 'PY'
import fnmatch, json, os, re, sys
from pathlib import Path

ROOT, OUT, MODS = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
# the module_blacklist every profile must carry (profiles/_safety.toml blacklist.required; tests/probe checks they agree)
SAFETY_REQUIRED = ["cros_ec_debugfs", "tpm_tis_i2c_cr50", "cros_ec_lpcs", "cros_ec_usb", "spi_intel_pci",
                   "spi_intel", "i2c_dev", "thunderbolt", "tpm", "tpm_tis", "tpm_tis_core", "tpm_tis_spi",
                   "cros_ec", "cros_ec_dev", "cros_ec_chardev", "gsmi", "chromeos_acpi", "nvram", "mtd",
                   "spi_nor", "spidev", "intel_quicki2c", "intel_quickspi", "at24", "cros_hps_i2c",
                   "i2c_cros_ec_tunnel", "cros_peripheral_charger", "cros_ec_spi", "cros_ec_i2c",
                   "cros_ec_ishtp", "cros_ec_sysfs", "cros_ec_vbc", "cros_ec_typec", "cros_typec_switch",
                   "cros_usbpd_notify", "cros_usbpd_charger", "cros_usbpd_logger", "cros_charge_control",
                   "cros_ec_ucsi", "cros_ec_keyb", "cros_ec_sensorhub", "cros_ec_sensors_core",
                   "cros_ec_sensors", "cros_ec_accel_legacy", "cros_ec_baro", "cros_ec_lid_angle",
                   "cros_ec_light_prox", "cros_ec_lightbar", "cros_ec_pwrmon", "cros_ec_cec", "leds_cros_ec",
                   "hid_google_hammer", "wilco_ec", "wilco_charger", "wilco_ec_debugfs", "wilco_ec_telem",
                   "rtc_wilco_ec"]


def rd(rel):
    try:
        return (ROOT / rel).read_text(errors="replace").strip()
    except OSError:
        return ""


def ls(rel):
    try:
        return sorted(os.listdir(ROOT / rel))
    except OSError:
        return []


def link(rel):
    try:
        return os.readlink(ROOT / rel)
    except OSError:
        return ""


def hexint(s):
    try:
        return int(s, 16)
    except ValueError:
        return None


def write(name, text):
    fd = os.open(OUT / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)


# ---- lapis kernel: modules (vendor_dlkm + system_dlkm) and the drivers built into its kernel
def bus(s):
    return s.split(":", 1)[0].rstrip("*")


patterns = {}                                    # bus -> [(module, compiled pattern)], compiled once
for line in MODS.read_text().splitlines():
    if line.startswith("#") or "\t" not in line:
        continue
    m, p = line.split("\t", 1)
    if p != "-":
        patterns.setdefault(bus(p), []).append((m, re.compile(fnmatch.translate(p))))


def modules_for(alias):
    return sorted({m for m, rx in patterns.get(bus(alias), []) if rx.match(alias)})


# facts of the lapis GKI configuration (kernel build 16302624): =y drivers that have no module, and what is not there
BUILTIN_PCI_CLASS = {0x010802: "nvme (built in)", 0x0C0330: "xhci_pci (built in)"}
UNSUPPORTED_PCI_CLASS = {0x0106: "SATA/AHCI: NOT SUPPORTED (the lapis kernel has no ATA drivers)"}
ACPI_NOTES = {
    "ACPI0003": "AC adapter: no lapis driver (no ac.ko, CONFIG_ACPI_AC off): no charging indicator",
    "PNP0C0C": "power button: built in (button)", "PNP0C0D": "lid switch: built in (button)",
    "PNP0C0E": "sleep button: built in (button)", "PNP0303": "i8042 keyboard: built in (atkbd)",
    "PNP0C09": "ACPI embedded controller: built in (ec)",
    "GOOG0004": "ChromeOS EC: blocked on purpose (module_blacklist)", "GOOG0002": "ChromeOS keyboard backlight (cros_kbd_led_backlight)",
    "GOOG0005": "Cr50 I2C TPM: blocked on purpose", "MSFT0101": "TPM 2.0: blocked on purpose",
    "PNP0C31": "TPM 1.2: blocked on purpose", "ACPI0008": "ambient light sensor",
}
# Intel GPUs that xe binds only with xe.force_probe=<id> (Gen12 before Meteor Lake: TGL, RKL, ADL-S/P/N, RPL, DG1);
# a heuristic by device id prefix - check the xe source of the lapis kernel before relying on it
XE_FORCE_PROBE_PREFIX = ("9a", "4c", "468", "469", "46a", "46b", "46c", "46d", "462", "a78", "a7a", "a72", "490")

# ---- facts
dmi = {k: rd(f"sys/class/dmi/id/{k}") for k in ("sys_vendor", "product_name", "product_version", "product_family",
                                                "board_vendor", "board_name", "bios_vendor", "bios_version", "bios_date")}
cpu = {}
for line in rd("proc/cpuinfo").splitlines():
    if not line.strip():
        break
    k, _, v = line.partition(":")
    cpu[k.strip()] = v.strip()
cpu = {k: cpu.get(k, "") for k in ("vendor_id", "cpu family", "model", "stepping", "model name", "microcode")}
fw = {"uefi": (ROOT / "sys/firmware/efi").is_dir(), "coreboot": "coreboot" in (dmi["bios_vendor"] + dmi["bios_version"]).lower()}
sb = ROOT / "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
try:
    fw["secure_boot"] = {1: "on", 0: "off"}.get(sb.read_bytes()[4:5][0], "unknown") if sb.exists() else "unknown"
except (OSError, IndexError):
    fw["secure_boot"] = "unknown"

def list_dir(rel):
    """None when the directory is missing or cannot be listed. [] is a real empty directory."""
    try:
        return sorted(os.listdir(ROOT / rel))
    except OSError:
        return None


pci_names = list_dir("sys/bus/pci/devices")
if pci_names is None:
    pci = None
else:
    pci = []
    for d in pci_names:
        b = f"sys/bus/pci/devices/{d}"
        ven, dev, cls = hexint(rd(f"{b}/vendor")), hexint(rd(f"{b}/device")), hexint(rd(f"{b}/class"))
        if ven is None or dev is None or cls is None:
            pci.append({"slot": d, "unread": True})
            continue
        sv, sd = hexint(rd(f"{b}/subsystem_vendor")) or 0, hexint(rd(f"{b}/subsystem_device")) or 0
        alias = rd(f"{b}/modalias") or f"pci:v{ven:08X}d{dev:08X}sv{sv:08X}sd{sd:08X}bc{cls >> 16:02X}sc{(cls >> 8) & 0xFF:02X}i{cls & 0xFF:02X}"
        pci.append({"slot": d, "id": f"{ven:04x}:{dev:04x}", "class": f"{cls:06x}", "driver": os.path.basename(link(f"{b}/driver")),
                    "alias": alias, "lapis": modules_for(alias)})
pci_rows = pci if isinstance(pci, list) else []

usb = []
for d in ls("sys/bus/usb/devices"):
    b = f"sys/bus/usb/devices/{d}"
    if ":" in d or not rd(f"{b}/idVendor"):
        continue
    ifs = []
    for i in ls(b):
        if i.startswith(d + ":"):
            ia = rd(f"{b}/{i}/modalias")
            ifs.append({"if": i, "class": rd(f"{b}/{i}/bInterfaceClass"), "driver": os.path.basename(link(f"{b}/{i}/driver")),
                        "alias": ia, "lapis": modules_for(ia) if ia else []})
    usb.append({"port": d, "id": f"{rd(b + '/idVendor')}:{rd(b + '/idProduct')}", "product": rd(f"{b}/product"),
                "manufacturer": rd(f"{b}/manufacturer"), "interfaces": ifs})

acpi = []
for d in ls("sys/bus/acpi/devices"):
    b = f"sys/bus/acpi/devices/{d}"
    hid = rd(f"{b}/hid")
    if not hid:
        continue
    alias = rd(f"{b}/modalias") or f"acpi:{hid}:"
    st = rd(f"{b}/status")
    acpi.append({"name": d, "hid": hid, "path": rd(f"{b}/path"), "status": st, "alias": alias,
                 "present": st == "" or (st.isdigit() and int(st) & 1 == 1), "lapis": modules_for(alias)})

net = []
for n in ls("sys/class/net"):
    if n == "lo":
        continue
    dl = link(f"sys/class/net/{n}/device")
    net.append({"name": n, "wireless": (ROOT / f"sys/class/net/{n}/wireless").exists() or (ROOT / f"sys/class/net/{n}/phy80211").exists(),
                "device": os.path.basename(dl), "bus": "usb" if "/usb" in dl or re.match(r"\d+-[\d.]+:", os.path.basename(dl)) else "pci"})
bt = [{"name": h, "device": os.path.basename(link(f"sys/class/bluetooth/{h}/device"))} for h in ls("sys/class/bluetooth") if ":" not in h]

cards = rd("proc/asound/cards")
codecs = []
for c in ls("proc/asound"):
    if re.fullmatch(r"card\d+", c):
        for f in ls(f"proc/asound/{c}"):
            if f.startswith("codec#"):
                first = rd(f"proc/asound/{c}/{f}").splitlines()[:1]
                codecs.append(f"{c} {f}: {first[0] if first else ''}")
card_ids = [rd(f"sys/class/sound/{c}/id") for c in ls("sys/class/sound") if re.fullmatch(r"card\d+", c)]

storage = []
for blk in ls("sys/block"):
    if blk.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "fd")):
        continue
    path = link(f"sys/block/{blk}")
    kind = ("nvme" if blk.startswith("nvme") else "emmc/sd" if blk.startswith("mmcblk") else
            "usb" if "/usb" in path else "sata" if "/ata" in path else "other")
    size = rd(f"sys/block/{blk}/size")
    storage.append({"name": blk, "kind": kind, "removable": rd(f"sys/block/{blk}/removable") == "1",
                    "size_gb": round(int(size) * 512 / 1e9, 1) if size.isdigit() else None,
                    "model": rd(f"sys/block/{blk}/device/model") or rd(f"sys/block/{blk}/device/name")})

inputs, cur = [], {}
for line in rd("proc/bus/input/devices").splitlines() + [""]:
    if not line.strip():
        if cur:
            inputs.append(cur)
        cur = {}
        continue
    t, _, v = line.partition(": ")
    if t == "I":
        parts = dict(x.split("=", 1) for x in v.split() if "=" in x)
        cur["bus"] = parts.get("Bus", "")
        cur["vendor"] = parts.get("Vendor", "")
        cur["product"] = parts.get("Product", "")
    elif t == "N":
        cur["name"] = v.partition("=")[2].strip('"')
    elif t == "H":
        cur["handlers"] = v.partition("=")[2].split()
BUSNAME = {"0018": "i2c", "0003": "usb", "0011": "i8042", "0019": "host", "001e": "spi", "0005": "bluetooth"}


def input_kind(i):
    n = i.get("name", "").lower()
    for k, words in (("touchpad", ("touchpad", "trackpad")), ("touchscreen", ("touchscreen",)), ("stylus", ("stylus", "pen")),
                     ("keyboard", ("keyboard",)), ("power button", ("power button",)), ("lid switch", ("lid switch",))):
        if any(w in n for w in words):
            return k
    if i.get("bus") == "0018" and "mouse" in " ".join(i.get("handlers", [])):
        return "touchpad (i2c, by handler)"
    return "other"


for i in inputs:
    i["kind"] = input_kind(i)
    i["busname"] = BUSNAME.get(i.get("bus", "").lower(), i.get("bus", ""))
power = [{"name": p, "type": rd(f"sys/class/power_supply/{p}/type")} for p in ls("sys/class/power_supply")]
backlight = ls("sys/class/backlight")
leds = []
for l in ls("sys/class/leds"):
    target = link(f"sys/class/leds/{l}")
    leds.append({"name": l, "path": "/sys/" + target.split("/../", 1)[-1].lstrip("./").split("/sys/", 1)[-1] if target else ""})
tpm = [{"name": t, "version": rd(f"sys/class/tpm/{t}/tpm_version_major")} for t in ls("sys/class/tpm")]
mem_sleep = rd("sys/power/mem_sleep")

facts = {"dmi": dmi, "cpu": cpu, "firmware": fw, "pci": pci, "usb": usb, "acpi": acpi, "net": net, "bluetooth": bt,
         "audio": {"cards": cards, "card_ids": card_ids, "codecs": codecs}, "storage": storage, "input": inputs,
         "power_supply": power, "mem_sleep": mem_sleep, "backlight": backlight, "leds": leds, "tpm": tpm}

# ---- coverage
cov, verdict = [], {}
if pci is None:
    cov.append("pci  (bus not read)")
for p in pci_rows:
    if p.get("unread"):
        cov.append(f"pci  {p.get('slot', '')}  unread")
        continue
    cls = int(p["class"], 16)
    if cls in BUILTIN_PCI_CLASS:
        v = BUILTIN_PCI_CLASS[cls]
    elif cls >> 8 in UNSUPPORTED_PCI_CLASS:
        v = UNSUPPORTED_PCI_CLASS[cls >> 8]
    elif p["lapis"]:
        v = "lapis module: " + ", ".join(p["lapis"])
    else:
        v = "no lapis driver"
    p["verdict"] = v
    cov.append(f"pci  {p['slot']}  {p['id']}  class {p['class']}  host driver {p['driver'] or '-'}  ->  {v}")
for u in usb:
    for i in u["interfaces"]:
        v = ("lapis module: " + ", ".join(i["lapis"])) if i["lapis"] else "no lapis driver"
        cov.append(f"usb  {i['if']}  {u['id']}  class {i['class']}  {u['product']}  host driver {i['driver'] or '-'}  ->  {v}")
for a in acpi:
    if not a["present"]:
        continue
    note = ACPI_NOTES.get(a["hid"], "")
    v = note or (("lapis module: " + ", ".join(a["lapis"])) if a["lapis"] else "")
    if v:
        cov.append(f"acpi {a['name']}  {a['hid']}  ->  {v}")
gpus = [p for p in pci_rows if not p.get("unread") and str(p.get("class", "")).startswith("03")]
gpu_lines = []
force_probe = []
for g in gpus:
    ven, dev = g["id"].split(":")
    if ven == "8086" and "xe" in g["lapis"]:
        need = dev.startswith(XE_FORCE_PROBE_PREFIX)
        gpu_lines.append(f"{g['id']}: xe" + (f" (needs xe.force_probe={dev})" if need else ""))
        if need:
            force_probe.append(f"xe.force_probe={dev}")
    elif g["lapis"]:
        gpu_lines.append(f"{g['id']}: {', '.join(g['lapis'])}")
    else:
        gpu_lines.append(f"{g['id']}: NO lapis GPU driver -> only the generic UEFI framebuffer fallback (research: AMD/generic M1)")
# A GPU lapis drives (xe) keeps the image's own stack; otherwise the GOP framebuffer + SwiftShader.
# Any unread slot is an incomplete list: not "no display controller", and not a stack choice.
if pci is None:
    verdict["gpu"] = ["PCI bus not read"]
    stack = "unknown"
    stack_note = "PCI bus not read; choose after a full probe"
elif any(p.get("unread") for p in pci_rows):
    verdict["gpu"] = ["PCI list incomplete"]
    stack = "unknown"
    stack_note = "PCI device unread; choose after a full probe"
else:
    verdict["gpu"] = gpu_lines or ["no display controller found"]
    stack = "native" if any(g["lapis"] for g in gpus) else "gop-swiftshader"
    stack_note = "native (a lapis GPU driver binds) | gop-swiftshader (GOP framebuffer, SwiftShader)"
boot = [s for s in storage if s["kind"] in ("nvme", "emmc/sd", "sata") and not s["removable"]]
verdict["internal disk"] = [f"{s['name']} {s['kind']} {s['size_gb']} GB {s['model']}" + ("  <- NOT usable: no SATA in lapis" if s["kind"] == "sata" else "")
                            for s in boot] or ["none found"]
wl = [n for n in net if n["wireless"]]
verdict["wifi"] = []
for n in wl:
    dev = next((p for p in pci_rows if p.get("slot") == n["device"]), None)
    if dev and not dev.get("unread"):
        verdict["wifi"].append(f"{n['name']} {dev['id']} host driver {dev['driver'] or '-'}: {dev['verdict']}")
    elif dev:
        verdict["wifi"].append(f"{n['name']} unread")
    else:
        verdict["wifi"].append(f"{n['name']} on {n['bus']} ({n['device']})")
verdict["wifi"] = verdict["wifi"] or ["no wireless interface found"]
audio_pci = [p for p in pci_rows if not p.get("unread") and str(p.get("class", "")).startswith(("0403", "0401", "0480"))]
verdict["audio"] = [f"{p['id']} host driver {p['driver'] or '-'}: {p['verdict']}" for p in audio_pci] + [f"codec {c}" for c in codecs] or ["no audio controller found"]
verdict["input"] = [f"{i['kind']}: {i.get('name', '')} ({i['busname']})" for i in inputs if i["kind"] != "other"]
acpi_hids = {a["hid"] for a in acpi if a["present"]}
warn = []
if not fw["uefi"]:
    warn.append("not booted in UEFI mode: Googlebook needs UEFI (on a Chromebook: MrChromebox UEFI Full ROM, docs/FIRMWARE.md)")
if fw["secure_boot"] == "on":
    warn.append("Secure Boot is on: the Googlebook stick is not signed for it; turning it off can trigger a BitLocker recovery prompt on Windows machines (REQUIRED before changing Secure Boot: back up the 48-digit BitLocker recovery key for every encrypted volume to another device; Suspend-BitLocker is not an alternative)")
if any(s["kind"] == "sata" for s in boot) and not any(s["kind"] in ("nvme", "emmc/sd") for s in boot):
    warn.append("the only internal disk is SATA, which lapis cannot see: Googlebook can only run from USB here")
if any(s["kind"] == "emmc/sd" for s in boot):
    warn.append("eMMC internal disk: sdhci-pci makes it visible to Googlebook on USB; a profile must hide it like nvme_init hides NVMe")
if "GOOG0004" in acpi_hids:
    warn.append("ChromeOS EC present: keep every cros_ec transport blocked (profiles/_safety.toml); see profiles/kano for a full list")
if cpu["vendor_id"] == "GenuineIntel" and cpu["model"] == "154" and cpu["stepping"].isdigit() and int(cpu["stepping"]) < 3:
    warn.append("Alder Lake-P stepping below 3: probably an engineering sample (see docs/FIRMWARE.md)")
if "ACPI0003" in acpi_hids:
    warn.append("AC adapter (ACPI0003) has no lapis driver: no charging indicator")
if pci is None:
    warn.append("PCI device list was not read")
elif any(p.get("unread") for p in pci_rows):
    warn.append("PCI device unread")
elif not gpus or all("NO lapis GPU driver" in l for l in gpu_lines):
    warn.append("no lapis GPU driver for this machine's GPU: graphics needs the generic framebuffer fallback (profiles/generic-x86; on AMD Strix Point / HX 370 use profiles/amd-hx370 instead of profile.draft.toml for the AMD fTPM/PSP/SPI/SDHCI blacklist and dual safe/native boot entries; not hardware-tested); the CPU must be Intel 12th gen / AMD Zen 5 or newer (docs/COMPATIBILITY.md)")

# ---- draft profile
mid = re.sub(r"[^a-z0-9]+", "-", dmi["product_name"].lower()).strip("-") or "unknown"
ID = mid.upper()


def q(s):
    return json.dumps(s, ensure_ascii=False)


def qlist(xs, indent=11):
    return "[" + (",\n" + " " * indent).join(q(x) for x in xs) + "]"


kbd = next((l["path"] for l in leds if "kbd_backlight" in l["name"] and l["path"]), "")
kbd_leds = kbd.rsplit("/", 1)[0] if kbd.endswith("kbd_backlight") else ""
amd_note = (
    "# NOTE (AMD GPU 1002:*): on AMD Strix Point / Ryzen AI 9 HX 370, use profiles/amd-hx370/profile.toml (which carries\n"
    "# the AMD fTPM/PSP/SPI/SDHCI blacklist and dual safe/native boot entries) instead of this bare gop-swiftshader draft.\n"
    if any(g["id"].startswith("1002:") for g in gpus) else ""
)
draft = f"""# DRAFT made by install/gbos-probe.sh on {q(dmi['sys_vendor'])} {q(dmi['product_name'])}. Every value needs checking; status
# stays probe-only until the image boots on this machine. Compare with profiles/kano/profile.toml and docs/NEW-DEVICE.md.
# Put it in profiles/{mid}/profile.toml (the directory name must equal model.id).
{amd_note}schema = 1
kmods = {qlist(["gbos_efifb"] if stack == "gop-swiftshader" else [])}

[model]
id = {q(mid)}
name = {q((dmi['sys_vendor'] + ' ' + dmi['product_name']).strip())}
status = "probe-only"
image_name = {q('googlebook-' + mid + '.img')}
dmi = {{ sys_vendor = {q(dmi['sys_vendor'])}, product_name = {q(dmi['product_name'])} }}

[entries]
quiet = {q('Googlebook ' + ID + ' quiet')}
debug = {q('Googlebook ' + ID + ' serial-debug')}

[cmdline]
stock_drop = []
# GPU: {'; '.join(verdict['gpu'])}
add = {qlist(force_probe)}
tail = []
debug_drop = []
pci_stub_ids = []
first_stage_drop = []

[blacklist]
# the project-wide minimum (profiles/_safety.toml); a ChromeOS device needs every cros_ec module, see profiles/kano
modules = {qlist(SAFETY_REQUIRED)}
debug_add = []
allow = []

[firmware_sources]
linux_firmware_tag = ""
sof_bin_url = ""
sof_bin_sha256 = ""
# [[firmware]] entries: the files the drivers ask for (dmesg "Direct firmware load"), pinned by SHA-256

[audio]
# cards: {', '.join(card_ids) or 'none'}; codecs: {'; '.join(codecs) or 'none'}
card = ""
config_dir = ""
sources = ""

[wifi]
modprobe = []

[display]
# {stack_note}
stack = {q(stack)}
hwc_backend = ""
build_prop_set = []

[input]
# keyboard backlight LEDs found: {kbd_leds or 'none'} (a path other than lapis' own needs a vendor.py hook, see profiles/kano)
kbd_backlight_leds = ""
power_button = "stock"

[hooks]
vendor = ""
"""

# ---- report
r = [f"gbos-probe report for {dmi['sys_vendor']} {dmi['product_name']} ({dmi['board_name']})",
     f"firmware: {dmi['bios_vendor']} {dmi['bios_version']} {dmi['bios_date']}; UEFI {'yes' if fw['uefi'] else 'no'}, "
     f"coreboot {'yes' if fw['coreboot'] else 'no'}, Secure Boot {fw['secure_boot']}",
     f"cpu: {cpu['vendor_id']} family {cpu['cpu family']} model {cpu['model']} stepping {cpu['stepping']} ({cpu['model name']})", ""]
for k in ("gpu", "internal disk", "wifi", "audio", "input"):
    r.append(f"{k}:")
    r += [f"  {x}" for x in verdict[k]] or ["  -"]
r += ["", "bluetooth: " + (", ".join(f"{b['name']} ({b['device']})" for b in bt) or "none"),
      "power supplies: " + (", ".join(f"{p['name']}={p['type']}" for p in power) or "none") + f"; mem_sleep: {mem_sleep or '-'}",
      "backlight: " + (", ".join(backlight) or "none") + "; LEDs: " + (", ".join(l["name"] for l in leds) or "none"),
      "tpm: " + (", ".join(f"{t['name']} v{t['version']}" for t in tpm) or "none") + " (blocked in every Googlebook entry)", ""]
r += ["warnings:"] + [f"  ! {w}" for w in warn] if warn else ["warnings: none"]
r += ["", "next: coverage.txt (every device), profile.draft.toml (starting point), docs/NEW-DEVICE.md"]
write("report.txt", "\n".join(r) + "\n")
write("coverage.txt", "# device -> lapis driver (module from install/probe/lapis-modules.tsv, or built into the lapis kernel)\n" + "\n".join(cov) + "\n")
write("profile.draft.toml", draft)
write("probe.json", json.dumps(facts, indent=1, sort_keys=True) + "\n")
print("\n".join(r))
print(f"\nwritten: {OUT}/report.txt coverage.txt profile.draft.toml probe.json")
PY
