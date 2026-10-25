#ifndef GOOGLEBOOKOS_SRC_VENDOR_VENDOR_PATCHER_H_
#define GOOGLEBOOKOS_SRC_VENDOR_VENDOR_PATCHER_H_

#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "src/erofs/erofs.h"
#include "src/profile/googlebookos_profile.h"

namespace googlebookos::vendor {

struct VendorPatchSummary {
  std::vector<std::string> removed_paths;
  std::vector<std::string> added_paths;
  std::vector<std::string> modified_paths;
};

// Returns the exact list of 18 firmware/flash tool paths removed from the
// stock lapis vendor tree for CX3402 safety (`fwupd*`, `flashrom*`, `futility*`,
// `ectool`, `crossystem`, `firmware/ap-ec-fw.zip`, etc.).
std::vector<std::string> GetVendorRemovedPaths();

// Applies the CX3402 (`marasov`) vendor tree modifications to an extracted
// vendor directory and updates the EROFS metadata map (`vmap`) for `mkfs.erofs`.
VendorPatchSummary ApplyMarasovVendorPatches(
    const std::filesystem::path& vendor_tree,
    std::map<std::string, gbos::ErofsEntryMeta>* vmap,
    const googlebookos::profile::DeviceProfile& prof,
    const std::filesystem::path& firmware_dir,
    const std::filesystem::path& audio_src_dir,
    const std::filesystem::path& ac_ko_path,
    const std::filesystem::path& wakefilter_ko_path);

}  // namespace googlebookos::vendor

#endif  // GOOGLEBOOKOS_SRC_VENDOR_VENDOR_PATCHER_H_
