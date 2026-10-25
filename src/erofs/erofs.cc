#include "src/erofs/erofs.h"

#include <sys/stat.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <vector>

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

uint64_t ReadBE64(const uint8_t* p) {
  uint64_t v = 0;
  for (int i = 0; i < 8; ++i) {
    v = (v << 8) | p[i];
  }
  return v;
}

size_t AlignUp4(size_t x) { return (x + 3) & ~static_cast<size_t>(3); }

std::string FormatUuidBE(const uint8_t* u) {
  char buf[40];
  std::snprintf(buf, sizeof(buf),
                "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7],
                u[8], u[9], u[10], u[11], u[12], u[13], u[14], u[15]);
  return std::string(buf);
}

std::pair<std::pair<uint8_t, std::string>, std::vector<uint8_t>> ParseXattrEntry(
    std::span<const uint8_t> data,
    size_t pos,
    size_t* next_pos) {
  uint8_t nlen = data[pos];
  uint8_t nidx = data[pos + 1];
  uint16_t vsz = ReadLE16(data.data() + pos + 2);
  std::string name(reinterpret_cast<const char*>(data.data() + pos + 4), nlen);
  std::vector<uint8_t> val(data.begin() + pos + 4 + nlen,
                           data.begin() + pos + 4 + nlen + vsz);
  *next_pos = AlignUp4(pos + 4 + nlen + vsz);
  return {{nidx, std::move(name)}, std::move(val)};
}

std::string RegexEscape(std::string_view s) {
  std::string out;
  out.reserve(s.size() * 2);
  for (char c : s) {
    if (std::strchr(".^$*+?()[]{}\\|", c) != nullptr) {
      out.push_back('\\');
    }
    out.push_back(c);
  }
  return out;
}

}  // namespace

std::optional<uint64_t> ReadAvbOriginalImageSize(std::span<const uint8_t> tail_64,
                                                 std::string* error) {
  if (tail_64.size() < 64) {
    if (error) *error = "AVB footer buffer smaller than 64 bytes";
    return std::nullopt;
  }
  const uint8_t* ftr = tail_64.data() + tail_64.size() - 64;
  if (std::memcmp(ftr, "AVBf", 4) != 0) {
    if (error) *error = "Missing 'AVBf' magic in AVB footer";
    return std::nullopt;
  }
  return ReadBE64(ftr + 12);
}

std::optional<uint64_t> ReadAvbOriginalImageSizeFromFile(
    const std::filesystem::path& img_path,
    std::string* error) {
  std::ifstream f(img_path, std::ios::binary | std::ios::ate);
  if (!f) {
    if (error) *error = "Cannot open file: " + img_path.string();
    return std::nullopt;
  }
  auto sz = f.tellg();
  if (sz < 64) {
    if (error) *error = "File smaller than 64-byte AVB footer";
    return std::nullopt;
  }
  f.seekg(sz - static_cast<std::streamoff>(64), std::ios::beg);
  std::vector<uint8_t> tail(64);
  f.read(reinterpret_cast<char*>(tail.data()), 64);
  return ReadAvbOriginalImageSize(tail, error);
}

std::optional<std::string> ReadErofsUuid(std::span<const uint8_t> erofs_head,
                                         std::string* error) {
  if (erofs_head.size() < 1024 + 64) {
    if (error) *error = "Buffer too small for EROFS superblock";
    return std::nullopt;
  }
  const uint8_t* sb = erofs_head.data() + 1024;
  if (ReadLE32(sb) != 0xE0F5E1E2U) {
    if (error) *error = "Missing EROFS superblock magic 0xE0F5E1E2";
    return std::nullopt;
  }
  return FormatUuidBE(sb + 48);
}

std::optional<std::string> ReadErofsUuidFromFile(
    const std::filesystem::path& erofs_path,
    std::string* error) {
  std::ifstream f(erofs_path, std::ios::binary);
  if (!f) {
    if (error) *error = "Cannot open EROFS file: " + erofs_path.string();
    return std::nullopt;
  }
  std::vector<uint8_t> head(2048);
  f.read(reinterpret_cast<char*>(head.data()), static_cast<std::streamsize>(head.size()));
  head.resize(static_cast<size_t>(f.gcount()));
  return ReadErofsUuid(head, error);
}

