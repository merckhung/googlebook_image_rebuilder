#ifndef GOOGLEBOOKOS_SRC_GPT_GPT_H_
#define GOOGLEBOOKOS_SRC_GPT_GPT_H_

#include <cstdint>
#include <filesystem>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace gbos {

struct GptPartitionEntry {
  uint32_t index = 0;  // 1-based partition index
  std::string name;
  std::string type_guid;
  std::string unique_guid;
  uint64_t first_lba = 0;
  uint64_t last_lba = 0;
  uint64_t start_bytes = 0;
  uint64_t size_bytes = 0;
  uint64_t attributes = 0;
};

struct GptTable {
  std::string disk_guid;
  uint64_t current_lba = 0;
  uint64_t backup_lba = 0;
  uint64_t first_usable_lba = 0;
  uint64_t last_usable_lba = 0;
  uint64_t entries_lba = 0;
  uint32_t num_entries = 0;
  uint32_t entry_size = 0;
  uint32_t header_crc32 = 0;
  uint32_t entries_crc32 = 0;
  bool header_crc_valid = false;
  bool entries_crc_valid = false;
  std::vector<GptPartitionEntry> partitions;

  const GptPartitionEntry* FindPartition(std::string_view part_name) const;
};

// Formats a 16-byte mixed-endian GUID into lowercase UUID string.
std::string FormatGuid(std::span<const uint8_t> guid_bytes);

// Parses a lowercase UUID string into 16 mixed-endian GPT GUID bytes.
bool ParseGuid(std::string_view uuid_str, uint8_t out_guid[16]);

// Parses the primary GPT from an in-memory buffer containing at least the first
// 34 LBAs (17408 bytes).
std::optional<GptTable> ParseGptBuffer(std::span<const uint8_t> buf,
                                       std::string* error = nullptr);

// Reads and validates the primary GPT from a disk image file.
std::optional<GptTable> ReadGptFromFile(const std::filesystem::path& img_path,
                                        std::string* error = nullptr);

// Builds a valid GPT header + partition table in `disk_buf` (which must be at least
// 34 * 512 bytes, and if large enough for the backup GPT at the end of `disk_buf`,
// writes the backup GPT too).
bool WriteGptToBuffer(std::span<uint8_t> disk_buf,
                      std::string_view disk_guid,
                      std::span<const GptPartitionEntry> partitions,
                      std::string* error = nullptr);

// Writes the `layout.env` file (`ESP_OFF`, `SUPER_OFF`, `VBMETA_OFF`, `BOOT_OFF`,
// `VENDOR_BOOT_OFF`, `INIT_BOOT_OFF`, and their `_SIZE` and `_GUID` variables).
bool WriteLayoutEnv(const std::filesystem::path& img_path,
                    const std::filesystem::path& out_env,
                    std::string* error = nullptr);

// Splices `part_file` into `img_path` at `offset` within a partition slot of `slot_size`
// bytes, zero-filling the remainder of the slot. Refuses symlinks or non-regular files.
bool SplicePartition(const std::filesystem::path& img_path,
                     uint64_t offset,
                     uint64_t slot_size,
                     const std::filesystem::path& part_file,
                     std::string* error = nullptr);

}  // namespace gbos

#endif  // GOOGLEBOOKOS_SRC_GPT_GPT_H_
