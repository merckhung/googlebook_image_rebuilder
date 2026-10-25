#ifndef GOOGLEBOOKOS_SRC_LP_LP_METADATA_H_
#define GOOGLEBOOKOS_SRC_LP_LP_METADATA_H_

#include <cstdint>
#include <filesystem>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace gbos {

struct LpExtent {
  uint64_t num_sectors = 0;
  uint32_t target_type = 0;  // 0 = LP_TARGET_TYPE_LINEAR
  uint64_t target_data = 0;  // physical 512-byte sector in super
  uint32_t target_source = 0;
};

struct LpPartition {
  std::string name;
  uint32_t attributes = 0;
  uint32_t first_extent_index = 0;
  uint32_t num_extents = 0;
  uint32_t group_index = 0;
  std::string group_name;
  uint64_t total_bytes = 0;
};

struct LpGroup {
  std::string name;
  uint32_t flags = 0;
  uint64_t maximum_size = 0;
};

struct LpBlockDevice {
  uint64_t first_logical_sector = 0;
  uint32_t alignment = 0;
  uint32_t alignment_offset = 0;
  uint64_t size = 0;
  std::string partition_name;
  uint32_t flags = 0;
};

struct LpMetadata {
  uint32_t metadata_max_size = 0;
  uint32_t metadata_slot_count = 0;
  uint32_t logical_block_size = 0;
  uint16_t major_version = 0;
  uint16_t minor_version = 0;
  std::vector<LpPartition> partitions;
  std::vector<LpExtent> extents;
  std::vector<LpGroup> groups;
  std::vector<LpBlockDevice> block_devices;

  const LpPartition* FindPartition(std::string_view part_name) const;
};

// Parses LP metadata from a buffer starting at `super` offset 0 (needs at least 16 KiB).
std::optional<LpMetadata> ParseLpMetadataBuffer(std::span<const uint8_t> super_head,
                                                std::string* error = nullptr);

// Reads LP metadata from `img_path` at `super_offset`.
std::optional<LpMetadata> ReadLpMetadataFromFile(const std::filesystem::path& img_path,
                                                 uint64_t super_offset,
                                                 std::string* error = nullptr);

// Extracts a single logical partition (`part_name`, e.g., "vendor_a") from `img_path`
// at `super_offset` to `out_file`.
bool ExtractLogicalPartition(const std::filesystem::path& img_path,
                             uint64_t super_offset,
                             std::string_view part_name,
                             const std::filesystem::path& out_file,
                             std::string* error = nullptr);

// Extracts all non-empty logical partitions from `super` into `out_dir` and writes `lp.json`.
bool ExtractAllLogicalPartitions(const std::filesystem::path& img_path,
                                 uint64_t super_offset,
                                 const std::filesystem::path& out_dir,
                                 std::string* error = nullptr);

}  // namespace gbos

#endif  // GOOGLEBOOKOS_SRC_LP_LP_METADATA_H_