std::optional<ErofsImageMeta> WalkErofsImageBuffer(std::span<const uint8_t> data,
                                                   std::string* error) {
  if (data.size() < 1024 + 128) {
    if (error) *error = "EROFS buffer too small";
    return std::nullopt;
  }
  const uint8_t* sb = data.data() + 1024;
  if (ReadLE32(sb) != 0xE0F5E1E2U) {
    if (error) *error = "Invalid EROFS superblock magic";
    return std::nullopt;
  }

  ErofsImageMeta meta;
  uint8_t blkszbits = sb[12];
  if (blkszbits < 9 || blkszbits > 16) {
    if (error) *error = "Invalid EROFS blkszbits";
    return std::nullopt;
  }
  meta.block_size = 1U << blkszbits;
  meta.root_nid = ReadLE16(sb + 14);
  uint32_t meta_blk = ReadLE32(sb + 36);
  uint32_t xattr_blk = ReadLE32(sb + 40);
  meta.uuid = FormatUuidBE(sb + 48);

  size_t meta_off = static_cast<size_t>(meta_blk) * meta.block_size;
  size_t xattr_off = static_cast<size_t>(xattr_blk) * meta.block_size;

  struct ParsedInode {
    uint16_t mode = 0;
    uint32_t uid = 0;
    uint32_t gid = 0;
    uint64_t size = 0;
    uint32_t raw_blk = 0;
    uint8_t datalayout = 0;
    size_t tail_off = 0;
    std::string selinux;
    uint64_t caps = 0;
  };

  auto read_inode = [&](uint64_t nid, ParsedInode* out) -> bool {
    size_t pos = meta_off + static_cast<size_t>(nid) * 32;
    if (pos + 32 > data.size()) return false;
    uint16_t fmt = ReadLE16(data.data() + pos);
    uint8_t ver = fmt & 1;
    out->datalayout = (fmt >> 1) & 7;
    size_t isz = 0;
    uint16_t xic = 0;
    if (ver == 0) {
      xic = ReadLE16(data.data() + pos + 2);
      out->mode = ReadLE16(data.data() + pos + 4);
      out->size = ReadLE32(data.data() + pos + 8);
      out->raw_blk = ReadLE32(data.data() + pos + 16);
      out->uid = ReadLE16(data.data() + pos + 24);
      out->gid = ReadLE16(data.data() + pos + 26);
      isz = 32;
    } else {
      if (pos + 64 > data.size()) return false;
      xic = ReadLE16(data.data() + pos + 2);
      out->mode = ReadLE16(data.data() + pos + 4);
      out->size = ReadLE64(data.data() + pos + 8);
      out->raw_blk = ReadLE32(data.data() + pos + 16);
      out->uid = ReadLE32(data.data() + pos + 24);
      out->gid = ReadLE32(data.data() + pos + 28);
      isz = 64;
    }
    size_t xsz = (xic > 0) ? (12 + static_cast<size_t>(xic - 1) * 4) : 0;
    out->tail_off = pos + isz + xsz;
    if (out->tail_off > data.size()) return false;

    if (xic > 0) {
      size_t xp = pos + isz;
      uint8_t sh_count = data[xp + 8];
      size_t cur = xp + 12;
      std::vector<std::pair<std::pair<uint8_t, std::string>, std::vector<uint8_t>>> xattrs;
      for (uint8_t i = 0; i < sh_count; ++i) {
        uint32_t sid = ReadLE32(data.data() + cur);
        cur += 4;
        size_t sp = xattr_off + static_cast<size_t>(sid) * 4;
        if (sp + 4 <= data.size()) {
          size_t dummy = 0;
          xattrs.push_back(ParseXattrEntry(data, sp, &dummy));
        }
      }
      size_t end = xp + xsz;
      while (cur + 4 <= end) {
        xattrs.push_back(ParseXattrEntry(data, cur, &cur));
      }
      for (const auto& [k, v] : xattrs) {
        if (k.first == 6 && k.second == "selinux") {
          size_t slen = v.size();
          while (slen > 0 && v[slen - 1] == 0) --slen;
          out->selinux.assign(reinterpret_cast<const char*>(v.data()), slen);
        } else if (k.first == 6 && k.second == "capability" && v.size() >= 20) {
          uint32_t lo = ReadLE32(v.data() + 4);
          uint32_t hi = ReadLE32(v.data() + 12);
          out->caps = static_cast<uint64_t>(lo) | (static_cast<uint64_t>(hi) << 32);
        }
      }
    }
    return true;
  };

  auto read_dir_bytes = [&](const ParsedInode& ino) -> std::vector<uint8_t> {
    uint64_t sz = ino.size;
    uint64_t nfull = sz / meta.block_size;
    uint64_t rem = sz % meta.block_size;
    std::vector<uint8_t> out;
    out.reserve(static_cast<size_t>(sz));
    for (uint64_t b = 0; b < nfull; ++b) {
      size_t boff = static_cast<size_t>(ino.raw_blk + b) * meta.block_size;
      if (boff + meta.block_size <= data.size()) {
        out.insert(out.end(), data.begin() + boff,
                   data.begin() + boff + meta.block_size);
      }
    }
    if (rem > 0) {
      if (ino.datalayout == 2) {
        if (ino.tail_off + rem <= data.size()) {
          out.insert(out.end(), data.begin() + ino.tail_off,
                     data.begin() + ino.tail_off + static_cast<size_t>(rem));
        }
      } else {
        size_t boff = static_cast<size_t>(ino.raw_blk + nfull) * meta.block_size;
        if (boff + rem <= data.size()) {
          out.insert(out.end(), data.begin() + boff,
                     data.begin() + boff + static_cast<size_t>(rem));
        }
      }
    }
    return out;
  };

  auto walk = [&](auto& self, uint64_t nid, const std::string& rel) -> bool {
    ParsedInode ino;
    if (!read_inode(nid, &ino)) return false;
    meta.entries[rel] = ErofsEntryMeta{
        .mode = static_cast<uint32_t>(ino.mode & 07777),
        .uid = ino.uid,
        .gid = ino.gid,
        .selinux = ino.selinux,
        .caps = ino.caps,
    };
    if (S_ISDIR(ino.mode)) {
      std::vector<uint8_t> raw = read_dir_bytes(ino);
      size_t off = 0;
      while (off < raw.size()) {
        size_t chunk_len = std::min<size_t>(meta.block_size, raw.size() - off);
        const uint8_t* blk = raw.data() + off;
        if (chunk_len < 12) break;
        uint16_t first_nameoff = ReadLE16(blk + 8);
        size_t nent = first_nameoff / 12;
        std::vector<std::tuple<uint64_t, uint16_t, uint8_t>> ents;
        for (size_t i = 0; i < nent; ++i) {
          if ((i + 1) * 12 > chunk_len) break;
          uint64_t cnid = ReadLE64(blk + i * 12);
          uint16_t noff = ReadLE16(blk + i * 12 + 8);
          uint8_t ftype = blk[i * 12 + 10];
          ents.emplace_back(cnid, noff, ftype);
        }
        for (size_t i = 0; i < ents.size(); ++i) {
          auto [cnid, noff, ftype] = ents[i];
          size_t end_off = (i + 1 < ents.size()) ? std::get<1>(ents[i + 1]) : chunk_len;
          if (noff >= chunk_len || end_off > chunk_len || end_off < noff) continue;
          size_t nlen = end_off - noff;
          while (nlen > 0 && blk[noff + nlen - 1] == 0) --nlen;
          std::string name(reinterpret_cast<const char*>(blk + noff), nlen);
          if (name == "." || name == ".." || name.empty()) continue;
          std::string child_rel = rel.empty() ? name : (rel + "/" + name);
          if (!self(self, cnid, child_rel)) return false;
        }
        off += meta.block_size;
      }
    }
    return true;
  };

  if (!walk(walk, meta.root_nid, "")) {
    if (error) *error = "Failed to walk EROFS directory tree";
    return std::nullopt;
  }
  return meta;
}

