#!/usr/bin/env python3
"""Verify an isolated UEFI boot of the selected public recovery image."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def qmp(path, command, arguments=None):
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(10)
        stream.connect(str(path))
        with stream.makefile("rwb") as channel:
            channel.readline()
            for name, args in (("qmp_capabilities", None), (command, arguments)):
                channel.write(json.dumps({"execute": name, **({"arguments": args} if args else {})}).encode() + b"\n")
                channel.flush()
                while True:
                    reply = json.loads(channel.readline())
                    if "error" in reply:
                        raise RuntimeError(reply["error"])
                    if "return" in reply:
                        break
            return reply["return"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--build", default="16658739")
    parser.add_argument("--timeout", type=int, default=420)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    image = args.image.resolve()
    overlay = args.work / "disk.qcow2"
    if overlay.exists():
        raise SystemExit("Use a fresh boot work directory.")
    config = tomllib.loads((ROOT / "profiles" / args.model / "profile.toml").read_text())
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", str(image), str(overlay)], check=True)
    code = Path(os.environ.get("GBOS_OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd"))
    template = Path(os.environ.get("GBOS_OVMF_VARS", "/usr/share/OVMF/OVMF_VARS_4M.fd"))
    shutil.copyfile(template, args.work / "vars.fd")
    forward, server = free_port(), free_port()
    address = "127.0.0.1:" + str(forward)
    adb_env = dict(os.environ, ADB_VENDOR_KEYS=str(args.key.resolve()))
    adb_base = ["adb", "-P", str(server)]

    def adb(*words, check=True):
        result = subprocess.run([*adb_base, "-s", address, *words], env=adb_env,
                                capture_output=True, text=True, timeout=15)
        if check and result.returncode:
            raise RuntimeError(result.stderr + result.stdout)
        return result.stdout.strip()

    cpu = "host"
    if args.model in ("marasov", "screebo", "lg-15u50u"):
        cpu += ",-avx512f,-avx512dq,-avx512cd,-avx512bw,-avx512vl"
    with tempfile.TemporaryDirectory(prefix="image-boot-") as temporary:
        sock = Path(temporary) / "qmp.sock"
        dmi = config["model"]["dmi"]
        command = [os.environ.get("GBOS_QEMU", "qemu-system-x86_64"), "-machine", "q35,accel=kvm",
                   "-cpu", cpu, "-smp", "4", "-m", "8192", "-no-reboot", "-vga", "none", "-display", "none",
                   "-device", "bochs-display,id=gfx,romfile=,bus=pcie.0,addr=0x5",
                   "-smbios", "type=1,manufacturer=" + dmi.get("sys_vendor", "Image Rebuilder") + ",product=" + dmi.get("product_name", "UEFI Test"),
                   "-drive", "if=pflash,format=raw,readonly=on,file=" + str(code),
                   "-drive", "if=pflash,format=raw,file=" + str(args.work / "vars.fd"),
                   "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x14.0",
                   "-drive", "file=" + str(overlay) + ",if=none,id=stick,format=qcow2",
                   "-device", "usb-storage,bus=xhci.0,drive=stick,bootindex=1",
                   "-device", "usb-kbd,bus=xhci.0", "-device", "usb-tablet,bus=xhci.0",
                   "-netdev", "user,id=testnet,restrict=on,hostfwd=tcp:127.0.0.1:" + str(forward) + "-:5555",
                   "-device", "e1000,netdev=testnet,romfile=,bus=pcie.0,addr=0x7",
                   "-audiodev", "none,id=sound", "-device", "ich9-intel-hda,id=hda,bus=pcie.0,addr=0x1b",
                   "-device", "hda-duplex,bus=hda.0,audiodev=sound,mixer=off",
                   "-serial", "file:" + str(args.work / "serial.log"), "-monitor", "none",
                   "-qmp", "unix:" + str(sock) + ",server=on,wait=off"]
        subprocess.run([*adb_base, "start-server"], env=adb_env, check=True, capture_output=True)
        with (args.work / "qemu.log").open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("UEFI test exited before Android boot completed.")
                    try:
                        subprocess.run([*adb_base, "connect", address], env=adb_env,
                                       capture_output=True, timeout=5)
                        if adb("shell", "getprop sys.boot_completed") == "1":
                            break
                    except (RuntimeError, subprocess.TimeoutExpired):
                        pass
                    time.sleep(3)
                else:
                    raise RuntimeError("Android boot completion was not reached within the test window.")
                fingerprint = adb("shell", "getprop ro.build.fingerprint")
                if "/" + args.build + ":user/release-keys" not in fingerprint:
                    raise RuntimeError("Booted image is not the selected public release.")
                report = {"target": args.model, "recovery_build": args.build,
                          "fingerprint": fingerprint, "boot_completed": True}
                (args.work / "PASS.json").write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report), flush=True)
            finally:
                if process.poll() is None:
                    try:
                        qmp(sock, "quit")
                    except (OSError, RuntimeError):
                        process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                subprocess.run([*adb_base, "kill-server"], env=adb_env, capture_output=True, timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
