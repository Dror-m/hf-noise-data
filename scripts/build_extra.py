#!/usr/bin/env python3
"""Add noise sources from open datasets other than OpenStreetMap to a built region.

Usage: build_extra.py REGION_DIR REGION.poly CACHE_DIR

Runs after build_tiles.py and writes into the same 0.2 deg tiles, in the same compact
element format, so the app reads them like OSM elements. Every added element carries
"hfnoise:src" (dataset name) and has a negative id, so it never collides with an OSM id.

Datasets (downloaded once per workflow run into CACHE_DIR):
  grw         Global Renewables Watch v1.0 (Microsoft AI for Good + The Nature Conservancy),
              solar PV farm polygons and onshore wind turbine points detected in
              PlanetScope imagery, 2024 Q2. MIT licence.
              https://github.com/microsoft/global-renewables-watch
              -> power=plant + plant:source=solar (polygon), power=generator + generator:source=wind (node)
              Skipped where OSM already maps the same farm (centroid inside an OSM solar area)
              or turbine (OSM wind generator within 150 m).
  gridfinder  Gridfinder v1.1.1 (Arderne et al. 2020, World Bank ESMAP), predicted
              distribution lines from night lights and roads. CC BY 4.0.
              https://zenodo.org/records/3628142
              -> power=minor_line + hfnoise:estimated=yes (shown in the app as a separate,
              "estimated" category). Rows that Gridfinder copied from OSM are dropped.

A dataset that cannot be downloaded or read is skipped with a warning; the OSM tiles stay valid.
"""
import json
import math
import os
import subprocess
import sys
import zlib

sys.path.insert(0, os.path.dirname(__file__))
from build_tiles import TILE, tile_keys, tkey, load_poly, in_ring, simplify  # noqa: E402

DATASETS = {
    "grw_solar": ("https://github.com/microsoft/global-renewables-watch/releases/download/v1.0/solar_all_2024q2_v1.gpkg", "solar_all_2024q2_v1.gpkg"),
    "grw_wind": ("https://github.com/microsoft/global-renewables-watch/releases/download/v1.0/wind_all_2024q2_v1.gpkg", "wind_all_2024q2_v1.gpkg"),
    "gridfinder": ("https://zenodo.org/records/3628142/files/grid.gpkg", "gridfinder_grid.gpkg"),
}
WIND_DEDUPE_M = 150


def fetch(cache, key):
    url, name = DATASETS[key]
    path = os.path.join(cache, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(cache, exist_ok=True)
    try:
        subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "15", "-sS", "-o", path + ".part", url], check=True)
        os.replace(path + ".part", path)
        print(f"downloaded {name}: {os.path.getsize(path) / 1e6:.0f} MB", flush=True)
        return path
    except Exception as e:
        print(f"::warning::cannot download {key} ({e}); skipped", flush=True)
        return None


def read_bbox(path, bbox):
    """GeoDataFrame of the features in bbox (lon0, lat0, lon1, lat1), in WGS84."""
    import geopandas as gpd
    import pyogrio
    info = pyogrio.read_info(path)
    crs = info.get("crs")
    q = bbox
    if crs and "4326" not in str(crs):
        from pyproj import Transformer
        tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        xs, ys = tr.transform([bbox[0], bbox[2], bbox[0], bbox[2]], [bbox[1], bbox[1], bbox[3], bbox[3]])
        q = (min(xs), min(ys), max(xs), max(ys))
    df = gpd.read_file(path, bbox=q, engine="pyogrio")
    if crs and "4326" not in str(crs):
        df = df.to_crs("EPSG:4326")
    return df


def ll(x, y):
    return [round(y, 6), round(x, 6)]


