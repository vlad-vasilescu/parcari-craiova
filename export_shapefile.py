#!/usr/bin/env python3
"""Export the parking lots in blocuri/*.json as two zipped polygon shapefiles.

Both copy the Roermond examples in shapefiles/ (WGS84, EPSG:4326, same columns):

- craiova_<date>_4326.zip: one record per parking spot (inactive ones left out).
  Columns with no Craiova equivalent are left empty (text) or null (numbers).
- scanplanner_craiova_<date>.zip: one record per neighbourhood, with the total
  spot count of its lots.

Craiova's data has no neighbourhoods and OpenStreetMap only has a point for
each, so every lot goes to the nearest point, and a zone's polygon is the
convex hull of its lots cut back to that point's Voronoi cell (so zones never
overlap). The points are fetched from Overpass once and cached in
neighbourhoods.json; delete that file to refetch.

With --zones ZIP, the zones come from the polygon shapefile in ZIP instead
(e.g. a hand-drawn scanplanner file, its "name" column), and only the spots
file is written: each spot goes to the zone holding its centroid, or the
nearest one if it's outside all of them.

Usage: python3 export_shapefile.py [--zones ZIP] [out_dir]   (default shapefiles/)
"""
import glob
import io
import json
import math
import os
import struct
import sys
import time
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
IN_DIR = os.path.join(HERE, "blocuri")
NEIGHBOURHOODS = os.path.join(HERE, "neighbourhoods.json")
CITY = "Craiova"
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = """[out:json][timeout:90];
area["name"="Craiova"]["boundary"="administrative"]->.a;
nwr(area.a)["place"~"^(suburb|quarter|neighbourhood)$"]["name"];
out tags center;"""
# Scan planning parameters per zone, copied from the Roermond example (same for every zone there)
WTP_SHORT, WTP_LONG, VF_TYPE, VF_COUNT = 75, 97, "week", 1

# (name, type, length, decimals): the Roermond schemas, in order
SPOT_FIELDS = [("id", "N", 20, 0)] + [(n, "C", 254, 0) for n in (
    "street", "city", "district", "neighborh")] + [("spot_count", "N", 20, 0)] + [
    (n, "C", 254, 0) for n in ("spot_type", "city_limit", "road_type", "off_statem", "area_id",
                               "license_pl", "off_type")] + [
    ("overstay", "N", 20, 0), ("deviation", "N", 20, 0)] + [
    (n, "C", 254, 0) for n in ("park_code", "shape_id", "roof_id", "zone_id")] + [
    ("tarif", "N", 20, 0)] + [(d + "_hours", "C", 254, 0) for d in (
    "mon", "tue", "wed", "thu", "fri", "sat", "sun")] + [
    ("municipal", "C", 254, 0), ("off_code", "C", 254, 0)]
ZONE_FIELDS = [("id", "N", 24, 15), ("name", "C", 254, 0), ("spot_count", "N", 20, 0),
               ("wtp_short", "N", 24, 15), ("wtp_long", "N", 24, 15), ("vf_type", "C", 10, 0),
               ("vf_count", "N", 20, 0)]

PRJ = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
       'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')

QMD = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.38.1-Grenoble">
  <identifier></identifier>
  <parentidentifier></parentidentifier>
  <language></language>
  <type>dataset</type>
  <title></title>
  <abstract></abstract>
  <links/>
  <dates/>
  <fees></fees>
  <encoding></encoding>
  <crs>
    <spatialrefsys nativeFormat="Wkt">
      <wkt>GEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble",MEMBER["World Geodetic System 1984 (Transit)"],MEMBER["World Geodetic System 1984 (G730)"],MEMBER["World Geodetic System 1984 (G873)"],MEMBER["World Geodetic System 1984 (G1150)"],MEMBER["World Geodetic System 1984 (G1674)"],MEMBER["World Geodetic System 1984 (G1762)"],MEMBER["World Geodetic System 1984 (G2139)"],ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]],ENSEMBLEACCURACY[2.0]],PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],CS[ellipsoidal,2],AXIS["geodetic latitude (Lat)",north,ORDER[1],ANGLEUNIT["degree",0.0174532925199433]],AXIS["geodetic longitude (Lon)",east,ORDER[2],ANGLEUNIT["degree",0.0174532925199433]],USAGE[SCOPE["Horizontal component of 3D system."],AREA["World."],BBOX[-90,-180,90,180]],ID["EPSG",4326]]</wkt>
      <proj4>+proj=longlat +datum=WGS84 +no_defs</proj4>
      <srsid>3452</srsid>
      <srid>4326</srid>
      <authid>EPSG:4326</authid>
      <description>WGS 84</description>
      <projectionacronym>longlat</projectionacronym>
      <ellipsoidacronym>EPSG:7030</ellipsoidacronym>
      <geographicflag>true</geographicflag>
    </spatialrefsys>
  </crs>
  <extent/>
