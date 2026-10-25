#include "src/erofs/erofs.h"

#include <iostream>
#include <string>

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr << "Usage: erofs_tool <uuid <erofs_file> | avb-orig-size <img_file> | walk <erofs_file> | write-configs <erofs_file> <mount_point> <file_contexts_out> <fs_config_out>>\n";
    return 2;
  }
  std::string cmd = argv[1];
  if (cmd == "uuid" && argc == 3) {
    std::string err;
    auto u = gbos::ReadErofsUuidFromFile(argv[2], &err);
    if (!u) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    std::cout << *u << "\n";
    return 0;
  }
  if (cmd == "avb-orig-size" && argc == 3) {
    std::string err;
    auto sz = gbos::ReadAvbOriginalImageSizeFromFile(argv[2], &err);
    if (!sz) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    std::cout << *sz << "\n";
    return 0;
  }
  if (cmd == "walk" && argc == 3) {
    std::string err;
    auto meta = gbos::WalkErofsImageFile(argv[2], &err);
    if (!meta) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    std::cout << "uuid=" << meta->uuid << " entries=" << meta->entries.size() << "\n";
    return 0;
  }
  if (cmd == "write-configs" && argc == 6) {
    std::string err;
    auto meta = gbos::WalkErofsImageFile(argv[2], &err);
    if (!meta || !gbos::WriteErofsConfigs(argv[3], meta->entries, argv[4], argv[5], &err)) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    return 0;
  }
  return 2;
}
