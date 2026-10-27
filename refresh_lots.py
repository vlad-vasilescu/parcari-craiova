#!/usr/bin/env python3
"""Refetch every known parking lot with as few requests as possible, then rebuild and deploy site/.

A bloc's response contains every lot within 30 m of it, so refetching a bloc
refreshes all of its lots. This picks the smallest set of blocs whose files
together list every lot in blocuri/ (a minimum set cover, solved exactly; about
420 of the ~3.5k blocs), refetches those like fetch_blocuri.py does (same auth,
delay and backoff), then runs build_site.py and deploy_site.py. build_site.py lets
the newest file's copy of a lot win, so the fresh copies replace the stale ones in
other blocs' files.

Usage: python3 refresh_lots.py [--dry-run] [--no-deploy] [out_dir]   (default site/)
       --dry-run only prints the bloc ids that would be refetched.
       --no-deploy rebuilds out_dir without deploying it.
"""
import glob
import json
import os
import sys
import time

import build_site
import deploy_site
import fetch_blocuri


def load_lots():
    """Return {bloc id: frozenset of lot ids} for every bloc file that lists lots."""
    sets = {}
    for path in glob.glob(os.path.join(fetch_blocuri.OUT_DIR, "*.json")):
        with open(path, encoding="utf-8") as f:
            b = json.load(f)
        if b["parking_lots"]:
            sets[b["id"]] = frozenset(lot["id"] for lot in b["parking_lots"])
    return sets


def index(sets):
    by = {}
    for s, v in sets.items():
        for e in v:
            by.setdefault(e, set()).add(s)
    return by


def reduce(sets, elems):
    """Apply the standard set-cover reductions until none applies.

    Returns (forced, sets, elems): sets that every optimal cover can include,
    and the smaller instance left to solve.
    """
    forced = []
    sets = {s: v & elems for s, v in sets.items() if v & elems}
    changed = True
    while changed:
        changed = False
        by = index(sets)
        # An element only one set covers forces that set.
        for e in list(elems):
            if e in elems and len(by[e]) == 1:
                s = next(iter(by[e]))
                forced.append(s)
                elems -= sets.pop(s)
                changed = True
        if changed:
            sets = {s: v & elems for s, v in sets.items() if v & elems}
            continue
        # A set inside another set is never needed (of equal sets, keep the first).
        keep = {}
        for s in sorted(sets, key=lambda s: (-len(sets[s]), s)):
            supersets = set.intersection(*(by[e] for e in sets[s]))
            if any(t in keep for t in supersets):
                changed = True
            else:
                keep[s] = sets[s]
        sets = keep
        if changed:
            continue
        # If every set covering e2 also covers e1, covering e2 covers e1 too: drop e1.
        by = index(sets)
        drop = set()
        for e1 in sorted(elems, key=lambda e: -len(by[e])):
            for e2 in set().union(*(sets[s] for s in by[e1])):
                if e2 != e1 and e2 not in drop and by[e2] <= by[e1] and (by[e2] != by[e1] or e2 < e1):
                    drop.add(e1)
                    break
        if drop:
            elems -= drop
            sets = {s: v - drop for s, v in sets.items() if v - drop}
            changed = True
    return forced, sets, elems


def components(sets, elems):
    """Split into independent sub-instances: [(elems, sets), ...]."""
    parent = {e: e for e in elems}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for v in sets.values():
        first, *rest = v
        for e in rest:
            parent[find(e)] = find(first)
    comps = {}
    for e in elems:
        comps.setdefault(find(e), set()).add(e)
    return [(c, {s: v for s, v in sets.items() if v & c}) for c in comps.values()]


def greedy(sets, elems):
    elems, cover = set(elems), []
    while elems:
        s = max(sets, key=lambda s: (len(sets[s] & elems), -s))
        cover.append(s)
        elems -= sets[s]
    return cover


def solve(sets, elems, limit):
    """Return a smallest cover if it has fewer than limit sets, else None (branch and bound)."""
    forced, sets, elems = reduce(sets, set(elems))
    if not elems:
        return forced if len(forced) < limit else None
    budget = limit - len(forced)
    comps = components(sets, elems)
    if len(comps) > 1:
        cover = []
        for c, cs in comps:
            g = greedy(cs, c)
            cover += solve(cs, c, len(g)) or g
        return forced + cover if len(cover) < budget else None
    by = index(sets)
    # Lower bound: elements with pairwise disjoint sets of covering sets each need their own set.
    used, bound = set(), 0
    for e in sorted(elems, key=lambda e: len(by[e])):
        if not by[e] & used:
            used |= by[e]
            bound += 1
    if bound >= budget:
        return None
    # Branch on the element with the fewest covering sets: one of them must be taken.
    e = min(elems, key=lambda e: (len(by[e]), e))
    best = None
    for s in sorted(by[e], key=lambda s: -len(sets[s])):
        rest = {t: v - sets[s] for t, v in sets.items() if t != s}
        r = solve(rest, elems - sets[s], budget - 1)
        if r is not None:
            best = [s] + r
            budget = len(best)
    return forced + best if best is not None else None


def min_cover(sets):
    elems = frozenset().union(*sets.values())
    g = greedy(sets, elems)
    return sorted(solve(sets, elems, len(g)) or g)


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    deploy = "--no-deploy" not in args
    args = [a for a in args if a not in ("--dry-run", "--no-deploy")]
    out_dir = args[0] if args else os.path.join(fetch_blocuri.HERE, "site")

    sets = load_lots()
    if not sets:
        sys.exit(f"no blocs with parking lots in {fetch_blocuri.OUT_DIR}")
    ids = min_cover(sets)
    n_lots = len(frozenset().union(*sets.values()))
    print(f"{len(ids)} of {len(sets)} blocs cover all {n_lots} lots")
    if dry_run:
        print(" ".join(map(str, ids)))
        return

    if not fetch_blocuri.load_cookies() and not fetch_blocuri.login():
        sys.exit("No cookies in sample_curl.sh and login failed - set PARCARI_EMAIL / PARCARI_PASSWORD")
    saved, failed = 0, {}
    for n, bloc_id in enumerate(ids, 1):
        try:
            status, data = fetch_blocuri.fetch_bloc(bloc_id)
        except fetch_blocuri.AuthError as e:
            sys.exit(f"{e}\n{saved} blocs refetched; site not rebuilt")
        if data:
            fetch_blocuri.save(bloc_id, data)
            saved += 1
        else:
            failed[bloc_id] = status
        if n % 50 == 0:
            print(f"[{n}/{len(ids)}] saved={saved} failed={len(failed)}", flush=True)
        time.sleep(fetch_blocuri.DELAY)
    print(f"Refetched {saved} blocs, {len(failed)} failed")
    if failed:
        by_status = {}
        for i, s in failed.items():
            by_status.setdefault(s, []).append(i)
        for s, failed_ids in sorted(by_status.items()):
            print(f"  HTTP {s}: {len(failed_ids)} ids, e.g. {failed_ids[:10]}")
        # Their old files are kept, so those lots stay as they were.
        print("Lots near only failed blocs keep their old data")

    build_site.main(out_dir)
    if deploy and not deploy_site.main(out_dir):
        sys.exit(1)


if __name__ == "__main__":
    main()
