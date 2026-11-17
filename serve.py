#!/usr/bin/env python3
"""Tiny redirect + audio file server for the voice bridge.

Two jobs, stdlib only:

1. /t/<env>/<thread>  -> HTML page that bounces into the native app via
   the t3code:// deep link (meta refresh + JS + a manual "Open in T3"
   button, since in-app browsers are picky about custom schemes).
   The bridge points t3_web_url here so Telegram "Open thread" links
   become tappable https links that land in the native app.

2. /audio/<name>.ogg  -> static files from audio_dir (Slack link mode).
   Old files are NOT pruned here; bridge.py prunes on each voice send.

Run behind tailscale serve, e.g.:
  tailscale serve --bg --https=8443 http://127.0.0.1:3901
"""

import argparse
import html
import re
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SEG = re.compile(r"[A-Za-z0-9-]{1,64}")
AUDIO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.ogg")


def redirect_page(env, thread):
    deep = f"t3code://threads/{env}/{thread}"
    esc = html.escape(deep, quote=True)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0;url={esc}">
<title>Opening T3…</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;display:flex;min-height:90vh;align-items:center;justify-content:center;margin:0;background:#111;color:#eee;text-align:center}}a{{display:inline-block;margin-top:16px;padding:14px 28px;font-size:18px;background:#4f7cff;color:#fff;border-radius:12px;text-decoration:none}}.small{{margin-top:12px;font-size:12px;color:#888;word-break:break-all}}</style>
</head><body><div>
<div>Opening thread in T3…</div>
<div><a href="{esc}">Open in T3</a></div>
<div class="small">{esc}</div>
<script>window.location.replace({deep!r});</script>
</div></body></html>
""".encode()


class Handler(SimpleHTTPRequestHandler):
    audio_dir = "."

    def log_message(self, *args):
        pass  # stdout is journald; keep it quiet except errors

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "t" and SEG.fullmatch(parts[1] or "") and SEG.fullmatch(parts[2] or ""):
            body = redirect_page(parts[1], parts[2])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if len(parts) == 2 and parts[0] == "audio" and AUDIO.fullmatch(parts[1] or ""):
            f = Path(self.audio_dir) / parts[1]
            if f.is_file():
                self.send_response(200)
                self.send_header("Content-Type", "audio/ogg")
                self.send_header("Content-Length", str(f.stat().st_size))
                self.end_headers()
                with open(f, "rb") as fh:
                    while chunk := fh.read(65536):
                        self.wfile.write(chunk)
                return
            self.send_error(404, "no such audio")
            return
        if path in ("/", "/healthz"):
            body = b"T3 voice bridge redirector ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3901)
    ap.add_argument("--audio-dir", default="./audio")
    args = ap.parse_args()
    handler = partial(Handler)
    handler.audio_dir = str(Path(args.audio_dir).resolve())
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"redirector on 127.0.0.1:{args.port}, audio={handler.audio_dir}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
