#include <iostream>
#include <string>

#include "src/fetch/http_zip.h"

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr << "Usage: fetch_tool <list|extract|remote> <zip_or_url> [member] [output]\n";
    return 2;
  }
  try {
    std::string cmd = argv[1];
    std::string target = argv[2];
    if (cmd == "list") {
      for (const auto& e : googlebookos::fetch::ListLocalZipEntries(target)) {
        std::cout << e.uncompressed_size << "\t" << e.compressed_size << "\t"
                  << e.filename << "\n";
      }
      return 0;
    } else if (cmd == "extract" && argc >= 4) {
      auto bytes = googlebookos::fetch::ExtractLocalZipMember(target, argv[3]);
      std::cout.write(reinterpret_cast<const char*>(bytes.data()),
                      static_cast<std::streamsize>(bytes.size()));
      return 0;
    } else if (cmd == "remote" && argc >= 5) {
      googlebookos::fetch::FetchRemoteZipMember(target, argv[3], argv[4]);
      return 0;
    }
  } catch (const std::exception& e) {
    std::cerr << "fetch_tool error: " << e.what() << "\n";
    return 1;
  }
  return 2;
}
