"""Queue boundary and degraded metrics without inference requests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest

import ocr_runtime


class ConcurrencyTests(unittest.TestCase):
    def test_real_http_boundary_and_unknown_metrics(self):
        class Handler(BaseHTTPRequestHandler):
            body=b''
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(self.body)
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/metrics'
            cases=[('0',2),('1',2),('2',1),('10',1),('-1',1),('NaN',1),('Inf',1),('0.5',1)]
            for waiting,expected in cases:
                Handler.body=('vllm:num_requests_waiting{model="fixture"} '+waiting+'\n').encode()
                self.assertEqual(expected,ocr_runtime.concurrency_snapshot(url)['limit'],waiting)
            Handler.body=b'vllm:num_requests_waiting{engine="0"} 1\nvllm:num_requests_waiting{engine="1"} 1\n'
            self.assertEqual(1,ocr_runtime.concurrency_snapshot(url)['limit'])
            Handler.body=b'# no waiting series\n'
            result=ocr_runtime.concurrency_snapshot(url)
            self.assertEqual((1,None),(result['limit'],result['waiting']))
        finally:
            server.shutdown();server.server_close();thread.join()
        self.assertEqual(1,ocr_runtime.concurrency_snapshot(url)['limit'])
