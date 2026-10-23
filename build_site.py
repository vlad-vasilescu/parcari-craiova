#!/usr/bin/env python3
"""Build a static copy of the viewer for hosting without serve.py (e.g. Cloudflare Pages).

Writes index.html and data.json into out_dir. data.json holds every bloc in
blocuri/*.json, with each parking lot stored once instead of once per nearby
bloc (about 20x smaller). The viewer loads it when it exists and hides the
"Refetch this bloc" button, since a static host can't call the city's API.

Format: {"blocs": [bloc, ...], "lots": {id: lot}}. Each bloc gets "fetched"
(its file's mtime in ms) and its "parking_lots" become [[lot id, distance], ...];
lots keep everything except "distance". When copies of a lot differ, the one
from the newest file wins, as in the viewer.

Usage: python3 build_site.py [out_dir]   (default site/)
"""
import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IN_DIR = os.path.join(HERE, "blocuri")
MAX_FILE = 25 * 1024 * 1024  # Cloudflare Pages' per-file limit


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "site")
    files = sorted(glob.glob(os.path.join(IN_DIR, "*.json")), key=os.path.getmtime)
    if not files:
        sys.exit(f"no .json files in {IN_DIR}")

    # Oldest file first, so newer copies of a bloc or lot overwrite older ones.
    blocs, lots = {}, {}
    for path in files:
        with open(path, encoding="utf-8") as f:
            b = json.load(f)
        b["fetched"] = int(os.path.getmtime(path) * 1000)
        near = []
        for lot in b["parking_lots"]:
            near.append([lot["id"], lot.pop("distance")])
            lots[lot["id"]] = lot
        b["parking_lots"] = near
        blocs[b["id"]] = b

    os.makedirs(out_dir, exist_ok=True)
    data_path = os.path.join(out_dir, "data.json")
    with open(data_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"blocs": list(blocs.values()), "lots": lots}, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(data_path + ".tmp", data_path)
    shutil.copy(os.path.join(HERE, "index.html"), out_dir)

    size = os.path.getsize(data_path)
    print(f"{len(blocs)} blocs, {len(lots)} lots -> {data_path} ({size / 1e6:.1f} MB)")
    if size > MAX_FILE:
        print("warning: over Cloudflare Pages' 25 MiB per-file limit", file=sys.stderr)


if __name__ == "__main__":
    main()
