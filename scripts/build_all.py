#!/usr/bin/env python3
"""Build the regions listed in regions.json into SITE/r/<id>/ and write SITE/regions.json.

Usage: build_all.py SITE [--only ID[,ID...]]

GitHub Pages replaces the whole site on every deploy, so every region must be in each
deployment. With --only, just the named regions are rebuilt from Geofabrik; every other
region is copied from the currently published site (much faster). A region that cannot
be copied is rebuilt. Without --only, every region is rebuilt (monthly refresh).
"""
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import urllib.request

GEOFABRIK = "https://download.geofabrik.de/"
LIVE = os.environ.get("LIVE_SITE", "https://dror-m.github.io/hf-noise-data/")


def get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=timeout) as r:
        return r.read()


def build(r, site):
    rid, path = r["id"], r["path"]
    subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "10", "-sS", "-o", "region.osm.pbf", GEOFABRIK + path + "-latest.osm.pbf"], check=True)
    subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "10", "-sS", "-o", "region.poly", GEOFABRIK + path + ".poly"], check=True)
    print(f"pbf size: {os.path.getsize('region.osm.pbf') / 1e6:.0f} MB", flush=True)
    dest = os.path.join(site, "r", rid)
    shutil.rmtree(dest, ignore_errors=True)
    try:
        subprocess.run([sys.executable, "scripts/build_tiles.py", "region.osm.pbf", "region.poly", dest], check=True)
    finally:
        for f in ("region.osm.pbf", "region.poly"):
            if os.path.exists(f):
                os.remove(f)
    return json.load(open(os.path.join(dest, "index.json")))


def copy_live(r, live_meta, site):
    """Copy a published region (index + every listed file) from the live site."""
    base = LIVE + live_meta["base"]
    idx = json.loads(get(base + "index.json"))
    files = ["tiles/%s.json" % k for k in idx["data"]] + ["borders/%s.json" % k for k in idx["bdata"]] + ["islands/%s.json" % k for k in idx.get("idata", [])] + ["ground/%s.json" % k for k in idx.get("gdata", [])]
    dest = os.path.join(site, "r", r["id"])
    shutil.rmtree(dest, ignore_errors=True)
    for sub in ("tiles", "borders", "islands", "ground"):
        os.makedirs(os.path.join(dest, sub), exist_ok=True)

    def fetch(f):
        last = None
        for _ in range(4):
            try:
                data = get(base + f)
                with open(os.path.join(dest, f), "wb") as fh:
                    fh.write(data)
                return
            except Exception as e:  # retry transient errors
                last = e
        raise RuntimeError(f"{f}: {last}")

    with concurrent.futures.ThreadPoolExecutor(16) as ex:
        list(ex.map(fetch, files))
    with open(os.path.join(dest, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(idx, fh, separators=(",", ":"))
    print(f"copied {len(files)} files from the live site", flush=True)
    return idx


def main():
    site = sys.argv[1]
    only = None
    if "--only" in sys.argv:
        only = {x for x in sys.argv[sys.argv.index("--only") + 1].split(",") if x}
    regions = json.load(open("regions.json", encoding="utf-8"))
    os.makedirs(site, exist_ok=True)

    live = {}
    if only is not None:
        try:
            live = {r["id"]: r for r in json.loads(get(LIVE + "regions.json")).get("regions", [])}
        except Exception as e:
            print(f"::warning::cannot read the live site ({e}); rebuilding everything", flush=True)
            only = None

    out, failed = [], []
    for r in regions:
        rid, path = r["id"], r["path"]
        print(f"::group::{rid} ({path})", flush=True)
        idx = None
        try:
            if only is not None and rid not in only and rid in live:
                try:
                    idx = copy_live(r, live[rid], site)
                except Exception as e:
                    print(f"::warning::copy of {rid} failed ({e}); rebuilding", flush=True)
            if idx is None:
                idx = build(r, site)
            out.append({"id": rid, "path": path, "name": r.get("name") or rid, "base": f"r/{rid}/",
                        "generated": idx["generated"], "osm_timestamp": idx.get("osm_timestamp"), "bbox": idx.get("bbox")})
        except subprocess.CalledProcessError as e:
            print(f"::error::region {rid} failed: {e}", flush=True)
            failed.append({"id": rid, "path": path, "name": r.get("name") or rid, "error": str(e)})
        print("::endgroup::", flush=True)
    with open(os.path.join(site, "regions.json"), "w", encoding="utf-8") as f:
        json.dump({"format": "hfnoise-regions", "regions": out, "failed": failed}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"built/copied {len(out)} regions, {len(failed)} failed", flush=True)
    if not out:
        sys.exit(1)


if __name__ == "__main__":
    main()
