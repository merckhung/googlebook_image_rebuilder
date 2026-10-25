#ifndef GOOGLEBOOKOS_SRC_INSTALLER_GOOGLEBOOKOS_INSTALLER_H_
#define GOOGLEBOOKOS_SRC_INSTALLER_GOOGLEBOOKOS_INSTALLER_H_

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace googlebookos::installer {

struct BlockDeviceInfo {
  std::string dev_path;       // e.g. "/dev/sda" or "/dev/nvme0n1"
  std::string sys_name;       // e.g. "sda"
  std::string vendor;         // e.g. "KIOXIA" or "SKhynix"
  std::string model;          // e.g. "THGJFGT0T44BAIL" (128GB UFS)
  std::string subsystem;      // "scsi" (ufshcd-pci / usb-storage) or "nvme"
  bool removable = false;
  uint64_t size_bytes = 0;
  bool is_ufs_internal = false;
};

struct dmi_identity {
  std::string sys_vendor;
  std::string product_name;
  std::string board_name;
  bool is_marasov = false;
  bool is_screebo = false;
};

// Reads DMI strings from a sysfs root (defaults to `/sys/class/dmi/id`)
// and identifies whether the machine is an ASUS Chromebook Plus CX34 (CX3402 / Marasov).
dmi_identity ProbeDmiIdentity(
    const std::filesystem::path& sys_dmi_dir = "/sys/class/dmi/id");

// Formats a partition device path for a given parent block device and 1-based
// partition number:
//   - `/dev/sda` + 13 -> `/dev/sda13` (128GB UFS or USB stick)
//   - `/dev/nvme0n1` + 13 -> `/dev/nvme0n1p13` (NVMe SSD)
//   - `/dev/mmcblk0` + 13 -> `/dev/mmcblk0p13`
std::string FormatPartitionDevicePath(const std::string& parent_dev,
                                      int part_num);

// Verifies all safety gates on a built `googlebook-cx3402.img` before flashing
// to USB stick or internal 128GB UFS storage (`ufshcd-pci`).
bool VerifyGooglebookOsImageBeforeInstall(const std::filesystem::path& img_path,
                                    std::string* error_msg);

// Generates a human-readable partition installation plan mapping the GPT
// partitions in `img_path` onto `target_dev` (e.g. `/dev/sda` for 128GB UFS or
// `/dev/nvme0n1` for PCIe NVMe).
std::string BuildUfsInstallPlan(const std::filesystem::path& img_path,
                                const std::string& target_dev,
                                std::string* error_msg);

}  // namespace googlebookos::installer

#endif  // GOOGLEBOOKOS_SRC_INSTALLER_GOOGLEBOOKOS_INSTALLER_H_
