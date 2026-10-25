#include "src/common/lz4_frame.h"

#include "src/common/sha256.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <fstream>

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

void WriteLE16(std::vector<uint8_t>* out, uint16_t v) {
  out->push_back(static_cast<uint8_t>(v & 0xFF));
  out->push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
}

void WriteLE32(std::vector<uint8_t>* out, uint32_t v) {
  out->push_back(static_cast<uint8_t>(v & 0xFF));
  out->push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
  out->push_back(static_cast<uint8_t>((v >> 16) & 0xFF));
  out->push_back(static_cast<uint8_t>((v >> 24) & 0xFF));
}

void WriteLE64(std::vector<uint8_t>* out, uint64_t v) {
  WriteLE32(out, static_cast<uint32_t>(v & 0xFFFFFFFFULL));
  WriteLE32(out, static_cast<uint32_t>((v >> 32) & 0xFFFFFFFFULL));
}

uint32_t Hash4(const uint8_t* p) {
  return (ReadLE32(p) * 2654435761U) >> 16;
}

}  // namespace

bool Lz4DecompressBlock(std::span<const uint8_t> src,
                        std::vector<uint8_t>* out,
                        std::string* error) {
  const uint8_t* ip = src.data();
  const uint8_t* const iend = ip + src.size();

  while (ip < iend) {
    uint8_t token = *ip++;
    size_t lit_len = token >> 4;
    if (lit_len == 15) {
      while (true) {
        if (ip >= iend) {
          if (error) *error = "LZ4 block truncated in literal length";
          return false;
        }
        uint8_t s = *ip++;
        lit_len += s;
        if (s != 255) break;
      }
    }
    if (static_cast<size_t>(iend - ip) < lit_len) {
      if (error) *error = "LZ4 block truncated in literals";
      return false;
    }
    out->insert(out->end(), ip, ip + lit_len);
    ip += lit_len;

    if (ip == iend) {
      return true;
    }

    if (iend - ip < 2) {
      if (error) *error = "LZ4 block truncated in match offset";
      return false;
    }
    uint16_t offset = ReadLE16(ip);
    ip += 2;
    if (offset == 0 || offset > out->size()) {
      if (error) *error = "LZ4 invalid match offset";
      return false;
    }

    size_t match_len = token & 0x0F;
    if (match_len == 15) {
      while (true) {
        if (ip >= iend) {
          if (error) *error = "LZ4 block truncated in match length";
          return false;
        }
        uint8_t s = *ip++;
        match_len += s;
        if (s != 255) break;
      }
    }
    match_len += 4;

    size_t match_pos = out->size() - offset;
    out->reserve(out->size() + match_len);
    for (size_t i = 0; i < match_len; ++i) {
      uint8_t b = (*out)[match_pos + i];
      out->push_back(b);
    }
  }
  return true;
}

