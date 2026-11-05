"""本地验收服务器：python scripts/web-local-server.py [port]"""
import http.server
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deliverables", "web-local")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0].split("#")[0]
        fs_path = os.path.join(ROOT, path.lstrip("/"))
        if path == "/" or not os.path.isfile(fs_path):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"SpeakEasy web (local mode): http://127.0.0.1:{PORT}")
    print("按 Ctrl+C 停止")
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
