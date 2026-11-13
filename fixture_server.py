import gzip
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit


@contextmanager
def site(recorder=None):
    routes = json.loads((Path(__file__).parent / 'fixtures/site.json').read_text())

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if recorder is not None:
                recorder.append({
                    'path': path,
                    'headers': {k: v for k, v in self.headers.items()},
                })
            route = routes.get(path, {'status': 404, 'body': 'not found'})
            if 'variants' in route:
                tenant = self.headers.get('X-Tenant', 'anonymous')
                language = self.headers.get('Accept-Language', 'en')
                key = tenant + ':' + language
                route = {**route, **route['variants'].get(key, route['variants']['anonymous:en'])}
            status = route['status']
            if route.get('etag') and self.headers.get('If-None-Match') == route['etag']:
                status = 304
            elif not self.headers.get('If-None-Match') and route.get('last_modified') and self.headers.get('If-Modified-Since') == route['last_modified']:
                status = 304
            if status == 304 and route.get('on_304'):
                # 模拟来源在重验证后调整缓存策略（例如 Vary 变化）。
                route = {**route, **route['on_304']}
            self.send_response(status)
            for key, header in [('etag', 'ETag'), ('last_modified', 'Last-Modified'), ('location', 'Location'), ('retry_after', 'Retry-After'), ('vary', 'Vary'), ('cache_control', 'Cache-Control')]:
                if key in route:
                    self.send_header(header, route[key])
            self.send_header('Content-Type', route.get('type', 'text/plain') + '; charset=utf-8')
            body = route['body'].replace('{base}', self.server.base).encode() if status != 304 else b''
            if status == 200 and 'gzip' in self.headers.get('Accept-Encoding', ''):
                body = gzip.compress(body, mtime=0)
                self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.base = 'http://127.0.0.1:' + str(server.server_port)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server.base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
