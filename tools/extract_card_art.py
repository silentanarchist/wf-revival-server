"""Extract the official card pictures for the card creator's preview.

Reads, from your own copy of the game:
  - the server's card data bundle (content/bundles/allcards_assets_all.bundle), for each card's
    id and which picture it uses;
  - the game's asset catalog (Warpforge_Data/StreamingAssets/aa/catalog.bin), for where each
    picture is stored;
  - the game's card art bundles (Warpforge_Data/StreamingAssets/aa/StandaloneWindows64/
    *cardassets_assets_all.bundle).
Writes content/files/card-art.zip: one JPEG per card, named by card id (AM3.jpg, ...), and for cards
and content/files/card-masks.zip: for the units whose art breaks out over the frame, the pop-out mask
(AM3.png: opaque where the art is drawn over the frame, half the picture's size). The server serves
files/card-art/<name> and files/card-masks/<name> straight out of the zips; no restart is needed.

Needs Python 3.9+ with UnityPy and Pillow:  pip install UnityPy Pillow

    python tools/extract_card_art.py --game "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Warhammer 40k Warpforge"

The pictures are the game's own art: keep the zip on your server, never publish it.
"""
import argparse
import io
import os
import struct
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)


class Catalog:
    """Just enough of a Unity Addressables binary catalog (catalog.bin) to find assets by key."""

    def __init__(self, path):
        with open(path, "rb") as f:
            self.d = f.read()
        self.keys_off = self._u(0, 8)[2]

    def _u(self, o, n=1):
        return struct.unpack_from("<%dI" % n, self.d, o)

    def _arr(self, o, size=4):
        if o == 0xFFFFFFFF:
            return []
        n = self._u(o - 4)[0]
        return [o + i for i in range(0, n, size)]

    def _raw(self, o):
        n = self._u(o - 4)[0]
        return self.d[o:o + n]

    def _enc(self, i):
        if i == 0xFFFFFFFF:
            return None
        if i & 0x80000000:
            return self._raw(i & 0x3FFFFFFF).decode("utf-16le")
        return self._raw(i & 0x3FFFFFFF).decode("latin1")

    def _string(self, i, sep="/"):
        if i == 0xFFFFFFFF:
            return None
        if not i & 0x40000000:
            return self._enc(i)
        parts, o = [], i & 0x3FFFFFFF
        while True:
            sid, nxt = self._u(o, 2)
            parts.append(self._enc(sid))
            if nxt == 0xFFFFFFFF:
                break
            o = nxt
        return sep.join(reversed(parts))

    def _typename(self, t):
        if t == 0xFFFFFFFF:
            return None
        _, c = self._u(t, 2)
        return self._string(c, ".") or ""

    def _obj(self, o):
        if o == 0xFFFFFFFF:
            return None
        t, dv = self._u(o, 2)
        tn = self._typename(t) or ""
        if "String" in tn:
            sid, sep = self._u(dv, 2)
            return self._string(sid, chr(sep & 0xFFFF) if sep & 0xFFFF else "/")
        if "Int32" in tn:
            return struct.unpack_from("<i", self.d, dv)[0]
        return None

    def location(self, o):
        pk, iid, prov, deps, dh, data, typ = struct.unpack_from("<4IiII", self.d, o)
        return {"internalId": self._string(iid), "type": self._typename(typ),
                "deps": [self._u(x)[0] for x in self._arr(deps)] if deps != 0xFFFFFFFF else []}

    def keys(self):
        for k in self._arr(self.keys_off, 8):
            kn, ls = self._u(k, 2)
            yield self._obj(kn), [self._u(x)[0] for x in self._arr(ls)]


