#include "src/boot/bootimg.h"

#include "src/common/lz4_frame.h"
#include "src/common/sha256.h"
#include "src/gpt/gpt.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <set>
#include <sstream>

namespace gbos {
namespace {

uint32_t ReadLE32(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) |
         (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

uint64_t ReadBE64(const uint8_t* p) {
  uint64_t v = 0;
  for (int i = 0; i < 8; ++i) {
    v = (v << 8) | p[i];
  }
  return v;
}

void WriteLE32(std::vector<uint8_t>* out, uint32_t v) {
  out->push_back(static_cast<uint8_t>(v & 0xFF));
  out->push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
  out->push_back(static_cast<uint8_t>((v >> 16) & 0xFF));
  out->push_back(static_cast<uint8_t>((v >> 24) & 0xFF));
}

uint64_t AlignUp(uint64_t x, uint64_t a) {
  return ((x + a - 1) / a) * a;
}

bool PreadFile(std::ifstream& f, uint64_t offset, size_t len, std::vector<uint8_t>* out) {
  out->resize(len);
  f.clear();
  f.seekg(static_cast<std::streamoff>(offset), std::ios::beg);
  if (!f) return false;
  f.read(reinterpret_cast<char*>(out->data()), static_cast<std::streamsize>(len));
  return static_cast<size_t>(f.gcount()) == len;
}

bool WriteBytesToFile(const std::filesystem::path& p, std::span<const uint8_t> data) {
  std::ofstream out(p, std::ios::binary | std::ios::trunc);
  if (!out) return false;
  out.write(reinterpret_cast<const char*>(data.data()),
            static_cast<std::streamsize>(data.size()));
  return out.good();
}

uint32_t ParseHex8(const uint8_t* p, bool* ok) {
  uint32_t val = 0;
  for (int i = 0; i < 8; ++i) {
    char c = static_cast<char>(p[i]);
    uint32_t d = 0;
    if (c >= '0' && c <= '9') {
      d = static_cast<uint32_t>(c - '0');
    } else if (c >= 'a' && c <= 'f') {
      d = static_cast<uint32_t>(c - 'a' + 10);
    } else if (c >= 'A' && c <= 'F') {
      d = static_cast<uint32_t>(c - 'A' + 10);
    } else {
      *ok = false;
      return 0;
    }
    val = (val << 4) | d;
  }
  return val;
}

}  // namespace

std::optional<VbmetaInfo> ParseVbmetaBuffer(std::span<const uint8_t> buf,
                                            std::string* error) {
  if (buf.size() < 256 || std::memcmp(buf.data(), "AVB0", 4) != 0) {
    if (error) *error = "Invalid AVB0 magic in vbmeta buffer";
    return std::nullopt;
  }
  uint64_t auth_size = ReadBE64(buf.data() + 12);
  uint64_t aux_size = ReadBE64(buf.data() + 20);
  uint64_t total = 256ULL + auth_size + aux_size;
  if (total > buf.size()) {
    if (error) *error = "vbmeta buffer shorter than header+auth+aux";
    return std::nullopt;
  }
  VbmetaInfo info;
  info.total_size = total;
  info.sha256_hex = Sha256Hex(buf.subspan(0, static_cast<size_t>(total)));
  return info;
}

std::optional<VbmetaInfo> ParseVbmetaFile(const std::filesystem::path& path,
                                          std::string* error) {
  std::ifstream f(path, std::ios::binary);
  if (!f) {
    if (error) *error = "Cannot open vbmeta file: " + path.string();
    return std::nullopt;
  }
  std::vector<uint8_t> buf((std::istreambuf_iterator<char>(f)),
                           std::istreambuf_iterator<char>());
  return ParseVbmetaBuffer(buf, error);
}

std::vector<uint8_t> AppendBootconfigTrailer(std::span<const uint8_t> archive_bytes,
                                             std::string_view bootconfig_text) {
  std::vector<uint8_t> out(archive_bytes.begin(), archive_bytes.end());
  std::string bc(bootconfig_text);
  if (!bc.empty() && bc.back() != '\n') {
    bc.push_back('\n');
  }
  uint32_t csum = 0;
  for (unsigned char c : bc) {
    csum = (csum + c) & 0xFFFFFFFFU;
  }
  out.insert(out.end(), bc.begin(), bc.end());
  WriteLE32(&out, static_cast<uint32_t>(bc.size()));
  WriteLE32(&out, csum);
  static constexpr char kMagic[] = "#BOOTCONFIG\n";
  out.insert(out.end(), kMagic, kMagic + 12);
  return out;
}

bool SplitBootconfigTrailer(std::span<const uint8_t> initrd_bytes,
                            std::vector<uint8_t>* archive_out,
                            std::string* bootconfig_out,
                            std::string* error) {
  static constexpr char kMagic[] = "#BOOTCONFIG\n";
  if (initrd_bytes.size() < 20 ||
      std::memcmp(initrd_bytes.data() + initrd_bytes.size() - 12, kMagic, 12) != 0) {
    if (error) *error = "Missing #BOOTCONFIG\\n trailer";
    return false;
  }
  size_t end = initrd_bytes.size() - 12;
  uint32_t size = ReadLE32(initrd_bytes.data() + end - 8);
  uint32_t expected_csum = ReadLE32(initrd_bytes.data() + end - 4);
  if (size > end - 8) {
    if (error) *error = "Bootconfig size exceeds initrd size";
    return false;
  }
  size_t start = end - 8 - size;
  uint32_t got_csum = 0;
  for (size_t i = start; i < start + size; ++i) {
    got_csum = (got_csum + initrd_bytes[i]) & 0xFFFFFFFFU;
  }
  if (got_csum != expected_csum) {
    if (error) *error = "Bootconfig checksum mismatch";
    return false;
  }
  if (archive_out) {
    archive_out->assign(initrd_bytes.begin(), initrd_bytes.begin() + start);
  }
  if (bootconfig_out) {
    bootconfig_out->assign(
        reinterpret_cast<const char*>(initrd_bytes.data() + start), size);
  }
  return true;
}

bool MakeInitrdFile(const std::filesystem::path& out_path,
                    const std::filesystem::path& base_bootconfig_path,
                    std::span<const std::filesystem::path> cpio_paths,
                    std::span<const std::string> extra_bootconfig_kv,
                    std::string* error) {
  std::vector<uint8_t> archive;
  for (const auto& p : cpio_paths) {
    std::ifstream f(p, std::ios::binary);
    if (!f) {
      if (error) *error = "Cannot open CPIO file: " + p.string();
      return false;
    }
    archive.insert(archive.end(),
                   std::istreambuf_iterator<char>(f),
                   std::istreambuf_iterator<char>());
  }
  std::ifstream bcf(base_bootconfig_path, std::ios::binary);
  if (!bcf) {
    if (error) *error = "Cannot open bootconfig file: " + base_bootconfig_path.string();
    return false;
  }
  std::string bc_text((std::istreambuf_iterator<char>(bcf)),
                      std::istreambuf_iterator<char>());
  if (!bc_text.empty() && bc_text.back() != '\n') {
    bc_text.push_back('\n');
  }
  for (const auto& kv : extra_bootconfig_kv) {
    bc_text += kv + "\n";
  }
  auto final_bytes = AppendBootconfigTrailer(archive, bc_text);
  if (!WriteBytesToFile(out_path, final_bytes)) {
    if (error) *error = "Failed to write initrd file: " + out_path.string();
    return false;
  }
  return true;
}

bool ParseCpioNewc(std::span<const uint8_t> cpio_bytes,
                   std::vector<CpioEntry>* entries_out,
                   size_t* bytes_consumed,
                   std::string* error) {
  size_t pos = 0;
  while (pos < cpio_bytes.size() && cpio_bytes[pos] == 0) {
    ++pos;
  }
  while (pos < cpio_bytes.size()) {
    if (pos + 110 > cpio_bytes.size() ||
        std::memcmp(cpio_bytes.data() + pos, "070701", 6) != 0) {
      if (error) *error = "Invalid CPIO newc header magic at offset " + std::to_string(pos);
      return false;
    }
    bool ok = true;
    const uint8_t* h = cpio_bytes.data() + pos;
    CpioEntry e;
    e.ino = ParseHex8(h + 6, &ok);
    e.mode = ParseHex8(h + 14, &ok);
    e.uid = ParseHex8(h + 22, &ok);
    e.gid = ParseHex8(h + 30, &ok);
    e.nlink = ParseHex8(h + 38, &ok);
    e.mtime = ParseHex8(h + 46, &ok);
    uint32_t filesize = ParseHex8(h + 54, &ok);
    e.devmajor = ParseHex8(h + 62, &ok);
    e.devminor = ParseHex8(h + 70, &ok);
    e.rdevmajor = ParseHex8(h + 78, &ok);
    e.rdevminor = ParseHex8(h + 86, &ok);
    uint32_t namesize = ParseHex8(h + 94, &ok);
    if (!ok || namesize == 0) {
      if (error) *error = "Malformed CPIO hex field";
      return false;
    }
    size_t name_end = pos + 110 + namesize;
    size_t data_start = (name_end + 3) & ~static_cast<size_t>(3);
    size_t data_end = data_start + filesize;
    size_t next_pos = (data_end + 3) & ~static_cast<size_t>(3);
    if (name_end > cpio_bytes.size() || cpio_bytes[name_end - 1] != 0 ||
        data_end > cpio_bytes.size()) {
      if (error) *error = "Truncated CPIO entry";
      return false;
    }
    e.name.assign(reinterpret_cast<const char*>(cpio_bytes.data() + pos + 110),
                  namesize - 1);
    e.data.assign(cpio_bytes.begin() + data_start, cpio_bytes.begin() + data_end);
    pos = std::min(next_pos, cpio_bytes.size());
    if (e.name == "TRAILER!!!") {
      while (pos < cpio_bytes.size() && cpio_bytes[pos] == 0) {
        ++pos;
      }
      if (bytes_consumed) *bytes_consumed = pos;
      return true;
    }
    entries_out->push_back(std::move(e));
  }
  if (error) *error = "CPIO archive missing TRAILER!!!";
  return false;
}

std::vector<uint8_t> BuildCpioNewc(std::span<const CpioEntry> entries) {
  std::vector<uint8_t> out;
  auto emit = [&out](const CpioEntry& e, bool is_trailer) {
    std::string name = is_trailer ? "TRAILER!!!" : e.name;
    uint32_t namesize = static_cast<uint32_t>(name.size() + 1);
    uint32_t filesize = is_trailer ? 0 : static_cast<uint32_t>(e.data.size());
    char hdr[111];
    std::snprintf(hdr, sizeof(hdr),
                  "070701%08x%08x%08x%08x%08x%08x%08x%08x%08x%08x%08x%08x00000000",
                  is_trailer ? 0U : e.ino,
                  is_trailer ? 0U : e.mode,
                  is_trailer ? 0U : e.uid,
                  is_trailer ? 0U : e.gid,
                  is_trailer ? 1U : e.nlink,
                  is_trailer ? 0U : e.mtime,
                  filesize,
                  is_trailer ? 0U : e.devmajor,
                  is_trailer ? 0U : e.devminor,
                  is_trailer ? 0U : e.rdevmajor,
                  is_trailer ? 0U : e.rdevminor,
                  namesize);
    out.insert(out.end(), hdr, hdr + 110);
    out.insert(out.end(), name.begin(), name.end());
    out.push_back(0);
    while ((out.size() & 3) != 0) out.push_back(0);
    if (!is_trailer && !e.data.empty()) {
      out.insert(out.end(), e.data.begin(), e.data.end());
      while ((out.size() & 3) != 0) out.push_back(0);
    }
  };

  for (const auto& e : entries) {
    emit(e, false);
  }
  CpioEntry trailer;
  emit(trailer, true);
  return out;
}

bool PatchFirstStageRamdiskForPciStub(
    std::span<const uint8_t> ramdisk_cpio,
    std::span<const uint8_t> pci_stub_ko,
    std::span<const std::string> first_stage_drop,
    std::vector<uint8_t>* patched_cpio_out,
    std::string* error) {
  std::vector<CpioEntry> entries;
  size_t consumed = 0;
  if (!ParseCpioNewc(ramdisk_cpio, &entries, &consumed, error)) {
    return false;
  }

  std::set<std::string> drop_set(first_stage_drop.begin(), first_stage_drop.end());
  bool found_modules_load = false;
  bool found_pci_stub = false;
  uint32_t max_ino = 1000;

  for (auto& e : entries) {
    max_ino = std::max(max_ino, e.ino);
    std::string norm_name = e.name;
    if (norm_name.rfind("./", 0) == 0) norm_name = norm_name.substr(2);
    if (norm_name == "lib/modules/pci-stub.ko") {
      e.data.assign(pci_stub_ko.begin(), pci_stub_ko.end());
      found_pci_stub = true;
    } else if (norm_name == "lib/modules/modules.load") {
      found_modules_load = true;
      std::string text(reinterpret_cast<const char*>(e.data.data()), e.data.size());
      std::istringstream iss(text);
      std::string line;
      std::vector<std::string> out_lines = {"pci-stub.ko"};
      while (std::getline(iss, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line == "pci-stub.ko") continue;
        std::string base = std::filesystem::path(line).filename().string();
        if (drop_set.count(line) || drop_set.count(base)) continue;
        out_lines.push_back(line);
      }
      std::string new_text;
      for (const auto& l : out_lines) {
        new_text += l + "\n";
      }
      e.data.assign(new_text.begin(), new_text.end());
    }
  }

  if (!found_modules_load) {
    if (error) *error = "lib/modules/modules.load not found in first-stage CPIO";
    return false;
  }
  if (!found_pci_stub) {
    CpioEntry ko_entry;
    ko_entry.ino = max_ino + 1;
    ko_entry.mode = 0100644;
    ko_entry.nlink = 1;
    ko_entry.name = "lib/modules/pci-stub.ko";
    ko_entry.data.assign(pci_stub_ko.begin(), pci_stub_ko.end());
    entries.push_back(std::move(ko_entry));
  }

  *patched_cpio_out = BuildCpioNewc(entries);
  if (consumed < ramdisk_cpio.size()) {
    patched_cpio_out->insert(patched_cpio_out->end(),
                             ramdisk_cpio.begin() + consumed,
                             ramdisk_cpio.end());
  }
  return true;
}

bool ExtractBootArtifacts(const std::filesystem::path& img_path,
                          const std::filesystem::path& out_dir,
                          std::string* error) {
  auto gpt = ReadGptFromFile(img_path, error);
  if (!gpt) return false;

  const auto* boot_p = gpt->FindPartition("boot_a");
  const auto* vboot_p = gpt->FindPartition("vendor_boot_a");
  const auto* iboot_p = gpt->FindPartition("init_boot_a");
  const auto* vbmeta_p = gpt->FindPartition("vbmeta_a");
  if (!boot_p || !vboot_p || !iboot_p || !vbmeta_p) {
    if (error) *error = "Missing required boot_a/vendor_boot_a/init_boot_a/vbmeta_a partition";
    return false;
  }

  std::error_code ec;
  std::filesystem::create_directories(out_dir, ec);
  std::ifstream f(img_path, std::ios::binary);
  if (!f) {
    if (error) *error = "Cannot open image file: " + img_path.string();
    return false;
  }

  // 1. boot_a -> kernel.efi
  std::vector<uint8_t> hdr;
  if (!PreadFile(f, boot_p->start_bytes, 4096, &hdr) ||
      std::memcmp(hdr.data(), "ANDROID!", 8) != 0) {
    if (error) *error = "Invalid ANDROID! magic in boot_a";
    return false;
  }
  uint32_t ksz = ReadLE32(hdr.data() + 8);
  uint32_t boot_hv = ReadLE32(hdr.data() + 40);
  if (boot_hv != 4) {
    if (error) *error = "boot_a header version != 4";
    return false;
  }
  std::vector<uint8_t> kernel_bytes;
  if (!PreadFile(f, boot_p->start_bytes + 4096, ksz, &kernel_bytes) ||
      !WriteBytesToFile(out_dir / "kernel.efi", kernel_bytes)) {
    if (error) *error = "Failed to extract kernel.efi";
    return false;
  }

  // 2. vendor_boot_a -> vendor_cmdline.txt, vendor_bootconfig.txt, vendor_platform.cpio, vendor_dlkm.cpio
  if (!PreadFile(f, vboot_p->start_bytes, 4096, &hdr) ||
      std::memcmp(hdr.data(), "VNDRBOOT", 8) != 0) {
    if (error) *error = "Invalid VNDRBOOT magic in vendor_boot_a";
    return false;
  }
  uint32_t vhv = ReadLE32(hdr.data() + 8);
  uint32_t page = ReadLE32(hdr.data() + 12);
  uint32_t vrsz = ReadLE32(hdr.data() + 24);
  if (vhv != 4 || page == 0) {
    if (error) *error = "vendor_boot_a header version != 4";
    return false;
  }
  const uint8_t* cmdline_raw = hdr.data() + 28;
  size_t cmdline_len = 0;
  while (cmdline_len < 2048 && cmdline_raw[cmdline_len] != 0) ++cmdline_len;
  if (!WriteBytesToFile(out_dir / "vendor_cmdline.txt",
                        std::span<const uint8_t>(cmdline_raw, cmdline_len))) {
    if (error) *error = "Failed to write vendor_cmdline.txt";
    return false;
  }

  uint32_t hsz = ReadLE32(hdr.data() + 2096);
  uint32_t dtbsz = ReadLE32(hdr.data() + 2100);
  uint32_t tsz = ReadLE32(hdr.data() + 2112);
  uint32_t tnum = ReadLE32(hdr.data() + 2116);
  uint32_t tesz = ReadLE32(hdr.data() + 2120);
  uint32_t bcsz = ReadLE32(hdr.data() + 2124);

  uint64_t ram_off = vboot_p->start_bytes + AlignUp(hsz, page);
  uint64_t tab_off = ram_off + AlignUp(vrsz, page) + AlignUp(dtbsz, page);
  uint64_t bc_off = tab_off + AlignUp(tsz, page);

  std::vector<uint8_t> tab;
  if (!PreadFile(f, tab_off, tsz, &tab)) {
    if (error) *error = "Failed to read vendor_boot ramdisk table";
    return false;
  }
  for (uint32_t i = 0; i < tnum; ++i) {
    const uint8_t* e = tab.data() + static_cast<size_t>(i) * tesz;
    uint32_t sz = ReadLE32(e);
    uint32_t off = ReadLE32(e + 4);
    uint32_t typ = ReadLE32(e + 8);
    std::vector<uint8_t> lz4_blob;
    if (!PreadFile(f, ram_off + off, sz, &lz4_blob)) {
      if (error) *error = "Failed to read vendor ramdisk entry";
      return false;
    }
    std::vector<uint8_t> cpio;
    if (!Lz4DecompressFrame(lz4_blob, &cpio, error)) {
      return false;
    }
    std::string name = (typ == 1)   ? "vendor_platform.cpio"
                       : (typ == 2) ? "vendor_recovery.cpio"
                       : (typ == 3) ? "vendor_dlkm.cpio"
                                    : ("type" + std::to_string(typ) + ".cpio");
    if (!WriteBytesToFile(out_dir / name, cpio)) {
      if (error) *error = "Failed to write " + name;
      return false;
    }
  }

  std::vector<uint8_t> bc_bytes;
  if (!PreadFile(f, bc_off, bcsz, &bc_bytes) ||
      !WriteBytesToFile(out_dir / "vendor_bootconfig.txt", bc_bytes)) {
    if (error) *error = "Failed to write vendor_bootconfig.txt";
    return false;
  }

  // 3. init_boot_a -> init_boot.cpio
  if (!PreadFile(f, iboot_p->start_bytes, 4096, &hdr) ||
      std::memcmp(hdr.data(), "ANDROID!", 8) != 0) {
    if (error) *error = "Invalid ANDROID! magic in init_boot_a";
    return false;
  }
  uint32_t iksz = ReadLE32(hdr.data() + 8);
  uint32_t irsz = ReadLE32(hdr.data() + 12);
  std::vector<uint8_t> init_lz4;
  if (!PreadFile(f, iboot_p->start_bytes + 4096 + AlignUp(iksz, 4096), irsz, &init_lz4)) {
    if (error) *error = "Failed to read init_boot_a ramdisk";
    return false;
  }
  std::vector<uint8_t> init_cpio;
  if (!Lz4DecompressFrame(init_lz4, &init_cpio, error) ||
      !WriteBytesToFile(out_dir / "init_boot.cpio", init_cpio)) {
    return false;
  }

  // 4. vbmeta_a -> vbmeta_a.orig.img
  std::vector<uint8_t> vb_hdr;
  if (!PreadFile(f, vbmeta_p->start_bytes, 256, &vb_hdr) ||
      std::memcmp(vb_hdr.data(), "AVB0", 4) != 0) {
    if (error) *error = "Invalid AVB0 magic in vbmeta_a";
    return false;
  }
  uint64_t auth = ReadBE64(vb_hdr.data() + 12);
  uint64_t aux = ReadBE64(vb_hdr.data() + 20);
  std::vector<uint8_t> vb_full;
  if (!PreadFile(f, vbmeta_p->start_bytes, static_cast<size_t>(256 + auth + aux), &vb_full) ||
      !WriteBytesToFile(out_dir / "vbmeta_a.orig.img", vb_full)) {
    if (error) *error = "Failed to write vbmeta_a.orig.img";
    return false;
  }

  return true;
}

}  // namespace gbos