</qgis>
"""


def load_lots():
    """Return {lot id: (lot, street of the nearest building)}.

    Like the viewer, the copy from the newest file wins when neighbours' files
    disagree about a shared lot.
    """
    paths = sorted(glob.glob(os.path.join(IN_DIR, "*.json")), key=os.path.getmtime)
    lots, nearest = {}, {}
    for path in paths:
        with open(path) as f:
            bloc = json.load(f)
        for lot in bloc.get("parking_lots") or []:
            if not lot.get("geometry"):
                continue
            lots[lot["id"]] = lot
            d = lot.get("distance")
            d = float("inf") if d is None else d
            if lot["id"] not in nearest or d < nearest[lot["id"]][0]:
                nearest[lot["id"]] = (d, bloc.get("street") or "")
    return {i: (lot, nearest[i][1]) for i, lot in lots.items()}


def load_neighbourhoods():
    """Return [(name, lon, lat)] of Craiova's OpenStreetMap neighbourhoods, cached on disk."""
    if not os.path.exists(NEIGHBOURHOODS):
        req = urllib.request.Request(
            OVERPASS, urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode(),
            {"User-Agent": "parcari-test-export/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            elements = json.load(r)["elements"]
        points = {}
        for e in elements:
            c = e.get("center", e)
            points.setdefault(e["tags"]["name"], (c["lon"], c["lat"]))
        tmp = NEIGHBOURHOODS + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"source": "OpenStreetMap contributors, ODbL", "points": [
                {"name": n, "lon": x, "lat": y} for n, (x, y) in sorted(points.items())]},
                f, ensure_ascii=False, indent=1)
        os.replace(tmp, NEIGHBOURHOODS)
    with open(NEIGHBOURHOODS) as f:
        return [(p["name"], p["lon"], p["lat"]) for p in json.load(f)["points"]]


def outer_ring(geometry):
    return geometry["coordinates"][0] if geometry["type"] == "Polygon" else geometry["coordinates"][0][0]


def convex_hull(points):
    """Counter-clockwise hull (monotone chain), not closed."""
    points = sorted(set(points))
    if len(points) < 3:
        return points

    def half(pts):
        out = []
        for p in pts:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return half(points) + half(points[::-1])


def clip(poly, a, b):
    """Keep the part of convex poly closer to a than to b (Sutherland-Hodgman)."""
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    dx, dy = b[0] - a[0], b[1] - a[1]

    def side(p):
        return (p[0] - mx) * dx + (p[1] - my) * dy  # <= 0 on a's side

    out = []
    for p, q in zip(poly, poly[1:] + poly[:1]):
        sp, sq = side(p), side(q)
        if sp <= 0:
            out.append(p)
        if (sp < 0) != (sq < 0) and sp != sq:
            t = sp / (sp - sq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def zones(lots, neighbourhoods):
    """Return ({lot id: zone name}, [(name, spot count, geometry)]) for zones that have lots.

    Works on lon scaled by cos(latitude) so distances are isotropic; that's an
    affine map, so convex shapes stay convex when mapped back.
    """
    k = math.cos(math.radians(sum(lat for _, _, lat in neighbourhoods) / len(neighbourhoods)))
    sites = [(lon * k, lat) for _, lon, lat in neighbourhoods]
    of_lot, members = {}, {}
    for i, (lot, _) in lots.items():
        ring = outer_ring(lot["geometry"])
        if ring[0] == ring[-1]:
            ring = ring[:-1]
        cx = sum(p[0] for p in ring) / len(ring) * k
        cy = sum(p[1] for p in ring) / len(ring)
        z = min(range(len(sites)), key=lambda j: (sites[j][0] - cx) ** 2 + (sites[j][1] - cy) ** 2)
        of_lot[i] = neighbourhoods[z][0]
        members.setdefault(z, []).append(i)
    out = []
    for z, ids in sorted(members.items(), key=lambda m: neighbourhoods[m[0]][0]):
        poly = convex_hull([(x * k, y) for i in ids for x, y, *_ in outer_ring(lots[i][0]["geometry"])])
        for j, other in enumerate(sites):
            if j != z and len(poly) >= 3:
                poly = clip(poly, sites[z], other)
        if len(poly) < 3:
            continue
        ring = [[x / k, y] for x, y in poly]
        spots = sum(spot_count(lots[i][0]) for i in ids)
        out.append((neighbourhoods[z][0], spots, {"type": "Polygon", "coordinates": [ring]}))
    return of_lot, out


def read_zones(path):
    """Return [(name, rings)] from the polygon shapefile in zip `path` (name = its "name" column)."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        def member(ext):
            found = [n for n in names if n.lower().endswith(ext) and "__macosx" not in n.lower()]
            if len(found) != 1:
                sys.exit("%s: expected one %s file, found %d" % (path, ext, len(found)))
            return z.read(found[0])

        shp, dbf = member(".shp"), member(".dbf")
    count, header_len, record_len = struct.unpack("<IHH", dbf[4:12])
    fields = [(dbf[i:i + 11].split(b"\0")[0].decode(), dbf[i + 16])
              for i in range(32, header_len - 1, 32)]
    zone_names = []
    for r in range(count):
        off, rec = header_len + r * record_len + 1, {}
        for name, length in fields:
            rec[name] = dbf[off:off + length].decode("utf-8", "replace").strip()
            off += length
        zone_names.append(rec.get("name", ""))
    out, pos = [], 100
    for name in zone_names:
        _, length = struct.unpack(">2i", shp[pos:pos + 8])
        body = shp[pos + 8:pos + 8 + 2 * length]
        pos += 8 + 2 * length
        if struct.unpack("<i", body[:4])[0] != 5:
            continue  # null or non-polygon shape
        n_parts, n_points = struct.unpack("<2i", body[36:44])
        parts = list(struct.unpack("<%di" % n_parts, body[44:44 + 4 * n_parts])) + [n_points]
        coords = struct.unpack("<%dd" % (2 * n_points), body[44 + 4 * n_parts:44 + 4 * n_parts + 16 * n_points])
        points = list(zip(coords[::2], coords[1::2]))
        out.append((name, [points[a:b] for a, b in zip(parts, parts[1:])]))
    return out


def inside(rings, x, y):
    """Even-odd point in polygon over all rings (so holes and multiple parts both work)."""
    hit = False
    for ring in rings:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
            if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                hit = not hit
    return hit


def boundary_distance(rings, x, y, k):
    """Distance in degrees of latitude from (x, y) to the rings' edges, lon scaled by k."""
    best = float("inf")
    for ring in rings:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
            ax, ay, bx, by, px = x0 * k, y0, x1 * k, y1, x * k
            dx, dy = bx - ax, by - ay
            t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(ax + t * dx - px, ay + t * dy - y))
    return best


