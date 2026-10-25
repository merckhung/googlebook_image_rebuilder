"""Regression tests for recovery ramdisks, including the microcode prefix."""
import gzip
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gbos_base", HERE / "gbos_base.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
sys.path.insert(0, str(HERE.parent / "builder/lib"))
from early_isa import newc_entry


def archive(name, contents):
    body = newc_entry(name, contents, 1, 0o100644) + newc_entry("TRAILER!!!", b"", 2, 0)
    return body + bytes(-len(body) % 512)


class Ramdisks(unittest.TestCase):
    def test_existing_raw_and_lz4_bytes_unchanged(self):
        raw = archive("init", b"example init")
        self.assertEqual(base.ramdisk_decompress(raw), raw)
        packed = subprocess.check_output(["lz4", "-lc"], input=raw, stderr=subprocess.DEVNULL)
        self.assertEqual(base.ramdisk_decompress(packed), raw)

    def test_microcode_prefix_and_compressed_platform_both_survive(self):
        debug = archive("kernel/x86/microcode/GenuineIntel.bin", b"microcode fixture")
        platform = archive("first_stage_ramdisk/fstab.android-desktop", b"fstab contents")
        mixed = debug + gzip.compress(platform, mtime=0)
        self.assertEqual(base.ramdisk_decompress(mixed), debug + platform)
        self.assertEqual(base.ramdisk_decompress(gzip.compress(platform, mtime=0)), platform)

    def test_corrupt_or_unknown_archive_is_rejected(self):
        for bad in (b"unknown compression", archive("init", b"payload")[:120], b"070701" + b"0" * 104):
            with self.subTest(bad=bad[:16]), self.assertRaises((SystemExit, ValueError)):
                base.ramdisk_decompress(bad)


if __name__ == "__main__":
    unittest.main()
