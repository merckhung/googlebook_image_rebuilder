#include "src/vendor/vendor_patcher.h"

#include <algorithm>
#include <fstream>
#include <regex>
#include <sstream>
#include <stdexcept>

#include "src/audio/marasov_audio.h"

namespace googlebookos::vendor {
namespace {

std::string ReadFileText(const std::filesystem::path& p) {
  std::ifstream f(p);
  if (!f) throw std::runtime_error("Cannot read file: " + p.string());
  std::ostringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

void WriteFileText(const std::filesystem::path& p, const std::string& s) {
  std::filesystem::create_directories(p.parent_path());
  std::ofstream f(p, std::ios::binary | std::ios::trunc);
  if (!f) throw std::runtime_error("Cannot write file: " + p.string());
  f.write(s.data(), static_cast<std::streamsize>(s.size()));
}

}  // namespace

std::vector<std::string> GetVendorRemovedPaths() {
  return {
      "bin/fwupd-binder",
      "bin/fwupd_blocking_update",
      "bin/fwupdmgr",
      "bin/fw-updater-wrapper",
      "etc/init/fwupd.rc",
      "etc/fwupd",
      "etc/pki/fwupd",
      "etc/pki/fwupd-metadata",
      "usr/share/fwupd",
      "bin/flashrom",
      "lib64/libflashrom.so",
      "etc/init/flashrom.rc",
      "bin/futility",
      "bin/hw/vendor.google.desktop.futility-executor",
      "etc/init/futility-executor.rc",
      "etc/vintf/manifest/vendor.google.desktop.futility-executor.xml",
      "bin/ectool",
      "bin/crossystem",
      "firmware/ap-ec-fw.zip",
  };
}

VendorPatchSummary ApplyMarasovVendorPatches(
    const std::filesystem::path& vendor_tree,
    std::map<std::string, gbos::ErofsEntryMeta>* vmap,
    const googlebookos::profile::DeviceProfile& prof,
    const std::filesystem::path& firmware_dir,
    const std::filesystem::path& audio_src_dir,
    const std::filesystem::path& ac_ko_path,
    const std::filesystem::path& wakefilter_ko_path) {
  VendorPatchSummary summary;

  // 1. Patch build.prop for Alder Lake-U display & client composition.
  auto bp_path = vendor_tree / "build.prop";
  if (std::filesystem::exists(bp_path)) {
    std::string bt = ReadFileText(bp_path);
    for (const auto& [k, old_v, new_v] : prof.build_prop_set) {
      std::string target = "\n" + k + "=" + old_v + "\n";
      std::string repl = "\n" + k + "=" + new_v + "\n";
      auto pos = bt.find(target);
      if (pos != std::string::npos) {
        bt.replace(pos, target.size(), repl);
      }
    }
    auto odex_pos = bt.find("\nro.cp_system_other_odex=1\n");
    if (odex_pos != std::string::npos) {
      bt.replace(odex_pos, 26, "\nro.cp_system_other_odex=0\n");
    }
    bt += "\n# MARASOV debug: serial console shell\nro.debuggable=1\n";
    if (prof.display_hwc_backend == "client") {
      bt +=
          "# MARASOV: xe faults scanning Y-tiled buffers through DPT on ADL-U; "
          "compose everything into the client target\n"
          "vendor.hwc.backend_override=client\n";
    }
    WriteFileText(bp_path, bt);
    summary.modified_paths.push_back("build.prop");
  }

  // 2. Patch fstab.android-desktop (unencrypted f2fs /data without checkpoint=block, no USB voldmanaged).
  auto fstab_path = vendor_tree / "etc/fstab.android-desktop";
  if (std::filesystem::exists(fstab_path)) {
    std::string ft = ReadFileText(fstab_path);
    ft = std::regex_replace(
        ft, std::regex(R"((fileencryption|keydirectory|metadata_encryption)=[^,\s]*,?)"),
        "");
    ft = std::regex_replace(ft, std::regex(R"(,checkpoint=block)"), "");
    ft = std::regex_replace(
        ft, std::regex(R"( /data ext4 rw,nodev,noatime,nosuid,errors=panic )"),
        " /data f2fs nodev,noatime,nosuid,discard ");
    WriteFileText(fstab_path, ft);
    summary.modified_paths.push_back("etc/fstab.android-desktop");
  }

  // 3. Generate MARASOV DRAS audio configuration (`etc/audio/sof-rt5682_marasov/*`).
  if (std::filesystem::exists(audio_src_dir / "HiFi.conf") &&
      std::filesystem::exists(audio_src_dir / "sof-rt5682.card_settings")) {
    auto parsed = googlebookos::audio::ParseMarasovAudioSources(
        ReadFileText(audio_src_dir / "HiFi.conf"),
        ReadFileText(audio_src_dir / "sof-rt5682.card_settings"));
    auto xmls = googlebookos::audio::GenerateMarasovDrasXmls(parsed);
    std::string cdir = "etc/audio/" + prof.audio_config_dir;
    WriteFileText(vendor_tree / cdir / "audio_config.xml",
                  xmls.audio_config_xml);
    WriteFileText(vendor_tree / cdir / "mixer_paths.xml", xmls.mixer_paths_xml);
    WriteFileText(vendor_tree / cdir / "volume_curves.xml",
                  xmls.volume_curves_xml);
    summary.added_paths.push_back(cdir);
    summary.added_paths.push_back(cdir + "/audio_config.xml");
    summary.added_paths.push_back(cdir + "/mixer_paths.xml");
    summary.added_paths.push_back(cdir + "/volume_curves.xml");
  }

  // 4. Keyboard backlight LED path (`GOOG0004:00/GOOG0002:00/leds`).
  if (!prof.kbd_backlight_leds.empty()) {
    std::string ue_rel = "etc/ueventd/ueventd." + prof.model_id + ".rc";
    WriteFileText(
        vendor_tree / ue_rel,
        "# MARASOV: keyboard backlight LED (GOOG0002 under the EC node GOOG0004)\n" +
            prof.kbd_backlight_leds + "/* brightness 0660 system system\n");
    summary.added_paths.push_back(ue_rel);
  }

  // 5. Stage firmware and kernel modules if present.
  for (const auto& fw : prof.firmware) {
    auto src = firmware_dir / fw.path;
    if (std::filesystem::exists(src)) {
      auto dst = vendor_tree / "firmware" / fw.path;
      std::filesystem::create_directories(dst.parent_path());
      std::filesystem::copy_file(
          src, dst, std::filesystem::copy_options::overwrite_existing);
      summary.added_paths.push_back("firmware/" + fw.path);
    }
  }
  if (std::filesystem::exists(ac_ko_path) &&
      std::filesystem::exists(wakefilter_ko_path)) {
    std::filesystem::create_directories(vendor_tree / "lib/kano");
    std::filesystem::copy_file(
        ac_ko_path, vendor_tree / "lib/kano/ac.ko",
        std::filesystem::copy_options::overwrite_existing);
    std::filesystem::copy_file(
        wakefilter_ko_path, vendor_tree / "lib/kano/gbos_wakefilter.ko",
        std::filesystem::copy_options::overwrite_existing);
    WriteFileText(vendor_tree / "lib/kano/modules.dep",
                  "ac.ko:\ngbos_wakefilter.ko:\n");
    summary.added_paths.push_back("lib/kano/ac.ko");
    summary.added_paths.push_back("lib/kano/gbos_wakefilter.ko");
    summary.added_paths.push_back("lib/kano/modules.dep");
  }

  // 6. Remove firmware updaters and flash tools.
  for (const auto& rel : GetVendorRemovedPaths()) {
    auto p = vendor_tree / rel;
    if (std::filesystem::exists(p) || std::filesystem::is_symlink(p)) {
      std::filesystem::remove_all(p);
      summary.removed_paths.push_back(rel);
      if (vmap) vmap->erase(rel);
    }
  }

  return summary;
}

}  // namespace googlebookos::vendor
