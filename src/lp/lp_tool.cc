#include "src/lp/lp_metadata.h"

#include <cstdlib>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
  if (argc < 4) {
    std::cerr << "Usage: lp_tool <list <img> <super_off> | extract-one <img> <super_off> <part_name> <out_file> | extract-all <img> <super_off> <out_dir>>\n";
    return 2;
  }
  std::string cmd = argv[1];
  std::filesystem::path img = argv[2];
  uint64_t super_off = std::strtoull(argv[3], nullptr, 10);

  if (cmd == "list") {
    std::string err;
    auto meta = gbos::ReadLpMetadataFromFile(img, super_off, &err);
    if (!meta) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    for (const auto& p : meta->partitions) {
      std::cout << p.name << "\tgroup=" << p.group_name
                << "\textents=" << p.num_extents
                << "\tbytes=" << p.total_bytes << "\n";
    }
    return 0;
  }
  if (cmd == "extract-one" && argc == 6) {
    std::string err;
    if (!gbos::ExtractLogicalPartition(img, super_off, argv[4], argv[5], &err)) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    return 0;
  }
  if (cmd == "extract-all" && argc == 5) {
    std::string err;
    if (!gbos::ExtractAllLogicalPartitions(img, super_off, argv[4], &err)) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    return 0;
  }
  return 2;
}
