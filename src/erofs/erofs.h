#ifndef GOOGLEBOOKOS_SRC_EROFS_EROFS_H_
#define GOOGLEBOOKOS_SRC_EROFS_EROFS_H_

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <span>
#include <string>
#include <string_view>

namespace gbos {

struct ErofsEntryMeta {
  uint32_t mode = 0;  // Low 12 permission bits (07777)
  uint32_t uid = 0;
  uint32_t gid = 0;
  std::string selinux;
  uint64_t caps = 0;
};

struct ErofsImageMeta {
  std::string uuid;
  uint32_t block_size = 4096;
  uint16_t root_nid = 0;
  std::map<std::string, ErofsEntryMeta> entries;  // Keyed by relative path ("" for root)
};

// Reads the original pre-footer filesystem size from an AVB-footed partition image (`AVBf`).
std::optional<uint64_t> ReadAvbOriginalImageSize(std::span<const uint8_t> tail_64,
                                                 std::string* error = nullptr);
std::optional<uint64_t> ReadAvbOriginalImageSizeFromFile(
    const std::filesystem::path& img_path,
    std::string* error = nullptr);

// Reads the 16-byte UUID from an EROFS superblock at offset 1024.
std::optional<std::string> ReadErofsUuid(std::span<const uint8_t> erofs_head,
                                         std::string* error = nullptr);
std::optional<std::string> ReadErofsUuidFromFile(
    const std::filesystem::path& erofs_path,
    std::string* error = nullptr);

// Walks an EROFS image in memory and extracts uid/gid/mode/selinux/capabilities for every inode.
std::optional<ErofsImageMeta> WalkErofsImageBuffer(std::span<const uint8_t> data,
                                                   std::string* error = nullptr);
std::optional<ErofsImageMeta> WalkErofsImageFile(const std::filesystem::path& erofs_path,
                                                 std::string* error = nullptr);

// Writes `file_contexts` and `fs_config` for `mkfs.erofs --mount-point <mount_point>`.
bool WriteErofsConfigs(std::string_view mount_point,
                       const std::map<std::string, ErofsEntryMeta>& entries,
                       const std::filesystem::path& file_contexts_out,
                       const std::filesystem::path& fs_config_out,
                       std::string* error = nullptr);

}  // namespace gbos

#endif  // GOOGLEBOOKOS_SRC_EROFS_EROFS_H_
