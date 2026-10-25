#ifndef GOOGLEBOOKOS_SRC_COMMON_LZ4_FRAME_H_
#define GOOGLEBOOKOS_SRC_COMMON_LZ4_FRAME_H_

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <span>
#include <string>
#include <vector>

namespace gbos {

inline constexpr uint32_t kLz4FrameMagic = 0x184D2204U;
inline constexpr uint32_t kLz4LegacyMagic = 0x184C2102U;

// Decompresses a single raw LZ4 block into `out` (appending to `out`, so `out` may already
// contain previous history for dependent blocks). Returns false on corrupt input.
bool Lz4DecompressBlock(std::span<const uint8_t> src,
                        std::vector<uint8_t>* out,
                        std::string* error = nullptr);

// Compresses a single raw LZ4 block.
std::vector<uint8_t> Lz4CompressBlock(std::span<const uint8_t> src);

// Decompresses one or more concatenated LZ4 frames (Standard 0x184D2204 or Legacy 0x184C2102).
bool Lz4DecompressFrame(std::span<const uint8_t> src,
                        std::vector<uint8_t>* out,
                        std::string* error = nullptr);

// Compresses `src` into an LZ4 Standard Frame (or Legacy 0x184C2102 frame if `legacy == true`).
std::vector<uint8_t> Lz4CompressFrame(std::span<const uint8_t> src,
                                      bool legacy = false,
                                      bool include_content_size = false);

// Convenience file helpers.
bool Lz4DecompressFile(const std::filesystem::path& in_path,
                       const std::filesystem::path& out_path,
                       std::string* error = nullptr);

bool Lz4CompressFile(const std::filesystem::path& in_path,
                     const std::filesystem::path& out_path,
                     bool legacy = false,
                     std::string* error = nullptr);

}  // namespace gbos

#endif  // GOOGLEBOOKOS_SRC_COMMON_LZ4_FRAME_H_
