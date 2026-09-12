#!/usr/bin/env python3
"""一个假的被采应用，用来本地跑通 exporter。

只用标准库，没有依赖，clone 下来就能起。
真实要采的是另一个实验环境里的 Flask 应用，接口就是下面这两个：
    GET /            首页，慢一点，模拟真实处理耗时
    GET /api/stats   {"requests": 累计请求数, "queue_depth": 当前队列深度}

跑法：
    python examples/mock_app.py           # 0.0.0.0:8000
    python examples/mock_app.py --port 9000
"""

import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_stats = {"requests": 0, "queue_depth": 0}
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with _lock:
            _stats["requests"] += 1

        if self.path == "/api/stats":
            with _lock:
                # 队列深度随机波动一下，监控图上才有曲线可看
                _stats["queue_depth"] = random.randint(0, 20)
                body = json.dumps(_stats)
            self._send(200, body, "application/json")

        elif self.path == "/":
            # 模拟接口耗时，好让延迟直方图有数据
            time.sleep(random.uniform(0.02, 0.06))
            self._send(200, "mock app is running\n", "text/plain; charset=utf-8")

        elif self.path == "/health":
            self._send(200, json.dumps({"status": "ok"}), "application/json")

        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass  # 别刷屏


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("mock app 监听 %s:%d" % (args.host, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
