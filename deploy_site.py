#!/usr/bin/env python3
"""Deploy site/ (from build_site.py) to Cloudflare as the "parcari" Worker's static assets.

Uses the Workers static assets upload API directly (stdlib only, no wrangler):
send a manifest of every file's hash and size, upload the files Cloudflare
doesn't already have (in the buckets it asks for), then upload a new version of
the assets-only Worker, which deploys it. Unchanged files aren't sent again.

The API token comes from CLOUDFLARE_API_TOKEN, or else from
~/.config/parcari/cloudflare_token (kept out of the repo). It needs the
account's "Workers Scripts: Edit" permission.

Usage: python3 deploy_site.py [site_dir]   (default site/)
"""
import base64
import hashlib
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNT = "c33a4a303504473af1c6ff1a704de664"
SCRIPT = "parcari"
COMPATIBILITY_DATE = "2026-10-26"
API = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers"
TOKEN_FILE = os.path.expanduser("~/.config/parcari/cloudflare_token")


class DeployError(Exception):
    pass


def token():
    if os.environ.get("CLOUDFLARE_API_TOKEN"):
        return os.environ["CLOUDFLARE_API_TOKEN"]
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def multipart(parts):
    """parts: [(name, filename or None, content type, bytes)] -> (body, content type)."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, filename, ctype, data in parts:
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        body += f"--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n".encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def call(method, url, auth, body=None, ctype="application/json"):
    """Send an API request and return its "result", retrying on network errors and 5xx."""
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Authorization": f"Bearer {auth}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r).get("result")
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 3:
                try:
                    errors = json.load(e).get("errors")
                except ValueError:
                    errors = e.reason
                raise DeployError(f"{method} {url}: HTTP {e.code} {errors}") from None
        except urllib.error.URLError as e:
            if attempt == 3:
                raise DeployError(f"{method} {url}: {e.reason}") from None
        time.sleep(2 ** attempt)


def deploy(site_dir, tok):
    files = {}  # hash -> (path, size)
    manifest = {}
    for root, _, names in os.walk(site_dir):
        for name in sorted(names):
            path = os.path.join(root, name)
            with open(path, "rb") as f:
                data = f.read()
            # The hash wrangler uses: 32 hex chars of sha256(base64 content + extension).
            ext = os.path.splitext(name)[1].lstrip(".")
            h = hashlib.sha256(base64.b64encode(data) + ext.encode()).hexdigest()[:32]
            url_path = "/" + os.path.relpath(path, site_dir).replace(os.sep, "/")
            manifest[url_path] = {"hash": h, "size": len(data)}
            files[h] = (path, len(data))

    session = call("POST", f"{API}/scripts/{SCRIPT}/assets-upload-session", tok, {"manifest": manifest})
    jwt, buckets = session["jwt"], session.get("buckets") or []
    print(f"{len(manifest)} files, {sum(map(len, buckets))} to upload")
    for bucket in buckets:
        parts = []
        for h in bucket:
            path, size = files[h]
            with open(path, "rb") as f:
                data = base64.b64encode(f.read())
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            parts.append((h, h, ctype, data))
            print(f"  {os.path.relpath(path, site_dir)} ({size / 1e6:.1f} MB)", flush=True)
        body, ctype = multipart(parts)
        result = call("POST", f"{API}/assets/upload?base64=true", jwt, body, ctype)
        jwt = (result or {}).get("jwt") or jwt  # the last bucket's response holds the completion token

    metadata = {"compatibility_date": COMPATIBILITY_DATE, "assets": {"jwt": jwt}}
    body, ctype = multipart([("metadata", None, "application/json", json.dumps(metadata).encode())])
    result = call("PUT", f"{API}/scripts/{SCRIPT}", tok, body, ctype)
    print(f"Deployed {SCRIPT} (modified {result.get('modified_on')})")


def main(site_dir=None):
    """Deploy site_dir; returns True on success."""
    site_dir = site_dir or (sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "site"))
    if not os.path.isfile(os.path.join(site_dir, "data.json")):
        print(f"no data.json in {site_dir}, run build_site.py first", file=sys.stderr)
        return False
    tok = token()
    if not tok:
        print(f"no Cloudflare token: set CLOUDFLARE_API_TOKEN or write it to {TOKEN_FILE}", file=sys.stderr)
        return False
    try:
        deploy(site_dir, tok)
    except DeployError as e:
        print(f"deploy failed: {e}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
