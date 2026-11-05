"""
procreate_reader.py - parser for .procreate files (used by LayerPeek, also a small CLI).

Usage:
    python procreate_reader.py my_art.procreate          (or drag the file onto this script)
    python procreate_reader.py my_art.procreate --noflip (if the result is upside down)

Output goes to a folder "my_art_export" next to the file:
    thumbnail.png     - the preview Procreate stored in the file (always works)
    composite.png     - all visible layers merged (normal blend mode only)
    layers/NN_name.png - every layer on its own, transparent background
    info.txt          - canvas size, layer list
Requires: Python 3, Pillow, numpy   (pip install pillow numpy)
"""
import sys, os, re, zipfile, plistlib, io, traceback
import numpy as np
from PIL import Image


# ---------- pure-python LZO1X decompressor (Procreate tiles are LZO) ----------
def lzo1x_decompress(src):
    out = bytearray()
    ip, n = 0, len(src)
    state = 0  # 0 none, 1-3 trailing literals, 4 after long literal run
    t = src[0]
    if t > 17:
        ip = 1
        t -= 17
        out += src[ip:ip + t]
        ip += t
        state = 4 if t >= 4 else t
    while ip < n:
        t = src[ip]
        ip += 1
        if t < 16:
            if state == 0:
                if t == 0:
                    while src[ip] == 0:
                        t += 255
                        ip += 1
                    t += 15 + src[ip]
                    ip += 1
                ln = t + 3
                out += src[ip:ip + ln]
                ip += ln
                state = 4
                continue
            dist = 1 + (t >> 2) + (src[ip] << 2)
            ip += 1
            if state == 4:
                dist += 0x800
                ln = 3
            else:
                ln = 2
            trail = t & 3
        elif t >= 64:
            ln = (t >> 5) + 1
            dist = 1 + ((t >> 2) & 7) + (src[ip] << 3)
            ip += 1
            trail = t & 3
        elif t >= 32:
            ln = t & 31
            if ln == 0:
                while src[ip] == 0:
                    ln += 255
                    ip += 1
                ln += 31 + src[ip]
                ip += 1
            ln += 2
            v = src[ip] | (src[ip + 1] << 8)
            ip += 2
            dist = 1 + (v >> 2)
            trail = v & 3
        else:  # 16..31
            ln = t & 7
            if ln == 0:
                while src[ip] == 0:
                    ln += 255
                    ip += 1
                ln += 7 + src[ip]
                ip += 1
            ln += 2
            v = src[ip] | (src[ip + 1] << 8)
            ip += 2
            dist = 16384 + ((t & 8) << 11) + (v >> 2)
            if dist == 16384:
                break  # end of stream
            trail = v & 3
        start = len(out) - dist
        if start < 0:
            raise ValueError("bad LZO back-reference")
        if dist >= ln:
            out += out[start:start + ln]
        else:  # overlapping copy: repeat the pattern
            pat = bytes(out[start:])
            out += (pat * (ln // dist + 1))[:ln]
        if trail:
            out += src[ip:ip + trail]
            ip += trail
        state = trail
    return bytes(out)


def decompress_tile(data, expected):
    try:
        raw = lzo1x_decompress(data)
        if len(raw) == expected:
            return raw
    except Exception:
        pass
    try:  # newer files might use LZ4
        import lz4.block
        return lz4.block.decompress(data, uncompressed_size=expected)
    except Exception:
        pass
    if len(data) == expected:  # stored uncompressed
        return data
    raise ValueError("cannot decompress tile (%d bytes -> expected %d)" % (len(data), expected))


# ---------- NSKeyedArchiver plist resolver ----------
def resolve(objs, v, depth=0, stack=()):
    """Resolve NSKeyedArchiver UIDs. Cycle-safe and depth-limited (real files have loops)."""
    if depth > 6:
        return None
    if isinstance(v, plistlib.UID):
        if v.data in stack:
            return None
        return resolve(objs, objs[v.data], depth + 1, stack + (v.data,))
    if isinstance(v, dict):
        if "NS.objects" in v:
            return [resolve(objs, x, depth, stack) for x in v["NS.objects"]]
        return {k: resolve(objs, x, depth, stack) for k, x in v.items() if k != "$class"}
    if isinstance(v, list):
        return [resolve(objs, x, depth, stack) for x in v]
    return v


def parse_size(s):
    nums = re.findall(r"-?\d+(?:\.\d+)?", str(s))
    return int(float(nums[0])), int(float(nums[1]))


# ---------- layer decoding ----------
def load_layer(z, uuid, W, H, tile, names):
    img = np.zeros((H, W, 4), dtype=np.uint8)
    found = 0
    for nm in names:
        if not nm.startswith(uuid + "/") or not nm.endswith(".chunk"):
            continue
        m = re.match(r"(\d+)~(\d+)", os.path.basename(nm))
        if not m:
            continue
        col, row = int(m.group(1)), int(m.group(2))
        x0, y0 = col * tile, row * tile
        tw, th = min(tile, W - x0), min(tile, H - y0)
        if tw <= 0 or th <= 0:
            continue
        raw = decompress_tile(z.read(nm), tw * th * 4)
        img[y0:y0 + th, x0:x0 + tw] = np.frombuffer(raw, np.uint8).reshape(th, tw, 4)
        found += 1
    # Procreate stores premultiplied alpha -> convert to straight alpha
    a = img[..., 3:4].astype(np.float32)
    rgb = img[..., :3].astype(np.float32)
    rgb = np.where(a > 0, rgb * 255.0 / np.maximum(a, 1), 0)
    out = np.concatenate([np.clip(rgb, 0, 255), a], axis=2).astype(np.uint8)
    return out, found


def safe(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", str(s)).strip() or "layer"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flip = "--noflip" not in sys.argv
    if not args:
        print(__doc__)
        input("Press Enter to close...")
        return
    path = args[0]
    outdir = os.path.splitext(path)[0] + "_export"
    os.makedirs(os.path.join(outdir, "layers"), exist_ok=True)
    z = zipfile.ZipFile(path)
    names = z.namelist()
    info = []

    # 1) thumbnail - simplest and most reliable
    for cand in ("QuickLook/Thumbnail.png", "Thumbnail.png"):
        if cand in names:
            open(os.path.join(outdir, "thumbnail.png"), "wb").write(z.read(cand))
            print("Saved thumbnail.png")
            break

    # 2) document structure
    try:
        plist = plistlib.loads(z.read("Document.archive"))
        objs = plist["$objects"]
        root = resolve(objs, plist["$top"]["root"])
        W, H = parse_size(root["size"])
        tile = int(root.get("tileSize", 256))
        layers = list(reversed(root["layers"]))  # Procreate lists top layer first
        info.append("Canvas: %dx%d  tile=%d  layers=%d" % (W, H, tile, len(layers)))
        print(info[-1])
    except Exception:
        print("Could not read Document.archive; only the thumbnail was exported.")
        traceback.print_exc()
        return

    # 3) layers (stored bottom -> top)
    comp = np.zeros((H, W, 4), dtype=np.float32)
    for i, L in enumerate(layers):
        try:
            uuid = L["UUID"]
            name = L.get("name") or "Layer %d" % (i + 1)
            hidden = bool(L.get("hidden", False))
            opacity = float(L.get("opacity", 1.0))
            arr, nchunks = load_layer(z, uuid, W, H, tile, names)
            if flip:
                arr = arr[::-1]
            Image.fromarray(arr, "RGBA").save(
                os.path.join(outdir, "layers", "%02d_%s.png" % (i + 1, safe(name))))
            info.append("%02d  %-30s opacity=%.2f hidden=%s chunks=%d blend=%s" %
                        (i + 1, name, opacity, hidden, nchunks, L.get("blend", 0)))
            print(info[-1])
            if not hidden and nchunks:
                src = arr.astype(np.float32) / 255.0
                sa = src[..., 3:4] * opacity
                da = comp[..., 3:4]
                oa = sa + da * (1 - sa)
                orgb = (src[..., :3] * sa + comp[..., :3] * da * (1 - sa)) / np.maximum(oa, 1e-6)
                comp = np.concatenate([orgb, oa], axis=2)
        except Exception as e:
            info.append("%02d  FAILED: %s" % (i + 1, e))
            print(info[-1])

    Image.fromarray((np.clip(comp, 0, 1) * 255).astype(np.uint8), "RGBA").save(
        os.path.join(outdir, "composite.png"))
    open(os.path.join(outdir, "info.txt"), "w", encoding="utf-8").write("\n".join(info))
    print("Done ->", outdir)


if __name__ == "__main__":
    main()
