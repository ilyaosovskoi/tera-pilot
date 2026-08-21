#!/usr/bin/env python3
"""Local HTTP server that 302-redirects /start to the URL in target.txt."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/start":
            target = (Path(__file__).parent / "target.txt").read_text().strip()
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8931), Handler).serve_forever()