def spot_zones(spots, zone_list):
    """Return [zone name] for each spot in `spots`: the zone holding its centroid, else the nearest."""
    k = math.cos(math.radians(44.32))
    boxes = [bbox([p for r in rings for p in r]) for _, rings in zone_list]
    out, outside, overlap = [], [], 0
    for spot in spots:
        ring = outer_ring(spot["geometry"])
        if ring[0] == ring[-1]:
            ring = ring[:-1]
        x = sum(p[0] for p in ring) / len(ring)
        y = sum(p[1] for p in ring) / len(ring)
        hits = [j for j, (_, rings) in enumerate(zone_list)
                if boxes[j][0] <= x <= boxes[j][2] and boxes[j][1] <= y <= boxes[j][3] and inside(rings, x, y)]
        if len(hits) > 1:
            overlap += 1
        if not hits:
            hits = [min(range(len(zone_list)), key=lambda j: boundary_distance(zone_list[j][1], x, y, k))]
            outside.append(boundary_distance(zone_list[hits[0]][1], x, y, k) * 111320)
        out.append(zone_list[hits[0]][0])
    if outside:
        print("%d spots outside every zone, given to the nearest (up to %.0f m away)" % (len(outside), max(outside)))
    if overlap:
        print("%d spots in more than one zone, given to the first" % overlap)
    return out


def rings_of(geometry):
    """Shapefile rings: closed, outer rings clockwise, holes counter-clockwise."""
    polys = geometry["coordinates"]
    if geometry["type"] == "Polygon":
        polys = [polys]
    rings = []
    for poly in polys:
        for k, ring in enumerate(poly):
            ring = [(float(x), float(y)) for x, y, *_ in ring]
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:]))
            if (area > 0) == (k == 0):  # counter-clockwise outer, or clockwise hole
                ring.reverse()
            rings.append(ring)
    return rings


def active_spots(lot):
    return sorted((s for s in lot.get("residence_parking_spots") or []
                   if s.get("status") != "inactive" and s.get("geometry")), key=lambda s: s["id"])


def spot_count(lot):
    return len(active_spots(lot))


def spot_attributes(lot, spot, street, zone):
    return {
        "id": spot["id"],
        "street": street,
        "city": CITY,
        "neighborh": zone,
        "spot_count": 1,
        "spot_type": spot.get("spot_type") or "",
        "off_type": "residential",
        "area_id": zone,
        "park_code": lot.get("name") or "",
        "shape_id": spot.get("number") or "",
        "roof_id": zone,
        "municipal": CITY,
    }


def bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def write_shp(shapes):
    """Return (.shp bytes, .shx bytes) for a list of ring lists."""
    shp, shx = io.BytesIO(), io.BytesIO()
    offset = 100
    for n, rings in enumerate(shapes, 1):
        points = [p for r in rings for p in r]
        parts, start = [], 0
        for r in rings:
            parts.append(start)
            start += len(r)
        body = struct.pack("<i4d2i", 5, *bbox(points), len(rings), len(points))
        body += struct.pack("<%di" % len(parts), *parts)
        body += struct.pack("<%dd" % (2 * len(points)), *(c for p in points for c in p))
        shp.write(struct.pack(">2i", n, len(body) // 2) + body)
        shx.write(struct.pack(">2i", offset // 2, len(body) // 2))
        offset += 8 + len(body)
    box = bbox([p for rings in shapes for r in rings for p in r])

    def header(length):
        return (struct.pack(">7i", 9994, 0, 0, 0, 0, 0, length // 2)
                + struct.pack("<2i8d", 1000, 5, *box, 0, 0, 0, 0))

    shp, shx = shp.getvalue(), shx.getvalue()
    return header(100 + len(shp)) + shp, header(100 + len(shx)) + shx


def write_dbf(fields, records):
    today = time.localtime()
    record_len = 1 + sum(length for _, _, length, _ in fields)
    header_len = 32 + 32 * len(fields) + 1
    out = io.BytesIO()
    out.write(struct.pack("<4BIHH20x", 3, today.tm_year - 1900, today.tm_mon, today.tm_mday,
                          len(records), header_len, record_len))
    for name, kind, length, decimals in fields:
        out.write(struct.pack("<11sc4xBB14x", name.encode(), kind.encode(), length, decimals))
    out.write(b"\r")
    for rec in records:
        out.write(b" ")
        for name, kind, length, decimals in fields:
            value = rec.get(name)
            if kind == "N":
                text = "%.*f" % (decimals, value) if value is not None else None
                out.write(b"*" * length if text is None else text.rjust(length).encode())
            else:
                raw = (value or "").encode("utf-8")[:length]
                raw = raw.decode("utf-8", "ignore").encode("utf-8")  # don't split a character
                out.write(raw.ljust(length))
    out.write(b"\x1a")
    return out.getvalue()


def write_zip(path, base, fields, geometries, records):
    shp, shx = write_shp([rings_of(g) for g in geometries])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(base + ".cpg", "UTF-8")
        z.writestr(base + ".dbf", write_dbf(fields, records))
        z.writestr(base + ".prj", PRJ)
        z.writestr(base + ".qmd", QMD)
        z.writestr(base + ".shp", shp)
        z.writestr(base + ".shx", shx)
    print("wrote %d records to %s" % (len(records), path))


def main():
    lots = load_lots()
    if not lots:
        sys.exit("no parking lots found in " + IN_DIR)
    args = sys.argv[1:]
    zones_file = None
    if args[:1] == ["--zones"] and len(args) >= 2:
        zones_file, args = args[1], args[2:]
    out_dir = args[0] if args else os.path.join(HERE, "shapefiles")
    date = time.strftime("%Y%m%d")
    if zones_file:
        spots = [(lot, spot, street) for _, (lot, street) in sorted(lots.items())
                 for spot in active_spots(lot)]
        given = read_zones(zones_file)
        spots = [s + (z,) for s, z in zip(spots, spot_zones([s[1] for s in spots], given))]
        counts = {}
        for *_, z in spots:
            counts[z] = counts.get(z, 0) + 1
        for name, _ in given:
            print("%6d  %s" % (counts.get(name, 0), name))
        base = "craiova_%s_4326" % date
        write_zip(os.path.join(out_dir, base + ".zip"), base, SPOT_FIELDS,
                  [spot["geometry"] for _, spot, _, _ in spots],
                  [spot_attributes(*s) for s in spots])
        return
    zone_of, zone_list = zones(lots, load_neighbourhoods())
    spots = [(lot, spot, street, zone_of[i]) for i, (lot, street) in sorted(lots.items())
             for spot in active_spots(lot)]

    base = "craiova_%s_4326" % date
    write_zip(os.path.join(out_dir, base + ".zip"), base, SPOT_FIELDS,
              [spot["geometry"] for _, spot, _, _ in spots],
              [spot_attributes(*s) for s in spots])

    base = "scanplanner_craiova_%s" % date
    write_zip(os.path.join(out_dir, base + ".zip"), base, ZONE_FIELDS,
              [g for _, _, g in zone_list],
              [{"id": n, "name": name, "spot_count": spots, "wtp_short": WTP_SHORT,
                "wtp_long": WTP_LONG, "vf_type": VF_TYPE, "vf_count": VF_COUNT}
               for n, (name, spots, _) in enumerate(zone_list, 1)])


if __name__ == "__main__":
    main()
