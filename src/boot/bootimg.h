#ifndef GOOGLEBOOKOS_SRC_BOOT_BOOTIMG_H_
#define GOOGLEBOOKOS_SRC_BOOT_BOOTIMG_H_

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace gbos {

struct VbmetaInfo {
  std::string sha256_hex;
  uint64_t total_size = 0;
};

struct CpioEntry {
  uint32_t ino = 0;
  uint32_t mode = 0100644;
  uint32_t uid = 0;
  uint32_t gid = 0;
  uint32_t nlink = 1;
  uint32_t mtime = 0;
  uint32_t devmajor = 0;
  uint32_t devminor = 0;
  uint32_t rdevmajor = 0;
  uint32_t rdevminor = 0;
  std::string name;
  std::vector<uint8_t> data;
};

// Computes SHA-256 digest and struct size (256 + auth + aux) of an AVB0 vbmeta image.
std::optional<VbmetaInfo> ParseVbmetaBuffer(std::span<const uint8_t> buf,
                                            std::string* error = nullptr);
std::optional<VbmetaInfo> ParseVbmetaFile(const std::filesystem::path& path,
                                          std::string* error = nullptr);

// Appends an Android `#BOOTCONFIG\n` trailer to `archive_bytes` with `bootconfig_text`.
std::vector<uint8_t> AppendBootconfigTrailer(std::span<const uint8_t> archive_bytes,
                                             std::string_view bootconfig_text);

// Splits an initrd containing a `#BOOTCONFIG\n` trailer into (archive_bytes, bootconfig_text)
// and verifies the 32-bit additive checksum.
bool SplitBootconfigTrailer(std::span<const uint8_t> initrd_bytes,
                            std::vector<uint8_t>* archive_out,
                            std::string* bootconfig_out,
                            std::string* error = nullptr);

// Builds an initrd file from CPIO archives, base bootconfig file, and extra key=value parameters.
bool MakeInitrdFile(const std::filesystem::path& out_path,
                    const std::filesystem::path& base_bootconfig_path,
                    std::span<const std::filesystem::path> cpio_paths,
                    std::span<const std::string> extra_bootconfig_kv,
                    std::string* error = nullptr);

// Parses a single `070701` newc CPIO archive into entries (excluding TRAILER!!!).
bool ParseCpioNewc(std::span<const uint8_t> cpio_bytes,
                   std::vector<CpioEntry>* entries_out,
                   size_t* bytes_consumed = nullptr,
                   std::string* error = nullptr);

// Serializes CPIO entries into a `070701` newc CPIO archive ending with `TRAILER!!!`
// and 4-byte alignment padding.
std::vector<uint8_t> BuildCpioNewc(std::span<const CpioEntry> entries);

// Modifies a first-stage ramdisk CPIO archive by injecting `pci_stub_ko` at
// `lib/modules/pci-stub.ko`, prepending `pci-stub.ko` to `lib/modules/modules.load`,
// and dropping `first_stage_drop` modules from `lib/modules/modules.load`.
bool PatchFirstStageRamdiskForPciStub(
    std::span<const uint8_t> ramdisk_cpio,
    std::span<const uint8_t> pci_stub_ko,
    std::span<const std::string> first_stage_drop,
    std::vector<uint8_t>* patched_cpio_out,
    std::string* error = nullptr);

// Extracts `kernel.efi`, `vendor_platform.cpio`, `vendor_dlkm.cpio`, `init_boot.cpio`,
// `vendor_cmdline.txt`, `vendor_bootconfig.txt`, and `vbmeta_a.orig.img` from a GPT disk image.
bool ExtractBootArtifacts(const std::filesystem::path& img_path,
                          const std::filesystem::path& out_dir,
                          std::string* error = nullptr);

}  // namespace gbos

#endif  // GOOGLEBOOKOS_SRC_BOOT_BOOTIMG_H_
