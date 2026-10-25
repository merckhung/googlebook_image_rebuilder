#include "src/installer/googlebookos_installer.h"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <set>
#include <sstream>

#include "src/fat32/fat32.h"
#include "src/gpt/gpt.h"

namespace googlebookos::installer {
namespace {

std::string ReadTrimmed(const std::filesystem::path& p) {
  std::ifstream f(p);
  if (!f) return "";
  std::string s;
  std::getline(f, s);
  while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ')) {
    s.pop_back();
  }
  return s;
}

std::string ToLower(std::string s) {
  for (char& c : s) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return s;
}

}  // namespace

dmi_identity ProbeDmiIdentity(const std::filesystem::path& sys_dmi_dir) {
  dmi_identity id;
  id.sys_vendor = ReadTrimmed(sys_dmi_dir / "sys_vendor");
  id.product_name = ReadTrimmed(sys_dmi_dir / "product_name");
  id.board_name = ReadTrimmed(sys_dmi_dir / "board_name");
  std::string prod_lower = ToLower(id.product_name);
  std::string board_lower = ToLower(id.board_name);
  id.is_marasov = (prod_lower == "marasov" || board_lower == "marasov" ||
                   prod_lower.find("cx3402") != std::string::npos);
  id.is_screebo = (prod_lower == "screebo" || board_lower == "screebo" ||
                   prod_lower.find("cx5403") != std::string::npos);
  return id;
}

std::string FormatPartitionDevicePath(const std::string& parent_dev,
                                      int part_num) {
  if (parent_dev.empty()) return "";
  bool ends_with_digit =
      std::isdigit(static_cast<unsigned char>(parent_dev.back())) != 0;
  return parent_dev + (ends_with_digit ? "p" : "") + std::to_string(part_num);
}

bool VerifyGooglebookOsImageBeforeInstall(const std::filesystem::path& img_path,
                                    std::string* error_msg) {
  auto fail = [&](const std::string& msg) {
    if (error_msg) *error_msg = msg;
    return false;
  };

  try {
    std::string gpt_err;
    auto gpt_opt = gbos::ReadGptFromFile(img_path, &gpt_err);
    if (!gpt_opt) return fail("GPT error: " + gpt_err);
    std::set<std::string> names;
    const gbos::GptPartitionEntry* esp = nullptr;
    for (const auto& p : gpt_opt->partitions) {
      names.insert(p.name);
      if (p.name == "EFI-SYSTEM") esp = &p;
    }
    for (const char* req :
         {"EFI-SYSTEM", "boot_a", "init_boot_a", "vendor_boot_a",
          "vbmeta_a", "super", "metadata", "userdata"}) {
      if (names.find(req) == names.end()) {
        return fail(std::string("Missing required GPT partition: ") + req);
      }
    }
    if (!esp) return fail("ESP partition missing");

    auto entries =
        googlebookos::fat32::ListFat32Entries(img_path, esp->start_bytes);
    std::set<std::string> esp_paths;
    for (const auto& e : entries) {
      esp_paths.insert(ToLower(e.path));
    }
    for (const char* req_file :
         {"efi/boot/bootx64.efi", "loader/loader.conf",
          "loader/entries/lapis-quiet.conf", "loader/entries/lapis-debug.conf",
          "kernel.efi", "initrd.img"}) {
      if (esp_paths.find(req_file) == esp_paths.end()) {
        return fail(std::string("Missing required ESP file: ") + req_file);
      }
    }

    auto quiet_conf_bytes = googlebookos::fat32::ReadFat32File(
        img_path, esp->start_bytes, "loader/entries/lapis-quiet.conf");
    std::string quiet_conf(quiet_conf_bytes.begin(), quiet_conf_bytes.end());
    // Accept either a CX3402 (marasov, Alder Lake-U, SOF IPC3) or a CX5403
    // (screebo, Meteor Lake-U, SOF IPC4) entry.
    const bool is_marasov =
        quiet_conf.find("xe.force_probe=46b3") != std::string::npos;
    const bool is_screebo =
        quiet_conf.find("xe.force_probe=7d45") != std::string::npos;
    if (!is_marasov && !is_screebo) {
      return fail(
          "lapis-quiet.conf missing xe.force_probe=46b3 (CX3402) or "
          "xe.force_probe=7d45 (CX5403)");
    }
    if (is_marasov &&
        quiet_conf.find("snd_sof_pci.ipc_type=0") == std::string::npos) {
      return fail("lapis-quiet.conf missing snd_sof_pci.ipc_type=0 for marasov audio");
    }
    if (is_screebo &&
        quiet_conf.find("snd_sof_pci.ipc_type=1") == std::string::npos) {
      return fail("lapis-quiet.conf missing snd_sof_pci.ipc_type=1 for screebo audio");
    }
    return true;
  } catch (const std::exception& e) {
    return fail(e.what());
  }
}

std::string BuildUfsInstallPlan(const std::filesystem::path& img_path,
                                const std::string& target_dev,
                                std::string* error_msg) {
  if (!VerifyGooglebookOsImageBeforeInstall(img_path, error_msg)) {
    return "";
  }
  std::string gpt_err;
  auto gpt_opt = gbos::ReadGptFromFile(img_path, &gpt_err);
  if (!gpt_opt) {
    if (error_msg) *error_msg = "GPT error: " + gpt_err;
    return "";
  }

  std::ostringstream oss;
  oss << "Googlebook internal-disk install plan for "
      << target_dev << ":\n";
  for (const auto& p : gpt_opt->partitions) {
    int part_num = static_cast<int>(p.index + 1);
    std::string part_dev = FormatPartitionDevicePath(target_dev, part_num);
    oss << "  [" << part_num << "] " << part_dev << " <- " << p.name << " ("
        << (p.size_bytes / (1024 * 1024)) << " MiB";
    if (p.name == "userdata") {
      oss << ", expands to fill remaining target capacity + fresh f2fs";
    }
    oss << ")\n";
  }
  return oss.str();
}

}  // namespace googlebookos::installer
