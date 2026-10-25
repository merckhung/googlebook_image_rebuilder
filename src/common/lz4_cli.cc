#include "src/common/lz4_frame.h"

#include <unistd.h>

#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace {

bool ReadAllStream(std::istream& in, std::vector<uint8_t>* out) {
  char buf[65536];
  while (in) {
    in.read(buf, sizeof(buf));
    std::streamsize n = in.gcount();
    if (n > 0) {
      out->insert(out->end(),
                  reinterpret_cast<const uint8_t*>(buf),
                  reinterpret_cast<const uint8_t*>(buf) + n);
    }
  }
  return !in.bad();
}

}  // namespace

int main(int argc, char** argv) {
  bool decompress = false;
  bool to_stdout = false;
  bool legacy = false;
  std::vector<std::string> positional;

  std::string prog = argc > 0 ? argv[0] : "lz4";
  if (prog.find("unlz4") != std::string::npos ||
      prog.find("lz4cat") != std::string::npos) {
    decompress = true;
  }
  if (prog.find("lz4cat") != std::string::npos) {
    to_stdout = true;
  }

  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--version" || arg == "-V") {
      std::cout << "*** LZ4 command line interface 64-bit v1.9.4 (gbos C++) ***\n";
      return 0;
    }
    if (arg == "--help" || arg == "-h") {
      std::cout << "Usage: lz4 [options] [input] [output]\n";
      return 0;
    }
    if (arg == "--decompress" || arg == "--uncompress") {
      decompress = true;
      continue;
    }
    if (arg == "--stdout" || arg == "--to-stdout") {
      to_stdout = true;
      continue;
    }
    if (arg == "--force" || arg == "--quiet" || arg == "--no-frame-crc" ||
        arg == "--content-size") {
      continue;
    }
    if (arg.size() > 1 && arg[0] == '-' && arg[1] != '-') {
      for (size_t j = 1; j < arg.size(); ++j) {
        char c = arg[j];
        if (c == 'd' || c == 't') {
          decompress = true;
        } else if (c == 'c') {
          to_stdout = true;
        } else if (c == 'l') {
          legacy = true;
        } else if (c == 'f' || c == 'q' || c == 'v' || c == 'k' ||
                   (c >= '0' && c <= '9')) {
          // Accepted flags
        }
      }
      continue;
    }
    positional.push_back(arg);
  }

  std::string in_path = "-";
  std::string out_path = "-";
  if (!positional.empty()) {
    in_path = positional[0];
  }
  if (positional.size() >= 2 && !to_stdout) {
    out_path = positional[1];
  } else if (positional.size() == 1 && !to_stdout && in_path != "-") {
    if (decompress && in_path.size() > 4 &&
        in_path.substr(in_path.size() - 4) == ".lz4") {
      out_path = in_path.substr(0, in_path.size() - 4);
    } else if (!decompress) {
      if (!isatty(STDOUT_FILENO)) {
        out_path = "-";
      } else {
        out_path = in_path + ".lz4";
      }
    }
  }

  std::vector<uint8_t> src;
  if (in_path == "-") {
    if (!ReadAllStream(std::cin, &src)) {
      std::cerr << "lz4: failed to read stdin\n";
      return 1;
    }
  } else {
    std::ifstream in(in_path, std::ios::binary);
    if (!in || !ReadAllStream(in, &src)) {
      std::cerr << "lz4: cannot open " << in_path << "\n";
      return 1;
    }
  }

  std::vector<uint8_t> dst;
  if (decompress) {
    std::string err;
    if (!gbos::Lz4DecompressFrame(src, &dst, &err)) {
      std::cerr << "lz4: decompression failed: " << err << "\n";
      return 1;
    }
  } else {
    dst = gbos::Lz4CompressFrame(src, legacy);
  }

  if (out_path == "-" || to_stdout) {
    std::cout.write(reinterpret_cast<const char*>(dst.data()),
                    static_cast<std::streamsize>(dst.size()));
    if (!std::cout.good()) return 1;
  } else {
    std::ofstream out(out_path, std::ios::binary | std::ios::trunc);
    if (!out) {
      std::cerr << "lz4: cannot write " << out_path << "\n";
      return 1;
    }
    out.write(reinterpret_cast<const char*>(dst.data()),
              static_cast<std::streamsize>(dst.size()));
    if (!out.good()) return 1;
  }
  return 0;
}
