#!/usr/bin/env python3
"""
Keep-alive HTTP server + self-pinger for Render Free Web Service.

Render free web services spin down after 15 minutes of inactivity.
To prevent this we:
  1. Serve a tiny HTTP page on $PORT (Render requires a web service to bind a port).
  2. Ping our own public URL every 10 minutes so Render sees activity.

If RENDER_EXTERNAL_URL is set (Render sets it automatically), we self-ping it.
As a backup, also set up a free UptimeRobot monitor hitting your Render URL.
"""

import http.server
import socketserver
import threading
import time
import os
import urllib.request

PORT = int(os.environ.get("PORT", "10000"))


class KeepAliveHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            b"<title>Lenskart Bot</title>"
            b"<meta http-equiv='refresh' content='300'></head>"
            b"<body style='font-family:sans-serif;background:#0d1117;color:#58a6ff;"
            b"display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            b"<div style='text-align:center'>"
            b"<h1>&#9889; Lenskart Bot is running</h1>"
            b"<p>Telegram long-polling active &middot; 24/7</p>"
            b"<p style='color:#8b949e;font-size:0.85em'>"
            b"Auto-refresh + self-ping keeps this service awake.</p>"
            b"</div></body></html>"
        )

    def log_message(self, fmt, *args):
        pass  # silence access logs


def _self_ping_loop():
    """Ping our own Render URL every 10 minutes to prevent spin-down."""
    my_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not my_url:
        # Fallback: try to build from RENDER_SERVICE_ID (not always available)
        return
    # Ensure it has a scheme
    if not my_url.startswith("http"):
        my_url = "https://" + my_url
    print(f"Self-pinger started -> {my_url} (every 10 min)", flush=True)
    while True:
        time.sleep(600)  # 10 minutes
        try:
            req = urllib.request.Request(my_url, headers={"User-Agent": "keepalive/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                print(f"Self-ping OK ({resp.status})", flush=True)
        except Exception as e:
            print(f"Self-ping failed: {e}", flush=True)


def start_keepalive_server():
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("0.0.0.0", PORT), KeepAliveHandler)
    except OSError as e:
        print(f"Keep-alive server could not bind to port {PORT}: {e}", flush=True)
        return
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    print(f"Keep-alive server listening on port {PORT}", flush=True)
    # Start the self-pinger in the background
    threading.Thread(target=_self_ping_loop, daemon=True).start()


if __name__ == "__main__":
    start_keepalive_server()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("Stopped.")
