#!/usr/bin/env python3
"""Compare a rebuilt base image with the reference kano-real.img.

usage: compare-base.py REF_IMG CAND_IMG REF_INITRD CAND_INITRD [--md OUT.md] [--json OUT.json]

Reports: whole-file sha256, GPT header/entries, sha256 of every GPT partition
and of the gaps between them, every LP logical partition inside super, the ESP
file list (size, FAT mtime, sha256), the ESP and standalone initrd split into
cpio archive and bootconfig, and the vbmeta_a summary.  Read-only.
"""

import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gbos_base as g  # noqa: E402


def regions(parts, total):
    out, cur = [], 0
    for p in sorted(parts, key=lambda x: x["start"]):
        if p["start"] > cur:
            out.append((f"gap@{cur}", cur, p["start"] - cur))
        out.append((p["name"], p["start"], p["size"]))
        cur = p["start"] + p["size"]
    if cur < total:
        out.append((f"gap@{cur}", cur, total - cur))
    return out


def hash_regions(path, regs):
    whole = hashlib.sha256()
    res = {}
    with open(path, "rb") as f:
        for name, off, size in regs:
            h = hashlib.sha256()
            f.seek(off)
            left = size
            while left:
                b = f.read(min(left, 16 << 20))
                if not b:
                    raise SystemExit("short read")
                h.update(b)
                whole.update(b)
                left -= len(b)
            res[name] = h.hexdigest()
    return whole.hexdigest(), res


def lp_hashes(path, super_off):
    meta = g.lp_parse(path, super_off)
    out = {}
    with open(path, "rb") as f:
        for p in meta["partitions"]:
            h = hashlib.sha256()
            for e in p["extents"]:
                f.seek(super_off + e["data"] * 512)
                left = e["sectors"] * 512
                while left:
                    b = f.read(min(left, 16 << 20))
                    if not b:
                        raise SystemExit("short read")
                    h.update(b)
                    left -= len(b)
            out[p["name"]] = dict(size=p["size"], sha256=h.hexdigest() if p["extents"] else "-",
                                  extents=[(e["sectors"], e["data"]) for e in p["extents"]])
    first = meta["block_devices"][0]["first_logical_sector"] * 512
    meta_region = hashlib.sha256(g.pread(path, super_off, first)).hexdigest()
    return meta, out, meta_region


def fat_dt(d, t):
    return f"{1980 + (d >> 9)}-{(d >> 5) & 15:02}-{d & 31:02} {t >> 11:02}:{(t >> 5) & 63:02}:{(t & 31) * 2:02}"


def esp_files(path, off):
    fs = g.Fat32(path, off)
    out = {}

    def rec(cl, prefix):
        for ln, short, eoff, e in fs.entries(cl):
            if e[11] & 0x08:  # volume label
                out["<volume-label>"] = dict(name=e[0:11].decode(), mtime=fat_dt(struct.unpack_from("<H", e, 24)[0],
                                                                      struct.unpack_from("<H", e, 22)[0]))
                continue
            if short in (".", ".."):
                continue
            hi, lo = struct.unpack_from("<H", e, 20)[0], struct.unpack_from("<H", e, 26)[0]
            c = (hi << 16) | lo
            mtime = fat_dt(struct.unpack_from("<H", e, 24)[0], struct.unpack_from("<H", e, 22)[0])
            p = prefix + "/" + ln
            if e[11] & 0x10:
                out[p + "/"] = dict(mtime=mtime, clusters=f"{c}")
                rec(c, p)
            else:
                size = struct.unpack_from("<I", e, 28)[0]
                ch = fs.chain(c) if c else []
                data = b"".join(g.pread(path, fs.coff(x), fs.csz) for x in ch)[:size]
                out[p] = dict(size=size, mtime=mtime, sha256=hashlib.sha256(data).hexdigest(),
                              clusters=f"{ch[0]}-{ch[-1]}" if ch else "-", data=data)
    rec(fs.root, "")
    return out


def initrd_split(data):
    arch, bc = g.split_bootconfig(data)
    return dict(size=len(data), archive_sha256=hashlib.sha256(arch).hexdigest(), archive_size=len(arch),
                bootconfig=bc.decode(), bootconfig_size=len(bc))


