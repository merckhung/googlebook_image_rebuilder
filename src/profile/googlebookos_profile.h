#ifndef GOOGLEBOOKOS_SRC_PROFILE_GOOGLEBOOKOS_PROFILE_H_
#define GOOGLEBOOKOS_SRC_PROFILE_GOOGLEBOOKOS_PROFILE_H_

#include <filesystem>
#include <string>
#include <tuple>
#include <vector>

namespace googlebookos::profile {

struct FirmwarePin {
  std::string path;
  std::string from;
  std::string src;
  std::string sha256;
};

struct DeviceProfile {
  std::string model_id = "marasov";
  std::string model_name =
      "ASUS Chromebook Plus CX34 (Model CX3402, 128GB UFS, i3-1215U)";
  // Form factor: 14" FHD (1920x1080) 16:9 Clamshell with 180° lay-flat hinge
  // (NOT a 360° Flip convertible; no tablet-mode lid-angle or EC accel sensors).
  std::string form_factor = "clamshell-180";
  std::string status = "verified";
  std::string image_name = "googlebook-cx3402.img";
  std::string dmi_sys_vendor = "Google";
  std::string dmi_product_name = "Marasov";

  std::string entry_quiet_title = "Googlebook CX3402 quiet";
  std::string entry_debug_title = "Googlebook CX3402 safe serial-debug";

  std::vector<std::string> kmods = {"ac", "gbos_wakefilter"};

  std::vector<std::string> cmdline_stock_drop;
  std::vector<std::string> cmdline_add;
  std::vector<std::string> cmdline_tail;
  std::vector<std::string> cmdline_debug_drop;
  std::vector<std::string> pci_stub_ids;
  std::vector<std::string> first_stage_drop;

  std::vector<std::string> blacklist_modules;
  std::vector<std::string> blacklist_debug_add;
  std::vector<std::string> blacklist_allow;

  std::string linux_firmware_tag = "20260810";
  std::string sof_bin_url =
      "https://github.com/thesofproject/sof-bin/releases/download/v2025.12.2/sof-bin-2025.12.2.tar.gz";
  std::string sof_bin_sha256 =
      "533f63e3a6d94c09ce05a782657b675fa683ff20787c0979226cf563ec79f517";
  std::vector<FirmwarePin> firmware;

  std::string audio_card = "sof-rt5682";
  std::string audio_config_dir = "sof-rt5682_marasov";
  std::string audio_sources = "audio-src.list";

  std::vector<std::string> wifi_modprobe = {"iwlmvm"};

  std::string display_stack = "native";
  std::string display_hwc_backend = "client";
  std::vector<std::tuple<std::string, std::string, std::string>> build_prop_set;

  std::string kbd_backlight_leds =
      "/sys/devices/platform/GOOG0004:00/GOOG0002:00/leds";
  std::string power_button = "acpi";
  bool camera_libcamera = false;
  std::string hooks_vendor = "vendor.py";
};

// Returns the canonical built-in CX3402 (`marasov`) hardware profile.
DeviceProfile GetDefaultProfile();

// Returns the built-in CX5403 (`screebo`) hardware profile: ASUS ExpertBook CX54
// Chromebook Plus (Model CX5403CMA, Core Ultra 5 115U, Meteor Lake-U, rex/screebo).
// Mirrors profiles/screebo/profile.toml.
DeviceProfile GetScreeboCx5403Profile();

// Returns the built-in profile for `model` ("marasov" or "screebo"). Sets
// *found to false (and returns the marasov profile) for any other name.
DeviceProfile GetProfileByModel(const std::string& model, bool* found);

// Validates the CX5403 (`screebo`) profile:
//   - Every forbidden module in _safety.toml is blacklisted; allowed ones are not.
//   - `7d45` (Meteor Lake-U Intel Graphics) is first in `xe.force_probe`, no `*`.
//   - SOF stays IPC4 (no `snd_sof_pci.ipc_type=0`), the MTL rt1019+rt5682
//     topology and the Meteor Lake DMC/GuC/HuC/GSC firmware are pinned.
//   - Both systemd-boot command lines fit within the x86 boot budget.
bool ValidateScreeboProfile(const DeviceProfile& prof, std::string* error_msg);

// Dispatches to ValidateMarasovProfile or ValidateScreeboProfile by model_id.
bool ValidateProfile(const DeviceProfile& prof, std::string* error_msg);

// Validates the CX3402 profile against all safety invariants:
//   - Every forbidden module in _safety.toml is blacklisted.
//   - Allowed modules (`cros_ec_proto`, `cros_kbd_led_backlight`) are not blacklisted.
//   - `46b3` (Alder Lake-U GT1 on i3-1215U) is first in `xe.force_probe` with no wildcard `*`.
//   - Both systemd-boot command lines fit within the x86 kernel boot budget (<= 1756 bytes).
//   - All 11 firmware blobs have valid 64-hex SHA-256 digests.
bool ValidateMarasovProfile(const DeviceProfile& prof, std::string* error_msg);

// Generates the systemd-boot kernel command-line `options` string for `quiet` or `debug`
// given the stock lapis boot_a cmdline.
std::string BuildSystemdBootOptions(const DeviceProfile& prof,
                                    const std::string& stock_cmdline,
                                    bool is_debug_entry);

// Generates the complete systemd-boot entry file (`lapis-quiet.conf` or `lapis-debug.conf`).
std::string BuildSystemdBootEntryConf(const DeviceProfile& prof,
                                      const std::string& stock_cmdline,
                                      bool is_debug_entry);

}  // namespace googlebookos::profile

#endif  // GOOGLEBOOKOS_SRC_PROFILE_GOOGLEBOOKOS_PROFILE_H_