std::optional<ErofsImageMeta> WalkErofsImageFile(const std::filesystem::path& erofs_path,
                                                 std::string* error) {
  std::ifstream f(erofs_path, std::ios::binary);
  if (!f) {
    if (error) *error = "Cannot open EROFS image: " + erofs_path.string();
    return std::nullopt;
  }
  std::vector<uint8_t> data((std::istreambuf_iterator<char>(f)),
                            std::istreambuf_iterator<char>());
  return WalkErofsImageBuffer(data, error);
}

bool WriteErofsConfigs(std::string_view mount_point,
                       const std::map<std::string, ErofsEntryMeta>& entries,
                       const std::filesystem::path& file_contexts_out,
                       const std::filesystem::path& fs_config_out,
                       std::string* error) {
  std::string mp(mount_point);
  while (!mp.empty() && mp.front() == '/') mp.erase(mp.begin());
  while (!mp.empty() && mp.back() == '/') mp.pop_back();

  auto root_it = entries.find("");
  if (root_it == entries.end()) {
    if (error) *error = "Root entry missing from EROFS metadata map";
    return false;
  }

  std::ostringstream fc;
  std::ostringstream fsc;

  fc << "/" << RegexEscape(mp) << " " << root_it->second.selinux << "\n";
  fc << "/" << RegexEscape(mp) << "/ " << root_it->second.selinux << "\n";
  fsc << "/ " << root_it->second.uid << " " << root_it->second.gid << " "
      << std::oct << std::setw(4) << std::setfill('0') << root_it->second.mode
      << std::dec << "\n";
  fsc << mp << " " << root_it->second.uid << " " << root_it->second.gid << " "
      << std::oct << std::setw(4) << std::setfill('0') << root_it->second.mode
      << std::dec << "\n";

  for (const auto& [rel, m] : entries) {
    if (rel.empty()) continue;
    fc << "/" << RegexEscape(mp + "/" + rel) << " " << m.selinux << "\n";
    fsc << mp << "/" << rel << " " << m.uid << " " << m.gid << " "
        << std::oct << std::setw(4) << std::setfill('0') << m.mode << std::dec;
    if (m.caps != 0) {
      fsc << " capabilities=0x" << std::hex << m.caps << std::dec;
    }
    fsc << "\n";
  }

  std::ofstream fco(file_contexts_out, std::ios::binary | std::ios::trunc);
  std::ofstream fso(fs_config_out, std::ios::binary | std::ios::trunc);
  if (!fco || !fso) {
    if (error) *error = "Cannot write file_contexts or fs_config";
    return false;
  }
  fco << fc.str();
  fso << fsc.str();
  return fco.good() && fso.good();
}

}  // namespace gbos
