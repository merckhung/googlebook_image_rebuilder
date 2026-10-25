#include <iostream>

#include "src/vendor/vendor_patcher.h"

int main() {
  auto removed = googlebookos::vendor::GetVendorRemovedPaths();
  std::cout << "googlebookos vendor safety removal list (" << removed.size()
            << " paths):\n";
  for (const auto& p : removed) {
    std::cout << "  - " << p << "\n";
  }
  return 0;
}
