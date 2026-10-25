#ifndef GOOGLEBOOKOS_SRC_FAT32_FAT32_H_
#define GOOGLEBOOKOS_SRC_FAT32_FAT32_H_

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace googlebookos::fat32 {

struct FatFileInput {
  std::string path;               // e.g. "EFI/BOOT/BOOTX64.EFI"
  std::vector<uint8_t> content;
};

struct FatEntryInfo {
  std::string path;
  bool is_dir = false;
  uint32_t size = 0;
  uint32_t first_cluster = 0;
};

// Builds a deterministic FAT32 image with 4 KiB clusters (8 sectors/cluster)
// containing the specified directories and files with VFAT long file names.
std::vector<uint8_t> BuildFat32Image(uint64_t total_size_bytes,
                                     const std::string& volume_label,
                                     uint32_t volume_id,
                                     const std::vector<FatFileInput>& files);

// Writes a FAT32 partition directly into a disk image at `partition_offset`.
void WriteFat32Partition(const std::filesystem::path& disk_path,
                         uint64_t partition_offset,
                         uint64_t partition_size_bytes,
                         const std::string& volume_label, uint32_t volume_id,
                         const std::vector<FatFileInput>& files);

// Walks a FAT32 image (or slice of a disk image) and returns all directory/file entries.
std::vector<FatEntryInfo> ListFat32Entries(const std::filesystem::path& disk_path,
                                           uint64_t partition_offset = 0);

// Reads a file from a FAT32 image by path (case-insensitive).
std::vector<uint8_t> ReadFat32File(const std::filesystem::path& disk_path,
                                   uint64_t partition_offset,
                                   const std::string& file_path);

}  // namespace googlebookos::fat32

#endif  // GOOGLEBOOKOS_SRC_FAT32_FAT32_H_