std::vector<uint8_t> Lz4CompressBlock(std::span<const uint8_t> src) {
  std::vector<uint8_t> out;
  if (src.empty()) return out;

  out.reserve(src.size() + (src.size() / 255) + 16);
  const uint8_t* const base = src.data();
  const uint8_t* ip = base;
  const uint8_t* anchor = ip;
  const uint8_t* const iend = base + src.size();
  const uint8_t* const mflimit = (src.size() >= 12) ? (iend - 12) : base;
  const uint8_t* const matchlimit = (src.size() >= 5) ? (iend - 5) : base;

  if (src.size() < 13) {
    size_t lit_len = src.size();
    uint8_t token = static_cast<uint8_t>(std::min<size_t>(lit_len, 15) << 4);
    out.push_back(token);
    if (lit_len >= 15) {
      size_t rem = lit_len - 15;
      while (rem >= 255) {
        out.push_back(255);
        rem -= 255;
      }
      out.push_back(static_cast<uint8_t>(rem));
    }
    out.insert(out.end(), base, iend);
    return out;
  }

  std::vector<int32_t> hash_table(65536, -1);
  ++ip;

  while (ip <= mflimit) {
    const uint8_t* match = nullptr;
    while (ip <= mflimit) {
      uint32_t h = Hash4(ip);
      int32_t ref_idx = hash_table[h];
      hash_table[h] = static_cast<int32_t>(ip - base);
      if (ref_idx >= 0) {
        const uint8_t* cand = base + ref_idx;
        if (ip - cand <= 65535 && ReadLE32(cand) == ReadLE32(ip)) {
          match = cand;
          break;
        }
      }
      ++ip;
    }
    if (!match) break;

    while (ip > anchor && match > base && ip[-1] == match[-1]) {
      --ip;
      --match;
    }

    size_t lit_len = static_cast<size_t>(ip - anchor);
    size_t token_pos = out.size();
    out.push_back(0);
    if (lit_len >= 15) {
      size_t rem = lit_len - 15;
      while (rem >= 255) {
        out.push_back(255);
        rem -= 255;
      }
      out.push_back(static_cast<uint8_t>(rem));
    }
    out.insert(out.end(), anchor, anchor + lit_len);

    uint16_t offset = static_cast<uint16_t>(ip - match);
    WriteLE16(&out, offset);

    ip += 4;
    match += 4;
    const uint8_t* match_start = ip;
    while (ip < matchlimit && *ip == *match) {
      ++ip;
      ++match;
    }
    size_t match_len_minus_4 = static_cast<size_t>(ip - match_start);

    uint8_t lit_nibble = static_cast<uint8_t>(std::min<size_t>(lit_len, 15));
    uint8_t match_nibble = static_cast<uint8_t>(std::min<size_t>(match_len_minus_4, 15));
    out[token_pos] = static_cast<uint8_t>((lit_nibble << 4) | match_nibble);

    if (match_len_minus_4 >= 15) {
      size_t rem = match_len_minus_4 - 15;
      while (rem >= 255) {
        out.push_back(255);
        rem -= 255;
      }
      out.push_back(static_cast<uint8_t>(rem));
    }

    anchor = ip;
    if (ip > mflimit) break;
    hash_table[Hash4(ip - 2)] = static_cast<int32_t>((ip - 2) - base);
  }

  size_t last_lit = static_cast<size_t>(iend - anchor);
  uint8_t token = static_cast<uint8_t>(std::min<size_t>(last_lit, 15) << 4);
  out.push_back(token);
  if (last_lit >= 15) {
    size_t rem = last_lit - 15;
    while (rem >= 255) {
      out.push_back(255);
      rem -= 255;
    }
    out.push_back(static_cast<uint8_t>(rem));
  }
  out.insert(out.end(), anchor, iend);
  return out;
}

