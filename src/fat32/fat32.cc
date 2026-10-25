#include "src/fat32/fat32.h"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>

namespace googlebookos::fat32 {
namespace {

constexpr uint32_t kBytesPerSector = 512;
constexpr uint32_t kSectorsPerCluster = 8;
constexpr uint32_t kClusterSize = kBytesPerSector * kSectorsPerCluster;  // 4096
constexpr uint32_t kReservedSectors = 32;
constexpr uint32_t kNumFats = 2;
constexpr uint32_t kFatEoc = 0x0FFFFFFF;

void PutLe16(uint8_t* p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v & 0xFF);
  p[1] = static_cast<uint8_t>((v >> 8) & 0xFF);
}

void PutLe32(uint8_t* p, uint32_t v) {
  p[0] = static_cast<uint8_t>(v & 0xFF);
  p[1] = static_cast<uint8_t>((v >> 8) & 0xFF);
  p[2] = static_cast<uint8_t>((v >> 16) & 0xFF);
  p[3] = static_cast<uint8_t>((v >> 24) & 0xFF);
}

uint16_t GetLe16(const uint8_t* p) {
  return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8);
}

uint32_t GetLe32(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

std::vector<std::string> SplitPath(const std::string& path) {
  std::vector<std::string> parts;
  std::stringstream ss(path);
  std::string item;
  while (std::getline(ss, item, '/')) {
    if (!item.empty()) parts.push_back(item);
  }
  return parts;
}

uint8_t LfnChecksum(const uint8_t short_name[11]) {
  uint8_t sum = 0;
  for (int i = 0; i < 11; ++i) {
    sum = static_cast<uint8_t>(((sum & 1) ? 0x80 : 0) + (sum >> 1) + short_name[i]);
  }
  return sum;
}

void MakeShortName(const std::string& name, int collision_idx, uint8_t out[11]) {
  std::memset(out, ' ', 11);
  auto dot = name.rfind('.');
  std::string base = (dot == std::string::npos) ? name : name.substr(0, dot);
  std::string ext = (dot == std::string::npos) ? "" : name.substr(dot + 1);

  std::string clean_base;
  for (char c : base) {
    if (std::isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '-') {
      clean_base.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
    }
  }
  if (clean_base.empty()) clean_base = "FILE";

  std::string clean_ext;
  for (char c : ext) {
    if (std::isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '-') {
      clean_ext.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
    }
  }

  bool needs_tilde = (collision_idx > 0) || (clean_base.size() > 8) ||
                     (clean_ext.size() > 3) || (clean_base != base) ||
                     (clean_ext != ext);
  if (needs_tilde) {
    std::string suffix = "~" + std::to_string(collision_idx == 0 ? 1 : collision_idx);
    size_t keep = std::min(clean_base.size(), 8 - suffix.size());
    std::string s = clean_base.substr(0, keep) + suffix;
    std::memcpy(out, s.data(), s.size());
  } else {
    std::memcpy(out, clean_base.data(), std::min<size_t>(8, clean_base.size()));
  }
  std::memcpy(out + 8, clean_ext.data(), std::min<size_t>(3, clean_ext.size()));
}

void AppendDirEntryWithLfn(std::vector<uint8_t>* dir_buf,
                           const std::string& name,
                           const uint8_t short_name[11], uint8_t attr,
                           uint32_t first_cluster, uint32_t file_size) {
  // Check if LFN is needed (always emit LFN unless name is "." or "..").
  if (name != "." && name != "..") {
    uint8_t csum = LfnChecksum(short_name);
    std::vector<uint16_t> u16(name.begin(), name.end());
    size_t num_lfn = (u16.size() + 12) / 13;
    for (size_t idx = num_lfn; idx >= 1; --idx) {
      uint8_t lfn[32];
      std::memset(lfn, 0xFF, sizeof(lfn));
      lfn[0] = static_cast<uint8_t>(idx | (idx == num_lfn ? 0x40 : 0));
      lfn[11] = 0x0F;  // ATTR_LONG_NAME
      lfn[12] = 0;
      lfn[13] = csum;
      lfn[26] = 0;
      lfn[27] = 0;

      const int offsets[13] = {1,  3,  5,  7,  9,  14, 16,
                               18, 20, 22, 24, 28, 30};
      size_t base_char = (idx - 1) * 13;
      for (size_t k = 0; k < 13; ++k) {
        size_t pos = base_char + k;
        uint16_t ch = 0xFFFF;
        if (pos < u16.size()) {
          ch = u16[pos];
        } else if (pos == u16.size()) {
          ch = 0x0000;
        }
        PutLe16(lfn + offsets[k], ch);
      }
      dir_buf->insert(dir_buf->end(), lfn, lfn + 32);
    }
  }

  uint8_t d[32]{};
  std::memcpy(d, short_name, 11);
  d[11] = attr;
  // Fixed timestamp: 2026-01-01 00:00:00 (date = ((2026-1980)<<9)|(1<<5)|1 = 0x5C21)
  PutLe16(d + 14, 0x0000);
  PutLe16(d + 16, 0x5C21);
  PutLe16(d + 18, 0x5C21);
  PutLe16(d + 20, static_cast<uint16_t>((first_cluster >> 16) & 0xFFFF));
  PutLe16(d + 22, 0x0000);
  PutLe16(d + 24, 0x5C21);
  PutLe16(d + 26, static_cast<uint16_t>(first_cluster & 0xFFFF));
  PutLe32(d + 28, file_size);
  dir_buf->insert(dir_buf->end(), d, d + 32);
}

struct DirNode {
  std::string name;
  uint32_t parent_cluster = 0;
  uint32_t first_cluster = 0;
  uint32_t num_clusters = 1;
  std::map<std::string, DirNode> subdirs;
  std::vector<const FatFileInput*> files;
};

}  // namespace