def vbmeta_info(path, off):
    hdr = g.pread(path, off, 256)
    auth, aux = struct.unpack_from(">QQ", hdr, 12)
    blob = g.pread(path, off, 256 + auth + aux)
    alg = struct.unpack_from(">I", hdr, 28)[0]
    pk_off, pk_size = struct.unpack_from(">QQ", hdr, 64)
    rb = struct.unpack_from(">Q", hdr, 112)[0]
    rel = hdr[128:176].split(b"\0")[0].decode()
    pk = blob[256 + auth + pk_off:256 + auth + pk_off + pk_size]
    return dict(size=len(blob), digest=hashlib.sha256(blob).hexdigest(), algorithm=alg,
                pubkey_sha1=hashlib.sha1(pk).hexdigest(), rollback_index=rb, release=rel)


def main():
    args = sys.argv[1:]
    md_out = js_out = None
    if "--md" in args:
        i = args.index("--md"); md_out = args[i + 1]; del args[i:i + 2]
    if "--json" in args:
        i = args.index("--json"); js_out = args[i + 1]; del args[i:i + 2]
    ref, cand, ref_initrd, cand_initrd = args
    L = []
    res = {}

    pr, pc = g.gpt_parts(ref), g.gpt_parts(cand)
    sr, sc = os.path.getsize(ref), os.path.getsize(cand)
    strip = lambda ps: [{k: p[k] for k in ("index", "name", "type", "guid", "start", "size", "attr")} for p in ps]
    gpt_same = strip(pr) == strip(pc) and sr == sc
    hdr_same = g.pread(ref, 0, 34 * 512) == g.pread(cand, 0, 34 * 512) and \
        g.pread(ref, sr - 33 * 512, 33 * 512) == g.pread(cand, sc - 33 * 512, 33 * 512)
    regs = regions(pr, sr)
    wr, hr = hash_regions(ref, regs)
    wc, hc = hash_regions(cand, regions(pc, sc))
    res.update(whole=dict(ref=wr, cand=wc), gpt_layout_same=gpt_same, gpt_bytes_same=hdr_same,
               partitions={n: dict(ref=hr[n], cand=hc.get(n)) for n, _, _ in regs})

    L.append("## 整檔與 GPT\n")
    L.append("| 項目 | 參考 kano-real.img | 重建 | 相同 |\n|---|---|---|---|")
    L.append(f"| 整檔 sha256 | `{wr}` | `{wc}` | {'是' if wr == wc else '**否**'} |")
    L.append(f"| 檔案大小 | {sr} | {sc} | {'是' if sr == sc else '**否**'} |")
    L.append(f"| GPT 表（名稱、type、GUID、範圍、屬性） | {len(pr)} 項 | {len(pc)} 項 | {'是' if gpt_same else '**否**'} |")
    L.append(f"| 主/備份 GPT 原始位元組（LBA 0–33、最後 33 個 LBA） | | | {'是' if hdr_same else '**否**'} |")
    L.append("\n## GPT 分割區 sha256\n")
    L.append("| # | 分割區 | 起點 (byte) | 大小 | 參考 sha256 | 重建 sha256 | 相同 |\n|---|---|---|---|---|---|---|")
    idx = {p["name"]: p for p in pr}
    for n, off, size in regs:
        a, b = hr[n], hc.get(n)
        num = idx[n]["index"] if n in idx else ""
        L.append(f"| {num} | {n} | {off} | {size} | `{a[:16]}` | `{(b or '-')[:16]}` | {'是' if a == b else '**否**'} |")

    so = idx["super"]["start"]
    mr, lr, mrr = lp_hashes(ref, so)
    mc, lc, mrc = lp_hashes(cand, so)
    res["lp"] = dict(metadata_same=(mr == mc), metadata_region=dict(ref=mrr, cand=mrc),
                     parts={k: dict(ref=lr[k], cand=lc.get(k)) for k in lr})
    L.append("\n## super 內的邏輯分割區（LP）\n")
    L.append(f"LP 中繼資料（geometry、header、groups、extents）解析結果相同：{'是' if mr == mc else '**否**'}；"
             f"super 開頭中繼資料區（{mr['block_devices'][0]['first_logical_sector'] * 512} bytes）位元組相同：{'是' if mrr == mrc else '**否**'}。\n")
    L.append("| 邏輯分割區 | 大小 | 參考 sha256 | 重建 sha256 | 相同 |\n|---|---|---|---|---|")
    for k, v in lr.items():
        w = lc.get(k, {})
        L.append(f"| {k} | {v['size']} | `{v['sha256'][:16]}` | `{w.get('sha256', '-')[:16]}` | {'是' if v == w else '**否**'} |")

    eo = idx["EFI-SYSTEM"]["start"]
    er, ec = esp_files(ref, eo), esp_files(cand, eo)
    L.append("\n## ESP 檔案\n")
    L.append("| 路徑 | 大小 | FAT mtime | 叢集 | 參考 sha256 | 重建 sha256 | 相同 |\n|---|---|---|---|---|---|---|")
    for k in sorted(set(er) | set(ec)):
        a, b = er.get(k, {}), ec.get(k, {})
        same = {x: y for x, y in a.items() if x != "data"} == {x: y for x, y in b.items() if x != "data"}
        tag = lambda x: x.get("sha256", x.get("name", "(目錄)" if k.endswith("/") else "-"))[:16]
        L.append(f"| {k} | {a.get('size', '')} | {a.get('mtime', '')} | {a.get('clusters', '')} | "
                 f"`{tag(a)}` | `{tag(b)}` | {'是' if same else '**否**'} |")
    res["esp"] = {k: dict(ref={x: y for x, y in er.get(k, {}).items() if x != "data"},
                          cand={x: y for x, y in ec.get(k, {}).items() if x != "data"}) for k in set(er) | set(ec)}

    ir = {"ESP /initrd.img（參考）": initrd_split(er["/initrd.img"]["data"]),
          "ESP /initrd.img（重建）": initrd_split(ec["/initrd.img"]["data"]),
          "獨立 initrd.img（參考）": initrd_split(open(ref_initrd, "rb").read()),
          "獨立 initrd.img（重建）": initrd_split(open(cand_initrd, "rb").read())}
    res["initrd"] = ir
    L.append("\n## initrd：cpio 封存與 bootconfig\n")
    L.append("| 檔案 | 大小 | 封存 sha256 | bootconfig bytes | vbmeta.digest |\n|---|---|---|---|---|")
    for k, v in ir.items():
        dg = [l.split("=", 1)[1] for l in v["bootconfig"].splitlines() if l.startswith("androidboot.vbmeta.digest=")]
        L.append(f"| {k} | {v['size']} | `{v['archive_sha256'][:16]}` | {v['bootconfig_size']} | `{(dg or ['-'])[0][:16]}` |")
    a, b = ir["獨立 initrd.img（參考）"]["bootconfig"], ir["獨立 initrd.img（重建）"]["bootconfig"]
    L.append(f"\n獨立 initrd 的 bootconfig 完全相同：{'是' if a == b else '**否**'}")
    if a != b:
        import difflib
        L.append("\n```diff\n" + "\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), "參考", "重建", lineterm="")) + "\n```")

    vo = idx["vbmeta_a"]["start"]
    vr, vc = vbmeta_info(ref, vo), vbmeta_info(cand, vo)
    res["vbmeta"] = dict(ref=vr, cand=vc)
    L.append("\n## vbmeta_a\n")
    L.append("| 欄位 | 參考 | 重建 |\n|---|---|---|")
    for k in vr:
        L.append(f"| {k} | `{vr[k]}` | `{vc[k]}` |")

    md = "\n".join(L) + "\n"
    if md_out:
        open(md_out, "w").write(md)
    if js_out:
        open(js_out, "w").write(json.dumps(res, indent=1, default=str))
    print(md)
    differences = md.count("**否**")
    print("IDENTICAL" if not differences else f"{differences} difference(s)")
    sys.exit(1 if differences else 0)


if __name__ == "__main__":
    main()
