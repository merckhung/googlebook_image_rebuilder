#include "src/common/sha256.h"

#include <openssl/evp.h>
#include <zlib.h>

#include <algorithm>
#include <cctype>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <sstream>

namespace gbos {
namespace {

std::string BytesToHex(std::span<const uint8_t> bytes) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(bytes.size() * 2);
  for (uint8_t b : bytes) {
    out.push_back(kHex[(b >> 4) & 0xF]);
    out.push_back(kHex[b & 0xF]);
  }
  return out;
}

uint32_t RotL32(uint32_t x, int r) { return (x << r) | (x >> (32 - r)); }

uint32_t ReadLE32(const uint8_t* p) {
  uint32_t v;
  std::memcpy(&v, p, sizeof(v));
  return v;
}

}  // namespace

Sha256Hasher::Sha256Hasher() {
  EVP_MD_CTX* ctx = EVP_MD_CTX_new();
  EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr);
  ctx_ = ctx;
}

Sha256Hasher::~Sha256Hasher() {
  if (ctx_) {
    EVP_MD_CTX_free(static_cast<EVP_MD_CTX*>(ctx_));
  }
}

void Sha256Hasher::Update(const void* data, size_t len) {
  if (len > 0) {
    EVP_DigestUpdate(static_cast<EVP_MD_CTX*>(ctx_), data, len);
  }
}

std::vector<uint8_t> Sha256Hasher::FinalizeRaw() {
  std::vector<uint8_t> digest(EVP_MAX_MD_SIZE);
  unsigned int len = 0;
  EVP_DigestFinal_ex(static_cast<EVP_MD_CTX*>(ctx_), digest.data(), &len);
  digest.resize(len);
  return digest;
}

std::string Sha256Hasher::FinalizeHex() {
  auto raw = FinalizeRaw();
  return BytesToHex(raw);
}

std::vector<uint8_t> Sha256Raw(std::span<const uint8_t> data) {
  Sha256Hasher h;
  h.Update(data);
  return h.FinalizeRaw();
}

std::string Sha256Hex(std::span<const uint8_t> data) {
  Sha256Hasher h;
  h.Update(data);
  return h.FinalizeHex();
}

std::string Sha256Hex(std::string_view data) {
  return Sha256Hex(std::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(data.data()), data.size()));
}

std::string Sha256FileHex(const std::filesystem::path& path,
                          uint64_t offset,
                          uint64_t length) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return "";
  if (offset > 0) {
    f.seekg(static_cast<std::streamoff>(offset), std::ios::beg);
    if (!f) return "";
  }
  Sha256Hasher h;
  std::vector<uint8_t> buf(1024 * 1024);
  uint64_t remaining = length;
  while (f) {
    size_t to_read = buf.size();
    if (length > 0 && remaining < to_read) {
      to_read = static_cast<size_t>(remaining);
    }
    if (to_read == 0) break;
    f.read(reinterpret_cast<char*>(buf.data()), static_cast<std::streamsize>(to_read));
    std::streamsize n = f.gcount();
    if (n <= 0) break;
    h.Update(buf.data(), static_cast<size_t>(n));
    if (length > 0) {
      remaining -= static_cast<uint64_t>(n);
      if (remaining == 0) break;
    }
  }
  if (length > 0 && remaining != 0) return "";
  return h.FinalizeHex();
}

std::string Sha1Hex(std::span<const uint8_t> data) {
  EVP_MD_CTX* ctx = EVP_MD_CTX_new();
  EVP_DigestInit_ex(ctx, EVP_sha1(), nullptr);
  if (!data.empty()) {
    EVP_DigestUpdate(ctx, data.data(), data.size());
  }
  std::vector<uint8_t> digest(EVP_MAX_MD_SIZE);
  unsigned int len = 0;
  EVP_DigestFinal_ex(ctx, digest.data(), &len);
  EVP_MD_CTX_free(ctx);
  digest.resize(len);
  return BytesToHex(digest);
}

std::string Sha1FileHex(const std::filesystem::path& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return "";
  std::vector<uint8_t> data((std::istreambuf_iterator<char>(f)),
                            std::istreambuf_iterator<char>());
  return Sha1Hex(data);
}

bool VerifyFileSha256(const std::filesystem::path& path,
                      std::string_view expected_hex,
                      std::string* actual_hex) {
  std::string got = Sha256FileHex(path);
  if (actual_hex) *actual_hex = got;
  if (got.size() != expected_hex.size()) return false;
  for (size_t i = 0; i < got.size(); ++i) {
    if (std::tolower(static_cast<unsigned char>(got[i])) !=
        std::tolower(static_cast<unsigned char>(expected_hex[i]))) {
      return false;
    }
  }
  return true;
}

uint32_t Crc32(std::span<const uint8_t> data, uint32_t initial) {
  uLong crc = initial == 0 ? ::crc32(0L, Z_NULL, 0) : initial;
  if (!data.empty()) {
    crc = ::crc32(crc, data.data(), static_cast<uInt>(data.size()));
  }
  return static_cast<uint32_t>(crc);
}

uint32_t XxHash32(std::span<const uint8_t> data, uint32_t seed) {
  static constexpr uint32_t kPrime1 = 0x9E3779B1U;
  static constexpr uint32_t kPrime2 = 0x85EBCA77U;
  static constexpr uint32_t kPrime3 = 0xC2B2AE3DU;
  static constexpr uint32_t kPrime4 = 0x27D4EB2FU;
  static constexpr uint32_t kPrime5 = 0x165667B1U;

  const uint8_t* p = data.data();
  const uint8_t* const bEnd = p + data.size();
  uint32_t h32;

  if (data.size() >= 16) {
    const uint8_t* const limit = bEnd - 16;
    uint32_t v1 = seed + kPrime1 + kPrime2;
    uint32_t v2 = seed + kPrime2;
    uint32_t v3 = seed + 0;
    uint32_t v4 = seed - kPrime1;
    do {
      v1 = RotL32(v1 + ReadLE32(p) * kPrime2, 13) * kPrime1;
      p += 4;
      v2 = RotL32(v2 + ReadLE32(p) * kPrime2, 13) * kPrime1;
      p += 4;
      v3 = RotL32(v3 + ReadLE32(p) * kPrime2, 13) * kPrime1;
      p += 4;
      v4 = RotL32(v4 + ReadLE32(p) * kPrime2, 13) * kPrime1;
      p += 4;
    } while (p <= limit);
    h32 = RotL32(v1, 1) + RotL32(v2, 7) + RotL32(v3, 12) + RotL32(v4, 18);
  } else {
    h32 = seed + kPrime5;
  }

  h32 += static_cast<uint32_t>(data.size());

  while (p + 4 <= bEnd) {
    h32 += ReadLE32(p) * kPrime3;
    h32 = RotL32(h32, 17) * kPrime4;
    p += 4;
  }

  while (p < bEnd) {
    h32 += (*p) * kPrime5;
    h32 = RotL32(h32, 11) * kPrime1;
    p++;
  }

  h32 ^= h32 >> 15;
  h32 *= kPrime2;
  h32 ^= h32 >> 13;
  h32 *= kPrime3;
  h32 ^= h32 >> 16;
  return h32;
}

}  // namespace gbos