std::vector<uint8_t> BuildFat32Image(uint64_t total_size_bytes,
                                     const std::string& volume_label,
                                     uint32_t volume_id,
                                     const std::vector<FatFileInput>& files) {
  if (total_size_bytes < 36ULL * 1024 * 1024 ||
      total_size_bytes % kBytesPerSector != 0) {
    throw std::runtime_error(
        "FAT32 total_size_bytes must be >= 36 MiB and a multiple of 512");
  }
  const uint32_t total_sectors =
      static_cast<uint32_t>(total_size_bytes / kBytesPerSector);

  // Calculate FAT size in sectors (4 bytes per cluster).
  uint32_t approx_clusters = total_sectors / kSectorsPerCluster + 2;
  uint32_t fat_sectors =
      ((approx_clusters * 4 + kBytesPerSector - 1) / kBytesPerSector +
       kSectorsPerCluster - 1) &
      ~(kSectorsPerCluster - 1);
  uint32_t data_start_sector = kReservedSectors + kNumFats * fat_sectors;
  uint32_t data_clusters =
      (total_sectors - data_start_sector) / kSectorsPerCluster;

  std::vector<uint8_t> img(total_size_bytes, 0);
  std::vector<uint32_t> fat(data_clusters + 2, 0);
  fat[0] = 0x0FFFFFF8;
  fat[1] = 0x0FFFFFFF;

  uint32_t next_cluster = 2;
  auto alloc_clusters = [&](uint32_t count) -> uint32_t {
    if (count == 0) count = 1;
    if (next_cluster + count > fat.size()) {
      throw std::runtime_error("FAT32 image out of clusters");
    }
    uint32_t start = next_cluster;
    for (uint32_t i = 0; i < count; ++i) {
      fat[start + i] = (i + 1 == count) ? kFatEoc : (start + i + 1);
    }
    next_cluster += count;
    return start;
  };

  auto cluster_offset = [&](uint32_t cluster) -> uint64_t {
    return static_cast<uint64_t>(data_start_sector +
                                 (cluster - 2) * kSectorsPerCluster) *
           kBytesPerSector;
  };

  // Build directory tree.
  DirNode root;
  root.name = "";
  for (const auto& f : files) {
    auto parts = SplitPath(f.path);
    if (parts.empty()) continue;
    DirNode* cur = &root;
    for (size_t i = 0; i + 1 < parts.size(); ++i) {
      auto& child = cur->subdirs[parts[i]];
      child.name = parts[i];
      cur = &child;
    }
    cur->files.push_back(&f);
  }

  // Pass 1: allocate 1 cluster per directory (sufficient for up to ~60 LFN entries).
  auto assign_dir_clusters = [&](auto& self, DirNode* node,
                                 uint32_t parent_cl) -> void {
    node->parent_cluster = parent_cl;
    node->first_cluster = alloc_clusters(1);
    for (auto& [_, sub] : node->subdirs) {
      self(self, &sub, node->first_cluster);
    }
  };
  assign_dir_clusters(assign_dir_clusters, &root, 2);

  // Pass 2: allocate file clusters and write directory + file payloads.
  auto write_dir = [&](auto& self, DirNode* node, bool is_root) -> void {
    std::vector<uint8_t> dir_bytes;
    std::set<std::string> used_short;
    if (is_root && !volume_label.empty()) {
      uint8_t lbl[11];
      std::memset(lbl, ' ', 11);
      for (size_t i = 0; i < std::min<size_t>(11, volume_label.size()); ++i) {
        lbl[i] = static_cast<uint8_t>(
            std::toupper(static_cast<unsigned char>(volume_label[i])));
      }
      AppendDirEntryWithLfn(&dir_bytes, ".", lbl, 0x08, 0, 0);
    } else if (!is_root) {
      uint8_t dot[11], dotdot[11];
      std::memset(dot, ' ', 11);
      dot[0] = '.';
      std::memset(dotdot, ' ', 11);
      dotdot[0] = '.';
      dotdot[1] = '.';
      AppendDirEntryWithLfn(&dir_bytes, ".", dot, 0x10, node->first_cluster, 0);
      AppendDirEntryWithLfn(&dir_bytes, "..", dotdot, 0x10,
                            node->parent_cluster == 2 ? 0 : node->parent_cluster,
                            0);
    }

    auto make_unique_short = [&](const std::string& name, uint8_t sname[11]) {
      for (int idx = 0; idx < 1000; ++idx) {
        MakeShortName(name, idx, sname);
        std::string key(reinterpret_cast<const char*>(sname), 11);
        if (used_short.insert(key).second) return;
      }
      throw std::runtime_error("Too many short name collisions");
    };

    for (auto& [sub_name, sub] : node->subdirs) {
      uint8_t sname[11];
      make_unique_short(sub_name, sname);
      AppendDirEntryWithLfn(&dir_bytes, sub_name, sname, 0x10,
                            sub.first_cluster, 0);
      self(self, &sub, false);
    }

    for (const FatFileInput* f : node->files) {
      auto parts = SplitPath(f->path);
      std::string fname = parts.back();
      uint8_t sname[11];
      make_unique_short(fname, sname);
      uint32_t f_clusters = static_cast<uint32_t>(
          (f->content.size() + kClusterSize - 1) / kClusterSize);
      uint32_t f_start =
          f->content.empty() ? 0 : alloc_clusters(f_clusters);
      if (!f->content.empty()) {
        std::memcpy(img.data() + cluster_offset(f_start), f->content.data(),
                    f->content.size());
      }
      AppendDirEntryWithLfn(&dir_bytes, fname, sname, 0x20, f_start,
                            static_cast<uint32_t>(f->content.size()));
    }

    if (dir_bytes.size() > kClusterSize) {
      throw std::runtime_error("Directory exceeds 1 cluster");
    }
    std::memcpy(img.data() + cluster_offset(node->first_cluster),
                dir_bytes.data(), dir_bytes.size());
  };
  write_dir(write_dir, &root, true);

  // Write Boot Sector (sector 0) and Backup Boot Sector (sector 6).
  uint8_t bs[512]{};
  bs[0] = 0xEB;
  bs[1] = 0x58;
  bs[2] = 0x90;
  std::memcpy(bs + 3, "MSDOS5.0", 8);
  PutLe16(bs + 11, kBytesPerSector);
  bs[13] = kSectorsPerCluster;
  PutLe16(bs + 14, kReservedSectors);
  bs[16] = kNumFats;
  PutLe16(bs + 17, 0);
  PutLe16(bs + 19, 0);
  bs[21] = 0xF8;
  PutLe16(bs + 22, 0);
  PutLe16(bs + 24, 63);
  PutLe16(bs + 26, 255);
  PutLe32(bs + 28, 0);
  PutLe32(bs + 32, total_sectors);
  PutLe32(bs + 36, fat_sectors);
  PutLe16(bs + 40, 0);
  PutLe16(bs + 42, 0);
  PutLe32(bs + 44, 2);  // Root cluster = 2
  PutLe16(bs + 48, 1);  // FSInfo sector = 1
  PutLe16(bs + 50, 6);  // Backup boot sector = 6
  bs[64] = 0x80;
  bs[66] = 0x29;
  PutLe32(bs + 67, volume_id);
  std::memset(bs + 71, ' ', 11);
  for (size_t i = 0; i < std::min<size_t>(11, volume_label.size()); ++i) {
    bs[71 + i] = static_cast<uint8_t>(
        std::toupper(static_cast<unsigned char>(volume_label[i])));
  }
  std::memcpy(bs + 82, "FAT32   ", 8);
  bs[510] = 0x55;
  bs[511] = 0xAA;
  std::memcpy(img.data(), bs, 512);
  std::memcpy(img.data() + 6 * 512, bs, 512);

  // Write FSInfo Sector (sector 1 and backup at sector 7).
  uint8_t fsi[512]{};
  PutLe32(fsi + 0, 0x41615252);
  PutLe32(fsi + 484, 0x61417272);
  PutLe32(fsi + 488, data_clusters - (next_cluster - 2));
  PutLe32(fsi + 492, next_cluster);
  fsi[510] = 0x55;
  fsi[511] = 0xAA;
  std::memcpy(img.data() + 1 * 512, fsi, 512);
  std::memcpy(img.data() + 7 * 512, fsi, 512);

  // Write FAT1 and FAT2.
  for (uint32_t f_idx = 0; f_idx < kNumFats; ++f_idx) {
    uint8_t* fat_ptr =
        img.data() +
        static_cast<uint64_t>(kReservedSectors + f_idx * fat_sectors) *
            kBytesPerSector;
    for (size_t i = 0; i < next_cluster; ++i) {
      PutLe32(fat_ptr + i * 4, fat[i]);
    }
  }

  return img;
}

