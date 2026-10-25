#include "src/kmod/kmod_verifier.h"

#include <cstring>
#include <fstream>
#include <stdexcept>

#include "src/common/sha256.h"

namespace googlebookos::kmod {
namespace {

constexpr const char* kExpectedGkiVermagicPrefix = "6.18.21-android17-5";
constexpr const char* kExpectedGkiVermagicTail =
    "SMP preempt mod_unload modversions ";

uint16_t GetLe16(const uint8_t* p) {
  return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8);
}

uint32_t GetLe32(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

uint64_t GetLe64(const uint8_t* p) {
  return static_cast<uint64_t>(GetLe32(p)) |
         (static_cast<uint64_t>(GetLe32(p + 4)) << 32);
}

}  // namespace

KoModinfo InspectKernelModule(const std::filesystem::path& ko_path) {
  std::ifstream f(ko_path, std::ios::binary | std::ios::ate);
  if (!f) {
    throw std::runtime_error("Cannot open kernel module: " + ko_path.string());
  }
  size_t sz = static_cast<size_t>(f.tellg());
  std::vector<uint8_t> b(sz);
  f.seekg(0);
  f.read(reinterpret_cast<char*>(b.data()), static_cast<std::streamsize>(sz));

  if (b.size() < 64 || std::memcmp(b.data(), "\x7F" "ELF\x02\x01\x01", 7) != 0) {
    throw std::runtime_error("Not a 64-bit little-endian ELF file: " +
                             ko_path.string());
  }
  uint16_t e_type = GetLe16(b.data() + 16);
  uint16_t e_machine = GetLe16(b.data() + 18);
  if (e_type != 1 || e_machine != 62) {
    throw std::runtime_error("Not an x86_64 ET_REL kernel module: " +
                             ko_path.string());
  }

  uint64_t shoff = GetLe64(b.data() + 40);
  uint16_t shentsize = GetLe16(b.data() + 58);
  uint16_t shnum = GetLe16(b.data() + 60);
  uint16_t shstrndx = GetLe16(b.data() + 62);
  if (shoff + static_cast<uint64_t>(shentsize) * shnum > b.size() ||
      shstrndx >= shnum) {
    throw std::runtime_error("Truncated ELF section headers");
  }

  auto sec_hdr = [&](uint16_t idx) -> const uint8_t* {
    return b.data() + shoff + static_cast<uint64_t>(idx) * shentsize;
  };
  uint64_t shstr_off = GetLe64(sec_hdr(shstrndx) + 24);
  uint64_t shstr_sz = GetLe64(sec_hdr(shstrndx) + 32);
  if (shstr_off + shstr_sz > b.size()) {
    throw std::runtime_error("Invalid .shstrtab section");
  }

  KoModinfo info;
  info.sha256 = gbos::Sha256Hex(b);

  for (uint16_t i = 0; i < shnum; ++i) {
    const uint8_t* sh = sec_hdr(i);
    uint32_t name_off = GetLe32(sh);
    uint64_t off = GetLe64(sh + 24);
    uint64_t size = GetLe64(sh + 32);
    if (name_off >= shstr_sz || off + size > b.size()) continue;
    std::string sname(reinterpret_cast<const char*>(b.data() + shstr_off + name_off));

    if (sname == ".modinfo") {
      size_t p = static_cast<size_t>(off);
      size_t end = static_cast<size_t>(off + size);
      while (p < end) {
        size_t nul = p;
        while (nul < end && b[nul] != 0) ++nul;
        std::string entry(reinterpret_cast<const char*>(b.data() + p), nul - p);
        if (entry.rfind("name=", 0) == 0) {
          info.name = entry.substr(5);
        } else if (entry.rfind("vermagic=", 0) == 0) {
          info.vermagic = entry.substr(9);
        } else if (entry.rfind("firmware=", 0) == 0) {
          info.firmware.push_back(entry.substr(9));
        }
        p = nul + 1;
      }
    } else if (sname == "__versions") {
      for (size_t p = static_cast<size_t>(off);
           p + 64 <= static_cast<size_t>(off + size); p += 64) {
        uint32_t crc = GetLe32(b.data() + p);
        std::string sym(reinterpret_cast<const char*>(b.data() + p + 8));
        if (!sym.empty()) {
          info.versions[sym] = crc;
        }
      }
    }
  }
  return info;
}

bool VerifyPinnedGkiModule(const std::filesystem::path& ko_path,
                           const std::string& expected_modname,
                           const std::string& expected_sha256,
                           std::string* error_msg) {
  try {
    auto info = InspectKernelModule(ko_path);
    if (info.name != expected_modname) {
      if (error_msg) {
        *error_msg = "Expected module name " + expected_modname + ", got " +
                     info.name;
      }
      return false;
    }
    if (info.vermagic.rfind(kExpectedGkiVermagicPrefix, 0) != 0 ||
        info.vermagic.size() < std::strlen(kExpectedGkiVermagicTail) ||
        info.vermagic.substr(info.vermagic.size() -
                             std::strlen(kExpectedGkiVermagicTail)) !=
            kExpectedGkiVermagicTail) {
      if (error_msg) {
        *error_msg = "Unexpected vermagic: " + info.vermagic;
      }
      return false;
    }
    if (!expected_sha256.empty() && info.sha256 != expected_sha256) {
      if (error_msg) {
        *error_msg = "SHA-256 mismatch: expected " + expected_sha256 +
                     ", got " + info.sha256;
      }
      return false;
    }
    return true;
  } catch (const std::exception& e) {
    if (error_msg) *error_msg = e.what();
    return false;
  }
}

}  // namespace googlebookos::kmod
