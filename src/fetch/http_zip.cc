#include "src/fetch/http_zip.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace googlebookos::fetch {
namespace {

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

std::vector<ZipCentralEntry> ParseZipCentralDirectory(
    const std::vector<uint8_t>& tail_bytes, uint64_t file_total_size) {
  if (tail_bytes.size() < 22) {
    throw std::runtime_error("ZIP tail too small for EOCD");
  }
  const uint64_t tail_file_offset = file_total_size - tail_bytes.size();

  // Locate EOCD signature 0x06054b50 from the end.
  ssize_t eocd_pos = -1;
  for (ssize_t i = static_cast<ssize_t>(tail_bytes.size()) - 22; i >= 0; --i) {
    if (tail_bytes[i] == 'P' && tail_bytes[i + 1] == 'K' &&
        tail_bytes[i + 2] == 5 && tail_bytes[i + 3] == 6) {
      eocd_pos = i;
      break;
    }
  }
  if (eocd_pos < 0) {
    throw std::runtime_error("ZIP End of Central Directory not found");
  }

  uint64_t cd_size = GetLe32(tail_bytes.data() + eocd_pos + 12);
  uint64_t cd_offset = GetLe32(tail_bytes.data() + eocd_pos + 16);

  // Check for ZIP64 EOCD Locator (20 bytes before EOCD: PK\x06\x07).
  if (eocd_pos >= 20 && tail_bytes[eocd_pos - 20] == 'P' &&
      tail_bytes[eocd_pos - 19] == 'K' && tail_bytes[eocd_pos - 18] == 6 &&
      tail_bytes[eocd_pos - 17] == 7) {
    uint64_t zip64_eocd_off = GetLe64(tail_bytes.data() + eocd_pos - 12);
    if (zip64_eocd_off >= tail_file_offset) {
      size_t rel = static_cast<size_t>(zip64_eocd_off - tail_file_offset);
      if (rel + 56 <= tail_bytes.size() && tail_bytes[rel] == 'P' &&
          tail_bytes[rel + 1] == 'K' && tail_bytes[rel + 2] == 6 &&
          tail_bytes[rel + 3] == 6) {
        cd_size = GetLe64(tail_bytes.data() + rel + 40);
        cd_offset = GetLe64(tail_bytes.data() + rel + 48);
      }
    }
  }

  if (cd_offset < tail_file_offset ||
      cd_offset + cd_size > file_total_size) {
    throw std::runtime_error(
        "Central directory lies outside provided tail buffer");
  }

  size_t pos = static_cast<size_t>(cd_offset - tail_file_offset);
  size_t end = pos + static_cast<size_t>(cd_size);
  std::vector<ZipCentralEntry> out;

  while (pos + 46 <= end && pos + 46 <= tail_bytes.size()) {
    const uint8_t* p = tail_bytes.data() + pos;
    if (p[0] != 'P' || p[1] != 'K' || p[2] != 1 || p[3] != 2) {
      break;
    }
    ZipCentralEntry e;
    e.compression_method = GetLe16(p + 10);
    e.crc32 = GetLe32(p + 16);
    e.compressed_size = GetLe32(p + 20);
    e.uncompressed_size = GetLe32(p + 24);
    uint16_t name_len = GetLe16(p + 28);
    uint16_t extra_len = GetLe16(p + 30);
    uint16_t comment_len = GetLe16(p + 32);
    e.local_header_offset = GetLe32(p + 42);

    if (pos + 46 + name_len + extra_len + comment_len > tail_bytes.size()) {
      break;
    }
    e.filename.assign(reinterpret_cast<const char*>(p + 46), name_len);

    // Parse ZIP64 extra field (0x0001) if present.
    const uint8_t* ex = p + 46 + name_len;
    size_t ex_pos = 0;
    while (ex_pos + 4 <= extra_len) {
      uint16_t tag = GetLe16(ex + ex_pos);
      uint16_t sz = GetLe16(ex + ex_pos + 2);
      if (ex_pos + 4 + sz > extra_len) break;
      if (tag == 0x0001) {
        size_t q = ex_pos + 4;
        if (e.uncompressed_size == 0xFFFFFFFFULL && q + 8 <= ex_pos + 4 + sz) {
          e.uncompressed_size = GetLe64(ex + q);
          q += 8;
        }
        if (e.compressed_size == 0xFFFFFFFFULL && q + 8 <= ex_pos + 4 + sz) {
          e.compressed_size = GetLe64(ex + q);
          q += 8;
        }
        if (e.local_header_offset == 0xFFFFFFFFULL &&
            q + 8 <= ex_pos + 4 + sz) {
          e.local_header_offset = GetLe64(ex + q);
          q += 8;
        }
      }
      ex_pos += 4 + sz;
    }

    out.push_back(std::move(e));
    pos += 46 + name_len + extra_len + comment_len;
  }
  return out;
}

std::vector<ZipCentralEntry> ListLocalZipEntries(
    const std::filesystem::path& zip_path) {
  std::ifstream f(zip_path, std::ios::binary | std::ios::ate);
  if (!f) {
    throw std::runtime_error("Cannot open ZIP file: " + zip_path.string());
  }
  uint64_t total_size = static_cast<uint64_t>(f.tellg());
  uint64_t tail_len = std::min<uint64_t>(total_size, 256 * 1024);
  std::vector<uint8_t> tail(tail_len);
  f.seekg(static_cast<std::streamoff>(total_size - tail_len));
  f.read(reinterpret_cast<char*>(tail.data()),
         static_cast<std::streamsize>(tail_len));
  return ParseZipCentralDirectory(tail, total_size);
}

std::vector<uint8_t> ExtractLocalZipMember(
    const std::filesystem::path& zip_path, const std::string& member_name) {
  auto entries = ListLocalZipEntries(zip_path);
  const ZipCentralEntry* found = nullptr;
  for (const auto& e : entries) {
    if (e.filename == member_name) {
      found = &e;
      break;
    }
  }
  if (!found) {
    throw std::runtime_error("ZIP member not found: " + member_name);
  }
  if (found->compression_method == 0) {
    std::ifstream f(zip_path, std::ios::binary);
    f.seekg(static_cast<std::streamoff>(found->local_header_offset));
    uint8_t lh[30]{};
    f.read(reinterpret_cast<char*>(lh), 30);
    if (lh[0] != 'P' || lh[1] != 'K' || lh[2] != 3 || lh[3] != 4) {
      throw std::runtime_error("Invalid ZIP local file header");
    }
    uint16_t name_len = GetLe16(lh + 26);
    uint16_t extra_len = GetLe16(lh + 28);
    f.seekg(static_cast<std::streamoff>(found->local_header_offset + 30 +
                                        name_len + extra_len));
    std::vector<uint8_t> out(found->uncompressed_size);
    f.read(reinterpret_cast<char*>(out.data()),
           static_cast<std::streamsize>(out.size()));
    return out;
  }

  // Fallback to python3 zipfile for deflated members when zlib headers aren't linked.
  std::filesystem::path tmp =
      std::filesystem::temp_directory_path() /
      ("gbos_zip_extract_" + std::to_string(found->crc32));
  std::ostringstream cmd;
  cmd << "python3 -c 'import sys, zipfile; "
      << "zf = zipfile.ZipFile(sys.argv[1]); "
      << "open(sys.argv[3], \"wb\").write(zf.read(sys.argv[2]))' "
      << "'" << zip_path.string() << "' '" << member_name << "' '"
      << tmp.string() << "'";
  if (std::system(cmd.str().c_str()) != 0) {
    throw std::runtime_error("Failed to extract deflated ZIP member: " +
                             member_name);
  }
  std::ifstream in(tmp, std::ios::binary | std::ios::ate);
  std::vector<uint8_t> out(static_cast<size_t>(in.tellg()));
  in.seekg(0);
  in.read(reinterpret_cast<char*>(out.data()),
          static_cast<std::streamsize>(out.size()));
  std::filesystem::remove(tmp);
  return out;
}

void FetchRemoteZipMember(const std::string& url,
                          const std::string& member_name,
                          const std::filesystem::path& output_path) {
  std::filesystem::create_directories(output_path.parent_path());
  std::ostringstream cmd;
  cmd << "python3 base/http-zip.py '" << url << "' '" << member_name << "' '"
      << output_path.string() << "'";
  if (std::system(cmd.str().c_str()) != 0) {
    throw std::runtime_error("Failed to fetch remote ZIP member " +
                             member_name + " from " + url);
  }
}

}  // namespace googlebookos::fetch
