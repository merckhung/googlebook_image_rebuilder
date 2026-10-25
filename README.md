# googlebookos Image Rebuilder

`googlebookos` is a toolkit for adapting public **Android 17 desktop USB
recovery images** to the ASUS Chromebook Plus CX34, ASUS ExpertBook CX54, and
LG gram Book 15U50U. It produces a model-specific image that boots through UEFI,
with optional tooling for installation onto internal storage and later updates.

The rebuild starts with a released Lapis recovery image. It verifies the input,
extracts its partitions, applies the selected device profile, supplies pinned
firmware and matching kernel modules, and regenerates the vendor filesystem,
verified-boot metadata, and UEFI boot files. Hardware-bound credential services
are replaced with public AOSP software-backed services so the image can run on
these target systems. A local AVB key signs the rebuilt partitions.

The result is a raw USB disk image plus compressed archives and checksums.
Profiles and scripts are versioned here; downloaded OS images, generated
binaries, logs, and signing keys stay in ignored local directories. The default
input is public stable release **16658739 / CL3B.260622.276.R4**.

The recovery is downloaded from the [public recovery catalog](https://dl.google.com/dl/edgedl/device/recovery/production_recovery.json).
Download URLs, ZIP/image hashes, and sizes are pinned in
[`base/recoveries.json`](base/recoveries.json). Required public upstream URLs
and hardware identifiers are retained in the source.

## Targets and build results

These profiles are experimental. Match the exact model and board before using
an image. Build and boot-check results are recorded in
[`docs/build-results.json`](docs/build-results.json).

<!-- BUILD_RESULTS_START -->
| Target | Profile | Image build | Boot check |
| --- | --- | --- | --- |
| ASUS Chromebook Plus CX34 / CX3402 | `marasov` | Passed | Passed |
| ASUS ExpertBook CX54 / CX5403 | `screebo` | Passed | Passed |
| LG gram Book 15U50U-H.AR55U1 | `lg-15u50u` | Passed | Passed (serial debug) |
<!-- BUILD_RESULTS_END -->

LG reached Android boot completion using the existing **serial-debug** UEFI
menu entry; its initial quiet-entry test timed out after 420 seconds. Boot
checks do not establish full compatibility on the physical laptops.

## Demo video

[Watch or download the demo video](docs/media/demo.mp4) — 44 seconds, portrait
720 × 1280 at 30 fps, H.264 video with AAC audio, **8.6 MB**.
See [media details](docs/media/README.md) for playback and GitHub attachment
instructions.

## Build

Use a Linux x86-64 host with Python 3.11 or later. Allow at least **100 GB** of
free space; building several models without compaction needs more. Builds run
as a normal user. Boot checks require access to `/dev/kvm` and OVMF.

On Debian/Ubuntu:

```bash
sudo apt install curl python3 git make gcc g++ lz4 zstd unzip \
  dosfstools mtools openssl tar xz-utils diffutils e2fsprogs gdisk \
  flex bison libelf-dev libdw-dev libssl-dev qemu-system-x86 qemu-utils ovmf adb
```

Build one model:

```bash
python3 scripts/build.py --model screebo
```

Other profile names are listed in the table above. The shared base and matching
kernel modules are reused across targets. Downloads are checked against their
pinned digests. The default local setup opens the desktop without requiring an
online account; use `--standard-setup` to retain the normal setup flow.

To build all targets, run an additional isolated boot check, and retain
compressed outputs:

```bash
python3 scripts/build.py --model all --boot-check --compact
```

`--boot-check` creates a local test ADB key under the ignored `work/keys/` directory
and includes its public key in the image. Without that option, the wrapper does
not add this network ADB configuration.

Use `--zip /path/to/recovery.zip` to reuse a downloaded public archive, or
`--recovery-bin /path/to/recovery.bin` for its already-extracted image. Both
paths must match the selected public release's checksum. An unsupported
recovery build is refused.

Outputs are stored under `out/`:

```text
googlebook-cx5403-16658739.img.zst
googlebook-cx5403-16658739.img.zst.sha256
googlebook-cx5403-16658739.img.sha256
```

`--compact` removes generated raw-image intermediates after archiving. Logs and
the local signing key remain under `work/`. Keep `work/base/keys/vbmeta.pem`
private and backed up: future updates to an installation require the same AVB
key. Use `--key /path/to/existing-key.pem` for the first build when retaining an
existing installation's key.

`--no-selftest` skips the integrated boot suite and retains a
`.not-self-tested` marker. The checked-in build report records whether that
suite ran; the additional boot check is recorded separately.

## Write a USB

The write erases the selected USB disk. Identify it by its full device serial
and stable `/dev/disk/by-id/usb-...` path.

```bash
cd out
sha256sum -c googlebook-cx5403-16658739.img.zst.sha256
zstd -d googlebook-cx5403-16658739.img.zst
cd ..
sudo bash install/write-usb.sh out/googlebook-cx5403-16658739.img \
  /dev/disk/by-id/usb-YOUR_USB_DEVICE
```

The writer checks the image, disk identity, mounted/root-disk exclusions, full
readback, and GPT. It requires the disk serial to confirm. If an untested marker
is present, read the warning before accepting it.

Boot the USB's UEFI entry on the matching laptop. A live image does not install
itself onto internal storage. For a separate installer or a data-preserving
internal update, see [installation instructions](docs/installation.md).

For Chromebook firmware preparation, read
**[CX34/CX54 firmware write-protection unlocking — dangerous](docs/firmware-write-protection.md)**.
This is a separate, manual procedure with board-specific requirements.

## Source checks and licensing

Install [Bazelisk](https://github.com/bazelbuild/bazelisk#installation) or Bazel
**9.1.0** for the optional native tools and their unit tests. The image rebuild
wrapper uses the Python/shell tooling and does not require Bazel.

```bash
python3 scripts/audit_repository.py
bazel test //tests:googlebookos_unit_test
```

Original project tooling is [MIT licensed](LICENSE). GPL-marked kernel
components and upstream materials retain their own licenses; see
[third-party license scope](THIRD_PARTY.md).
Recovery images, firmware downloads, signing keys, and build outputs are
excluded from the source repository.
