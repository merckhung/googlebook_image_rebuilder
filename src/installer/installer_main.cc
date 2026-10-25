#include <iostream>
#include <string>

#include "src/installer/googlebookos_installer.h"

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "Usage: googlebookos_installer <verify|probe-dmi|plan-ufs> [args...]\n";
    return 2;
  }
  std::string cmd = argv[1];
  if (cmd == "verify" && argc >= 3) {
    std::string err;
    if (!googlebookos::installer::VerifyGooglebookOsImageBeforeInstall(argv[2], &err)) {
      std::cerr << "Image verification failed: " << err << "\n";
      return 1;
    }
    std::cout << "Googlebook image verified OK: " << argv[2] << "\n";
    return 0;
  } else if (cmd == "probe-dmi") {
    std::string dir = (argc >= 3) ? argv[2] : "/sys/class/dmi/id";
    auto id = googlebookos::installer::ProbeDmiIdentity(dir);
    std::cout << "sys_vendor:   " << id.sys_vendor << "\n"
              << "product_name: " << id.product_name << "\n"
              << "board_name:   " << id.board_name << "\n"
              << "is_marasov:   " << (id.is_marasov ? "yes" : "no") << "\n"
              << "is_screebo:   " << (id.is_screebo ? "yes" : "no") << "\n";
    return 0;
  } else if (cmd == "plan-ufs" && argc >= 4) {
    std::string err;
    std::string plan =
        googlebookos::installer::BuildUfsInstallPlan(argv[2], argv[3], &err);
    if (plan.empty()) {
      std::cerr << "Install plan error: " << err << "\n";
      return 1;
    }
    std::cout << plan;
    return 0;
  }
  return 2;
}
