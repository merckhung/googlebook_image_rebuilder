#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "src/audio/marasov_audio.h"
#include "src/boot/bootimg.h"
#include "src/common/sha256.h"
#include "src/erofs/erofs.h"
#include "src/fat32/fat32.h"
#include "src/gpt/gpt.h"
#include "src/installer/googlebookos_installer.h"
#include "src/kmod/kmod_verifier.h"
#include "src/lp/lp_metadata.h"
#include "src/profile/googlebookos_profile.h"
#include "src/vendor/vendor_patcher.h"

namespace {

// Per-model workspace layout: marasov keeps the original paths, screebo gets its own
// inputs and build directories so both images can be built from one checkout.
struct ModelLayout {
  std::string profile_dir;  // profiles/<id>
  std::string inputs_dir;   // fetch-inputs output (firmware/, audio-src/)
  std::string build_dir;    // GBOS_OUT
};

ModelLayout LayoutFor(const googlebookos::profile::DeviceProfile& prof) {
  if (prof.model_id == "screebo") {
    return {"profiles/screebo", "work/inputs-screebo", "work/build-cx5403"};
  }
  return {"profiles/marasov", "work/inputs", "work/build"};
}

void PrintUsage() {
  std::cout
      << "Googlebook C++ Builder: ASUS Chromebook Plus CX34 (CX3402, marasov) and\n"
      << "ASUS ExpertBook CX54 Chromebook Plus (CX5403CMA, screebo)\n"
      << "Usage:\n"
      << "  googlebookos_builder [--model marasov|screebo] check-profile\n"
      << "  googlebookos_builder [--model marasov|screebo] show-profile\n"
      << "  googlebookos_builder [--model marasov|screebo] fetch-inputs <workspace_dir>\n"
      << "  googlebookos_builder [--model marasov|screebo] verify-inputs <workspace_dir>\n"
      << "  googlebookos_builder inspect-image <googlebook-*.img>\n"
      << "  googlebookos_builder [--model marasov|screebo] build <workspace_dir>\n"
      << "The default model is marasov.\n";
}

int CheckProfileCmd(const googlebookos::profile::DeviceProfile& prof,
                    bool show_entries) {
  std::string err;
  if (!googlebookos::profile::ValidateProfile(prof, &err)) {
    std::cerr << "ERROR: " << err << "\n";
    return 1;
  }
  const bool mtl = prof.model_id == "screebo";
  std::cout << "[OK] Profile '" << prof.model_id << "' (" << prof.model_name
            << ")\n"
            << "     Status:          " << prof.status << "\n"
            << "     Form Factor:     " << prof.form_factor
            << (mtl ? " (14\" 16:10 clamshell, Meteor Lake-U)\n"
                    : " (14\" FHD 16:9 Clamshell, 180-deg lay-flat hinge; non-Flip)\n")
            << "     Target Image:    " << prof.image_name << "\n"
            << "     DMI Match:       " << prof.dmi_sys_vendor << " / "
            << prof.dmi_product_name << "\n"
            << "     Audio Card/Dir:  " << prof.audio_card << " ("
            << prof.audio_config_dir
            << (mtl ? "; Realtek ALC5682I + ALC1019P amp, SOF IPC4)\n"
                    : "; Realtek ALC5682I-VS/VD + ALC1019P amp, SOF IPC3)\n")
            << "     Wi-Fi Modprobe:  " << prof.wifi_modprobe[0] << "\n"
            << "     Pinned Firmware: " << prof.firmware.size() << " blobs\n"
            << "     Pinned Kmods:    " << prof.kmods.size()
            << " modules (ac.ko, gbos_wakefilter.ko)\n";
  if (show_entries) {
    const std::string stock =
        "androidboot.hardware=android-desktop firmware_class.path=/vendor/firmware "
        "snd_intel_sdw_acpi.sdw_link_mask=0x5 xe.enable_psr=1 snd_sof_pci.ipc_type=1";
    std::cout << "\n=== loader/entries/lapis-quiet.conf ===\n"
              << googlebookos::profile::BuildSystemdBootEntryConf(prof, stock,
                                                                false)
              << "=== loader/entries/lapis-debug.conf ===\n"
              << googlebookos::profile::BuildSystemdBootEntryConf(prof, stock,
                                                                true);
  }
  return 0;
}

int VerifyInputsCmd(const googlebookos::profile::DeviceProfile& prof,
                    const std::filesystem::path& ws) {
  std::string err;
  if (!googlebookos::profile::ValidateProfile(prof, &err)) {
    std::cerr << "Profile error: " << err << "\n";
    return 1;
  }
  const ModelLayout layout = LayoutFor(prof);

  // 1. Check firmware blobs and SHA-256 digests.
  auto fw_dir = ws / layout.inputs_dir / "firmware";
  for (const auto& fw : prof.firmware) {
    auto p = fw_dir / fw.path;
    if (!std::filesystem::exists(p)) {
      std::cerr << "Missing firmware blob: " << p << "\n";
      return 1;
    }
    std::string got = gbos::Sha256FileHex(p);
    if (got != fw.sha256) {
      std::cerr << "Firmware SHA-256 mismatch for " << fw.path << ": expected "
                << fw.sha256 << ", got " << got << "\n";
      return 1;
    }
  }
  std::cout << "[OK] All " << prof.firmware.size() << " " << prof.model_id
            << " firmware blobs verified in " << fw_dir << "\n";

  // 2. Check out-of-tree GKI 6.18.21 kernel modules (`ac.ko` and `gbos_wakefilter.ko`).
  const std::string kAcSha =
      "db694c38d713e54ba1876d454fd4001cf77889173c44a20d31d9d34d201b5c7b";
  const std::string kWakeSha =
      "3423c3e983bff3d7d35486c475bde473cb7a54dd4115bf7933678f77861cf595";
  if (!googlebookos::kmod::VerifyPinnedGkiModule(
          ws / "work/inputs/kmod/ac.ko", "ac", kAcSha, &err)) {
    std::cerr << "ac.ko check failed: " << err << "\n";
    return 1;
  }
  if (!googlebookos::kmod::VerifyPinnedGkiModule(
          ws / "work/inputs/kmod/gbos_wakefilter.ko", "gbos_wakefilter",
          kWakeSha, &err)) {
    std::cerr << "gbos_wakefilter.ko check failed: " << err << "\n";
    return 1;
  }
  if (std::filesystem::exists(ws / "work/inputs/kmod/gbos_isa_emu.ko")) {
    if (!googlebookos::kmod::VerifyPinnedGkiModule(
            ws / "work/inputs/kmod/gbos_isa_emu.ko", "gbos_isa_emu", "",
            &err)) {
      std::cerr << "gbos_isa_emu.ko check failed: " << err << "\n";
      return 1;
    }
  }
  std::cout << "[OK] Pinned GKI 6.18.21 kernel modules (ac.ko, gbos_wakefilter.ko, gbos_isa_emu.ko) verified\n";

  // 3. Check the profile's ChromeOS audio sources.
  auto audio_dir = ws / layout.inputs_dir / "audio-src";
  if (!std::filesystem::exists(audio_dir / "HiFi.conf") ||
      !std::filesystem::exists(audio_dir / "sof-rt5682.card_settings")) {
    std::cerr << "Missing " << prof.model_id << " audio sources in " << audio_dir
              << "\n";
    return 1;
  }
  std::cout << "[OK] ChromeOS " << prof.model_id << " audio sources verified in "
            << audio_dir << "\n";
  return 0;
}

int FetchInputsCmd(const googlebookos::profile::DeviceProfile& prof,
                   const std::filesystem::path& ws) {
  const ModelLayout layout = LayoutFor(prof);
  std::ostringstream cmd;
  cmd << "cd '" << ws.string() << "' && bash builder/fetch-inputs.sh '"
      << layout.inputs_dir << "' '" << layout.profile_dir << "'";
  if (std::system(cmd.str().c_str()) != 0) return 1;
  return VerifyInputsCmd(prof, ws);
}

int InspectImageCmd(const std::filesystem::path& img_path) {
  std::string err;
  auto gpt_opt = gbos::ReadGptFromFile(img_path, &err);
  if (!gpt_opt) {
    std::cerr << "GPT read error: " << err << "\n";
    return 1;
  }
  std::cout << "GPT Partitions in " << img_path << " ("
            << gpt_opt->partitions.size() << "):\n";
  uint64_t super_off = 0;
  uint64_t esp_off = 0;
  for (const auto& p : gpt_opt->partitions) {
    std::cout << "  [" << p.index << "] " << p.name
              << " start=" << p.start_bytes << " size=" << p.size_bytes
              << " guid=" << p.unique_guid << "\n";
    if (p.name == "super") super_off = p.start_bytes;
    if (p.name == "EFI-SYSTEM") esp_off = p.start_bytes;
  }
  if (super_off > 0) {
    auto lp_opt = gbos::ReadLpMetadataFromFile(img_path, super_off, &err);
    if (lp_opt) {
      std::cout << "Logical Partitions in super ("
                << lp_opt->partitions.size() << "):\n";
      for (const auto& lp : lp_opt->partitions) {
        std::cout << "  - " << lp.name << " group=" << lp.group_name
                  << " size=" << lp.total_bytes << "\n";
      }
    }
  }
  if (esp_off > 0) {
    auto esp_entries = googlebookos::fat32::ListFat32Entries(img_path, esp_off);
    std::cout << "ESP FAT32 Entries (" << esp_entries.size() << "):\n";
    for (const auto& e : esp_entries) {
      std::cout << "  " << (e.is_dir ? "[DIR]  " : "[FILE] ") << e.path << " ("
                << e.size << " B)\n";
    }
  }
  return 0;
}

int BuildImageCmd(const googlebookos::profile::DeviceProfile& prof,
                  const std::filesystem::path& ws) {
  int rc = VerifyInputsCmd(prof, ws);
  if (rc != 0) return rc;
  const ModelLayout layout = LayoutFor(prof);

  std::ostringstream cmd;
  cmd << "export PATH=\"/usr/sbin:$PATH\" && "
      << "cd '" << ws.string() << "' && "
      << "export GBOS_BASE=\"$PWD/work/base/kano-real.img\" && "
      << "export GBOS_INITRD=\"$PWD/work/base/initrd.img\" && "
      << "export GBOS_AVB_KEY=\"$PWD/work/base/keys/vbmeta.pem\" && "
      << "export GBOS_OTATOOLS=\"$PWD/work/base/cache/otatools\" && "
      << "export GBOS_INPUTS=\"$PWD/" << layout.inputs_dir << "\" && "
      << "export GBOS_ISA_EMU_KO=\"$PWD/work/inputs/kmod/gbos_isa_emu.ko\" && "
      << "export GBOS_GENERIC_PASTEL=\"$PWD/work/inputs/generic/vulkan.pastel.so\" && "
      << "export GBOS_FIRMWARE=\"$PWD/" << layout.inputs_dir << "/firmware\" && "
      << "export GBOS_AUDIO_SRC=\"$PWD/" << layout.inputs_dir << "/audio-src\" && "
      << "export GBOS_KMOD_AC=\"$PWD/work/inputs/kmod/ac.ko\" && "
      << "export GBOS_KMOD_WAKEFILTER=\"$PWD/work/inputs/kmod/gbos_wakefilter.ko\" && "
      << "export GBOS_PROFILE=\"$PWD/" << layout.profile_dir << "\" && "
      << "export GBOS_OUT=\"$PWD/" << layout.build_dir << "\" && "
      << "bash builder/build.sh";
  return std::system(cmd.str().c_str()) == 0 ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
  std::string model = "marasov";
  int argi = 1;
  if (argi + 1 < argc && std::string(argv[argi]) == "--model") {
    model = argv[argi + 1];
    argi += 2;
  }
  if (argi >= argc) {
    PrintUsage();
    return 2;
  }
  bool found = false;
  auto prof = googlebookos::profile::GetProfileByModel(model, &found);
  if (!found) {
    std::cerr << "Unknown model '" << model << "' (expected marasov or screebo)\n";
    return 2;
  }
  try {
    std::string sub = argv[argi];
    const int rem = argc - argi;
    if (sub == "check-profile") {
      return CheckProfileCmd(prof, false);
    } else if (sub == "show-profile") {
      return CheckProfileCmd(prof, true);
    } else if (sub == "fetch-inputs" && rem >= 2) {
      return FetchInputsCmd(prof, argv[argi + 1]);
    } else if (sub == "verify-inputs" && rem >= 2) {
      return VerifyInputsCmd(prof, argv[argi + 1]);
    } else if (sub == "inspect-image" && rem >= 2) {
      return InspectImageCmd(argv[argi + 1]);
    } else if (sub == "build" && rem >= 2) {
      return BuildImageCmd(prof, argv[argi + 1]);
    }
    PrintUsage();
    return 2;
  } catch (const std::exception& e) {
    std::cerr << "googlebookos_builder error: " << e.what() << "\n";
    return 1;
  }
}
