#!/usr/bin/env bash
# Built-in camera for the KANO (IPU6 + OV2740): cross-build upstream libcamera 0.7.2 (simple pipeline, SoftISP on the
# CPU, simple IPA with the OV2740 tuning) and its Android camera3 HAL (generic platform) for x86_64 Android.
# Output: <workdir>/out/vendor/ (+ out/SHA256SUMS), the GBOS_CAMERA_OVERLAY for builder/build.sh.
# usage: builder/camera/build-libcamera.sh <workdir>
# Needs: Linux x86-64, Android NDK r28 (NDK=...), CMake 3.22+ (CMAKE=...), curl, make, patch, python3 (a venv with
# meson, ninja, ply, jinja2, pyyaml and patchelf is created in <workdir>/venv).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
mkdir -p "${1:?usage: build-libcamera.sh <workdir>}"
W=$(cd "$1" && pwd -P)
NDK=${NDK:-$HOME/Android/Sdk/ndk/28.2.13676358}
CMAKE=${CMAKE:-$HOME/Android/Sdk/cmake/3.22.1/bin/cmake}
API=33
TC=$NDK/toolchains/llvm/prebuilt/linux-x86_64
CC=$TC/bin/x86_64-linux-android$API-clang; CXX=$TC/bin/x86_64-linux-android$API-clang++
SYS=$W/sysroot; OUT=$W/out/vendor
[ -x "$CMAKE" ] || { echo "CMake not found (set CMAKE=)"; exit 2; }
[ -x "$CC" ] || { echo "Android NDK clang not found under $NDK (set NDK=)"; exit 2; }
[ -d "$W/venv" ] || { python3 -m venv "$W/venv" && "$W/venv/bin/pip" -q install meson ninja ply jinja2 pyyaml patchelf; }
. "$W/venv/bin/activate"
mkdir -p "$W/dl" "$SYS/lib/pkgconfig" "$SYS/include"
fetch() {  # url sha256 file
  if [ ! -f "$W/dl/$3" ]; then curl -sfL -o "$W/dl/$3.part" "$1"; mv -f "$W/dl/$3.part" "$W/dl/$3"; fi
  echo "$2  $W/dl/$3" | sha256sum -c --quiet || { echo "checksum mismatch: $3 ($(sha256sum < "$W/dl/$3" | cut -c1-64))"; exit 1; }
}
# pinned inputs (sha256 recorded on first fetch, 2026-10-13)
fetch https://gitlab.freedesktop.org/camera/libcamera/-/archive/v0.7.2/libcamera-v0.7.2.tar.gz \
  64881a4bafbda02f34861153d8f0d42be9ff46f5598408f9b585b07a10c448c9 libcamera-v0.7.2.tar.gz
fetch https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/3.2.0/libjpeg-turbo-3.2.0.tar.gz \
  6f30092cef9fb839779646608f4ee14ae3cbac989c47fa05e841b0841f09878e libjpeg-turbo-3.2.0.tar.gz
fetch https://github.com/libexif/libexif/releases/download/v0.6.25/libexif-0.6.25.tar.xz \
  62f74cf3bf673a6e24d2de68f6741643718541f83aca5947e76e3978c25dce83 libexif-0.6.25.tar.xz

# libjpeg-turbo (static, no SIMD: no nasm needed)
if [ ! -f "$SYS/lib/libjpeg.a" ]; then
  rm -rf "$W/jpeg" && mkdir -p "$W/jpeg" && tar -C "$W/jpeg" --strip-components=1 -xzf "$W/dl/libjpeg-turbo-3.2.0.tar.gz"
  "$CMAKE" -S "$W/jpeg" -B "$W/jpeg/b" -G Ninja -DCMAKE_MAKE_PROGRAM="$(command -v ninja)" \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" -DANDROID_ABI=x86_64 -DANDROID_PLATFORM=$API \
    -DCMAKE_BUILD_TYPE=Release -DENABLE_SHARED=OFF -DENABLE_STATIC=ON -DWITH_SIMD=OFF -DWITH_TURBOJPEG=OFF \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_INSTALL_PREFIX="$SYS" -DCMAKE_INSTALL_LIBDIR=lib > "$W/jpeg.log"
  ninja -C "$W/jpeg/b" install >> "$W/jpeg.log"