void WriteFat32Partition(const std::filesystem::path& disk_path,
                         uint64_t partition_offset,
                         uint64_t partition_size_bytes,
                         const std::string& volume_label, uint32_t volume_id,
                         const std::vector<FatFileInput>& files) {
  auto fat_img =
      BuildFat32Image(partition_size_bytes, volume_label, volume_id, files);
  std::fstream f(disk_path, std::ios::in | std::ios::out | std::ios::binary);
  if (!f) {
    throw std::runtime_error("Cannot open disk image for FAT32 write: " +
                             disk_path.string());
  }
  f.seekp(static_cast<std::streamoff>(partition_offset));
  f.write(reinterpret_cast<const char*>(fat_img.data()),
          static_cast<std::streamsize>(fat_img.size()));
}

namespace {

struct FatReader {
  std::ifstream f;
  uint64_t part_off = 0;
  uint32_t bytes_per_sec = 512;
  uint32_t sec_per_clus = 8;
  uint32_t rsvd_sec = 32;
  uint32_t num_fats = 2;
  uint32_t fat_sz = 0;
  uint32_t root_clus = 2;

  FatReader(const std::filesystem::path& path, uint64_t offset)
      : f(path, std::ios::binary), part_off(offset) {
    if (!f) {
      throw std::runtime_error("Cannot open FAT32 image: " + path.string());
    }
    uint8_t bs[512]{};
    f.seekg(static_cast<std::streamoff>(part_off));
    f.read(reinterpret_cast<char*>(bs), 512);
    if (bs[510] != 0x55 || bs[511] != 0xAA) {
      throw std::runtime_error("Invalid FAT32 boot sector signature");
    }
    bytes_per_sec = GetLe16(bs + 11);
    sec_per_clus = bs[13];
    rsvd_sec = GetLe16(bs + 14);
    num_fats = bs[16];
    fat_sz = GetLe32(bs + 36);
    root_clus = GetLe32(bs + 44);
  }

