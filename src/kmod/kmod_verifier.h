#ifndef GOOGLEBOOKOS_SRC_KMOD_KMOD_VERIFIER_H_
#define GOOGLEBOOKOS_SRC_KMOD_KMOD_VERIFIER_H_

#include <cstdint>
#include <filesystem>
#include <map>
#include <string>
#include <vector>

namespace googlebookos::kmod {

struct KoModinfo {
  std::string name;
  std::string vermagic;
  std::vector<std::string> firmware;
  std::map<std::string, uint32_t> versions;  // symbol -> CRC
  std::string sha256;
};

// Parses an ELF64 relocatable kernel module (.ko), extracting `.modinfo`
// (`name=`, `vermagic=`, `firmware=`) and `__versions` modversion CRCs.
KoModinfo InspectKernelModule(const std::filesystem::path& ko_path);

// Verifies that an out-of-tree GKI 6.18.21 kernel module (`ac.ko` or
// `gbos_wakefilter.ko`) matches the expected module name, GKI vermagic
// (`6.18.21-android17-5-g3938c8d97142-ab16302624 SMP preempt mod_unload modversions `),
// and pinned SHA-256 digest.
bool VerifyPinnedGkiModule(const std::filesystem::path& ko_path,
                           const std::string& expected_modname,
                           const std::string& expected_sha256,
                           std::string* error_msg);

}  // namespace googlebookos::kmod

#endif  // GOOGLEBOOKOS_SRC_KMOD_KMOD_VERIFIER_H_
