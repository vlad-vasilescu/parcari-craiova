#!/usr/bin/env python3
"""Fetch parking data for a range of bloc ids into blocuri/<id>.json.

Cookies (accessToken / refreshToken) are read from sample_curl.sh if it exists;
when the access token expires the script refreshes it the same way the website
does. If there are no cookies, or the refresh fails, it logs in with
PARCARI_EMAIL / PARCARI_PASSWORD from the environment (prompting for whatever
is missing when run in a terminal).

Usage: python3 fetch_blocuri.py [start_id] [end_id]   (inclusive, default 5050 8521)
"""
import getpass
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://parcari-ticketing.primariacraiova.ro/api/"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "blocuri")
DELAY = 0.2  # seconds between requests, to be gentle with the server

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [
    ("Accept", "application/json, text/plain, */*"),
    ("Referer", "https://parcari-ticketing.primariacraiova.ro/adrese/true"),
    ("User-Agent", "Mozilla/5.0"),
]


def load_cookies():
    """Load cookies from sample_curl.sh; return False if there are none."""
    path = os.path.join(HERE, "sample_curl.sh")
    if not os.path.exists(path):
        return False
    m = re.search(r"-b '([^']+)'", open(path).read())
    if not m:
        return False
    raw = m.group(1)
    for part in raw.split(";"):
        name, value = part.strip().split("=", 1)
        jar.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, "parcari-ticketing.primariacraiova.ro", True, False,
            "/", True, True, None, False, None, None, {}))
    return True


def get(path):
    """Return (status, body bytes)."""
    try:
        with opener.open(API + path, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def refresh():
    status, _ = get("person/refresh")
    return status == 200


credentials = {}
login_error = ""  # why the last login() failed, for serve.py to pass on
interactive = sys.stdin.isatty()  # whether login() may prompt for missing credentials


class AuthError(Exception):
    pass


def login():
    """Log in like the website's login form; the server sets the token cookies."""
    global login_error
    if not credentials:
        email = os.environ.get("PARCARI_EMAIL")
        password = os.environ.get("PARCARI_PASSWORD")
        if interactive:
            email = email or input("Email: ")
            password = password or getpass.getpass("Password: ")
        if not email or not password:
            login_error = "no credentials"
            return False
        credentials.update(EMAIL=email, password=password)
    jar.clear()  # drop stale tokens so only the fresh ones get sent
    req = urllib.request.Request(
        API + "person/login", data=json.dumps(credentials).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(req, timeout=30) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        login_error = {401: "wrong password", 403: "unknown email"}.get(e.code, f"HTTP {e.code}")
        print(f"Login failed: {login_error}", file=sys.stderr)
        return False


def fetch(bloc_id):
    path = f"immobile/{bloc_id}?distance_to_parking_lot=30"
    for attempt in range(5):
        status, body = get(path)
        if status == 401:
            if not refresh() and not login():
                raise AuthError("Token refresh and login failed - set PARCARI_EMAIL / PARCARI_PASSWORD"
                                " or paste fresh cookies into sample_curl.sh")
            continue
        if status == 429 or status >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        return status, body
    return status, body


def fetch_bloc(bloc_id):
    """Return (status, data); data is None unless the response is a bloc with parking_lots."""
    status, body = fetch(bloc_id)
    data = None
    if status == 200:
        try:
            data = json.loads(body)
        except ValueError:
            pass
    return status, data if isinstance(data, dict) and "parking_lots" in data else None


def save(bloc_id, data):
    out = os.path.join(OUT_DIR, f"{bloc_id}.json")
    tmp = out + ".tmp"  # written aside and renamed, so the viewer never reads a half-written file
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, out)


def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 8521
    if not load_cookies() and not login():
        sys.exit("No cookies in sample_curl.sh and login failed - set PARCARI_EMAIL / PARCARI_PASSWORD")
    saved, skipped, missing = 0, 0, {}
    for bloc_id in range(start, end + 1):
        out = os.path.join(OUT_DIR, f"{bloc_id}.json")
        if os.path.exists(out):
            skipped += 1
            continue
        try:
            status, data = fetch_bloc(bloc_id)
        except AuthError as e:
            sys.exit(str(e))
        if data:
            save(bloc_id, data)
            saved += 1
        else:
            missing[bloc_id] = status
        if (bloc_id - start) % 100 == 0:
            print(f"[{bloc_id}] saved={saved} skipped={skipped} missing={len(missing)}", flush=True)
        time.sleep(DELAY)
    print(f"Done: saved={saved} already-present={skipped} not-saved={len(missing)}")
    if missing:
        by_status = {}
        for i, s in missing.items():
            by_status.setdefault(s, []).append(i)
        for s, ids in sorted(by_status.items()):
            print(f"  HTTP {s}: {len(ids)} ids, e.g. {ids[:10]}")


if __name__ == "__main__":
    main()
