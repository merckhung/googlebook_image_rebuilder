# CX34 / CX54 Chromebook firmware write-protection unlocking

> **DANGER: This procedure changes firmware security settings. A mistake can
> leave the laptop unable to boot, erase local data, and require a debug cable
> or external flash programmer for recovery. Opening CCD also weakens physical
> security. Back up your files and firmware before proceeding. Keep reliable
> external power connected. Firmware replacement may affect warranty coverage.**

Firmware preparation is separate from rebuilding or writing an OS USB image.
An already working UEFI installation does not need this procedure again for an
OS image update. Keep the LG laptop's existing UEFI firmware.

## Identify the board first

Read the HWID on the Chromebook recovery/developer screen. Use the board name,
not only the retail name, when checking firmware support.

| Laptop | Required board | Security chip | Unlock route |
| --- | --- | --- | --- |
| ASUS Chromebook Plus CX34 / CX3402 | `MARASOV` | Cr50 | Open CCD, then use a SuzyQable |
| ASUS ExpertBook CX54 / CX5403 | `SCREEBO` | Ti50 | Open CCD; disable RO verification and hardware WP |

These rows follow the [firmware maintainer's device list](https://docs.mrchromebox.tech/docs/supported-devices.html).
Stop if your HWID differs. In stock ChromeOS, `chrome://system` → `tpm_version`
also identifies `GSC_VERSION_CR50` or `GSC_VERSION_TI50`.

## Prepare and make a stock-firmware backup

Back up personal files before entering Developer Mode; changing boot modes can
erase local storage. Follow the [Developer Mode instructions](https://docs.mrchromebox.tech/docs/boot-modes/developer.html).
Use only a personally owned, unmanaged device. An enrollment or policy refusal
requires the administrator's help.

At the ChromeOS login screen, open VT2 with `Ctrl+Alt+F2` and log in as `chronos`.
The commands below run on the Chromebook, outside a Linux container.

```bash
cd "$HOME"
curl -fLO https://mrchromebox.tech/firmware-util.sh
less firmware-util.sh
sudo bash firmware-util.sh
```

Select **Backup Current Firmware** and save the backup on another device.
Exit without flashing at this stage. Keep that backup private; it can contain
device identifiers. This backup operation does not require WP to be disabled.
See the [Firmware Utility documentation](https://docs.mrchromebox.tech/docs/fwscript.html).

## Open CCD: both boards

From the Chromebook's VT2 shell:

```bash
sudo gsctool -a -o
```

Follow each physical-presence prompt exactly: press Power when requested and
wait between requests. The sequence can take several minutes. Successful
opening can reboot the device and exit Developer Mode; re-enter Developer Mode
and return to VT2 before continuing. Opening CCD and disabling flash WP are
separate steps. See the [CCD opening procedure](https://docs.mrchromebox.tech/docs/firmware/wp/disabling.html#opening-ccd-with-gsctool).

## CX54 / SCREEBO: Ti50 preparation

Ti50 verifies both the read-only firmware and software-WP configuration.
**Set `AllowUnverifiedRo` before changing software WP or flashing Full ROM.**
Battery disconnection alone does not unlock Ti50 firmware.

In the Chromebook VT2 shell, after CCD is Open:

```bash
sudo gsctool -a -I AllowUnverifiedRo:always
sudo gsctool -a -w disable
```

Confirm physical-presence requests. RO verification remains disabled; the
hardware-WP override from this command is temporary. Complete flashing before
a security-chip reboot or loss of all power. The optional SuzyQable route below
supports persistent WP control. The upstream
[RO firmware unlock guide](https://www.chromium.org/chromium-os/developer-library/guides/device/ro-firmware-unlock/)
explains the Ti50 boot failure and recovery implications.

## CX34 / MARASOV: SuzyQable route

After opening CCD, connect a real SuzyQable between a Linux host and the
Chromebook's CCD-capable USB-C port. This is a debug-accessory cable, not an
ordinary USB-C cable. If it does not enumerate, check port and plug orientation.

On the Linux host:

```bash
lsusb -d 18d1:5014   # Cr50, expected for MARASOV
lsusb -d 18d1:504a   # Ti50, expected for SCREEBO
ls -l /dev/serial/by-id/
```

Identify the GSC console among the three serial interfaces. Do not assume an
unrelated `/dev/ttyUSB0` is the correct device. Open the verified GSC interface
using a serial terminal at **115200 baud, 8 data bits, no parity, 1 stop bit**,
with flow control disabled. See the [CCD cable/interface documentation](https://chromium.googlesource.com/chromiumos/third_party/hdctools/+/HEAD/docs/ccd.md).

At the **GSC console**, rather than a Linux shell:

```text
ccd
ccd reset factory
wp disable
wp disable atboot
ccd
wp
```

**`ccd reset factory` makes debug capabilities broadly available.** Treat this
as a deliberate security reduction, not a routine diagnostic. Older Cr50
firmware may name the WP command `wp false`; consult the console's `help wp`.
For SCREEBO using this persistent cable route, verify that
`AllowUnverifiedRo` is `Always` before any software-WP change or Full ROM write.
Follow the current [board-appropriate WP procedure](https://docs.mrchromebox.tech/docs/firmware/wp/disabling.html).

## Verify, then use the supported flashing utility

Back in the Chromebook VT2 shell:

```bash
sudo gsctool -a -w
sudo gsctool -a -I
crossystem wpsw_cur
```

Check that hardware WP is disabled and `wpsw_cur` is `0`; on Ti50 also require
`AllowUnverifiedRo: Always`. If anything disagrees, stop. Use the Firmware Utility
to manage software WP and perform the supported UEFI Full ROM operation for the
exact board. Keep the firmware backup off-device and read every confirmation.
Manual software-WP commands are unnecessary when using that utility.

Closing CCD alone does not restore flash protection. On Ti50, blindly restoring
RO verification while custom firmware remains installed can prevent boot.
Use the documented [stock-firmware restoration procedure](https://docs.mrchromebox.tech/docs/reverting/)
when returning to the factory security configuration. For failures, consult
[SuzyQable recovery](https://docs.mrchromebox.tech/docs/support/unbricking/unbrick-suzyq.html)
before attempting further writes.
