#!/usr/bin/env python3
"""Build every region listed in regions.json into SITE/r/<id>/ and write SITE/regions.json."""
import json
import os
import shutil
import subprocess
import sys

BASE = "https://download.geofabrik.de/"


def main():
    site = sys.argv[1]
    regions = json.load(open("regions.json", encoding="utf-8"))
    os.makedirs(site, exist_ok=True)
    out, failed = [], []
    for r in regions:
        rid, path = r["id"], r["path"]
        print(f"::group::{rid} ({path})", flush=True)
        try:
            subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "10", "-sS", "-o", "region.osm.pbf", BASE + path + "-latest.osm.pbf"], check=True)
            subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "10", "-sS", "-o", "region.poly", BASE + path + ".poly"], check=True)
            print(f"pbf size: {os.path.getsize('region.osm.pbf') / 1e6:.0f} MB", flush=True)
            dest = os.path.join(site, "r", rid)
            shutil.rmtree(dest, ignore_errors=True)
            subprocess.run([sys.executable, "scripts/build_tiles.py", "region.osm.pbf", "region.poly", dest], check=True)
            idx = json.load(open(os.path.join(dest, "index.json")))
            out.append({"id": rid, "path": path, "name": r.get("name") or rid, "base": f"r/{rid}/",
                        "generated": idx["generated"], "osm_timestamp": idx.get("osm_timestamp"), "bbox": idx.get("bbox")})
        except subprocess.CalledProcessError as e:
            print(f"::error::region {rid} failed: {e}", flush=True)
            failed.append({"id": rid, "path": path, "name": r.get("name") or rid, "error": str(e)})
        finally:
            for f in ("region.osm.pbf", "region.poly"):
                if os.path.exists(f):
                    os.remove(f)
            print("::endgroup::", flush=True)
    with open(os.path.join(site, "regions.json"), "w", encoding="utf-8") as f:
        json.dump({"format": "hfnoise-regions", "regions": out, "failed": failed}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"built {len(out)} regions, {len(failed)} failed", flush=True)
    if not out:
        sys.exit(1)


if __name__ == "__main__":
    main()
