#!/usr/bin/env python3
"""Rebuild target images from the checked-in public recovery registry."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
ORDER = ("screebo", "marasov", "lg-15u50u")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(args, *, env=None, log=None):
    subprocess.run(list(map(str, args)), cwd=ROOT, env=env, check=True,
                   stdout=log, stderr=subprocess.STDOUT if log else None)


def public_pins(build):
    registry = json.loads((ROOT / "base/recoveries.json").read_text())
    if build not in registry["recoveries"]:
        raise SystemExit("Recovery is not in the public release registry: " + build)
    return registry["recoveries"][build]


def ensure_base(args, work, env):
    pins = public_pins(args.build)
    base = work / "base"
    if (base / "BUILD-DONE").exists():
        info = (base / "build-info.txt").read_text()
        if not any(line.split() == ["Lapis", "build", args.build] for line in info.splitlines()):
            raise SystemExit("The shared base belongs to another recovery; choose a new --work directory.")
        for line in (base / "SHA256SUMS").read_text().splitlines():
            expected, filename = line.split(maxsplit=1)
            if digest(base / Path(filename).name) != expected:
                raise SystemExit("Shared base checksum mismatch: " + Path(filename).name)
        if args.key and digest(args.key) != digest(base / "keys/vbmeta.pem"):
            raise SystemExit("The shared base uses another AVB key; choose a new --work directory.")
        return base
    cache = work / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    command = ["bash", ROOT / "base/build-base.sh", "--out", base, "--cache", cache]
    if args.recovery_bin:
        if digest(args.recovery_bin) != pins["image_sha256"]:
            raise SystemExit("Raw input does not match the selected public recovery.")
        command += ["--recovery-bin", args.recovery_bin]
    else:
        archive = args.zip or cache / ("lapis-" + args.build + ".zip")
        if not archive.exists():
            partial = Path(str(archive) + ".partial")
            run(["curl", "-fL", "--retry", "4", "-o", partial, pins["url"]])
            if digest(partial) != pins["zip_sha256"]:
                raise SystemExit("Downloaded public recovery checksum mismatch.")
            partial.rename(archive)
        if digest(archive) != pins["zip_sha256"]:
            raise SystemExit("Recovery ZIP checksum mismatch.")
        command += ["--zip", archive]
    if args.key:
        command += ["--key", args.key]
    with (work / "logs/base.log").open("w") as log:
        run(command, env=env, log=log)
    if args.key:
        local_key = base / "keys/vbmeta.pem"
        local_key.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(args.key, local_key)
        local_key.chmod(0o600)
    return base


def ensure_modules(work, env):
    output = work / "inputs/modules"
    sys.path.insert(0, str(ROOT / "builder/lib"))
    import lapis
    pins = lapis.load(env["GBOS_LAPIS_BUILD"])
    names = {"ac": "AC_KO_SHA", "gbos_wakefilter": "WAKEFILTER_KO_SHA", "gbos_isa_emu": "ISA_KO_SHA"}
    if all((output / (name + ".ko")).is_file() and
           digest(output / (name + ".ko")) == pins[key] for name, key in names.items()):
        return output
    if output.exists():
        output.rename(output.with_name("modules.previous-" + str(time.time_ns())))
    output.mkdir(parents=True)
    source = work / "cache/module-build"
    with (work / "logs/modules.log").open("w") as log:
        for script in ("build-ac.sh", "build-gbos_wakefilter.sh", "build-gbos_isa_emu.sh"):
            run(["bash", ROOT / "builder/kmod" / script, source, output], env=env, log=log)
    for name, key in names.items():
        if digest(output / (name + ".ko")) != pins[key]:
            raise SystemExit("Module does not match its public-kernel pin: " + name)
    return output


def test_key(work):
    key = work / "keys/test-adb"
    key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not key.exists():
        run(["adb", "keygen", key])
    public = Path(str(key) + ".pub")
    # The optional image key contains a neutral label, never a host account name.
    public.write_text(public.read_text().split()[0] + " image-rebuilder\n")
    key.chmod(0o600)
    return key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("all", *ORDER), required=True)
    parser.add_argument("--build", default="16658739")
    parser.add_argument("--work", type=Path, default=ROOT / "work")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--zip", type=Path)
    inputs.add_argument("--recovery-bin", type=Path)
    parser.add_argument("--key", type=Path, help="Existing RSA-4096 AVB signing key")
    parser.add_argument("--no-selftest", action="store_true", help="Keep the untested marker; skip the integrated boot suite")
    parser.add_argument("--boot-check", action="store_true", help="Run an additional isolated boot check with a local test key")
    parser.add_argument("--compact", action="store_true", help="Archive images, then remove their generated large intermediates")
    parser.add_argument("--standard-setup", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Build as a normal user; root is needed only for writing a USB disk.")
    public_pins(args.build)
    for name in ("key", "zip", "recovery_bin"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.expanduser().resolve())
    work = args.work.resolve()
    for directory in (work, work / "logs", work / "inputs", ROOT / "out"):
        directory.mkdir(parents=True, exist_ok=True)
    allowed = {"GBOS_QEMU", "GBOS_OVMF_CODE", "GBOS_OVMF_VARS", "GBOS_DEV_KERNEL_DIR"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GBOS_") or k in allowed}
    env["GBOS_LAPIS_BUILD"] = args.build
    base = ensure_base(args, work, env)
    models = ORDER if args.model == "all" else (args.model,)
    modules = ensure_modules(work, env)
    boot_key = test_key(work) if args.boot_check else None
    report_path = ROOT / "docs/build-results.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {"recovery_build": args.build, "models": {}}
    if report["recovery_build"] != args.build:
        report = {"recovery_build": args.build, "models": {}}
    for model in models:
        print("Building " + model, flush=True)
        profile = ROOT / "profiles" / model
        config = tomllib.loads((profile / "profile.toml").read_text())
        output = work / "builds" / model
        image_name = Path(config["model"]["image_name"]).stem + "-" + args.build + ".img"
        result = {"build": "pending", "boot": "not run", "integrated_selftest": not args.no_selftest}
        current = dict(env, GBOS_PROFILE=str(profile), GBOS_BASE=str(base / "kano-real.img"),
                       GBOS_INITRD=str(base / "initrd.img"), GBOS_OUT=str(output),
                       GBOS_OTATOOLS=str(work / "cache/otatools"), GBOS_AVB_KEY=str(base / "keys/vbmeta.pem"),
                       GBOS_INPUTS=str(work / "inputs" / model), GBOS_IMAGE_NAME=image_name,
                       GBOS_SKIP_SETUP="0" if args.standard_setup else "1",
                       GBOS_KMOD_TOOLCHAIN=str(work / "cache/module-build"))
        if args.no_selftest:
            current["GBOS_SKIP_SELFTEST"] = "1"
        for name, variable in (("ac", "GBOS_KMOD_AC"), ("gbos_wakefilter", "GBOS_KMOD_WAKEFILTER"), ("gbos_isa_emu", "GBOS_ISA_EMU_KO")):
            if name in config["kmods"]:
                current[variable] = str(modules / (name + ".ko"))
        if boot_key:
            current["GBOS_ADB_PUBKEY"] = str(boot_key) + ".pub"
        try:
            if output.exists() and any(output.iterdir()):
                raise RuntimeError("Build directory already exists; use a fresh --work directory for another run.")
            with (work / "logs" / (model + ".log")).open("w") as log:
                run(["bash", ROOT / "builder/fetch-inputs.sh", current["GBOS_INPUTS"], profile], env=current, log=log)
                if "gop-swiftshader" in (config["display"]["stack"], config["display"].get("fallback")):
                    run(["bash", ROOT / "builder/fetch-generic-inputs.sh", current["GBOS_INPUTS"]], env=current, log=log)
                run(["bash", ROOT / "builder/build.sh"], env=current, log=log)
            image = output / "image" / image_name
            result.update(build="passed", image_sha256=Path(str(image) + ".sha256").read_text().split()[0])
            if args.boot_check:
                command = [sys.executable, ROOT / "scripts/boot_check.py", "--image", image,
                           "--model", model, "--key", boot_key, "--work", work / "boot" / model,
                           "--build", args.build]
                with (work / "logs" / (model + "-boot.log")).open("w") as log:
                    boot = subprocess.run(list(map(str, command)), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                result["boot"] = "passed" if boot.returncode == 0 else "failed"
            archive = ROOT / "out" / (image_name + ".zst")
            run(["zstd", "-T2", "-3", "-f", image, "-o", archive])
            archive_hash = digest(archive)
            Path(str(archive) + ".sha256").write_text(archive_hash + "  " + archive.name + "\n")
            shutil.copy2(Path(str(image) + ".sha256"), ROOT / "out" / (image_name + ".sha256"))
            marker = Path(str(image) + ".not-self-tested")
            if marker.exists():
                shutil.copy2(marker, ROOT / "out" / (image_name + ".not-self-tested"))
            result.update(artifact="out/" + archive.name, archive_sha256=archive_hash)
            if args.compact:
                image.unlink()
                (output / "vendor/stage-vendor.img").unlink(missing_ok=True)
        except (subprocess.CalledProcessError, RuntimeError) as error:
            result["build"] = "failed"
            result["reason"] = "Build check failed; see the local target log."
            print(model + ": " + result["reason"], flush=True)
        report["models"][model] = result
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(model + ": " + result["build"] + "; boot=" + result["boot"], flush=True)
    return 0 if all(report["models"][m]["build"] == "passed" for m in models) else 1


if __name__ == "__main__":
    sys.exit(main())
