# Pins of the public lapis build 16658739 (CL3B.260622.276.R4). base/build-base.sh sources this file; install/install.sh
# only reads the plain ZIP_URL= and ZIP_SHA256= lines with sed (the arrays below need bash 4).
# A new recovery build is a new file base/builds/<build>.sh with the same variables.
# shellcheck shell=bash disable=SC2034
# Official recovery ZIP, verified against the 2026-10-22 public catalog MD5. Index:
# https://dl.google.com/dl/edgedl/device/recovery/production_recovery.json
ZIP_URL=https://dl.google.com/device/recovery/lapis-user/16658739/recovery.zip
ZIP_SHA256=97980097708f472962ba267d3801211eadbe9e3a8fa61408846ed23b3147b9e7
ZIP_MEMBER=android-desktop_signed_recovery_image.bin
BIN_SHA256=7180493e2efed15611859c03113d1787d10d9f104be32dfd080be420e5e52f27
BIN_SIZE=19069493760
MTOOLS_REF_VERSION=4.0.48
VENDOR_UUID=4d286de7-7133-55bf-8283-b414f127b88a
VENDOR_FINGERPRINT=ASUS/lapis/lapis:17/CL3B.260622.276.R4/16658739:user/release-keys
VENDOR_SECURITY_PATCH=2026-10-01
VBMETA_ROLLBACK_INDEX=1369
EDITED_VENDOR_BUILD_PROP_SHA256=b3c5bf14d093dc12ccca3dc3a5c586bb4667a5af824c1cbc460c29ef2624d843
EDITED_SYSTEM_EXT_MANIFEST_SHA256=4e393ce518613aa273dc2b2cadddbed30b8588bab6692f2dd461ac0545cbc34e

# ---------------------------------------------------------------- expected intermediates
# Key-independent (any key): checked always.
declare -A EXPECT=(
  [kernel.efi]=7816c5d5b3500093e597b7362ecab103d5a32141b100d3eb7a8b33221fce7f41
  [vendor_platform.cpio]=3946480ee9d4af20d940a11f129c225bf45081802808b1e357a35beef641e1f9
  [vendor_dlkm.cpio]=1d1d2d17f886fed2c9246ffc0013a09d81a4bc240739bf2fbf5de9bf627ab13a
  [init_boot.cpio]=f330e44dc4910c2308936195a757ad6290700b830b10226cad8d740c6a592cf6
  [vendor_bootconfig.txt]=3e4a12b3c1606b852194b0f2bb70ce8318227c3f66f0631fb5a325fd2115b2cf
  [vendor_cmdline.txt]=311e95df564037a863af096fb6265f15e8340e4b60e2736676e43d81d876aee7
  [vbmeta_a.orig.img]=b84c7927b0501477f46119682488849fb7e372d65c2da708451f945552210e94
  [initrd.partuuid.img]=348ff595c8027f6f4df3064ecbf0e8df09a3ca97291d1d59275a4f488a927cd1
  [esp.img]=a56d2e55e2ba53a74b317b45e1153277699fc31a3884be43805a1d8f3c956285
  [system_a.img]=15fc09de36d3cdb889837c640ac3e11cf720d956ff03f3f73c9bcec7cac714d0
  [system_b.img]=89539428c58b29dc8d1267bd96c053c1f501379f427af1e16fc0f4cbf9703e32
  [vendor_a.img]=a1491aa4da413df96de30f2c91bd896ca939070d0ade922d460cf84820d89513
  [product_a.img]=8955c4cf4696c7ae0e6ad557ecf06f2e2f2b3a16abfa26ee9599ca3feeed741c
  [system_ext_a.img]=8ceacee60ce99137f0d5e4d21d05498924b091f198a072db2604a4445764efb6
  [system_dlkm_a.img]=3f8673318e744c7b87291796d0db94e6282085b63d28aa797d84fef8344ada3e
  [vendor_dlkm_a.img]=b7427fd22620d8685062a6efe7e0a7587323731397f7e50ab03a31e304cae41e
  [vendor.erofs]=e35888babc6f6c7cbf3fcaf14552c4ab70b0b3ec41db46cba50e4ccf88b97cb4
  [vendor.img]=250f90e286ea1d0ee7059a8237de52da02bf27b91917f164b4dd5236c12fc99b
  [system_ext.erofs]=b98626effdc3b96fbfea1f0395dfeb00617467d25fa385e3544854ea3076abbb
  [system_ext.img]=ca63455e44d45a3217eeea14ad67e74feb0be4d56a6df323ea0210b4e8528e6a
  [super.img]=560693df43b2bb8cb768f772ac505ada1d033438e7994545c561e3c6bf4906aa
)
# Reference build public-key fingerprint; the private key is never distributed.
REF_PUBKEY_SHA1=5d6032d330663ecf1783d13ee4a9fb48db3330b9
declare -A EXPECT_REFKEY=(
  [vbmeta.new.img]=7617762453daf98d442288ff3ddaa90cf40f3ec4c6019fa71649c9cf4916ad7f
  [initrd.img]=a89c8e90ac63eef0db3e85778f14fc9e1d7ee973620151b697aa6d0bcc44255f
  [kano-real.img]=f7c4ea84d77b4ceaa6b4c3a73fa295830b514e3657781cef6b0f0f1edb04cc8c
)

# Sizes reserved for the rebuilt vendor and system_ext partitions.
VENDOR_PART_SIZE=452984832
SYSEXT_PART_SIZE=209715200

# Extra modules for the pinned public recovery kernel.
AC_KO_SHA=db694c38d713e54ba1876d454fd4001cf77889173c44a20d31d9d34d201b5c7b
WAKEFILTER_KO_SHA=3423c3e983bff3d7d35486c475bde473cb7a54dd4115bf7933678f77861cf595
ISA_KO_SHA=9af91efd96d00449a7fde852a225e33eeba052d413628fec08c94b96c9415e44
EFIFB_KO_SHA=2b4a97781c2a24927413dd4a78ec1f624fc7c5aaaa6ddbb25574c66ab65fd1d9
