#include "src/lp/lp_metadata.h"

#include <algorithm>
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

std::string ReadFixedString(const uint8_t* p, size_t max_len) {
  size_t len = 0;
  while (len < max_len && p[len] != 0) ++len;
  return std::string(reinterpret_cast<const char*>(p), len);
}

}  // namespace

const LpPartition* LpMetadata::FindPartition(std::string_view part_name) const {
  for (const auto& p : partitions) {
    if (p.name == part_name) return &p;
  }
  return nullptr;
}

std::optional<LpMetadata> ParseLpMetadataBuffer(std::span<const uint8_t> super_head,
                                                std::string* error) {
  if (super_head.size() < 12288 + 256) {
    if (error) *error = "Buffer too small for LP metadata";
    return std::nullopt;
  }
  const uint8_t* geo = super_head.data() + 4096;
  if (std::memcmp(geo, "gDla", 4) != 0) {
    if (error) *error = "Missing LP geometry magic 'gDla' at super+4096";
    return std::nullopt;
  }
  LpMetadata meta;
  meta.metadata_max_size = ReadLE32(geo + 40);
  meta.metadata_slot_count = ReadLE32(geo + 44);
  meta.logical_block_size = ReadLE32(geo + 48);

  const uint8_t* hdr = super_head.data() + 12288;
  if (std::memcmp(hdr, "0PLA", 4) != 0) {
    if (error) *error = "Missing LP header magic '0PLA' at super+12288";
    return std::nullopt;
  }
  meta.major_version = ReadLE16(hdr + 4);
  meta.minor_version = ReadLE16(hdr + 6);
  uint32_t hdr_size = ReadLE32(hdr + 8);
  uint32_t tables_size = ReadLE32(hdr + 44);
  if (12288ULL + hdr_size + tables_size > super_head.size()) {
    if (error) *error = "Buffer too small for LP metadata tables";
    return std::nullopt;
  }

  const uint8_t* tables = hdr + hdr_size;
  auto read_td = [&](size_t off) {
    uint32_t t_off = ReadLE32(hdr + off);
    uint32_t t_num = ReadLE32(hdr + off + 4);
    uint32_t t_esz = ReadLE32(hdr + off + 8);
    return std::tuple<uint32_t, uint32_t, uint32_t>(t_off, t_num, t_esz);
  };

  auto [p_off, p_num, p_esz] = read_td(80);
  auto [e_off, e_num, e_esz] = read_td(92);
  auto [g_off, g_num, g_esz] = read_td(104);
  auto [b_off, b_num, b_esz] = read_td(116);

  for (uint32_t i = 0; i < g_num; ++i) {
    const uint8_t* g = tables + g_off + i * g_esz;
    LpGroup grp;
    grp.name = ReadFixedString(g, 36);
    grp.flags = ReadLE32(g + 36);
    grp.maximum_size = ReadLE64(g + 40);
    meta.groups.push_back(std::move(grp));
  }

  for (uint32_t i = 0; i < e_num; ++i) {
    const uint8_t* e = tables + e_off + i * e_esz;
    LpExtent ext;
    ext.num_sectors = ReadLE64(e);
    ext.target_type = ReadLE32(e + 8);
    ext.target_data = ReadLE64(e + 12);
    ext.target_source = ReadLE32(e + 20);
    meta.extents.push_back(ext);
  }

  for (uint32_t i = 0; i < p_num; ++i) {
    const uint8_t* p = tables + p_off + i * p_esz;
    LpPartition part;
    part.name = ReadFixedString(p, 36);
    part.attributes = ReadLE32(p + 36);
    part.first_extent_index = ReadLE32(p + 40);
    part.num_extents = ReadLE32(p + 44);
    part.group_index = ReadLE32(p + 48);
    if (part.group_index < meta.groups.size()) {
      part.group_name = meta.groups[part.group_index].name;
    }
    uint64_t sectors = 0;
    for (uint32_t j = 0; j < part.num_extents; ++j) {
      uint32_t idx = part.first_extent_index + j;
      if (idx < meta.extents.size()) {
        sectors += meta.extents[idx].num_sectors;
      }
    }
    part.total_bytes = sectors * 512ULL;
    meta.partitions.push_back(std::move(part));
  }

  for (uint32_t i = 0; i < b_num; ++i) {
    const uint8_t* b = tables + b_off + i * b_esz;
    LpBlockDevice bd;
    bd.first_logical_sector = ReadLE64(b);
    bd.alignment = ReadLE32(b + 8);
    bd.alignment_offset = ReadLE32(b + 12);
    bd.size = ReadLE64(b + 16);
    bd.partition_name = ReadFixedString(b + 24, 36);
    bd.flags = ReadLE32(b + 60);
    meta.block_devices.push_back(std::move(bd));
  }

  return meta;
}

