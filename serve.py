#!/usr/bin/env python3
"""Serve the viewer like `python3 -m http.server`, plus POST /api/refetch/<id>, which downloads one
bloc again into blocuri/<id>.json and returns it, and POST /api/login, which logs in with the
{"email", "password"} JSON the viewer's login dialog sends. Auth otherwise works as in
fetch_blocuri.py, except that it never prompts in the terminal: without cookies or
PARCARI_EMAIL / PARCARI_PASSWORD, the viewer asks for the login on the first refetch.

Listens on localhost only, since the endpoints make requests with your session.

Usage: python3 serve.py [port]   (default 8000)
"""
import functools
import http.server
import json
import re
import sys
import threading

import fetch_blocuri as fb

lock = threading.Lock()  # one API call at a time: the cookie jar and credentials are shared


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Revalidate every time, so a reload after a refetch can't show a heuristically cached old file.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_POST(self):
        if self.path == "/api/login":
            return self.login()
        m = re.fullmatch(r"/api/refetch/(\d+)", self.path)
        if not m:
            return self.send_error(404)
        bloc_id = int(m.group(1))
        try:
            with lock:
                status, data = fb.fetch_bloc(bloc_id)
                if data:
                    fb.save(bloc_id, data)
        except fb.AuthError:
            return self.reply(401, {"error": "not logged in, or the session expired"})
        except Exception as e:  # network errors, timeouts
            return self.reply(502, {"error": f"{type(e).__name__}: {e}"})
        if not data:
            return self.reply(502, {"error": f"the API answered HTTP {status} without bloc data"})
        print(f"Refetched bloc {bloc_id}", flush=True)
        self.reply(200, data)

    def login(self):
        # Requiring JSON means another site's page can't post here without a CORS preflight, which fails.
        if self.headers.get_content_type() != "application/json":
            return self.send_error(415)
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 10000)))
            email, password = body["email"].strip(), body["password"]
        except (ValueError, KeyError, TypeError, AttributeError):
            return self.reply(400, {"error": "expected {\"email\": ..., \"password\": ...}"})
        try:
            with lock:
                fb.credentials.clear()
                fb.credentials.update(EMAIL=email, password=password)
                ok = fb.login()
                if not ok:
                    fb.credentials.clear()  # so a later refetch asks again instead of retrying them
        except Exception as e:  # network errors, timeouts
            return self.reply(502, {"error": f"{type(e).__name__}: {e}"})
        if not ok:
            return self.reply(401, {"error": f"Login failed: {fb.login_error}"})
        print(f"Logged in as {email}", flush=True)
        self.reply(200, {})

    def reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    fb.interactive = False  # a prompt in the middle of a request would just hang it; the viewer asks instead
    # Log in now if there are no cookies but the credentials are in the environment.
    if not fb.load_cookies() and not fb.login():
        print("No cookies and no login with PARCARI_EMAIL / PARCARI_PASSWORD - the viewer will ask for a login"
              " on the first refetch", file=sys.stderr)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), functools.partial(Handler, directory=fb.HERE))
    print(f"Serving on http://localhost:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
