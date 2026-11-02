#!/usr/bin/env python3
"""Build pre-tiled HF man-made noise source data from an OpenStreetMap extract.

Usage: build_tiles.py REGION.osm.pbf REGION.poly OUT_DIR

Output (served by GitHub Pages):
  OUT_DIR/index.json             metadata, tile coverage and the list of non-empty tiles
  OUT_DIR/tiles/<lat>_<lon>.json noise-source elements in a 0.2 deg tile (compact format)
  OUT_DIR/borders/<lat>_<lon>.json  national border lines in a 1 deg tile

Compact element format (expanded to Overpass-style JSON by the app):
  node:     {"t":"n","id":ID,"c":[lat,lon],"tags":{...}}
  way:      {"t":"w","id":ID,"tags":{...},"g":[[lat,lon],...]}
  relation: {"t":"r","id":ID,"tags":{...},"m":[[role,[[lat,lon],...]],...]}
"""
import datetime
import json
import math
import os
import sys

import osmium

TILE = 0.2      # source tiles, must match TILE in the app
BTILE = 1       # border tiles, must match the app
EPS = 1e-7

LANDUSE = {"residential", "industrial", "commercial", "retail", "quarry", "greenhouse_horticulture", "farmyard"}
MAN_MADE = {"mast", "tower", "communications_tower", "water_works", "pumping_station", "desalination_plant", "wastewater_plant"}
BUILDINGS = {"data_center", "greenhouse", "cowshed", "farm_auxiliary", "sty", "stable", "chicken_coop"}
KEEP_TAGS = {
    "power", "voltage", "generator:source", "plant:source", "generator:method", "generator:type",
    "location", "generator:place", "amenity", "railway", "electrified", "tunnel", "highway",
    "man_made", "tower:type", "communication:mobile_phone", "communication:microwave",
    "communication:radio", "communication:television", "communication:am_radio", "telecom",
    "building", "landuse", "barrier", "fence_type", "name", "name:he", "type",
}


def want(t):
    p = t.get("power")
    if p in ("line", "minor_line", "substation", "transformer", "plant", "generator"):
        return True
    if t.get("landuse") in LANDUSE:
        return True
    if t.get("railway") in ("rail", "light_rail", "tram"):
        return True
    if t.get("highway") in ("motorway", "trunk", "primary"):
        return True
    if t.get("barrier") == "fence" and t.get("fence_type") == "electric":
        return True
    if t.get("amenity") in ("charging_station", "hospital"):
        return True
    if t.get("man_made") in MAN_MADE:
        return True
    if t.get("telecom") == "data_center":
        return True
    if t.get("building") in BUILDINGS:
        return True
    return False


def tagdict(o):
    return {t.k: t.v for t in o.tags}


def slim(t):
    return {k: v for k, v in t.items() if k in KEEP_TAGS or k.startswith("communication:")}


def is_border(t):
    return t.get("boundary") == "administrative" and t.get("admin_level") == "2"


class RelationPass(osmium.SimpleHandler):
    """Pass 1: find multipolygon relations we need and national border relations."""

    def __init__(self):
        super().__init__()
        self.rels = {}           # id -> {"tags":..., "members":[(way_id, role)]}
        self.member_ways = set()
        self.border_ways = set()

    def relation(self, r):
        t = tagdict(r)
        ways = [(m.ref, m.role) for m in r.members if m.type == "w"]
        if is_border(t):
            self.border_ways.update(w for w, _ in ways)
        if t.get("type") in ("multipolygon",) and want(t):
            self.rels[r.id] = {"tags": slim(t), "members": ways}
            self.member_ways.update(w for w, _ in ways)


class GeometryPass(osmium.SimpleHandler):
    """Pass 2 (with node locations): nodes, ways, member-way and border-way geometry."""

    def __init__(self, member_ways, border_ways):
        super().__init__()
        self.member_ways = member_ways
        self.border_ways = border_ways
        self.elements = []
        self.member_geom = {}
        self.borders = []

    def node(self, n):
        if not n.tags or not n.location.valid():
            return
        t = tagdict(n)
        if want(t):
            self.elements.append({"t": "n", "id": n.id, "c": [round(n.location.lat, 6), round(n.location.lon, 6)], "tags": slim(t)})

    def way(self, w):
        t = tagdict(w)
        w_want = want(t)
        in_rel = w.id in self.member_ways
        in_border = w.id in self.border_ways
        if not (w_want or in_rel or in_border):
            return
        g = []
        for nr in w.nodes:
            loc = nr.location
            if loc.valid():
                g.append([round(loc.lat, 6), round(loc.lon, 6)])
        if len(g) < 1:
            return
        if w_want:
            self.elements.append({"t": "w", "id": w.id, "tags": slim(t), "g": g})
        if in_rel:
            self.member_geom[w.id] = g
        if in_border and t.get("maritime") != "yes" and t.get("natural") != "coastline" and len(g) >= 2:
            self.borders.append([w.id, g])


