#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>

#include "src/fat32/fat32.h"

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr << "Usage: fat32_tool <list|cat> <image> [offset] [path]\n";
    return 2;
  }
  try {
    std::string cmd = argv[1];
    std::string img = argv[2];
    uint64_t off = (argc >= 4) ? std::stoull(argv[3]) : 0;
    if (cmd == "list") {
      for (const auto& e : googlebookos::fat32::ListFat32Entries(img, off)) {
        std::cout << (e.is_dir ? "d " : "f ") << e.size << "\t" << e.path << "\n";
      }
      return 0;
    } else if (cmd == "cat" && argc >= 5) {
      auto data = googlebookos::fat32::ReadFat32File(img, off, argv[4]);
      std::cout.write(reinterpret_cast<const char*>(data.data()),
                      static_cast<std::streamsize>(data.size()));
      return 0;
    }
  } catch (const std::exception& e) {
    std::cerr << "fat32_tool error: " << e.what() << "\n";
    return 1;
  }
  return 2;
}