def card_pictures(cards_bundle, UnityPy):
    """card id -> asset GUID of its picture."""
    env = UnityPy.load(cards_bundle)
    out = {}
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            t = o.read_typetree()
        except Exception:
            continue
        ref = t.get("cardImageReference")
        cid = t.get("uniqueId")
        if cid and isinstance(ref, dict) and ref.get("m_AssetGUID"):
            out[cid] = ref["m_AssetGUID"]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", required=True, help="the game's folder (the one holding Warpforge_Data)")
    ap.add_argument("--cards", default=os.path.join(SERVER, "content", "bundles", "allcards_assets_all.bundle"),
                    help="the card data bundle (default: the server's content/bundles/allcards_assets_all.bundle)")
    ap.add_argument("--out", default=os.path.join(SERVER, "content", "files", "card-art.zip"),
                    help="the zip to write (default: content/files/card-art.zip)")
    ap.add_argument("--size", type=int, default=384, help="picture size in pixels (default 384; the game's are 1024)")
    ap.add_argument("--quality", type=int, default=80, help="JPEG quality (default 80)")
    args = ap.parse_args()

    try:
        import UnityPy
        from PIL import Image, ImageStat
    except ImportError:
        sys.exit("This needs UnityPy and Pillow. Install them with:  pip install UnityPy Pillow")

    aa = os.path.join(args.game, "Warpforge_Data", "StreamingAssets", "aa")
    catalog_path = os.path.join(aa, "catalog.bin")
    bundles_dir = os.path.join(aa, "StandaloneWindows64")
    for need in (catalog_path, bundles_dir, args.cards):
        if not os.path.exists(need):
            sys.exit("Not found: %s" % need)

    print("Reading the cards ...")
    pictures = card_pictures(args.cards, UnityPy)
    print("  %d cards name a picture" % len(pictures))

    print("Reading the game's catalog ...")
    cat = Catalog(catalog_path)
    wanted = set(pictures.values())
    where = {}                                   # GUID -> (bundle file, id inside the bundle)
    cache = {}
    for key, locs in cat.keys():
        if key not in wanted:
            continue
        for l in locs:
            loc = cache.get(l) or cache.setdefault(l, cat.location(l))
            if loc["type"] == "UnityEngine.Texture2D" and loc["deps"]:
                dep = cache.get(loc["deps"][0]) or cache.setdefault(loc["deps"][0], cat.location(loc["deps"][0]))
                where[key] = (os.path.basename((dep["internalId"] or "").replace("\\", "/")), loc["internalId"])
                break
    by_bundle = {}
    for cid, guid in pictures.items():
        if guid in where:
            b, iid = where[guid]
            by_bundle.setdefault(b, {}).setdefault(iid, []).append(cid)
    print("  %d pictures in %d art bundles" % (sum(len(v) for v in by_bundle.values()), len(by_bundle)))

    tmp = args.out + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    written = 0
    masks_out = os.path.join(os.path.dirname(os.path.abspath(args.out)), "card-masks.zip")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z, zipfile.ZipFile(masks_out + ".tmp", "w", zipfile.ZIP_STORED) as zm:
        for b, ids in sorted(by_bundle.items()):
            path = os.path.join(bundles_dir, b)
            if not os.path.exists(path):
                print("  missing bundle, skipped: %s" % b)
                continue
            env = UnityPy.load(path)
            textures = {k: v for k, v in env.container.items() if v.type.name == "Texture2D"}
            n = 0
            for iid, cids in ids.items():
                if iid not in textures:
                    continue
                src = textures[iid].read().image.convert("RGBA")
                # The colour channels are the whole picture. The alpha channel is NOT see-through: it is the
                # pop-out mask (white where the art is drawn over the frame), so it is kept apart.
                img = src.convert("RGB").resize((args.size, args.size), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=args.quality, optimize=True)
                mask = src.split()[3]
                lo, hi = mask.getextrema()
                pop = None
                if hi > 0 and ImageStat.Stat(mask).mean[0] < 200:       # stratagems' masks are (nearly) all white: not used
                    pop = io.BytesIO()
                    m = mask.resize((args.size // 2, args.size // 2), Image.LANCZOS)        # soft edges: half size is plenty
                    Image.merge("LA", (Image.new("L", m.size, 255), m)).save(pop, "PNG", optimize=True)   # see-through except the pop-out
                for cid in cids:
                    z.writestr(cid + ".jpg", buf.getvalue())
                    if pop:
                        zm.writestr(cid + ".png", pop.getvalue())        # opacity: how much of the picture shows over the frame
                    written += 1
                    n += 1
            print("  %-60s %4d" % (b, n))
    os.replace(tmp, args.out)
    os.replace(masks_out + ".tmp", masks_out)
    print("Wrote %d pictures to %s (%.1f MB)" % (written, args.out, os.path.getsize(args.out) / 1e6))


if __name__ == "__main__":
    main()
