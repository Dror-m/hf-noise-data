#!/usr/bin/env python3
"""Build pre-tiled HF man-made noise source data from an OpenStreetMap extract.

Usage: build_tiles.py REGION.osm.pbf REGION.poly OUT_DIR

Output (served by GitHub Pages):
  OUT_DIR/index.json             metadata, tile coverage and the list of non-empty tiles
  OUT_DIR/tiles/<lat>_<lon>.json noise-source elements in a 0.2 deg tile (compact format)
  OUT_DIR/borders/<lat>_<lon>.json  national border lines in a 1 deg tile
  OUT_DIR/islands/<lat>_<lon>.json  island outlines (place=island/islet) in a 1 deg tile
  OUT_DIR/ground/<lat>_<lon>.json   ground cover for propagation in a 0.2 deg tile:
      {"coast":[[[lat,lon],...],...],      coastline ways, land on the left / sea on the right
       "sea":[[ring,...],...],            salt pans etc. treated as sea water
       "water":[[ring,...],...],          lakes and reservoirs
       "wet":[[ring,...],...]}            irrigated / wet land (farmland, orchards, wetlands, ...)

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

LANDUSE = {"residential", "industrial", "commercial", "retail", "quarry", "greenhouse_horticulture", "farmyard", "military"}
MAN_MADE = {"mast", "tower", "communications_tower", "water_works", "pumping_station", "desalination_plant", "wastewater_plant"}
BUILDINGS = {"data_center", "greenhouse", "cowshed", "farm_auxiliary", "sty", "stable", "chicken_coop"}
KEEP_TAGS = {
    "power", "voltage", "generator:source", "plant:source", "generator:method", "generator:type",
    "location", "generator:place", "amenity", "railway", "electrified", "tunnel", "highway",
    "man_made", "tower:type", "communication:mobile_phone", "communication:microwave",
    "communication:radio", "communication:television", "communication:am_radio", "telecom",
    "building", "landuse", "barrier", "fence_type", "name", "name:he", "type", "aeroway",
}


def want(t):
    p = t.get("power")
    if p in ("line", "minor_line", "substation", "transformer", "plant", "generator"):
        return True
    if t.get("landuse") in LANDUSE:
        return True
    if t.get("railway") in ("rail", "light_rail", "tram"):
        return True
    # secondary roads are noise sources in the app; tertiary roads only guide the estimated distribution lines
    if t.get("highway") in ("motorway", "trunk", "primary", "secondary", "tertiary"):
        return True
    if t.get("barrier") == "fence" and t.get("fence_type") == "electric":
        return True
    if t.get("aeroway") == "aerodrome":
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


WET_LANDUSE = {"farmland", "orchard", "vineyard", "meadow", "greenhouse_horticulture", "plant_nursery", "allotments"}


def ground_class(t):
    """Ground class of an area for ground-wave attenuation, or None."""
    if t.get("landuse") == "salt_pond":
        return "sea"
    if t.get("natural") == "water" or t.get("landuse") in ("reservoir", "basin") or t.get("waterway") == "riverbank":
        return "water"
    if t.get("landuse") in WET_LANDUSE or t.get("natural") == "wetland" or t.get("leisure") == "golf_course":
        return "wet"
    return None


def is_island(t):
    return t.get("place") in ("island", "islet")


def is_border(t):
    return t.get("boundary") == "administrative" and t.get("admin_level") == "2"


class RelationPass(osmium.SimpleHandler):
    """Pass 1: find multipolygon relations we need and national border relations."""

    def __init__(self):
        super().__init__()
        self.rels = {}           # id -> {"tags":..., "members":[(way_id, role)]}
        self.member_ways = set()
        self.border_ways = set()
        self.island_rels = {}    # id -> {"name":..., "ways":[outer way ids]}
        self.island_ways = set()
        self.ground_rels = {}    # id -> {"cls":..., "ways":[way ids]}
        self.ground_ways = set()

    def relation(self, r):
        t = tagdict(r)
        ways = [(m.ref, m.role) for m in r.members if m.type == "w"]
        if is_border(t):
            self.border_ways.update(w for w, _ in ways)
        gc = ground_class(t)
        if gc and t.get("type") == "multipolygon":
            self.ground_rels[r.id] = {"cls": gc, "ways": [w for w, _ in ways]}
            self.ground_ways.update(w for w, _ in ways)
        if is_island(t):
            outer = [w for w, role in ways if role != "inner"]
            self.island_rels[r.id] = {"name": t.get("name:he") or t.get("name:en") or t.get("name") or "", "ways": outer}
            self.island_ways.update(outer)
        if t.get("type") in ("multipolygon",) and want(t):
            self.rels[r.id] = {"tags": slim(t), "members": ways}
            self.member_ways.update(w for w, _ in ways)


class GeometryPass(osmium.SimpleHandler):
    """Pass 2 (with node locations): nodes, ways, member-way and border-way geometry."""

    def __init__(self, member_ways, border_ways, island_ways, ground_ways=frozenset()):
        super().__init__()
        self.ground_ways = ground_ways
        self.ground_geom = {}
        self.coast = []           # [ [ [lat,lon],... ] ]
        self.ground_polys = []    # [cls, [ring]]
        self.member_ways = member_ways
        self.border_ways = border_ways
        self.island_ways = island_ways
        self.elements = []
        self.member_geom = {}
        self.borders = []
        self.island_geom = {}
        self.island_closed = []   # [id, name, ring] from closed ways tagged place=island/islet

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
        in_isl = w.id in self.island_ways
        isl_way = is_island(t)
        in_ground = w.id in self.ground_ways
        gc = ground_class(t)
        coast = t.get("natural") == "coastline"
        if not (w_want or in_rel or in_border or in_isl or isl_way or in_ground or gc or coast):
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
        if in_isl:
            self.island_geom[w.id] = g
        if in_ground:
            self.ground_geom[w.id] = g
        if coast and len(g) >= 2:
            self.coast.append(g)
        if gc and len(g) >= 4 and g[0] == g[-1]:
            self.ground_polys.append([gc, [g]])
        if isl_way and len(g) >= 4 and g[0] == g[-1]:
            self.island_closed.append(["w" + str(w.id), t.get("name:he") or t.get("name:en") or t.get("name") or "", g])


def join_ways(lines):
    """Merge polylines that share end points into chains (closed chains are rings)."""
    lines = [l for l in lines if len(l) >= 2]
    key = lambda p: (p[0], p[1])
    ends = {}
    for i, l in enumerate(lines):
        ends.setdefault(key(l[0]), []).append(i)
        ends.setdefault(key(l[-1]), []).append(i)
    used = [False] * len(lines)
    out = []
    for i in range(len(lines)):
        if used[i]:
            continue
        used[i] = True
        ch = list(lines[i])
        grown = True
        while grown and key(ch[0]) != key(ch[-1]):
            grown = False
            for tail in (True, False):
                p = key(ch[-1] if tail else ch[0])
                for j in ends.get(p, []):
                    if used[j]:
                        continue
                    l = lines[j]
                    if tail:
                        ch += (l[1:] if key(l[0]) == p else l[::-1][1:])
                    else:
                        ch = (l[:-1] if key(l[-1]) == p else l[::-1][:-1]) + ch
                    used[j] = True
                    grown = True
                    break
                if grown:
                    break
        out.append(ch)
    return out


def simplify(ring, tol_m):
    """Douglas-Peucker in metres (local projection)."""
    if len(ring) <= 4:
        return ring
    lat0 = ring[0][0]
    mx, my = 111320 * math.cos(math.radians(lat0)), 110574
    q = [((p[1]) * mx, (p[0]) * my) for p in ring]
    keep = [False] * len(q)
    keep[0] = keep[-1] = True
    stack = [(0, len(q) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = q[a]
        bx, by = q[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy or 1e-9
        md, mi = -1, -1
        for i in range(a + 1, b):
            t = max(0, min(1, ((q[i][0] - ax) * dx + (q[i][1] - ay) * dy) / L2))
            d = math.hypot(q[i][0] - ax - t * dx, q[i][1] - ay - t * dy)
            if d > md:
                md, mi = d, i
        if md > tol_m:
            keep[mi] = True
            stack += [(a, mi), (mi, b)]
    return [p for p, k in zip(ring, keep) if k]


def ring_area_km2(r):
    lat0 = sum(p[0] for p in r) / len(r)
    mx, my = 111320 * math.cos(math.radians(lat0)), 110574
    a = 0
    for i in range(len(r)):
        j = (i + 1) % len(r)
        a += r[i][1] * mx * r[j][0] * my - r[j][1] * mx * r[i][0] * my
    return abs(a) / 2 / 1e6


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

    p2 = GeometryPass(p1.member_ways, p1.border_ways, p1.island_ways, p1.ground_ways)
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

    islands = list(p2.island_closed)
    for rid, r in p1.island_rels.items():
        for k, ring in enumerate(join_ways([p2.island_geom[w] for w in r["ways"] if w in p2.island_geom])):
            if len(ring) >= 4 and ring[0] == ring[-1]:
                islands.append(["r%d_%d" % (rid, k), r["name"], ring])
    itiles = {}
    for iid, name, ring in islands:
        area = ring_area_km2(ring)
        r2, tol = ring, 5
        while len(r2) > 3000:
            r2 = simplify(ring, tol)
            tol *= 2
        rec = {"id": iid, "name": name, "a": round(area, 3), "ring": r2}
        for ij in tile_keys(ring, BTILE):
            itiles.setdefault(ij, []).append(rec)
    os.makedirs(os.path.join(out, "islands"), exist_ok=True)
    for (i, j), recs in itiles.items():
        with open(os.path.join(out, "islands", tkey(i, j, BTILE) + ".json"), "w", encoding="utf-8") as f:
            json.dump({"islands": recs}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"islands: {len(islands)} in {len(itiles)} tiles", flush=True)

    # ground cover tiles (simplified to ~10 m; sea is derived in the app from coastline direction)
    gpolys = list(p2.ground_polys)
    for rid, r in p1.ground_rels.items():
        rr = [ring for ring in join_ways([p2.ground_geom[w] for w in r["ways"] if w in p2.ground_geom]) if len(ring) >= 4 and ring[0] == ring[-1]]
        if rr:
            gpolys.append([r["cls"], rr])
    gtiles = {}
    for cls, rr in gpolys:
        rr2 = [simplify(ring, 10) for ring in rr]
        rr2 = [ring for ring in rr2 if len(ring) >= 4]
        if not rr2:
            continue
        for ij in tile_keys([p for ring in rr for p in ring], TILE):
            gtiles.setdefault(ij, {}).setdefault(cls, []).append(rr2)
    for line in p2.coast:
        l2 = simplify(line, 10)
        for ij in tile_keys(line, TILE):
            gtiles.setdefault(ij, {}).setdefault("coast", []).append(l2)
    os.makedirs(os.path.join(out, "ground"), exist_ok=True)
    for (i, j), d in gtiles.items():
        with open(os.path.join(out, "ground", tkey(i, j, TILE) + ".json"), "w", encoding="utf-8") as f:
            json.dump(d, f, separators=(",", ":"))
    print(f"ground: {len(gpolys)} areas, {len(p2.coast)} coastline ways in {len(gtiles)} tiles", flush=True)

    rings = load_poly(poly)
    allpts = [p for r in rings for p in r]
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
        "idata": sorted(tkey(i, j, BTILE) for (i, j) in itiles),
        "gdata": sorted(tkey(i, j, TILE) for (i, j) in gtiles),
        "bbox": [min(p[1] for p in allpts), min(p[0] for p in allpts), max(p[1] for p in allpts), max(p[0] for p in allpts)],
    }
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    size = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(out) for fn in fns)
    print(f"tiles: {len(tiles)} with data / {len(index['cov'])} covered; border tiles: {len(btiles)}; total {size / 1e6:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
