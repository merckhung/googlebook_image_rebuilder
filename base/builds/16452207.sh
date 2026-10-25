# Pins of the public lapis build 16452207 (CL3B.260622.271.R1). base/build-base.sh sources this file; install/install.sh
# only reads the plain ZIP_URL= and ZIP_SHA256= lines with sed (the arrays below need bash 4).
# A new recovery build is a new file base/builds/<build>.sh with the same variables.
# shellcheck shell=bash disable=SC2034
# Official recovery ZIP. Index:
# https://dl.google.com/dl/edgedl/device/recovery/production_recovery.json
ZIP_URL=https://dl.google.com/device/recovery/lapis-user/16452207/recovery.zip
ZIP_SHA256=5bc64936c11d9eb268da03063a443b8fd0bbc0f84d266f9a014231653939f247
ZIP_MEMBER=android-desktop_signed_recovery_image.bin
BIN_SHA256=b10f519ad9b834b316bc3c9d7c33fc45438a8d1c69254c124b7c3cdf72d288c7
BIN_SIZE=19069493760
VENDOR_UUID=7aead136-f6d4-508e-9bf2-2517134fe885
VENDOR_FINGERPRINT=ASUS/lapis/lapis:17/CL3B.260622.271.R1/16452207:user/release-keys
VENDOR_SECURITY_PATCH=2026-09-01
VBMETA_ROLLBACK_INDEX=1339
EDITED_VENDOR_BUILD_PROP_SHA256=d3d938b4ca751f8aaa680b100bf8486e3bf81a2cef1eba51c8a9e6431946b6af
EDITED_SYSTEM_EXT_MANIFEST_SHA256=4e393ce518613aa273dc2b2cadddbed30b8588bab6692f2dd461ac0545cbc34e

# ---------------------------------------------------------------- expected intermediates
# Key-independent (any key): checked always.
declare -A EXPECT=(
  [kernel.efi]=7816c5d5b3500093e597b7362ecab103d5a32141b100d3eb7a8b33221fce7f41
  [vendor_platform.cpio]=3946480ee9d4af20d940a11f129c225bf45081802808b1e357a35beef641e1f9
  [vendor_dlkm.cpio]=1d1d2d17f886fed2c9246ffc0013a09d81a4bc240739bf2fbf5de9bf627ab13a
  [init_boot.cpio]=200baea4019ca640b413789f2ae9398d3da24155bde0e7b5b223063229e0cb82
  [vendor_bootconfig.txt]=3e4a12b3c1606b852194b0f2bb70ce8318227c3f66f0631fb5a325fd2115b2cf
  [vendor_cmdline.txt]=311e95df564037a863af096fb6265f15e8340e4b60e2736676e43d81d876aee7
  [vbmeta_a.orig.img]=bc270dbed03f9068d69d3da5dcbfcf80a947186d36b5dd4c5a6bcc5f7dc45ec2
  [initrd.partuuid.img]=623a985526d4bc45af3b5adfaf70d9a1c2cf824c545370020b71cc1dd0dff068
  [esp.img]=59df36eb623255fde8ae277f00a91bc8bb1676acd0c3a54c8104183edc63d0a7
  [system_a.img]=67034fd42103772320b04a8c4fd2421e3d0ed7579846d071c97d44de8a0f11f2
  [system_b.img]=2b99ca21e4a9d64afe9da782a4f39cb5a850348cb9da882a423bef66712fd527
  [vendor_a.img]=774cf8d955fae21eaf44333b57860b1baef41a6b2042ad16e6f4fcd2c15dc0a3
  [product_a.img]=428ace4c7044f8612c5eb238b0d92f9a38ab1e0d9c962351f84b5ee4731d09e5
  [system_ext_a.img]=551d23c8dd8ef9e473ba939006238fd7d61f9cae76051eee064a5c26deaa16fa
  [system_dlkm_a.img]=177d0d4782db99812ea359a0ff076c787b04f62bbf79cf948de2436ebc393953
  [vendor_dlkm_a.img]=5e0e5f7b515f15746ad63dc4ee2272dad6af8fe1c5f9b25f1e57e3b0e6fe157f
  [vendor.erofs]=da4ce412b80937e29ff0e78beb297c9a930bed59457b1f1845ae5ebe4f85197b
  [vendor.img]=d496bbfb7c931aae20443494e80d2671e8e4d10212e0e11f9b880823312a74f3
  [system_ext.erofs]=05f2b873426240fb35d9546d7a044bb3e66fb8849afd855e4e40a3b83ed3143c
  [system_ext.img]=2b748b1359714d61a8a232c599ddc7bed39bd37f17d89ac1a9e43d3ed6cfad12
  [super.img]=a64bb6065332489a457a5bf8b4e7bcd9121b0aeb2bef76f766764241c8692e6b
)
# Only with the original key (work/keys/vbmeta.pem, public key sha1 below).
REF_PUBKEY_SHA1=79c9857381efd8c1e0b45c9683bf0aecac4245f9
declare -A EXPECT_REFKEY=(
  [vbmeta.new.img]=8ad0c62e4da7879d55c2a1f5ede8b119e61220872510ea6550ccbe30fb9accd8
  [initrd.img]=a7ffe3f736b1b47d62cc07a5562ef652605086c9e0f12c0be1adcff6b98fb21d
  [kano-real.img]=1ce7e9589351aa2041d5a1dc795f8c850ae000370761528f3f700ba63e98f73c
)

# Sizes reserved for the rebuilt vendor and system_ext partitions.
VENDOR_PART_SIZE=452984832
SYSEXT_PART_SIZE=209715200


# Extra modules for the pinned public recovery kernel.
AC_KO_SHA=db694c38d713e54ba1876d454fd4001cf77889173c44a20d31d9d34d201b5c7b
WAKEFILTER_KO_SHA=3423c3e983bff3d7d35486c475bde473cb7a54dd4115bf7933678f77861cf595
ISA_KO_SHA=9af91efd96d00449a7fde852a225e33eeba052d413628fec08c94b96c9415e44
EFIFB_KO_SHA=2b4a97781c2a24927413dd4a78ec1f624fc7c5aaaa6ddbb25574c66ab65fd1d9
