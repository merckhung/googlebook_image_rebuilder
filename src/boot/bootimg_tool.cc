#include "src/boot/bootimg.h"

#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "Usage: bootimg_tool <extract|vbmeta-digest|make-initrd> ...\n";
    return 2;
  }
  std::string cmd = argv[1];
  if (cmd == "extract" && argc == 4) {
    std::string err;
    if (!gbos::ExtractBootArtifacts(argv[2], argv[3], &err)) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    return 0;
  }
  if (cmd == "vbmeta-digest" && argc == 3) {
    std::string err;
    auto info = gbos::ParseVbmetaFile(argv[2], &err);
    if (!info) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    std::cout << info->sha256_hex << " " << info->total_size << "\n";
    return 0;
  }
  if (cmd == "make-initrd" && argc >= 5) {
    std::filesystem::path out_path = argv[2];
    std::filesystem::path bc_path = argv[3];
    std::vector<std::filesystem::path> cpios;
    std::vector<std::string> extra_kv;
    bool in_kv = false;
    for (int i = 4; i < argc; ++i) {
      std::string a = argv[i];
      if (!in_kv && a == "--") {
        in_kv = true;
        continue;
      }
      if (in_kv) {
        extra_kv.push_back(a);
      } else {
        cpios.emplace_back(a);
      }
    }
    std::string err;
    if (!gbos::MakeInitrdFile(out_path, bc_path, cpios, extra_kv, &err)) {
      std::cerr << "ERROR: " << err << "\n";
      return 1;
    }
    return 0;
  }
  std::cerr << "Usage: bootimg_tool <extract <img> <outdir> | vbmeta-digest <vbmeta.img> | make-initrd <out> <bootconfig> <cpio...> -- <k=v...>>\n";
  return 2;
}
