#ifndef GOOGLEBOOKOS_SRC_FETCH_HTTP_ZIP_H_
#define GOOGLEBOOKOS_SRC_FETCH_HTTP_ZIP_H_

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace googlebookos::fetch {

struct ZipCentralEntry {
  std::string filename;
  uint16_t compression_method = 0;  // 0 = stored, 8 = deflate
  uint32_t crc32 = 0;
  uint64_t compressed_size = 0;
  uint64_t uncompressed_size = 0;
  uint64_t local_header_offset = 0;
};

// Parses a ZIP Central Directory (supporting both standard ZIP and ZIP64 EOCD)
// from an in-memory buffer of the tail of a ZIP archive.
std::vector<ZipCentralEntry> ParseZipCentralDirectory(
    const std::vector<uint8_t>& tail_bytes, uint64_t file_total_size);

// Lists entries in a local ZIP file (including >4 GiB ZIP64 recovery archives).
std::vector<ZipCentralEntry> ListLocalZipEntries(
    const std::filesystem::path& zip_path);

// Extracts a single member from a local ZIP file using stored or raw deflate
// decompression, or fetches a single member from a remote HTTP Range-capable URL.
std::vector<uint8_t> ExtractLocalZipMember(
    const std::filesystem::path& zip_path, const std::string& member_name);

// Fetches a single member from a remote ZIP archive via HTTP Range requests
// without downloading the entire archive (matches base/http-zip.py).
void FetchRemoteZipMember(const std::string& url,
                          const std::string& member_name,
                          const std::filesystem::path& output_path);

}  // namespace googlebookos::fetch

#endif  // GOOGLEBOOKOS_SRC_FETCH_HTTP_ZIP_H_
