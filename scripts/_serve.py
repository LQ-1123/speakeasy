import http.server, socketserver, os
os.chdir(r'D:\desktop\口语\backend\app\static\app')
class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/_next') or '.' in self.path.split('?')[0].split('/')[-1]:
            return super().do_GET()
        self.path = '/index.html'
        return super().do_GET()
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(('127.0.0.1', 8123), H) as httpd:
    print('serving on 8123')
    httpd.serve_forever()
