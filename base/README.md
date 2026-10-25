# Public recovery base

`recoveries.json` lists the supported public recovery ZIPs and the extracted
image checksums. `builds/<build>.sh` pins the intermediate filesystem, boot,
firmware and module inputs for each supported release.

The base builder verifies the recovery and required public AOSP tools,
replaces hardware-bound credential services, rebuilds the logical partitions,
and signs the result with a local RSA-4096 AVB key. The base is an intermediate
input for a target profile; use `scripts/build.py` to create a finished USB image.

The default public release is `16658739`. `16452207` remains available as a
pinned earlier public release. Unknown builds and mismatched input hashes are
refused. Downloaded images, tools and signing keys remain outside version
control.
