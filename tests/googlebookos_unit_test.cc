#include <cassert>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "src/audio/marasov_audio.h"
#include "src/boot/bootimg.h"
#include "src/common/lz4_frame.h"
#include "src/common/sha256.h"
#include "src/erofs/erofs.h"
#include "src/fat32/fat32.h"
#include "src/fetch/http_zip.h"
#include "src/gpt/gpt.h"
#include "src/installer/googlebookos_installer.h"
#include "src/kmod/kmod_verifier.h"
#include "src/lp/lp_metadata.h"
#include "src/profile/googlebookos_profile.h"
#include "src/vendor/vendor_patcher.h"

namespace {

void TestSha256() {
  std::string empty_sha = gbos::Sha256Hex(std::string_view(""));
  assert(empty_sha ==
         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  std::string abc_sha = gbos::Sha256Hex(std::string_view("abc"));
  assert(abc_sha ==
         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  std::cout << "[PASS] TestSha256\n";
}

void TestLz4StandardAndLegacy() {
  std::string payload =
      "ASUS Chromebook Plus CX34 (Model CX3402, 128GB UFS, i3-1215U) "
      "marasov brya sof-rt5682 iwlmvm ufshcd-pci ";
  for (int i = 0; i < 6; ++i) payload += payload;
  std::vector<uint8_t> raw(payload.begin(), payload.end());

  // 1. Standard frame round-trip
  auto std_frame = gbos::Lz4CompressFrame(raw, false, true);
  std::vector<uint8_t> dec_std;
  assert(gbos::Lz4DecompressFrame(std_frame, &dec_std));
  assert(dec_std == raw);

  // 2. Legacy frame round-trip
  auto leg_frame = gbos::Lz4CompressFrame(raw, true, false);
  std::vector<uint8_t> dec_leg;
  assert(gbos::Lz4DecompressFrame(leg_frame, &dec_leg));
  assert(dec_leg == raw);

  std::cout << "[PASS] TestLz4StandardAndLegacy\n";
}

void TestBootconfigTrailer() {
  std::vector<uint8_t> initrd = {'C', 'P', 'I', 'O', '_', 'D', 'A', 'T', 'A'};
  std::string params =
      "androidboot.hardware=android-desktop\n"
      "androidboot.hypervisor.protected_vm.supported=0\n";
  auto with_bc = gbos::AppendBootconfigTrailer(initrd, params);
  std::vector<uint8_t> split_arch;
  std::string split_bc;
  assert(gbos::SplitBootconfigTrailer(with_bc, &split_arch, &split_bc));
  assert(split_arch == initrd);
  assert(split_bc.find("androidboot.hardware=android-desktop") !=
         std::string::npos);
  std::cout << "[PASS] TestBootconfigTrailer\n";
}

void TestFat32BuilderAndReader() {
  auto tmp = std::filesystem::temp_directory_path() / "gbos_googlebookos_fat32_test.img";
  std::vector<googlebookos::fat32::FatFileInput> files = {
      {"EFI/BOOT/BOOTX64.EFI", {'M', 'Z', 0x90, 0x00}},
      {"loader/loader.conf",
       {'d', 'e', 'f', 'a', 'u', 'l', 't', ' ', 'l', 'a', 'p', 'i', 's', '-',
        'q', 'u', 'i', 'e', 't', '.', 'c', 'o', 'n', 'f', '\n'}},
      {"loader/entries/lapis-quiet.conf",
       {'t', 'i', 't', 'l', 'e', ' ', 'C', 'X', '3', '4', '0', '2', '\n'}},
  };
  auto img = googlebookos::fat32::BuildFat32Image(
      36ULL * 1024 * 1024, "GBOS-ESP", 0x3402CAFE, files);
  {
    std::ofstream out(tmp, std::ios::binary);
    out.write(reinterpret_cast<const char*>(img.data()),
              static_cast<std::streamsize>(img.size()));
  }
  auto entries = googlebookos::fat32::ListFat32Entries(tmp, 0);
  assert(entries.size() >= 5);
  auto read_loader =
      googlebookos::fat32::ReadFat32File(tmp, 0, "loader/loader.conf");
  std::string loader_str(read_loader.begin(), read_loader.end());
  assert(loader_str == "default lapis-quiet.conf\n");
  std::filesystem::remove(tmp);
  std::cout << "[PASS] TestFat32BuilderAndReader\n";
}

void TestDeviceProfileAndCmdline() {
  auto prof = googlebookos::profile::GetDefaultProfile();
  std::string err;
  assert(googlebookos::profile::ValidateMarasovProfile(prof, &err));
  assert(prof.model_id == "marasov");
  assert(prof.form_factor == "clamshell-180");
  assert(prof.image_name == "googlebook-cx3402.img");
  assert(prof.audio_card == "sof-rt5682");
  assert(prof.audio_config_dir == "sof-rt5682_marasov");

  std::string stock =
      "console=ttyS0,115200n8 androidboot.hardware=android-desktop "
      "snd_intel_sdw_acpi.sdw_link_mask=0x5 xe.enable_psr=1 snd_sof_pci.ipc_type=1";
  std::string quiet_opts =
      googlebookos::profile::BuildSystemdBootOptions(prof, stock, false);
  std::string debug_opts =
      googlebookos::profile::BuildSystemdBootOptions(prof, stock, true);

  assert(quiet_opts.find("xe.force_probe=46b3") != std::string::npos);
  assert(quiet_opts.find("snd_sof_pci.ipc_type=0") != std::string::npos);
  assert(quiet_opts.find("snd_sof_pci.ipc_type=1") == std::string::npos);
  assert(quiet_opts.find("keep_bootcon") == std::string::npos);
  assert(debug_opts.find("keep_bootcon") != std::string::npos);
  std::cout << "[PASS] TestDeviceProfileAndCmdline\n";
}

void TestScreeboCx5403ProfileAndCmdline() {
  bool found = false;
  auto prof = googlebookos::profile::GetProfileByModel("screebo", &found);
  assert(found);
  std::string err;
  assert(googlebookos::profile::ValidateScreeboProfile(prof, &err));
  assert(googlebookos::profile::ValidateProfile(prof, &err));
  assert(prof.model_id == "screebo");
  assert(prof.image_name == "googlebook-cx5403.img");
  assert(prof.dmi_product_name == "Screebo");
  assert(prof.audio_card == "sof-rt5682");
  assert(prof.audio_config_dir == "sof-rt5682_screebo");

  std::string stock =
      "console=ttyS0,115200n8 androidboot.hardware=android-desktop "
      "snd_intel_sdw_acpi.sdw_link_mask=0x5 xe.enable_psr=1 snd_sof_pci.ipc_type=1";
  std::string quiet_opts =
      googlebookos::profile::BuildSystemdBootOptions(prof, stock, false);
  std::string debug_opts =
      googlebookos::profile::BuildSystemdBootOptions(prof, stock, true);

  assert(quiet_opts.find("xe.force_probe=7d45") != std::string::npos);
  assert(quiet_opts.find("pci-stub.ids=8086:7e25,8086:7e26,8086:7e52,8086:7e30") !=
         std::string::npos);
  assert(quiet_opts.find("snd_sof_pci.ipc_type=1") != std::string::npos);
  assert(quiet_opts.find("snd_sof_pci.ipc_type=0") == std::string::npos);
  assert(debug_opts.find("0xfe02c000") != std::string::npos);
  std::cout << "[PASS] TestScreeboCx5403ProfileAndCmdline\n";
}

void TestMarasovAudioGenerator() {
  std::ostringstream cs;
  cs << "[Speaker]\n  volume_curve = explicit\n";
  for (int i = 100; i >= 0; --i) {
    cs << "  db_at_" << i << " = " << (-250 - (100 - i) * 50) << "\n";
  }
  cs << "[Headphone]\n  volume_curve = simple_step\n  volume_step = 70\n  max_volume = -650\n";

  std::string hifi = R"(
SectionDevice."Speaker".0 {
	Value {
		PlaybackPCM "hw:sofrt5682,0"
	}
}
SectionDevice."Headphone".0 {
	Value {
		PlaybackPCM "hw:sofrt5682,1"
	}
}
SectionDevice."Internal Mic".0 {
	Value {
		CapturePCM "hw:sofrt5682,99"
		CaptureChannelMap "2 3 -1 -1 -1 -1 -1 -1 -1 -1 -1"
	}
}
)";
  auto parsed = googlebookos::audio::ParseMarasovAudioSources(hifi, cs.str());
  assert(parsed.speaker_db_at_pct.size() == 101);
  assert(parsed.speaker_db_at_pct.at(100) == -250);
  assert(parsed.headphone_volume_step == 70);
  assert(parsed.headphone_max_volume == -650);

  auto xmls = googlebookos::audio::GenerateMarasovDrasXmls(parsed);
  assert(xmls.audio_config_xml.find("<SoundCardName>hw:sofrt5682</SoundCardName>") !=
         std::string::npos);
  assert(xmls.audio_config_xml.find(
             "<CaptureChannelMap>2 3 -1 -1 -1 -1 -1 -1 -1 -1 -1</CaptureChannelMap>") !=
         std::string::npos);
  assert(xmls.mixer_paths_xml.find("Left Spk Switch") != std::string::npos);
  assert(xmls.mixer_paths_xml.find("Stereo1 DAC MIXL DAC L1 Switch") !=
         std::string::npos);

  auto wav = googlebookos::audio::GenerateTestToneWav1kHz(1);
  assert(wav.size() == 44 + 48000 * 4);
  assert(std::memcmp(wav.data(), "RIFF", 4) == 0);

  std::string screebo_hifi = R"(
SectionDevice."Speaker".0 {
	Value {
		PlaybackPCM "hw:sofrt5682,1"
	}
}
SectionDevice."Headphone".0 {
	Value {
		PlaybackPCM "hw:sofrt5682,0"
	}
}
SectionDevice."Bluetooth SCO".0 {
	Value {
		PlaybackPCM "hw:sofrt5682,2"
	}
}
SectionDevice."Internal Mic".0 {
	Value {
		CapturePCM "hw:sofrt5682,99"
		CaptureChannelMap "2 3 -1 -1 -1 -1 -1 -1 -1 -1 -1"
	}
}
)";
  auto sp = googlebookos::audio::ParseScreeboAudioSources(screebo_hifi, cs.str());
  assert(sp.speaker_pcm == "hw:sofrt5682,1");
  assert(sp.headphone_pcm == "hw:sofrt5682,0");
  assert(sp.internal_mic_sensitivity == -2300);
  auto sxml = googlebookos::audio::GenerateScreeboDrasXmls(sp);
  assert(sxml.audio_config_xml.find("<PlaybackPCM>hw:sofrt5682,1</PlaybackPCM>") !=
         std::string::npos);
  assert(sxml.audio_config_xml.find("<IntrinsicSensitivity>-2300</IntrinsicSensitivity>") !=
         std::string::npos);
  assert(sxml.mixer_paths_xml.find("Dmic0 Capture Switch") !=
         std::string::npos);
  std::cout << "[PASS] TestMarasovAudioGenerator (marasov + screebo)\n";
}

void TestInstallerPartitionFormattingAndDmi() {
  assert(googlebookos::installer::FormatPartitionDevicePath("/dev/sda", 1) ==
         "/dev/sda1");
  assert(googlebookos::installer::FormatPartitionDevicePath("/dev/sda", 13) ==
         "/dev/sda13");
  assert(googlebookos::installer::FormatPartitionDevicePath("/dev/nvme0n1", 13) ==
         "/dev/nvme0n1p13");
  assert(googlebookos::installer::FormatPartitionDevicePath("/dev/mmcblk0", 13) ==
         "/dev/mmcblk0p13");

  auto tmp_dmi = std::filesystem::temp_directory_path() / "gbos_dmi_test";
  std::filesystem::create_directories(tmp_dmi);
  {
    std::ofstream(tmp_dmi / "sys_vendor") << "Google\n";
    std::ofstream(tmp_dmi / "product_name") << "Marasov\n";
    std::ofstream(tmp_dmi / "board_name") << "Marasov\n";
  }
  auto id = googlebookos::installer::ProbeDmiIdentity(tmp_dmi);
  assert(id.is_marasov);
  assert(!id.is_screebo);
  {
    std::ofstream(tmp_dmi / "product_name") << "Screebo\n";
    std::ofstream(tmp_dmi / "board_name") << "Screebo\n";
  }
  auto id2 = googlebookos::installer::ProbeDmiIdentity(tmp_dmi);
  assert(id2.is_screebo);
  assert(!id2.is_marasov);
  std::filesystem::remove_all(tmp_dmi);
  std::cout << "[PASS] TestInstallerPartitionFormattingAndDmi\n";
}

}  // namespace

int main() {
  TestSha256();
  TestLz4StandardAndLegacy();
  TestBootconfigTrailer();
  TestFat32BuilderAndReader();
  TestDeviceProfileAndCmdline();
  TestScreeboCx5403ProfileAndCmdline();
  TestMarasovAudioGenerator();
  TestInstallerPartitionFormattingAndDmi();
  std::cout << "ALL GOOGLEBOOKOS C++ UNIT TESTS PASSED\n";
  return 0;
}