bool Lz4DecompressFrame(std::span<const uint8_t> src,
                        std::vector<uint8_t>* out,
                        std::string* error) {
  size_t pos = 0;
  if (src.size() < 4) {
    if (error) *error = "LZ4 stream too short";
    return false;
  }

  while (pos < src.size()) {
    // Skip trailing zero padding if any between frames.
    if (src.size() - pos < 4) {
      if (error) *error = "LZ4 trailing bytes after frame";
      return false;
    }
    uint32_t magic = ReadLE32(src.data() + pos);
    pos += 4;

    if (magic == kLz4FrameMagic) {
      if (pos + 2 > src.size()) {
        if (error) *error = "LZ4 frame header truncated";
        return false;
      }
      uint8_t flg = src[pos];
      uint8_t version = (flg >> 6) & 0x03;
      if (version != 1) {
        if (error) *error = "Unsupported LZ4 frame version";
        return false;
      }
      bool block_checksum = ((flg >> 4) & 0x01) != 0;
      bool content_size_flag = ((flg >> 3) & 0x01) != 0;
      bool content_checksum_flag = ((flg >> 2) & 0x01) != 0;
      bool dict_id_flag = (flg & 0x01) != 0;

      size_t desc_len = 2 + (content_size_flag ? 8 : 0) + (dict_id_flag ? 4 : 0);
      if (pos + desc_len + 1 > src.size()) {
        if (error) *error = "LZ4 frame descriptor truncated";
        return false;
      }
      pos += desc_len + 1;  // Skip descriptor + HC byte

      size_t frame_out_start = out->size();
      while (true) {
        if (pos + 4 > src.size()) {
          if (error) *error = "LZ4 frame truncated before block size";
          return false;
        }
        uint32_t block_size = ReadLE32(src.data() + pos);
        pos += 4;
        if (block_size == 0) {
          if (content_checksum_flag) {
            if (pos + 4 > src.size()) {
              if (error) *error = "LZ4 frame truncated in content checksum";
              return false;
            }
            uint32_t expected_csum = ReadLE32(src.data() + pos);
            pos += 4;
            uint32_t got_csum = XxHash32(std::span<const uint8_t>(
                out->data() + frame_out_start, out->size() - frame_out_start));
            if (got_csum != expected_csum) {
              if (error) *error = "LZ4 frame content checksum mismatch";
              return false;
            }
          }
          break;
        }

        bool is_uncompressed = (block_size & 0x80000000U) != 0;
        uint32_t raw_size = block_size & 0x7FFFFFFFU;
        if (pos + raw_size > src.size()) {
          if (error) *error = "LZ4 block payload truncated";
          return false;
        }
        if (is_uncompressed) {
          out->insert(out->end(), src.data() + pos, src.data() + pos + raw_size);
        } else {
          if (!Lz4DecompressBlock(src.subspan(pos, raw_size), out, error)) {
            return false;
          }
        }
        pos += raw_size;
        if (block_checksum) {
          if (pos + 4 > src.size()) {
            if (error) *error = "LZ4 block checksum truncated";
            return false;
          }
          pos += 4;
        }
      }
    } else if (magic == kLz4LegacyMagic) {
      bool got_any_block = false;
      static constexpr uint32_t kMaxLegacyChunk =
          8 * 1024 * 1024 + (8 * 1024 * 1024) / 255 + 64;
      while (pos < src.size()) {
        if (pos + 4 > src.size()) {
          if (got_any_block) return true;
          if (error) *error = "LZ4 legacy frame truncated";
          return false;
        }
        uint32_t chunk_size = ReadLE32(src.data() + pos);
        if (chunk_size == kLz4LegacyMagic || chunk_size == kLz4FrameMagic ||
            (chunk_size >= 0x184D2A50U && chunk_size <= 0x184D2A5FU)) {
          break;  // Next concatenated frame
        }
        if (chunk_size == 0 || chunk_size > kMaxLegacyChunk ||
            pos + 4 + chunk_size > src.size()) {
          if (got_any_block) return true;
          if (error) *error = "LZ4 legacy block truncated";
          return false;
        }
        pos += 4;
        size_t prev_out_size = out->size();
        if (!Lz4DecompressBlock(src.subspan(pos, chunk_size), out, error)) {
          if (got_any_block) {
            out->resize(prev_out_size);
            return true;
          }
          return false;
        }
        got_any_block = true;
        pos += chunk_size;
      }
    } else if (magic >= 0x184D2A50U && magic <= 0x184D2A5FU) {
      // Skippable frame
      if (pos + 4 > src.size()) {
        if (error) *error = "LZ4 skippable frame truncated";
        return false;
      }
      uint32_t skip_len = ReadLE32(src.data() + pos);
      pos += 4;
      if (pos + skip_len > src.size()) {
        if (error) *error = "LZ4 skippable payload truncated";
        return false;
      }
      pos += skip_len;
    } else {
      if (error) *error = "Unrecognized LZ4 frame magic";
      return false;
    }
  }
  return true;
}

