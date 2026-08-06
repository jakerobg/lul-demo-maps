#!/usr/bin/env python3
"""Static server with HTTP Range support, which PMTiles requires.

Python's built-in http.server ignores Range headers and returns the whole
55MB archive for every tile request, so use this for local preview:

    python3 serve.py [port]
"""

import functools
import http.server
import os
import re
import socketserver
import sys


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(f.fileno()).st_size
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if not m:
            f.close()
            self.send_error(400, "Invalid Range")
            return None

        start, end = m.group(1), m.group(2)
        if start == "":  # suffix range: last N bytes
            start = max(0, size - int(end))
            end = size - 1
        else:
            start = int(start)
            end = int(end) if end else size - 1
        end = min(end, size - 1)
        if start > end:
            f.close()
            self.send_error(416, "Requested Range Not Satisfiable")
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        f.seek(start)
        return _Limited(f, end - start + 1)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


class _Limited:
    """File wrapper that stops after n bytes, for copyfile()."""

    def __init__(self, fh, n):
        self.fh, self.remaining = fh, n

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        if size < 0 or size > self.remaining:
            size = self.remaining
        data = self.fh.read(size)
        self.remaining -= len(data)
        return data

    def close(self):
        self.fh.close()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    handler = functools.partial(RangeHandler, directory=os.path.dirname(os.path.abspath(__file__)))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", port), handler) as httpd:
        print(f"Serving on http://localhost:{port}/coverage-map.html  (Range supported)")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
