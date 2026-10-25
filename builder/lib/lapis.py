"""Read the checked-in scalar/hash pins for a supported Lapis recovery build.

The shell pin file is read as data; this module never executes its contents.
"""
import os
import re
from pathlib import Path


def load(build=None):
    build = str(build or os.environ.get("GBOS_LAPIS_BUILD", "16658739"))
    if not re.fullmatch(r"[0-9]+", build):
        raise ValueError("invalid Lapis build: " + build)
    path = Path(__file__).resolve().parents[2] / "base/builds" / (build + ".sh")
    text = path.read_text()
    pins = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=([^\s\"'`$]+)$", text, re.M))
    pins["EXPECT"] = dict(re.findall(r"^\s+\[([^\]]+)\]=([0-9a-f]{64})$", text, re.M))
    pins["BUILD"] = build
    for key in ("VENDOR_UUID", "VENDOR_FINGERPRINT", "VENDOR_SECURITY_PATCH", "VBMETA_ROLLBACK_INDEX",
                "EDITED_VENDOR_BUILD_PROP_SHA256", "EDITED_SYSTEM_EXT_MANIFEST_SHA256"):
        if key not in pins:
            raise ValueError(f"Lapis build {build} has no {key} pin")
    return pins


if __name__ == "__main__":
    pins = load()
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--get":
        print(pins.get(sys.argv[2], ""))
    elif len(sys.argv) == 1:
        print(*(pins[k] for k in ("VENDOR_UUID", "VENDOR_FINGERPRINT", "VENDOR_SECURITY_PATCH", "VBMETA_ROLLBACK_INDEX")))
    else:
        raise SystemExit("usage: lapis.py [--get PIN]")
