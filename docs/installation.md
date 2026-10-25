# USB boot, internal installation and updates

Build the matching target image using the root README. A live image boots from
USB and leaves internal storage protected. A separate installer image is needed
to install or update internal storage.

## Prepare the maintenance payload

These commands require Linux x86-64, the packages listed in the README, and
working unprivileged user namespaces for maintenance-system assembly.
The package list and hashes are pinned in `install/stick/maint.pins`.

```bash
mkdir -p work/maintenance/cache
bash install/stick/make-maint.sh fetch --cache "$PWD/work/maintenance/cache"
bash install/stick/make-maint.sh assemble \
  --cache "$PWD/work/maintenance/cache" --out "$PWD/work/maintenance/system"
```

The assembly output directory must be new. The command downloads upstream
packages and creates `root/` and `xbootldr/` directories.

## Assemble an installer USB image

Example for CX54; substitute the image for your target:

```bash
zstd -d out/googlebook-cx5403-16658739.img.zst
python3 install/stick/make-pristine.py \
  out/googlebook-cx5403-16658739.img work/maintenance/pristine-cx54
bash install/make-installer-stick.sh \
  --image "$PWD/out/googlebook-cx5403-16658739.img" \
  --out "$PWD/out/googlebook-cx5403-installer.img" \
  --xbootldr "$PWD/work/maintenance/system/xbootldr" \
  --root "$PWD/work/maintenance/system/root" \
  --pristine "$PWD/work/maintenance/pristine-cx54" \
  --default-action update
sudo bash install/write-usb.sh out/googlebook-cx5403-installer.img \
  /dev/disk/by-id/usb-YOUR_USB_DEVICE
```

Use a USB disk larger than the generated image; a 64 GB disk is a convenient
choice. Writing it erases that USB disk. The maintenance image retains an
untested marker until the relevant checks have been performed; the writer
shows its warning and requires explicit confirmation.

## On the laptop

Boot the USB's UEFI entry and inspect the selected internal disk and serial.

* **Install** erases the selected internal disk and creates a new installation.
* **Update (keep data)** writes the other system slot and preserves data/state
  partitions. It requires the existing installation's AVB signing key.

Confirm the requested serial suffix and wait for the success message. Keep
power and USB connected throughout the operation. Then remove the USB and boot
the internal disk. Keep backups of personal files and the AVB key.

The updater checks the model and signing key. It refuses an incompatible
target, a mismatched key, or an in-use target disk. A freshly generated key
cannot update an installation signed with another key.

For Chromebook firmware setup, use the separate
[CX34/CX54 write-protection guide](firmware-write-protection.md). Ordinary OS
updates on an already configured UEFI laptop do not require another firmware
unlock or Full ROM flash.