std::optional<LpMetadata> ReadLpMetadataFromFile(const std::filesystem::path& img_path,
                                                 uint64_t super_offset,
                                                 std::string* error) {
  std::ifstream f(img_path, std::ios::binary);
  if (!f) {
    if (error) *error = "Cannot open image file: " + img_path.string();
    return std::nullopt;
  }
  f.seekg(static_cast<std::streamoff>(super_offset), std::ios::beg);
  std::vector<uint8_t> buf(128 * 1024);
  f.read(reinterpret_cast<char*>(buf.data()), static_cast<std::streamsize>(buf.size()));
  buf.resize(static_cast<size_t>(f.gcount()));
  return ParseLpMetadataBuffer(buf, error);
}

bool ExtractLogicalPartition(const std::filesystem::path& img_path,
                             uint64_t super_offset,
                             std::string_view part_name,
                             const std::filesystem::path& out_file,
                             std::string* error) {
  auto meta = ReadLpMetadataFromFile(img_path, super_offset, error);
  if (!meta) return false;
  const auto* part = meta->FindPartition(part_name);
  if (!part) {
    if (error) *error = "Logical partition not found: " + std::string(part_name);
    return false;
  }

  std::ifstream in(img_path, std::ios::binary);
  std::ofstream out(out_file, std::ios::binary | std::ios::trunc);
  if (!in || !out) {
    if (error) *error = "Cannot open input/output for logical partition extraction";
    return false;
  }

  std::vector<uint8_t> buf(4 * 1024 * 1024);
  for (uint32_t j = 0; j < part->num_extents; ++j) {
    const auto& ext = meta->extents[part->first_extent_index + j];
    if (ext.target_type != 0 || ext.target_source != 0) {
      if (error) *error = "Non-linear extent in logical partition " + part->name;
      return false;
    }
    uint64_t pos = super_offset + ext.target_data * 512ULL;
    uint64_t rem = ext.num_sectors * 512ULL;
    in.seekg(static_cast<std::streamoff>(pos), std::ios::beg);
    while (rem > 0) {
      size_t chunk = static_cast<size_t>(std::min<uint64_t>(rem, buf.size()));
      in.read(reinterpret_cast<char*>(buf.data()), static_cast<std::streamsize>(chunk));
      if (static_cast<size_t>(in.gcount()) != chunk) {
        if (error) *error = "Short read extracting logical partition " + part->name;
        return false;
      }
      out.write(reinterpret_cast<const char*>(buf.data()),
                static_cast<std::streamsize>(chunk));
      rem -= chunk;
    }
  }
  return out.good();
}

bool ExtractAllLogicalPartitions(const std::filesystem::path& img_path,
                                 uint64_t super_offset,
                                 const std::filesystem::path& out_dir,
                                 std::string* error) {
  auto meta = ReadLpMetadataFromFile(img_path, super_offset, error);
  if (!meta) return false;

  std::error_code ec;
  std::filesystem::create_directories(out_dir, ec);

  for (const auto& part : meta->partitions) {
    if (part.num_extents == 0) continue;
    if (!ExtractLogicalPartition(img_path, super_offset, part.name,
                                 out_dir / (part.name + ".img"), error)) {
      return false;
    }
  }
  return true;
}

}  // namespace gbos
