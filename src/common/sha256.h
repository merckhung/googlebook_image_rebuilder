#ifndef GOOGLEBOOKOS_SRC_COMMON_SHA256_H_
#define GOOGLEBOOKOS_SRC_COMMON_SHA256_H_

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace gbos {

// Computes lowercase hex SHA-256 of a memory buffer.
std::string Sha256Hex(std::span<const uint8_t> data);
std::string Sha256Hex(std::string_view data);

// Computes raw 32-byte SHA-256 of a memory buffer.
std::vector<uint8_t> Sha256Raw(std::span<const uint8_t> data);

// Computes lowercase hex SHA-256 of a file on disk. Returns empty string on I/O error.
std::string Sha256FileHex(const std::filesystem::path& path,
                          uint64_t offset = 0,
                          uint64_t length = 0);

// Computes lowercase hex SHA-1 of a memory buffer or file (used for AVB public key fingerprint).
std::string Sha1Hex(std::span<const uint8_t> data);
std::string Sha1FileHex(const std::filesystem::path& path);

// Verifies that a file's SHA-256 matches expected_hex (case-insensitive).
bool VerifyFileSha256(const std::filesystem::path& path,
                      std::string_view expected_hex,
                      std::string* actual_hex = nullptr);

// Incremental SHA-256 hasher.
class Sha256Hasher {
 public:
  Sha256Hasher();
  ~Sha256Hasher();
  Sha256Hasher(const Sha256Hasher&) = delete;
  Sha256Hasher& operator=(const Sha256Hasher&) = delete;

  void Update(const void* data, size_t len);
  void Update(std::span<const uint8_t> data) { Update(data.data(), data.size()); }
  std::vector<uint8_t> FinalizeRaw();
  std::string FinalizeHex();

 private:
  void* ctx_ = nullptr;
};

// Standard IEEE 802.3 CRC32 (used by GPT headers and partition tables).
uint32_t Crc32(std::span<const uint8_t> data, uint32_t initial = 0);

// xxHash32 (used by LZ4 frame format).
uint32_t XxHash32(std::span<const uint8_t> data, uint32_t seed = 0);

}  // namespace gbos

#endif  // GOOGLEBOOKOS_SRC_COMMON_SHA256_H_