std::vector<uint8_t> Lz4CompressFrame(std::span<const uint8_t> src,
                                      bool legacy,
                                      bool include_content_size) {
  std::vector<uint8_t> out;
  if (legacy) {
    WriteLE32(&out, kLz4LegacyMagic);
    static constexpr size_t kLegacyBlockSize = 8 * 1024 * 1024;
    size_t pos = 0;
    while (pos < src.size()) {
      size_t chunk_len = std::min<size_t>(kLegacyBlockSize, src.size() - pos);
      auto compressed = Lz4CompressBlock(src.subspan(pos, chunk_len));
      WriteLE32(&out, static_cast<uint32_t>(compressed.size()));
      out.insert(out.end(), compressed.begin(), compressed.end());
      pos += chunk_len;
    }
    return out;
  }

  WriteLE32(&out, kLz4FrameMagic);
  size_t desc_start = out.size();
  uint8_t flg = 0x64;  // Version=1 (01), BlockIndep=1, ContentChecksum=1
  if (include_content_size) flg |= 0x08;
  uint8_t bd = 0x70;  // 4 MiB max block size
  out.push_back(flg);
  out.push_back(bd);
  if (include_content_size) {
    WriteLE64(&out, static_cast<uint64_t>(src.size()));
  }
  uint8_t hc = static_cast<uint8_t>(
      (XxHash32(std::span<const uint8_t>(out.data() + desc_start,
                                         out.size() - desc_start),
                0) >>
       8) &
      0xFF);
  out.push_back(hc);

  static constexpr size_t kMaxBlockSize = 4 * 1024 * 1024;
  size_t pos = 0;
  while (pos < src.size()) {
    size_t chunk_len = std::min<size_t>(kMaxBlockSize, src.size() - pos);
    auto compressed = Lz4CompressBlock(src.subspan(pos, chunk_len));
    if (compressed.size() >= chunk_len) {
      WriteLE32(&out, static_cast<uint32_t>(chunk_len) | 0x80000000U);
      out.insert(out.end(), src.data() + pos, src.data() + pos + chunk_len);
    } else {
      WriteLE32(&out, static_cast<uint32_t>(compressed.size()));
      out.insert(out.end(), compressed.begin(), compressed.end());
    }
    pos += chunk_len;
  }

  WriteLE32(&out, 0);  // EndMark
  uint32_t content_csum = XxHash32(src, 0);
  WriteLE32(&out, content_csum);
  return out;
}

bool Lz4DecompressFile(const std::filesystem::path& in_path,
                       const std::filesystem::path& out_path,
                       std::string* error) {
  std::ifstream in(in_path, std::ios::binary);
  if (!in) {
    if (error) *error = "Cannot open input file: " + in_path.string();
    return false;
  }
  std::vector<uint8_t> src((std::istreambuf_iterator<char>(in)),
                           std::istreambuf_iterator<char>());
  std::vector<uint8_t> dst;
  if (!Lz4DecompressFrame(src, &dst, error)) {
    return false;
  }
  std::ofstream out(out_path, std::ios::binary | std::ios::trunc);
  if (!out) {
    if (error) *error = "Cannot open output file: " + out_path.string();
    return false;
  }
  out.write(reinterpret_cast<const char*>(dst.data()),
            static_cast<std::streamsize>(dst.size()));
  return out.good();
}

bool Lz4CompressFile(const std::filesystem::path& in_path,
                     const std::filesystem::path& out_path,
                     bool legacy,
                     std::string* error) {
  std::ifstream in(in_path, std::ios::binary);
  if (!in) {
    if (error) *error = "Cannot open input file: " + in_path.string();
    return false;
  }
  std::vector<uint8_t> src((std::istreambuf_iterator<char>(in)),
                           std::istreambuf_iterator<char>());
  auto dst = Lz4CompressFrame(src, legacy);
  std::ofstream out(out_path, std::ios::binary | std::ios::trunc);
  if (!out) {
    if (error) *error = "Cannot open output file: " + out_path.string();
    return false;
  }
  out.write(reinterpret_cast<const char*>(dst.data()),
            static_cast<std::streamsize>(dst.size()));
  return out.good();
}

}  // namespace gbos
