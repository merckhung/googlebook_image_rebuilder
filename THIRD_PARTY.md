# License scope and third-party components

The MIT license in `LICENSE` covers original project tooling and documentation,
except files carrying another license notice. It does not relicense the OS,
firmware, kernel, or downloaded third-party tools.

* `builder/kmod/gbos_isa_emu.c`, `gbos_efifb.c`, `gbos_wakefilter.c`,
  `isa-stage0.c`, `isa-user-memory.h`, and `test-isa-user-memory.c` retain their
  GPL-2.0 SPDX identifiers. The GPL text is in `licenses/GPL-2.0.txt`.
* The ACPI module source is downloaded from the pinned public kernel tree and
  retains the kernel's GPL license. Source URLs and revisions are in its build script.
* Generated Android audio XML retains upstream Apache-2.0 notices. Upstream
  notices are preserved when files are modified.
* Recovery images, AOSP tools, systemd-boot, firmware, and maintenance-system
  packages are fetched from their upstreams with pinned checksums. They remain
  subject to their upstream licenses and distribution terms.

Binary recovery images and signing keys are excluded from this source repository.
The demo video is supplied demonstration media and is outside the MIT source-code
grant. Product names, logos, artwork, and third-party audiovisual content retain
any applicable rights of their owners.