def coords_of(el):
    if el["t"] == "n":
        return [el["c"]]
    if el["t"] == "w":
        return el["g"]
    out = []
    for _, g in el["m"]:
        out.extend(g)
    return out


def tile_keys(coords, size):
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    i0, i1 = math.floor((min(lats) - EPS) / size), math.floor((max(lats) + EPS) / size)
    j0, j1 = math.floor((min(lons) - EPS) / size), math.floor((max(lons) + EPS) / size)
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            yield (i, j)


def tkey(i, j, size):
    if size == TILE:
        return f"{i * TILE:.3f}_{j * TILE:.3f}"
    return f"{i * size}_{j * size}"


def load_poly(path):
    """Geofabrik .poly -> list of outer rings [(lon, lat), ...] (holes are ignored)."""
    rings, cur, hole = [], None, False
    with open(path, encoding="utf-8") as f:
        lines = [l.strip() for l in f]
    for line in lines[1:]:
        if not line:
            continue
        if cur is None:
            if line == "END":
                break
            cur, hole = [], line.startswith("!")
            continue
        if line == "END":
            if not hole and len(cur) >= 3:
                rings.append(cur)
            cur = None
            continue
        a, b = line.split()[:2]
        cur.append((float(a), float(b)))
    return rings


def in_ring(lon, lat, ring):
    c = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


def coverage(rings, size):
    """Tiles that intersect the extract polygon (sampled on a grid plus polygon vertices)."""
    cov = set()
    allpts = [p for r in rings for p in r]
    lon0, lon1 = min(p[0] for p in allpts), max(p[0] for p in allpts)
    lat0, lat1 = min(p[1] for p in allpts), max(p[1] for p in allpts)
    for i in range(math.floor(lat0 / size), math.floor(lat1 / size) + 1):
        for j in range(math.floor(lon0 / size), math.floor(lon1 / size) + 1):
            s, w = i * size, j * size
            hit = False
            n = 8
            for a in range(n + 1):
                for b in range(n + 1):
                    lat, lon = s + size * a / n, w + size * b / n
                    if any(in_ring(lon, lat, r) for r in rings):
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                hit = any(s <= p[1] <= s + size and w <= p[0] <= w + size for p in allpts)
            if hit:
                cov.add(tkey(i, j, size))
    return cov


def osm_timestamp(pbf):
    try:
        rd = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING)
        ts = rd.header().get("osmosis_replication_timestamp")
        rd.close()
        return ts or None
    except Exception:
        return None


def main():
    pbf, poly, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(os.path.join(out, "tiles"), exist_ok=True)
    os.makedirs(os.path.join(out, "borders"), exist_ok=True)

    p1 = RelationPass()
    p1.apply_file(pbf)
    print(f"relations: {len(p1.rels)}, member ways: {len(p1.member_ways)}, border ways: {len(p1.border_ways)}", flush=True)

    p2 = GeometryPass(p1.member_ways, p1.border_ways)
    p2.apply_file(pbf, locations=True, idx="flex_mem")
    elements = p2.elements
    for rid, r in p1.rels.items():
        members = [[role or "outer", p2.member_geom[w]] for w, role in r["members"] if w in p2.member_geom]
        if members:
            elements.append({"t": "r", "id": rid, "tags": r["tags"], "m": members})
    print(f"elements: {len(elements)}, border ways kept: {len(p2.borders)}", flush=True)

    tiles = {}
    for el in elements:
        for ij in tile_keys(coords_of(el), TILE):
            tiles.setdefault(ij, []).append(el)
    for (i, j), els in tiles.items():
        with open(os.path.join(out, "tiles", tkey(i, j, TILE) + ".json"), "w", encoding="utf-8") as f:
            json.dump({"elements": els}, f, ensure_ascii=False, separators=(",", ":"))

    btiles = {}
    for wid, g in p2.borders:
        for ij in tile_keys(g, BTILE):
            btiles.setdefault(ij, []).append([wid, g])
    for (i, j), ws in btiles.items():
        with open(os.path.join(out, "borders", tkey(i, j, BTILE) + ".json"), "w", encoding="utf-8") as f:
            json.dump({"ways": ws}, f, separators=(",", ":"))

    rings = load_poly(poly)
    index = {
        "format": "hfnoise-tiles",
        "version": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "osm_timestamp": osm_timestamp(pbf),
        "tile": TILE,
        "btile": BTILE,
        "cov": sorted(coverage(rings, TILE)),
        "data": sorted(tkey(i, j, TILE) for (i, j) in tiles),
        "bcov": sorted(coverage(rings, BTILE)),
        "bdata": sorted(tkey(i, j, BTILE) for (i, j) in btiles),
    }
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    size = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(out) for fn in fns)
    print(f"tiles: {len(tiles)} with data / {len(index['cov'])} covered; border tiles: {len(btiles)}; total {size / 1e6:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