fi
# libexif (static)
if [ ! -f "$SYS/lib/libexif.a" ]; then
  rm -rf "$W/exif" && mkdir -p "$W/exif" && tar -C "$W/exif" --strip-components=1 -xJf "$W/dl/libexif-0.6.25.tar.xz"
  (cd "$W/exif" && CC=$CC CFLAGS="-O2 -fPIC" ./configure --host=x86_64-linux-android --prefix="$SYS" \
     --disable-shared --enable-static --disable-nls --disable-docs > "$W/exif.log" && make -j"$(nproc)" install >> "$W/exif.log")
fi
# link-time stub for libhardware.so (the real one is the device's; only hw_get_module is used)
if [ ! -f "$SYS/lib/libhardware.so" ]; then
  printf 'int hw_get_module(const char *id, const void **m) { (void)id; (void)m; return -1; }\nint hw_get_module_by_class(const char *c, const char *i, const void **m) { (void)c; (void)i; (void)m; return -1; }\n' > "$W/hwstub.c"
  "$CC" -shared -fPIC -Wl,-soname,libhardware.so -o "$SYS/lib/libhardware.so" "$W/hwstub.c"
  printf 'prefix=%s\nName: libhardware\nDescription: link stub\nVersion: 1\nLibs: -L${prefix}/lib -lhardware\nCflags:\n' "$SYS" > "$SYS/lib/pkgconfig/libhardware.pc"
fi
# libcamera
S=$W/libcamera-v0.7.2
if [ ! -d "$S" ]; then tar -C "$W" -xzf "$W/dl/libcamera-v0.7.2.tar.gz"; fi
# OV2740 tuning (CC0, calibrated from the CJFLE23 module's AIQB; libcamera-devel patch 26682, not merged in 0.7.2)
fetch https://patchwork.libcamera.org/patch/26682/mbox/ \
  17e2bcf995c8c188d9b62b6c3508f6958aaf524d70dcf3b9b210a6a33f574cdf ov2740-tuning.mbox
