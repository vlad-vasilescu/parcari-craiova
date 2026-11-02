#!/usr/bin/env python3
"""Build a static copy of the viewer for hosting without serve.py (deploy_site.py puts it on Cloudflare).

Writes index.html, data.json and data/<n>.json into out_dir. Together the
data/ chunks hold every bloc in blocuri/*.json, with each parking lot stored
once instead of once per nearby bloc (about 20x smaller). The viewer loads
data.json when it exists, then every chunk it lists, and hides the "Refetch
this bloc" button, since a static host can't call the city's API.

data.json is {"chunks": [[path, size in bytes], ...]}. Each chunk is
{"blocs": [bloc, ...], "lots": {id: lot}}, about CHUNK_SIZE bytes; a bloc's
lots can be in any chunk. Each bloc gets "fetched" (its file's mtime in ms)
and its "parking_lots" become [[lot id, distance], ...]; lots keep everything
except "distance". When copies of a lot differ, the one from the newest file
wins, as in the viewer.

Usage: python3 build_site.py [out_dir]   (default site/)
"""
import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IN_DIR = os.path.join(HERE, "blocuri")
CHUNK_DIR = "data"
CHUNK_SIZE = 2_000_000  # bytes of JSON per chunk, roughly


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write(path, text):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def chunks(blocs, lots):
    """Pack blocs, then lots, in id order into {"blocs", "lots"} dicts of about CHUNK_SIZE bytes each."""
    items = [("blocs", b["id"], b) for b in blocs.values()] + [("lots", i, lot) for i, lot in lots.items()]
    items.sort(key=lambda t: (t[0], t[1]))
    chunk, size = {"blocs": [], "lots": {}}, 0
    for kind, i, obj in items:
        if kind == "blocs":
            chunk["blocs"].append(obj)
        else:
            chunk["lots"][i] = obj
        size += len(dumps(obj).encode())
        if size >= CHUNK_SIZE:
            yield chunk
            chunk, size = {"blocs": [], "lots": {}}, 0
    if size:
        yield chunk


def main(out_dir=None):
    out_dir = out_dir or (sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "site"))
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

    # Rewrite the chunk directory from scratch, so deploy_site.py doesn't publish leftover chunks.
    chunk_dir = os.path.join(out_dir, CHUNK_DIR)
    shutil.rmtree(chunk_dir, ignore_errors=True)
    os.makedirs(chunk_dir)
    listed = []
    for n, chunk in enumerate(chunks(blocs, lots)):
        text = dumps(chunk)
        name = f"{CHUNK_DIR}/{n}.json"
        write(os.path.join(out_dir, name), text)
        listed.append([name, len(text.encode())])
    data_path = os.path.join(out_dir, "data.json")
    write(data_path, dumps({"chunks": listed}))
    shutil.copy(os.path.join(HERE, "index.html"), out_dir)

    total = sum(size for _, size in listed)
    print(f"{len(blocs)} blocs, {len(lots)} lots -> {data_path} + {len(listed)} chunks in {chunk_dir} "
          f"({total / 1e6:.1f} MB, largest {max(size for _, size in listed) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
