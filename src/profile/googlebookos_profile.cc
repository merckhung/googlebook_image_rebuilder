#include "src/profile/googlebookos_profile.h"

#include <algorithm>
#include <cctype>
#include <set>
#include <sstream>

namespace googlebookos::profile {
namespace {

constexpr size_t kMaxBootloaderCmdlineBytes = 1756;

bool IsHex64(const std::string& s) {
  if (s.size() != 64) return false;
  return std::all_of(s.begin(), s.end(), [](unsigned char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
  });
}

std::vector<std::string> SplitWhitespace(const std::string& s) {
  std::istringstream iss(s);
  std::vector<std::string> out;
  std::string tok;
  while (iss >> tok) out.push_back(tok);
  return out;
}

std::string JoinStrings(const std::vector<std::string>& items,
                        const std::string& sep) {
  std::ostringstream oss;
  for (size_t i = 0; i < items.size(); ++i) {
    if (i > 0) oss << sep;
    oss << items[i];
  }
  return oss.str();
}

}  // namespace

DeviceProfile GetDefaultProfile() {
  DeviceProfile p;
  p.cmdline_stock_drop = {
      "snd_intel_sdw_acpi.sdw_link_mask=0x5",
      "xe.enable_psr=1",
      "snd_sof_pci.ipc_type=1",
  };
  p.cmdline_add = {
      "loglevel=8",
      "ignore_loglevel",
      "earlycon=uart8250,mmio32,0xfe03e000,115200n8",
      "xe.force_probe=46b3,46a6,46a3,46a8,4626,4628,46b2,a721,a7a1,a7a9,a7ac,a7ad",
  };
  p.cmdline_tail = {
      "xe.enable_dpt=0",
      "xe.enable_psr=0",
      "xe.enable_fbc=0",
      "snd_sof_pci.ipc_type=0",
  };
  p.cmdline_debug_drop = {"8250.nr_uarts=4"};
  p.pci_stub_ids = {"8086:51a8", "8086:51a9", "8086:51aa", "8086:51ab"};
  p.first_stage_drop = {"8250_dw.ko", "intel-lpss-pci.ko", "intel-lpss.ko"};

  p.blacklist_modules = {
      "cros_ec_debugfs", "tpm_tis_i2c_cr50", "cros_ec_lpcs", "cros_ec_usb",
      "spi_intel_pci", "spi_intel", "i2c_dev", "thunderbolt", "tpm",
      "tpm_tis", "tpm_tis_core", "tpm_tis_spi", "cros_ec", "cros_ec_dev",
      "cros_ec_chardev", "gsmi", "chromeos_acpi", "nvram", "mtd", "spi_nor",
      "spidev", "intel_quicki2c", "intel_quickspi", "at24", "cros_hps_i2c",
      "i2c_cros_ec_tunnel", "cros_peripheral_charger", "cros_ec_spi",
      "cros_ec_i2c", "cros_ec_ishtp", "cros_ec_sysfs", "cros_ec_vbc",
      "cros_ec_typec", "cros_typec_switch", "cros_usbpd_notify",
      "cros_usbpd_charger", "cros_usbpd_logger", "cros_charge_control",
      "cros_ec_ucsi", "cros_ec_keyb", "cros_ec_sensorhub",
      "cros_ec_sensors_core", "cros_ec_sensors", "cros_ec_accel_legacy",
      "cros_ec_baro", "cros_ec_lid_angle", "cros_ec_light_prox",
      "cros_ec_lightbar", "cros_ec_pwrmon", "cros_ec_cec", "leds_cros_ec",
      "hid_google_hammer", "wilco_ec", "wilco_charger", "wilco_ec_debugfs",
      "wilco_ec_telem", "rtc_wilco_ec",
  };
  p.blacklist_debug_add = {"8250_dw", "intel_lpss_acpi"};
  p.blacklist_allow = {"cros_ec_proto", "cros_kbd_led_backlight"};

  p.firmware = {
      {"i915/adlp_dmc.bin", "linux-firmware", "i915/adlp_dmc.bin",
       "3516de2e134ddcf3b319c75d2e437779fecbd58cbb77234bd6f297c544e92ccb"},
      {"i915/adlp_guc_70.bin", "linux-firmware", "i915/adlp_guc_70.bin",
       "b7a800cbdcdf687e7b1c81f3316d040ab75fa3e5c698ebc356f077af17462ef8"},
      {"i915/tgl_huc.bin", "linux-firmware", "i915/tgl_huc.bin",
       "dbb1316bac13a76427ffa7827e52b5b9affe8a4cadd23dc22e2a54e35c53bdad"},
      {"iwlwifi-so-a0-gf-a0-89.ucode", "linux-firmware",
       "intel/iwlwifi/iwlwifi-so-a0-gf-a0-89.ucode",
       "7b53254bb28115f91b6eaa33b1f9a3526e32966105eaac587e259dd17790f384"},
      {"iwlwifi-so-a0-gf-a0.pnvm", "linux-firmware",
       "intel/iwlwifi/iwlwifi-so-a0-gf-a0.pnvm",
       "2aee7592ddbd6c88f1aa51be4ada188fce44765405e52d7f3056b46e62cf3f91"},
      {"intel/ibt-0040-0041.sfi", "linux-firmware", "intel/ibt-0040-0041.sfi",
       "8dcfb3a7c1592c11c3e80505d21ee78dbde3de0c830d8e3e856371c37717b62c"},
      {"intel/ibt-0040-0041.ddc", "linux-firmware", "intel/ibt-0040-0041.ddc",
       "fe272982577efdc289cfe3e8bedafca607c0b7d6c9df1aab1880e22ef930d077"},
      {"intel/sof/community/sof-adl.ri", "sof-bin",
       "sof-bin-2025.12.2/sof/community/sof-tgl.ri",
       "e3489dcb277e87eee8bc091ab7d51163a63c85541c22522775122b54263769f3"},
      {"intel/sof/sof-adl.ri", "sof-bin",
       "sof-bin-2025.12.2/sof/intel-signed/sof-adl.ri",
       "294e5a51b1d3fc4ceda57780927c2a2ec95bb5d23477f879a9fb648c6c01cb30"},
      {"intel/sof-tplg/sof-adl-rt1019-rt5682.tplg", "sof-bin",
       "sof-bin-2025.12.2/sof-tplg/sof-adl-rt1019-rt5682.tplg",
       "b0c08e8f0ef9067320b2f4251d110c57dfa28498827639b8250ec056d390675e"},
      {"intel/sof-tplg/sof-adl-rt5682.tplg", "sof-bin",
       "sof-bin-2025.12.2/sof-tplg/sof-adl-rt5682.tplg",
       "457886fdcdcaa5bad3a7b4868a3e76d4c98c0c1d02f59a1718a2edc9ac789afe"},
  };

  p.build_prop_set = {
      {"vendor.hwc.drm.enable_color_pipeline", "true", "false"},
      {"vendor.hwc.drm.force_color_mode", "13", "0"},
      {"vendor.hwc.drm.persistent_hdr", "true", "false"},
      {"vendor.hwc.use_log_gamma_lut", "true", "false"},
      {"ro.surface_flinger.has_wide_color_display", "true", "false"},
      {"ro.surface_flinger.has_HDR_display", "true", "false"},
  };
  return p;
}

DeviceProfile GetScreeboCx5403Profile() {
  // Same safety blacklist, kmods, input and display handling as marasov; the
  // SoC-specific parts (GPU ids, LPSS ids, console UART, SOF IPC4, firmware) are
  // Meteor Lake. Keep in sync with profiles/screebo/profile.toml.
  DeviceProfile p = GetDefaultProfile();
  p.model_id = "screebo";
  p.model_name =
      "ASUS ExpertBook CX54 Chromebook Plus (Model CX5403CMA, Core Ultra 5 115U)";
  p.form_factor = "clamshell";
  p.status = "experimental";
  p.image_name = "googlebook-cx5403.img";
  p.dmi_product_name = "Screebo";
  p.entry_quiet_title = "Googlebook CX5403 quiet";
  p.entry_debug_title = "Googlebook CX5403 safe serial-debug";

  p.cmdline_stock_drop = {
      "snd_intel_sdw_acpi.sdw_link_mask=0x5",
      "xe.enable_psr=1",
  };
  p.cmdline_add = {
      "loglevel=8",
      "ignore_loglevel",
      "earlycon=uart8250,mmio32,0xfe02c000,115200n8",
      "xe.force_probe=7d45,7d40,7d55,7dd5,7d41,7d51,7d60,7d67,7dd1",
  };
  p.cmdline_tail = {"xe.enable_psr=0"};
  p.pci_stub_ids = {"8086:7e25", "8086:7e26", "8086:7e52", "8086:7e30"};

  p.firmware = {
      {"i915/mtl_dmc.bin", "linux-firmware", "i915/mtl_dmc.bin",
       "1b3b826195d50fae6611d29e0eb348e641a3ada3e0436ee0b987124f47a0e0db"},
      {"i915/mtl_guc_70.bin", "linux-firmware", "i915/mtl_guc_70.bin",
       "7794f0b6abe5fcd9c6f47035dafe2199f30a6e7d230bd5a53fbf8005a60e5911"},
      {"i915/mtl_huc_gsc.bin", "linux-firmware", "i915/mtl_huc_gsc.bin",
       "3ea23f39e946122d1446565d1a07c0866bf3e3c8819b43134925371d877ca4cd"},
      {"i915/mtl_gsc_1.bin", "linux-firmware", "i915/mtl_gsc_1.bin",
       "01e8e2bb0eae90e3b4471703bb04d1bd13adb43373480fb10be6f101b877e0f3"},
      {"iwlwifi-ma-b0-gf-a0-89.ucode", "linux-firmware",
       "intel/iwlwifi/iwlwifi-ma-b0-gf-a0-89.ucode",
       "fd69e6042821a42a62da44112346066a5c88556193b15a188b397dabe711f768"},
      {"iwlwifi-ma-b0-gf-a0.pnvm", "linux-firmware",
       "intel/iwlwifi/iwlwifi-ma-b0-gf-a0.pnvm",
       "6b1526daa6552dcab485675f5ae1214ad6ab7c17414dae3c75ed1c36d53e8631"},
      {"intel/ibt-0180-0041.sfi", "linux-firmware", "intel/ibt-0180-0041.sfi",
       "205f22d76408283bad6d26e1fe21cc30cdb0c331b41de1e6d093c9ea2401feaa"},
      {"intel/ibt-0180-0041.ddc", "linux-firmware", "intel/ibt-0180-0041.ddc",
       "fe272982577efdc289cfe3e8bedafca607c0b7d6c9df1aab1880e22ef930d077"},
      {"intel/ipu/ipu6epmtl_fw.bin", "linux-firmware",
       "intel/ipu/ipu6epmtl_fw.bin",
       "5d563b75e53167f2a20f5bffbb6db1912c5a4c8e19bdc233ffd5e0e59cf51095"},
      {"intel/sof-ipc4/mtl/sof-mtl.ri", "sof-bin",
       "sof-bin-2025.12.2/sof-ipc4/mtl/intel-signed/sof-mtl.ri",
       "b549c4447cbd92c636feb6993a75ae0a6b1d831336d2723b82c9fcc03c0ae778"},
      {"intel/sof-ipc4-tplg/sof-mtl-rt1019-rt5682.tplg", "sof-bin",
       "sof-bin-2025.12.2/sof-ipc4-tplg/sof-mtl-rt1019-rt5682.tplg",
       "5e3a0431cb7c3786d3f1e89d3f52b245bf81db2bc1023e40097c35d7313d5e6b"},
  };

  p.audio_card = "sof-rt5682";
  p.audio_config_dir = "sof-rt5682_screebo";
  return p;
}

DeviceProfile GetProfileByModel(const std::string& model, bool* found) {
  if (found) *found = true;
  if (model == "marasov") return GetDefaultProfile();
  if (model == "screebo") return GetScreeboCx5403Profile();
  if (found) *found = false;
  return GetDefaultProfile();
}

namespace {

// Safety invariants shared by every built-in profile: the required blacklist,
// the allow list, and the x86 boot command-line budget.
bool CheckCommonSafety(const DeviceProfile& prof, std::string* error_msg) {
  auto fail = [&](const std::string& msg) {
    if (error_msg) *error_msg = msg;
    return false;
  };
  std::set<std::string> bl(prof.blacklist_modules.begin(),
                           prof.blacklist_modules.end());
  const std::vector<std::string> required_blacklist = {
      "cros_ec",         "cros_ec_lpcs",      "cros_ec_dev",
      "cros_ec_chardev", "tpm_tis_i2c_cr50",  "spi_intel_pci",
      "spi_intel",       "gsmi",              "chromeos_acpi",
      "nvram",           "at24",              "i2c_cros_ec_tunnel",
      "leds_cros_ec",    "cros_ec_keyb",      "cros_ec_lid_angle",
      "cros_ec_sensors", "cros_ec_sensorhub", "cros_ec_accel_legacy",
  };
  for (const auto& req : required_blacklist) {
    if (bl.find(req) == bl.end()) {
      return fail("Required safety blacklist module missing: " + req);
    }
  }
  for (const auto& allow : prof.blacklist_allow) {
    if (bl.find(allow) != bl.end()) {
      return fail("Allowed module must not be blacklisted: " + allow);
    }
  }
  for (const auto& fw : prof.firmware) {
    if (fw.path.empty() || !IsHex64(fw.sha256)) {
      return fail("Invalid firmware entry or sha256 for: " + fw.path);
    }
  }
  const std::string kSampleStockCmdline =
      "console=ttyS0,115200n8 earlycon=uart8250,io,0x3f8 "
      "androidboot.hardware=android-desktop firmware_class.path=/vendor/firmware "
      "snd_intel_sdw_acpi.sdw_link_mask=0x5 xe.enable_psr=1 "
      "snd_sof_pci.ipc_type=1 8250.nr_uarts=4";
  for (bool debug : {false, true}) {
    std::string opts =
        BuildSystemdBootOptions(prof, kSampleStockCmdline, debug);
    if (opts.size() > kMaxBootloaderCmdlineBytes) {
      return fail("Bootloader options exceed 1756-byte x86 budget");
    }
  }
  return true;
}

}  // namespace

bool ValidateScreeboProfile(const DeviceProfile& prof, std::string* error_msg) {
  auto fail = [&](const std::string& msg) {
    if (error_msg) *error_msg = msg;
    return false;
  };
  if (prof.model_id != "screebo") return fail("model_id must be screebo");
  if (prof.image_name != "googlebook-cx5403.img") {
    return fail("image_name must be googlebook-cx5403.img");
  }
  if (prof.dmi_product_name != "Screebo") {
    return fail("dmi product_name must be Screebo (ChromeOS frid Google_Screebo)");
  }
  if (prof.audio_card != "sof-rt5682" ||
      prof.audio_config_dir != "sof-rt5682_screebo") {
    return fail("audio card/config_dir mismatch for screebo");
  }
  bool found_force_probe = false;
  for (const auto& tok : prof.cmdline_add) {
    if (tok.rfind("xe.force_probe=", 0) == 0) {
      found_force_probe = true;
      std::string ids = tok.substr(std::string("xe.force_probe=").size());
      if (ids.find('*') != std::string::npos) {
        return fail("xe.force_probe must not contain wildcard '*'");
      }
      if (ids.rfind("7d45", 0) != 0) {
        return fail(
            "xe.force_probe must list 7d45 (Meteor Lake-U Intel Graphics) first");
      }
    }
  }
  if (!found_force_probe) return fail("xe.force_probe missing from cmdline_add");
  for (const auto& tok : prof.cmdline_tail) {
    if (tok == "snd_sof_pci.ipc_type=0") {
      return fail("Meteor Lake SOF is IPC4: snd_sof_pci.ipc_type=0 is wrong");
    }
  }
  for (const auto& tok : prof.cmdline_stock_drop) {
    if (tok == "snd_sof_pci.ipc_type=1") {
      return fail("screebo keeps the stock snd_sof_pci.ipc_type=1 (IPC4)");
    }
  }
  const std::vector<std::string> required_fw = {
      "i915/mtl_dmc.bin",
      "i915/mtl_guc_70.bin",
      "i915/mtl_huc_gsc.bin",
      "i915/mtl_gsc_1.bin",
      "iwlwifi-ma-b0-gf-a0-89.ucode",
      "intel/ibt-0180-0041.sfi",
      "intel/sof-ipc4/mtl/sof-mtl.ri",
      "intel/sof-ipc4-tplg/sof-mtl-rt1019-rt5682.tplg",
  };
  for (const auto& want : required_fw) {
    bool have = false;
    for (const auto& fw : prof.firmware) have = have || fw.path == want;
    if (!have) return fail("screebo firmware missing: " + want);
  }
  for (const auto& fw : prof.firmware) {
    if (fw.path.find("adl") != std::string::npos ||
        fw.path.find("iwlwifi-so-") != std::string::npos) {
      return fail("Alder Lake firmware in the Meteor Lake profile: " + fw.path);
    }
  }
  return CheckCommonSafety(prof, error_msg);
}

bool ValidateProfile(const DeviceProfile& prof, std::string* error_msg) {
  if (prof.model_id == "screebo") return ValidateScreeboProfile(prof, error_msg);
  return ValidateMarasovProfile(prof, error_msg);
}

bool ValidateMarasovProfile(const DeviceProfile& prof, std::string* error_msg) {
  auto fail = [&](const std::string& msg) {
    if (error_msg) *error_msg = msg;
    return false;
  };

  if (prof.model_id != "marasov") {
    return fail("model_id must be marasov");
  }
  if (prof.form_factor != "clamshell-180") {
    return fail(
        "form_factor must be clamshell-180 (ASUS Chromebook Plus CX34 CX3402 "
        "is a 180-degree lay-flat clamshell, not a Flip convertible)");
  }
  if (prof.image_name != "googlebook-cx3402.img") {
    return fail("image_name must be googlebook-cx3402.img");
  }
  if (prof.audio_card != "sof-rt5682" ||
      prof.audio_config_dir != "sof-rt5682_marasov") {
    return fail("audio card/config_dir mismatch for marasov");
  }

  std::set<std::string> bl(prof.blacklist_modules.begin(),
                           prof.blacklist_modules.end());
  const std::vector<std::string> required_blacklist = {
      "cros_ec",         "cros_ec_lpcs",      "cros_ec_dev",
      "cros_ec_chardev", "tpm_tis_i2c_cr50",  "spi_intel_pci",
      "spi_intel",       "gsmi",              "chromeos_acpi",
      "nvram",           "at24",              "i2c_cros_ec_tunnel",
      "leds_cros_ec",    "cros_ec_keyb",      "cros_ec_lid_angle",
      "cros_ec_sensors", "cros_ec_sensorhub", "cros_ec_accel_legacy",
  };
  for (const auto& req : required_blacklist) {
    if (bl.find(req) == bl.end()) {
      return fail("Required safety blacklist module missing: " + req);
    }
  }
  for (const auto& allow : prof.blacklist_allow) {
    if (bl.find(allow) != bl.end()) {
      return fail("Allowed module must not be blacklisted: " + allow);
    }
  }

  bool found_force_probe = false;
  for (const auto& tok : prof.cmdline_add) {
    if (tok.rfind("xe.force_probe=", 0) == 0) {
      found_force_probe = true;
      std::string ids = tok.substr(std::string("xe.force_probe=").size());
      if (ids.find('*') != std::string::npos) {
        return fail("xe.force_probe must not contain wildcard '*'");
      }
      if (ids.rfind("46b3", 0) != 0) {
        return fail(
            "xe.force_probe must list 46b3 (i3-1215U Alder Lake-U GT1) first");
      }
    }
  }
  if (!found_force_probe) {
    return fail("xe.force_probe missing from cmdline_add");
  }

  if (prof.firmware.size() != 11) {
    return fail("Expected 11 pinned firmware entries for marasov");
  }
  bool found_rt1019_tplg = false;
  for (const auto& fw : prof.firmware) {
    if (fw.path.empty() || !IsHex64(fw.sha256)) {
      return fail("Invalid firmware entry or sha256 for: " + fw.path);
    }
    if (fw.path == "intel/sof-tplg/sof-adl-rt1019-rt5682.tplg") {
      found_rt1019_tplg = true;
    }
    if (fw.path.find("max98360") != std::string::npos) {
      return fail(
          "marasov uses Realtek ALC1019P (sof-adl-rt1019-rt5682.tplg), not MAX98360A");
    }
  }
  if (!found_rt1019_tplg) {
    return fail("Missing sof-adl-rt1019-rt5682.tplg for marasov ALC1019P + ALC5682I-VS");
  }

  const std::string kSampleStockCmdline =
      "console=ttyS0,115200n8 earlycon=uart8250,io,0x3f8 "
      "androidboot.hardware=android-desktop firmware_class.path=/vendor/firmware "
      "snd_intel_sdw_acpi.sdw_link_mask=0x5 xe.enable_psr=1 "
      "snd_sof_pci.ipc_type=1 8250.nr_uarts=4";
  for (bool debug : {false, true}) {
    std::string opts =
        BuildSystemdBootOptions(prof, kSampleStockCmdline, debug);
    if (opts.size() > kMaxBootloaderCmdlineBytes) {
      return fail("Bootloader options exceed 1756-byte x86 budget");
    }
  }

  return true;
}

std::string BuildSystemdBootOptions(const DeviceProfile& prof,
                                    const std::string& stock_cmdline,
                                    bool is_debug_entry) {
  std::set<std::string> drop(prof.cmdline_stock_drop.begin(),
                             prof.cmdline_stock_drop.end());
  if (is_debug_entry) {
    drop.insert(prof.cmdline_debug_drop.begin(), prof.cmdline_debug_drop.end());
  }

  std::vector<std::string> tokens;
  for (const auto& t : SplitWhitespace(stock_cmdline)) {
    if (drop.count(t)) continue;
    if (t.rfind("console=", 0) == 0 || t.rfind("earlycon=", 0) == 0) continue;
    tokens.push_back(t);
  }

  for (const auto& t : prof.cmdline_add) {
    if (!is_debug_entry) {
      if (t == "ignore_loglevel" || t.rfind("loglevel=", 0) == 0 ||
          t.rfind("earlycon=", 0) == 0 || t.rfind("console=", 0) == 0) {
        continue;
      }
    }
    tokens.push_back(t);
  }
  if (is_debug_entry) {
    tokens.push_back("keep_bootcon");
  }

  std::vector<std::string> bl = prof.blacklist_modules;
  bl.insert(bl.end(), prof.blacklist_debug_add.begin(),
            prof.blacklist_debug_add.end());

  tokens.push_back("efi=noruntime");
  tokens.push_back("reboot=acpi");
  tokens.push_back("intremap=off");
  if (!prof.pci_stub_ids.empty()) {
    tokens.push_back("pci-stub.ids=" + JoinStrings(prof.pci_stub_ids, ","));
  }
  tokens.push_back("module_blacklist=" + JoinStrings(bl, ","));
  tokens.push_back("initcall_blacklist=cros_ec_LPC_driver_init");
  for (const auto& t : prof.cmdline_tail) {
    tokens.push_back(t);
  }
  return JoinStrings(tokens, " ");
}

std::string BuildSystemdBootEntryConf(const DeviceProfile& prof,
                                      const std::string& stock_cmdline,
                                      bool is_debug_entry) {
  std::ostringstream oss;
  oss << "title   "
      << (is_debug_entry ? prof.entry_debug_title : prof.entry_quiet_title)
      << "\n";
  oss << "linux   /vmlinuz\n";
  oss << "initrd  /initrd.img\n";
  oss << "options "
      << BuildSystemdBootOptions(prof, stock_cmdline, is_debug_entry) << "\n";
  return oss.str();
}

}  // namespace googlebookos::profile
