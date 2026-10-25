#include <iostream>
#include <string>

#include "src/profile/googlebookos_profile.h"

int main(int argc, char** argv) {
  std::string model = "marasov";
  int argi = 1;
  if (argi + 1 < argc && std::string(argv[argi]) == "--model") {
    model = argv[argi + 1];
    argi += 2;
  }
  bool found = false;
  auto prof = googlebookos::profile::GetProfileByModel(model, &found);
  if (!found) {
    std::cerr << "Unknown model '" << model << "'\n";
    return 2;
  }
  std::string err;
  if (!googlebookos::profile::ValidateProfile(prof, &err)) {
    std::cerr << "Profile validation failed: " << err << "\n";
    return 1;
  }
  std::string cmd = (argi < argc) ? argv[argi] : "check";
  if (cmd == "check") {
    std::cout << "profile " << prof.model_id << " ok (" << prof.model_name
              << ")\n";
    return 0;
  } else if (cmd == "entries") {
    std::string stock =
        "androidboot.hardware=android-desktop firmware_class.path=/vendor/firmware "
        "snd_intel_sdw_acpi.sdw_link_mask=0x5 xe.enable_psr=1 snd_sof_pci.ipc_type=1";
    std::cout << "=== lapis-quiet.conf ===\n"
              << googlebookos::profile::BuildSystemdBootEntryConf(prof, stock,
                                                                false)
              << "=== lapis-debug.conf ===\n"
              << googlebookos::profile::BuildSystemdBootEntryConf(prof, stock,
                                                                true);
    return 0;
  }
  return 2;
}