def rings_of(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    return []


def lines_of(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    return []


def ring_in(lat, lon, ring):
    """Point in a [[lat,lon],...] ring."""
    c = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


def main():
    out, poly, cache = sys.argv[1], sys.argv[2], sys.argv[3]
    idx_path = os.path.join(out, "index.json")
    idx = json.load(open(idx_path, encoding="utf-8"))
    rings = load_poly(poly)
    pts = [p for r in rings for p in r]
    bbox = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    inside = lambda lat, lon: any(in_ring(lon, lat, r) for r in rings)
    cov = set(idx["cov"])

    # existing OSM solar areas and wind turbines, for de-duplication
    osm_solar, osm_wind = [], []
    for k in idx["data"]:
        for el in json.load(open(os.path.join(out, "tiles", k + ".json"), encoding="utf-8"))["elements"]:
            t = el.get("tags", {})
            src = (t.get("generator:source", "") + ";" + t.get("plant:source", "")).lower()
            if t.get("power") not in ("plant", "generator"):
                continue
            if "solar" in src or "photovoltaic" in src:
                if el["t"] == "w" and len(el["g"]) >= 4:
                    osm_solar.append(el["g"])
                elif el["t"] == "r":
                    osm_solar.extend(g for role, g in el["m"] if role != "inner" and len(g) >= 4)
            elif "wind" in src:
                c = el["c"] if el["t"] == "n" else el["g"][0]
                osm_wind.append(c)
    sbox = [(min(p[0] for p in g), min(p[1] for p in g), max(p[0] for p in g), max(p[1] for p in g), g) for g in osm_solar]

    added = {}   # tile key -> [elements]
    # negative ids, unique per region (regions are merged in the app by type + id)
    nid = [-(zlib.crc32(os.path.basename(os.path.normpath(out)).encode()) % 1000) * 10**9]
    counts = {"grw_solar": 0, "grw_wind": 0, "gridfinder": 0, "grw_solar_dup": 0, "grw_wind_dup": 0}

    def put(el, coords):
        for ij in tile_keys(coords, TILE):
            k = tkey(*ij, TILE)
            if k in cov:
                added.setdefault(k, []).append(el)

    def new_id():
        nid[0] -= 1
        return nid[0]

    # --- Global Renewables Watch: solar farms
    p = fetch(cache, "grw_solar")
    if p:
        try:
            df = read_bbox(p, bbox)
            for geom in df.geometry:
                for pg in rings_of(geom):
                    c = pg.representative_point()
                    lat, lon = c.y, c.x
                    if not inside(lat, lon):
                        continue
                    if any(b[0] <= lat <= b[2] and b[1] <= lon <= b[3] and ring_in(lat, lon, b[4]) for b in sbox):
                        counts["grw_solar_dup"] += 1
                        continue
                    ring = simplify([ll(x, y) for x, y in pg.exterior.coords], 5)
                    if len(ring) < 4:
                        continue
                    el = {"t": "w", "id": new_id(), "tags": {"power": "plant", "plant:source": "solar", "hfnoise:src": "grw"}, "g": ring}
                    put(el, ring)
                    counts["grw_solar"] += 1
        except Exception as e:
            print(f"::warning::GRW solar failed ({e})", flush=True)

    # --- Global Renewables Watch: wind turbines
    p = fetch(cache, "grw_wind")
    if p:
        try:
            df = read_bbox(p, bbox)
            for geom in df.geometry:
                if geom is None or geom.is_empty:
                    continue
                c = geom if geom.geom_type == "Point" else geom.centroid
                lat, lon = c.y, c.x
                if not inside(lat, lon):
                    continue
                mx = 111320 * math.cos(math.radians(lat))
                if any(math.hypot((w[1] - lon) * mx, (w[0] - lat) * 110574) < WIND_DEDUPE_M for w in osm_wind):
                    counts["grw_wind_dup"] += 1
                    continue
                el = {"t": "n", "id": new_id(), "c": ll(lon, lat), "tags": {"power": "generator", "generator:source": "wind", "hfnoise:src": "grw"}}
                put(el, [el["c"]])
                counts["grw_wind"] += 1
        except Exception as e:
            print(f"::warning::GRW wind failed ({e})", flush=True)

    # --- Gridfinder: predicted distribution lines (OSM-derived rows dropped)
    p = fetch(cache, "gridfinder")
    if p:
        try:
            df = read_bbox(p, bbox)
            srccol = next((c for c in df.columns if c.lower() == "source"), None)
            for i, geom in enumerate(df.geometry):
                if srccol is not None and "osm" in str(df[srccol].iloc[i]).lower():
                    continue
                for ln in lines_of(geom):
                    g = [ll(x, y) for x, y in ln.coords]
                    if len(g) < 2:
                        continue
                    mid = g[len(g) // 2]
                    if not inside(mid[0], mid[1]):
                        continue
                    g = simplify(g, 10)
                    el = {"t": "w", "id": new_id(), "tags": {"power": "minor_line", "hfnoise:src": "gridfinder", "hfnoise:estimated": "yes"}, "g": g}
                    put(el, g)
                    counts["gridfinder"] += 1
        except Exception as e:
            print(f"::warning::Gridfinder failed ({e})", flush=True)

    # write the merged tiles and the index
    for k, els in added.items():
        f = os.path.join(out, "tiles", k + ".json")
        cur = json.load(open(f, encoding="utf-8"))["elements"] if os.path.exists(f) else []
        with open(f, "w", encoding="utf-8") as fh:
            json.dump({"elements": cur + els}, fh, ensure_ascii=False, separators=(",", ":"))
    idx["data"] = sorted(set(idx["data"]) | set(added))
    idx["extra"] = {"grw": "v1.0 (2024 Q2)", "gridfinder": "v1.1.1", "counts": counts}
    with open(idx_path, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, separators=(",", ":"))
    print("extra sources: " + ", ".join(f"{k} {v}" for k, v in counts.items()), flush=True)


if __name__ == "__main__":
    main()