  uint32_t NextCluster(uint32_t clus) {
    uint64_t off =
        part_off + static_cast<uint64_t>(rsvd_sec) * bytes_per_sec + clus * 4;
    uint8_t b[4]{};
    f.seekg(static_cast<std::streamoff>(off));
    f.read(reinterpret_cast<char*>(b), 4);
    return GetLe32(b) & 0x0FFFFFFF;
  }

  std::vector<uint8_t> ReadClusterChain(uint32_t start_clus,
                                        uint32_t max_bytes = 0) {
    std::vector<uint8_t> out;
    if (start_clus < 2) return out;
    uint32_t clus_sz = bytes_per_sec * sec_per_clus;
    uint32_t data_start_sec = rsvd_sec + num_fats * fat_sz;
    uint32_t cur = start_clus;
    while (cur >= 2 && cur < 0x0FFFFFF8) {
      uint64_t off =
          part_off +
          static_cast<uint64_t>(data_start_sec + (cur - 2) * sec_per_clus) *
              bytes_per_sec;
      size_t prev = out.size();
      out.resize(prev + clus_sz);
      f.seekg(static_cast<std::streamoff>(off));
      f.read(reinterpret_cast<char*>(out.data() + prev), clus_sz);
      if (max_bytes > 0 && out.size() >= max_bytes) {
        out.resize(max_bytes);
        break;
      }
      cur = NextCluster(cur);
    }
    return out;
  }
};

std::string ParseShortName(const uint8_t d[32]) {
  std::string base(reinterpret_cast<const char*>(d), 8);
  while (!base.empty() && base.back() == ' ') base.pop_back();
  std::string ext(reinterpret_cast<const char*>(d + 8), 3);
  while (!ext.empty() && ext.back() == ' ') ext.pop_back();
  return ext.empty() ? base : (base + "." + ext);
}

void WalkFatDir(FatReader* r, uint32_t dir_clus, const std::string& prefix,
                std::vector<FatEntryInfo>* out) {
  auto data = r->ReadClusterChain(dir_clus);
  std::map<int, std::string> lfn_parts;
  for (size_t off = 0; off + 32 <= data.size(); off += 32) {
    const uint8_t* d = data.data() + off;
    if (d[0] == 0x00) break;
    if (d[0] == 0xE5) {
      lfn_parts.clear();
      continue;
    }
    uint8_t attr = d[11];
    if (attr == 0x0F) {
      int ord = d[0] & 0x1F;
      if (d[0] & 0x40) lfn_parts.clear();
      const int offsets[13] = {1,  3,  5,  7,  9,  14, 16,
                               18, 20, 22, 24, 28, 30};
      std::string part;
      for (int k = 0; k < 13; ++k) {
        uint16_t ch = GetLe16(d + offsets[k]);
        if (ch == 0x0000 || ch == 0xFFFF) break;
        part.push_back(static_cast<char>(ch & 0xFF));
      }
      lfn_parts[ord] = part;
      continue;
    }
    if (attr & 0x08) {
      lfn_parts.clear();
      continue;
    }
    std::string name;
    if (!lfn_parts.empty()) {
      for (const auto& [_, p] : lfn_parts) name += p;
      lfn_parts.clear();
    } else {
      name = ParseShortName(d);
    }
    if (name == "." || name == "..") continue;

    uint32_t clus =
        (static_cast<uint32_t>(GetLe16(d + 20)) << 16) | GetLe16(d + 26);
    uint32_t sz = GetLe32(d + 28);
    bool is_dir = (attr & 0x10) != 0;
    std::string full = prefix.empty() ? name : (prefix + "/" + name);
    out->push_back(FatEntryInfo{full, is_dir, sz, clus});
    if (is_dir && clus >= 2) {
      WalkFatDir(r, clus, full, out);
    }
  }
}

std::string ToLower(std::string s) {
  for (char& c : s) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return s;
}

}  // namespace

std::vector<FatEntryInfo> ListFat32Entries(
    const std::filesystem::path& disk_path, uint64_t partition_offset) {
  FatReader r(disk_path, partition_offset);
  std::vector<FatEntryInfo> out;
  WalkFatDir(&r, r.root_clus, "", &out);
  return out;
}

std::vector<uint8_t> ReadFat32File(const std::filesystem::path& disk_path,
                                   uint64_t partition_offset,
                                   const std::string& file_path) {
  std::string want = ToLower(file_path);
  while (!want.empty() && want.front() == '/') want.erase(want.begin());
  FatReader r(disk_path, partition_offset);
  std::vector<FatEntryInfo> entries;
  WalkFatDir(&r, r.root_clus, "", &entries);
  for (const auto& e : entries) {
    if (!e.is_dir && ToLower(e.path) == want) {
      if (e.size == 0) return {};
      return r.ReadClusterChain(e.first_cluster, e.size);
    }
  }
  throw std::runtime_error("FAT32 file not found: " + file_path);
}

}  // namespace googlebookos::fat32