if [ ! -f "$S/src/ipa/simple/data/ov2740.yaml" ]; then patch -d "$S" -p1 -N -s < "$W/dl/ov2740-tuning.mbox"; fi
for p in "$HERE"/patches/*.patch; do
  [ -f "$p" ] || continue
  if patch -d "$S" -p1 -N --dry-run -s < "$p" > /dev/null 2>&1; then patch -d "$S" -p1 -N -s < "$p"; fi
done
# libyuv (static, from the source the libcamera wrap downloads; built here because meson's cmake subproject cannot
# drive the NDK toolchain)
if [ ! -f "$SYS/lib/libyuv.a" ]; then
  [ -d "$S/subprojects/libyuv" ] || (cd "$S" && meson subprojects download libyuv > "$W/yuv-download.log")
  "$CMAKE" -S "$S/subprojects/libyuv" -B "$W/yuv" -G Ninja -DCMAKE_MAKE_PROGRAM="$(command -v ninja)" \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" -DANDROID_ABI=x86_64 -DANDROID_PLATFORM=$API \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_DISABLE_FIND_PACKAGE_JPEG=ON > "$W/yuv.log"
  ninja -C "$W/yuv" yuv >> "$W/yuv.log"
  cp "$W/yuv/libyuv.a" "$SYS/lib/" && cp -a "$S/subprojects/libyuv/include/." "$SYS/include/"
  printf 'prefix=%s\nName: libyuv\nDescription: static\nVersion: 1\nLibs: -L${prefix}/lib -lyuv\nCflags: -I${prefix}/include\n' "$SYS" > "$SYS/lib/pkgconfig/libyuv.pc"
fi
cat > "$W/android-x86_64.ini" << EOF
[binaries]
c = '$CC'
cpp = '$CXX'
ar = '$TC/bin/llvm-ar'
strip = '$TC/bin/llvm-strip'
pkg-config = 'pkg-config'
cmake = '$CMAKE'

[properties]
pkg_config_libdir = '$SYS/lib/pkgconfig'

[built-in options]
c_args = ['-fPIC']
cpp_args = ['-fPIC']
cpp_link_args = ['-Wl,-rpath,/vendor/lib64/kanocam']
c_link_args = ['-Wl,-rpath,/vendor/lib64/kanocam']

[host_machine]
system = 'android'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
EOF
rm -rf "$W/build"
meson setup "$W/build" "$S" --cross-file "$W/android-x86_64.ini" --buildtype=release \
  --prefix=/vendor --libdir=lib64/kanocam --sysconfdir=etc --datadir=etc --libexecdir=lib64/kanocam/libexec \
  -Dandroid=enabled -Dandroid_platform=generic -Dpipelines=simple -Dipas=simple -Dsoftisp-gpu=disabled \
  -Dcam=disabled -Dqcam=disabled -Dlc-compliance=disabled -Dgstreamer=disabled -Dv4l2=disabled -Dpycamera=disabled \
  -Dtracing=disabled -Dudev=disabled -Ddocumentation=disabled -Dtest=false -Dlibdw=disabled -Dlibunwind=disabled \
  -Dlibyaml:default_library=static > "$W/meson-setup.log" 2>&1 \
  || { tail -40 "$W/meson-setup.log"; exit 1; }
ninja -C "$W/build" > "$W/ninja.log" 2>&1 || { tail -60 "$W/ninja.log"; exit 1; }
rm -rf "$W/stage" && DESTDIR="$W/stage" meson install -C "$W/build" --no-rebuild > "$W/install.log" 2>&1
# assemble the vendor overlay
rm -rf "$W/out" && mkdir -p "$OUT/lib64/hw" "$OUT/lib64/kanocam" "$OUT/etc"
cp -a "$W/stage/vendor/lib64/kanocam/." "$OUT/lib64/kanocam/"
mv "$OUT/lib64/kanocam/libcamera-hal.so" "$OUT/lib64/hw/camera.kano.so"
cp "$TC/sysroot/usr/lib/x86_64-linux-android/libc++_shared.so" "$OUT/lib64/kanocam/"
cp -a "$W/stage/vendor/etc/." "$OUT/etc/"
rm -rf "$OUT/lib64/kanocam/pkgconfig" "$OUT/lib64/kanocam/libexec"
find "$OUT" -name '*.a' -delete
# lapis' vendor has its own (older NDK) libc++_shared.so: give ours a private soname so the two can never be mixed up
mv "$OUT/lib64/kanocam/libc++_shared.so" "$OUT/lib64/kanocam/libc++_kanocam.so"
patchelf --set-soname libc++_kanocam.so "$OUT/lib64/kanocam/libc++_kanocam.so"
find "$OUT" -name '*.so' -type f ! -name 'libc++_kanocam.so' -exec patchelf --replace-needed libc++_shared.so libc++_kanocam.so {} \;
# the HAL's camera description (front camera, no rotation), keyed by libcamera's id of the OV2740 (its ACPI path)
cat > "$OUT/etc/libcamera/camera_hal.yaml" << 'YAML'
# Googlebook KANO: the user-facing OV2740 (ACPI \_SB.PCI0.I2C2.CAM0)
cameras:
  '\_SB_.PCI0.I2C2.CAM0':
    location: front
    rotation: 0
YAML
find "$OUT" -name '*.sign' -delete   # no signature check on the device (patch 0001): the files would only mislead
find "$OUT" -name '*.so' -type f -exec "$TC/bin/llvm-strip" --strip-unneeded {} +
find "$OUT" -type f | sort | while read -r f; do printf '%s  %s\n' "$(sha256sum < "$f" | cut -c1-64)" "${f#"$W/out/"}"; done > "$W/out/SHA256SUMS"
echo "BUILD OK: $(wc -l < "$W/out/SHA256SUMS") files"
