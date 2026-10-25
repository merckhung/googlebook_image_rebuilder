#!/usr/bin/env python3
"""Check the publishable source tree for private paths, keys and local artifacts."""
from pathlib import Path
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", "work", "out", "__pycache__"}


def paths():
    if (ROOT / ".git").exists():
        result = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True)
        return [ROOT / name for name in result.splitlines()]
    return [p for p in ROOT.rglob("*") if p.is_file() and not any(
        part in EXCLUDED or part.startswith("bazel-") for part in p.relative_to(ROOT).parts)]


def main():
    errors = []
    private_path = re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+/")
    companion = re.compile(r"\b(?:[a-z0-9]+_){1,3}emulator\b", re.I)
    non_release = "user" + "debug"
    email = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
    for path in paths():
        name = path.relative_to(ROOT).as_posix()
        if path.is_symlink():
            errors.append(name + ": symlinks require manual review")
            continue
        data = path.read_bytes()
        if name == "docs/media/demo.mp4":
            if len(data) > 10_000_000 or data[4:8] != b"ftyp":
                errors.append(name + ": invalid MP4 or exceeds the 10 MB media limit")
            continue
        if b"\0" in data or len(data) > 5_000_000:
            errors.append(name + ": unexpected binary or oversized source file")
            continue
        text = data.decode("utf-8")
        if private_path.search(text):
            errors.append(name + ": absolute host account path")
        if companion.search(text):
            errors.append(name + ": companion-project reference")
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", text):
            errors.append(name + ": private key")
        for line in text.splitlines():
            if non_release in line.lower() and not re.match(r"^\w+=https://ci\.android\.com/", line):
                errors.append(name + ": non-release recovery reference outside an upstream tool URL")
                break
        if name == "README.md" and re.search(r"emulat(?:or|ion)", text, re.I):
            errors.append(name + ": capture-method reference")
        if not name.startswith("licenses/"):
            for address in email.findall(text):
                if not address.endswith(("@example.invalid", "@example.com", "@example.test", "@tty1.service")):
                    errors.append(name + ": personal or organization email address")
    registry = json.loads((ROOT / "base/recoveries.json").read_text())
    for build, pin in registry["recoveries"].items():
        if not build.isdigit() or not pin["url"].startswith("https://dl.google.com/device/recovery/lapis-user/"):
            errors.append("base/recoveries.json: non-public recovery source")
        if not all(re.fullmatch(r"[0-9a-f]{64}", pin[k]) for k in ("zip_sha256", "image_sha256")):
            errors.append("base/recoveries.json: invalid checksum")
    if errors:
        print("\n".join(sorted(set(errors))))
        return 1
    print("PASS: publishable source contains no private host paths, private keys, local binaries or unapproved recovery references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
