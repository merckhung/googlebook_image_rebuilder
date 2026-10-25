#include <iostream>
#include <string>

#include "src/kmod/kmod_verifier.h"

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "Usage: kmod_tool <ko_path> [expected_modname] [expected_sha256]\n";
    return 2;
  }
  try {
    auto info = googlebookos::kmod::InspectKernelModule(argv[1]);
    std::cout << "name:     " << info.name << "\n"
              << "vermagic: " << info.vermagic << "\n"
              << "sha256:   " << info.sha256 << "\n"
              << "symbols:  " << info.versions.size() << "\n";
    if (argc >= 3) {
      std::string want_sha = (argc >= 4) ? argv[3] : "";
      std::string err;
      if (!googlebookos::kmod::VerifyPinnedGkiModule(argv[1], argv[2], want_sha,
                                                   &err)) {
        std::cerr << "Verification failed: " << err << "\n";
        return 1;
      }
      std::cout << "VERIFIED OK\n";
    }
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "kmod_tool error: " << e.what() << "\n";
    return 1;
  }
}
