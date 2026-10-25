#include "src/gpt/gpt.h"

#include <cstdlib>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "Usage: gpt_tool <list|layout|splice> ...\n";
    return 2;
  }
  std::string cmd = argv[1];
  if (cmd == "list" && argc == 3) {
    std::string err;
    auto gpt = gbos::ReadGptFromFile(argv[2], &err);
    if (!gpt) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    std::cout << "disk_guid=" << gpt->disk_guid
              << " header_crc_ok=" << (gpt->header_crc_valid ? "1" : "0")
              << " entries_crc_ok=" << (gpt->entries_crc_valid ? "1" : "0") << "\n";
    for (const auto& p : gpt->partitions) {
      std::cout << p.index << "\t" << p.name << "\toff=" << p.start_bytes
                << "\tsize=" << p.size_bytes << "\tguid=" << p.unique_guid << "\n";
    }
    return 0;
  }
  if (cmd == "layout" && argc == 4) {
    std::string err;
    if (!gbos::WriteLayoutEnv(argv[2], argv[3], &err)) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    return 0;
  }
  if (cmd == "splice" && argc == 6) {
    uint64_t off = std::strtoull(argv[3], nullptr, 10);
    uint64_t sz = std::strtoull(argv[4], nullptr, 10);
    std::string err;
    if (!gbos::SplicePartition(argv[2], off, sz, argv[5], &err)) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    return 0;
  }
  std::cerr << "Usage: gpt_tool <list <img_path> | layout <img_path> <out_env> | splice <img_path> <off> <size> <part_file>>\n";
  return 2;
}
