#include "src/gpt/gpt.h"

#include "src/common/sha256.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>

namespace gbos {
namespace {

uint16_t ReadLE16(const uint8_t* p) {
  return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8);
}

uint32_t ReadLE32(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) |
         (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

uint64_t ReadLE64(const uint8_t* p) {
  return static_cast<uint64_t>(ReadLE32(p)) |
         (static_cast<uint64_t>(ReadLE32(p + 4)) << 32);
}

void WriteLE16(uint8_t* p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v & 0xFF);
  p[1] = static_cast<uint8_t>((v >> 8) & 0xFF);
}

void WriteLE32(uint8_t* p, uint32_t v) {
  p[0] = static_cast<uint8_t>(v & 0xFF);
  p[1] = static_cast<uint8_t>((v >> 8) & 0xFF);
  p[2] = static_cast<uint8_t>((v >> 16) & 0xFF);
  p[3] = static_cast<uint8_t>((v >> 24) & 0xFF);
}

void WriteLE64(uint8_t* p, uint64_t v) {
  WriteLE32(p, static_cast<uint32_t>(v & 0xFFFFFFFFULL));
  WriteLE32(p + 4, static_cast<uint32_t>((v >> 32) & 0xFFFFFFFFULL));
}

int HexVal(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

bool ParseHexBytes(std::string_view hex, uint8_t* out, size_t count) {
  if (hex.size() != count * 2) return false;
  for (size_t i = 0; i < count; ++i) {
    int hi = HexVal(hex[i * 2]);
    int lo = HexVal(hex[i * 2 + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = static_cast<uint8_t>((hi << 4) | lo);
  }
  return true;
}

}  // namespace

std::string FormatGuid(std::span<const uint8_t> b) {
  if (b.size() < 16) return "";
  uint32_t d1 = ReadLE32(b.data());
  uint16_t d2 = ReadLE16(b.data() + 4);
  uint16_t d3 = ReadLE16(b.data() + 6);
  char buf[40];
  std::snprintf(buf, sizeof(buf),
                "%08x-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                d1, d2, d3,
                b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
  return std::string(buf);
}

bool ParseGuid(std::string_view uuid_str, uint8_t out_guid[16]) {
  if (uuid_str.size() != 36 || uuid_str[8] != '-' || uuid_str[13] != '-' ||
      uuid_str[18] != '-' || uuid_str[23] != '-') {
    return false;
  }
  uint8_t d1_be[4], d2_be[2], d3_be[2];
  if (!ParseHexBytes(uuid_str.substr(0, 8), d1_be, 4) ||
      !ParseHexBytes(uuid_str.substr(9, 4), d2_be, 2) ||
      !ParseHexBytes(uuid_str.substr(14, 4), d3_be, 2) ||
      !ParseHexBytes(uuid_str.substr(19, 4), out_guid + 8, 2) ||
      !ParseHexBytes(uuid_str.substr(24, 12), out_guid + 10, 6)) {
    return false;
  }
  out_guid[0] = d1_be[3];
  out_guid[1] = d1_be[2];
  out_guid[2] = d1_be[1];
  out_guid[3] = d1_be[0];
  out_guid[4] = d2_be[1];
  out_guid[5] = d2_be[0];
  out_guid[6] = d3_be[1];
  out_guid[7] = d3_be[0];
  return true;
}

const GptPartitionEntry* GptTable::FindPartition(std::string_view part_name) const {
  for (const auto& p : partitions) {
    if (p.name == part_name) return &p;
  }
  return nullptr;
}

std::optional<GptTable> ParseGptBuffer(std::span<const uint8_t> buf,
                                       std::string* error) {
  if (buf.size() < 1024) {
    if (error) *error = "Buffer too small for GPT header";
    return std::nullopt;
  }
  const uint8_t* hdr = buf.data() + 512;
  if (std::memcmp(hdr, "EFI PART", 8) != 0) {
    if (error) *error = "Missing 'EFI PART' signature at LBA 1";
    return std::nullopt;
  }
  uint32_t hdr_size = ReadLE32(hdr + 12);
  if (hdr_size < 92 || 512 + hdr_size > buf.size()) {
    if (error) *error = "Invalid GPT header size";
    return std::nullopt;
  }

  GptTable table;
  table.header_crc32 = ReadLE32(hdr + 16);
  std::vector<uint8_t> hdr_copy(hdr, hdr + hdr_size);
  WriteLE32(hdr_copy.data() + 16, 0);
  table.header_crc_valid = (Crc32(hdr_copy) == table.header_crc32);

  table.current_lba = ReadLE64(hdr + 24);
  table.backup_lba = ReadLE64(hdr + 32);
  table.first_usable_lba = ReadLE64(hdr + 40);
  table.last_usable_lba = ReadLE64(hdr + 48);
  table.disk_guid = FormatGuid(std::span<const uint8_t>(hdr + 56, 16));
  table.entries_lba = ReadLE64(hdr + 72);
  table.num_entries = ReadLE32(hdr + 80);
  table.entry_size = ReadLE32(hdr + 84);
  table.entries_crc32 = ReadLE32(hdr + 88);

  if (table.entry_size < 128 || table.num_entries > 1024) {
    if (error) *error = "Invalid GPT entry size or count";
    return std::nullopt;
  }
  uint64_t entries_off = table.entries_lba * 512;
  uint64_t entries_bytes = static_cast<uint64_t>(table.num_entries) * table.entry_size;
  if (entries_off + entries_bytes > buf.size()) {
    if (error) *error = "Buffer too small for GPT partition entries";
    return std::nullopt;
  }

  std::span<const uint8_t> entries_span(buf.data() + entries_off,
                                        static_cast<size_t>(entries_bytes));
  table.entries_crc_valid = (Crc32(entries_span) == table.entries_crc32);

  for (uint32_t i = 0; i < table.num_entries; ++i) {
    const uint8_t* e = entries_span.data() + static_cast<size_t>(i) * table.entry_size;
    bool all_zero = true;
    for (int j = 0; j < 16; ++j) {
      if (e[j] != 0) {
        all_zero = false;
        break;
      }
    }
    if (all_zero) continue;

    GptPartitionEntry part;
    part.index = i + 1;
    part.type_guid = FormatGuid(std::span<const uint8_t>(e, 16));
    part.unique_guid = FormatGuid(std::span<const uint8_t>(e + 16, 16));
    part.first_lba = ReadLE64(e + 32);
    part.last_lba = ReadLE64(e + 40);
    part.attributes = ReadLE64(e + 48);
    part.start_bytes = part.first_lba * 512ULL;
    part.size_bytes =
        (part.last_lba >= part.first_lba) ? ((part.last_lba - part.first_lba + 1ULL) * 512ULL) : 0;

    std::string name;
    for (size_t k = 0; k < 36; ++k) {
      uint16_t ch = ReadLE16(e + 56 + k * 2);
      if (ch == 0) break;
      name.push_back(static_cast<char>(ch & 0x7F));
    }
    part.name = std::move(name);
    table.partitions.push_back(std::move(part));
  }

  return table;
}

std::optional<GptTable> ReadGptFromFile(const std::filesystem::path& img_path,
                                        std::string* error) {
  std::ifstream f(img_path, std::ios::binary);
  if (!f) {
    if (error) *error = "Cannot open image file: " + img_path.string();
    return std::nullopt;
  }
  std::vector<uint8_t> buf(64 * 512);
  f.read(reinterpret_cast<char*>(buf.data()), static_cast<std::streamsize>(buf.size()));
  buf.resize(static_cast<size_t>(f.gcount()));
  return ParseGptBuffer(buf, error);
}

bool WriteGptToBuffer(std::span<uint8_t> disk_buf,
                      std::string_view disk_guid,
                      std::span<const GptPartitionEntry> partitions,
                      std::string* error) {
  if (disk_buf.size() < 34 * 512 || (disk_buf.size() % 512) != 0) {
    if (error) *error = "Disk buffer must be a multiple of 512 bytes and >= 34 LBAs";
    return false;
  }
  uint64_t total_lbas = disk_buf.size() / 512;
  std::memset(disk_buf.data(), 0, 34 * 512);

  // Protective MBR at LBA 0
  disk_buf[446 + 4] = 0xEE;
  WriteLE32(disk_buf.data() + 446 + 8, 1);
  uint64_t mbr_sectors = std::min<uint64_t>(total_lbas - 1, 0xFFFFFFFFULL);
  WriteLE32(disk_buf.data() + 446 + 12, static_cast<uint32_t>(mbr_sectors));
  disk_buf[510] = 0x55;
  disk_buf[511] = 0xAA;

  // Partition entries at LBA 2..33 (128 entries * 128 bytes = 16384 bytes)
  uint8_t* entries = disk_buf.data() + 2 * 512;
  for (const auto& p : partitions) {
    if (p.index == 0 || p.index > 128) {
      if (error) *error = "Partition index out of range 1..128";
      return false;
    }
    uint8_t* e = entries + (p.index - 1) * 128;
    if (!ParseGuid(p.type_guid, e) || !ParseGuid(p.unique_guid, e + 16)) {
      if (error) *error = "Invalid partition GUID string";
      return false;
    }
    WriteLE64(e + 32, p.first_lba);
    WriteLE64(e + 40, p.last_lba);
    WriteLE64(e + 48, p.attributes);
    for (size_t i = 0; i < std::min<size_t>(p.name.size(), 36); ++i) {
      WriteLE16(e + 56 + i * 2, static_cast<uint8_t>(p.name[i]));
    }
  }
  uint32_t entries_crc = Crc32(std::span<const uint8_t>(entries, 128 * 128));

  // Primary GPT header at LBA 1
  uint8_t* hdr = disk_buf.data() + 512;
  std::memcpy(hdr, "EFI PART", 8);
  WriteLE32(hdr + 8, 0x00010000U);  // Revision 1.0
  WriteLE32(hdr + 12, 92);          // Header size
  WriteLE32(hdr + 16, 0);           // CRC32 placeholder
  WriteLE32(hdr + 20, 0);
  WriteLE64(hdr + 24, 1);                 // MyLBA
  WriteLE64(hdr + 32, total_lbas - 1);    // AlternateLBA
  WriteLE64(hdr + 40, 34);                // FirstUsableLBA
  WriteLE64(hdr + 48, total_lbas - 34);   // LastUsableLBA
  if (!ParseGuid(disk_guid, hdr + 56)) {
    if (error) *error = "Invalid disk GUID";
    return false;
  }
  WriteLE64(hdr + 72, 2);    // PartitionEntryLBA
  WriteLE32(hdr + 80, 128);  // NumberOfPartitionEntries
  WriteLE32(hdr + 84, 128);  // SizeOfPartitionEntry
  WriteLE32(hdr + 88, entries_crc);
  uint32_t hdr_crc = Crc32(std::span<const uint8_t>(hdr, 92));
  WriteLE32(hdr + 16, hdr_crc);

  // Backup GPT if buffer is large enough (>= 68 LBAs)
  if (total_lbas >= 68) {
    uint8_t* bak_entries = disk_buf.data() + (total_lbas - 33) * 512;
    std::memcpy(bak_entries, entries, 32 * 512);
    uint8_t* bak_hdr = disk_buf.data() + (total_lbas - 1) * 512;
    std::memset(bak_hdr, 0, 512);
    std::memcpy(bak_hdr, hdr, 92);
    WriteLE32(bak_hdr + 16, 0);
    WriteLE64(bak_hdr + 24, total_lbas - 1);
    WriteLE64(bak_hdr + 32, 1);
    WriteLE64(bak_hdr + 72, total_lbas - 33);
    uint32_t bak_crc = Crc32(std::span<const uint8_t>(bak_hdr, 92));
    WriteLE32(bak_hdr + 16, bak_crc);
  }
  return true;
}

bool WriteLayoutEnv(const std::filesystem::path& img_path,
                    const std::filesystem::path& out_env,
                    std::string* error) {
  auto gpt = ReadGptFromFile(img_path, error);
  if (!gpt) return false;

  static constexpr std::pair<std::string_view, std::string_view> kWanted[] = {
      {"EFI-SYSTEM", "ESP"},
      {"super", "SUPER"},
      {"vbmeta_a", "VBMETA"},
      {"boot_a", "BOOT"},
      {"vendor_boot_a", "VENDOR_BOOT"},
      {"init_boot_a", "INIT_BOOT"},
  };

  std::ostringstream oss;
  for (const auto& [part_name, key] : kWanted) {
    const auto* p = gpt->FindPartition(part_name);
    if (!p) {
      if (error) *error = "GPT partition missing: " + std::string(part_name);
      return false;
    }
    oss << key << "_OFF=" << p->start_bytes << "\n"
        << key << "_SIZE=" << p->size_bytes << "\n"
        << key << "_GUID=" << p->unique_guid << "\n";
  }

  std::ofstream out(out_env, std::ios::binary | std::ios::trunc);
  if (!out) {
    if (error) *error = "Cannot write layout env file: " + out_env.string();
    return false;
  }
  out << oss.str();
  return out.good();
}

bool SplicePartition(const std::filesystem::path& img_path,
                     uint64_t offset,
                     uint64_t slot_size,
                     const std::filesystem::path& part_file,
                     std::string* error) {
  std::ifstream src(part_file, std::ios::binary | std::ios::ate);
  if (!src) {
    if (error) *error = "Cannot open partition file: " + part_file.string();
    return false;
  }
  uint64_t src_size = static_cast<uint64_t>(src.tellg());
  src.seekg(0, std::ios::beg);
  if (src_size > slot_size) {
    if (error) {
      *error = "Partition file " + part_file.string() + " (" +
               std::to_string(src_size) + " bytes) exceeds slot size (" +
               std::to_string(slot_size) + " bytes)";
    }
    return false;
  }

  int fd = ::open(img_path.c_str(), O_RDWR | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) {
    if (error) *error = "Cannot open target image (O_NOFOLLOW): " + img_path.string();
    return false;
  }
  struct stat st {};
  if (::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
    ::close(fd);
    if (error) *error = "Target image is not a regular file: " + img_path.string();
    return false;
  }
  if (offset + slot_size > static_cast<uint64_t>(st.st_size)) {
    ::close(fd);
    if (error) *error = "Splice range exceeds target image size";
    return false;
  }

  std::vector<uint8_t> buf(1024 * 1024);
  uint64_t pos = offset;
  uint64_t rem = src_size;
  while (rem > 0) {
    size_t chunk = static_cast<size_t>(std::min<uint64_t>(rem, buf.size()));
    src.read(reinterpret_cast<char*>(buf.data()), static_cast<std::streamsize>(chunk));
    if (static_cast<size_t>(src.gcount()) != chunk) {
      ::close(fd);
      if (error) *error = "Short read from partition file";
      return false;
    }
    ssize_t w = ::pwrite(fd, buf.data(), chunk, static_cast<off_t>(pos));
    if (w != static_cast<ssize_t>(chunk)) {
      ::close(fd);
      if (error) *error = "Short pwrite to target image";
      return false;
    }
    pos += chunk;
    rem -= chunk;
  }

  // Zero-fill remainder of slot
  std::fill(buf.begin(), buf.end(), 0);
  uint64_t zero_rem = slot_size - src_size;
  while (zero_rem > 0) {
    size_t chunk = static_cast<size_t>(std::min<uint64_t>(zero_rem, buf.size()));
    ssize_t w = ::pwrite(fd, buf.data(), chunk, static_cast<off_t>(pos));
    if (w != static_cast<ssize_t>(chunk)) {
      ::close(fd);
      if (error) *error = "Short zero-fill pwrite to target image";
      return false;
    }
    pos += chunk;
    zero_rem -= chunk;
  }

  ::close(fd);
  return true;
}

}  // namespace gbos
