#!/usr/bin/env python3
"""标签基数实验：把用户 IP 当标签，和聚合成固定标签集，差多少。

跑法：
    python experiments/cardinality_lab.py --points 10,100,1000,10000

每个规模单独起一个子进程测。Python 不会把内存还给系统，
同一个进程里连着测 10 -> 100 -> 1000 -> 10000，后面的读数会被前面的分配污染。
"""

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from wsgiref.simple_server import make_server as wsgi_make_server

import requests
from prometheus_client import CollectorRegistry, Counter, generate_latest, make_wsgi_app
from prometheus_client.exposition import ThreadingWSGIServer, _SilentHandler


def build(mode, n):
    """mode='bad' 用 user_ip 当标签；mode='good' 聚合成固定的 status 标签。"""
    reg = CollectorRegistry()
    if mode == "bad":
        metric = Counter("app_user_requests_total", "每个用户IP的请求数", ["user_ip"], registry=reg)
        for i in range(n):
            metric.labels(user_ip="10.%d.%d.%d" % (i // 65536 % 256, i // 256 % 256, i % 256)).inc(i % 7)
    else:
        metric = Counter("app_requests_total", "聚合后的请求数", ["status"], registry=reg)
        for i in range(n):
            metric.labels(status=["2xx", "3xx", "4xx", "5xx"][i % 4]).inc(i % 7)
    return reg


def mem():
    """当前进程的工作集。Windows 用 psapi，Linux 读 /proc。拿不到就返回空。"""
    try:
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes as wt

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(pmc)
            k32 = ctypes.WinDLL("kernel32")
            psapi = ctypes.WinDLL("psapi")
            # GetCurrentProcess() 返回伪句柄 -1，默认 c_int 在 64 位下会被截断，
            # 不显式声明 restype 的话下面这个调用直接返回 0
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            fn = psapi.GetProcessMemoryInfo
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), wt.DWORD]
            fn.restype = wt.BOOL
            if fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return pmc.PeakWorkingSetSize
            return 0

        with open("/proc/self/statm", encoding="utf-8") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def measure(reg, port, rounds=9):
    t0 = time.perf_counter()
    text = generate_latest(reg)
    gen_ms = (time.perf_counter() - t0) * 1000

    series = sum(1 for line in text.decode().splitlines() if line and not line.startswith("#"))

    httpd = wsgi_make_server("127.0.0.1", port, make_wsgi_app(reg), ThreadingWSGIServer, _SilentHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    times, size = [], 0
    try:
        url = "http://127.0.0.1:%d/metrics" % port
        # 用 Session 复用连接，不然每次都新建 TCP，握手开销会算进抓取时间里
        with requests.Session() as sess:
            sess.get(url, timeout=120)
            for _ in range(rounds):
                t1 = time.perf_counter()
                size = len(sess.get(url, timeout=120).content)
                times.append((time.perf_counter() - t1) * 1000)
                time.sleep(0.03)
    finally:
        httpd.shutdown()

    return {
        "series": series,
        "bytes": len(text),
        "http_bytes": size,
        "gen_ms": round(gen_ms, 3),
        "scrape_ms": round(statistics.median(times), 3),
    }


def run_one(mode, n, out_path):
    r = {"mode": mode, "n": n}
    r.update(measure(build(mode, n), free_port()))
    r["peak_mb"] = round(mem() / 1048576, 1)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=2)
    return 0


def spawn(mode, n, tmpdir):
    out = os.path.join(tmpdir, "single_%s_%d.json" % (mode, n))
    subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--single", mode, str(n), "--json-out", out],
        check=True, capture_output=True,
    )
    with open(out, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", default="10,100,1000,10000")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--single", nargs=2, metavar=("MODE", "N"))
    args = ap.parse_args()

    if args.single:
        return run_one(args.single[0], int(args.single[1]), args.json_out)

    points = [int(x) for x in args.points.split(",") if x.strip()]
    tmpdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(tmpdir, exist_ok=True)

    print("%-10s %-34s %-34s" % ("用户数", "user_ip 当标签", "聚合成 status 标签"))
    print("-" * 82)

    rows = []
    for n in points:
        bad = spawn("bad", n, tmpdir)
        good = spawn("good", n, tmpdir)
        rows.append((n, bad, good))
        print("%-10d %-34s %-34s" % (
            n,
            "%d 条 / %.1f KiB / %.1f ms" % (bad["series"], bad["bytes"] / 1024, bad["scrape_ms"]),
            "%d 条 / %.1f KiB / %.1f ms" % (good["series"], good["bytes"] / 1024, good["scrape_ms"]),
        ))

    n, bad, good = rows[-1]
    print("-" * 82)
    print("最大规模 %d 个用户：" % n)
    print("  序列数    %d -> %d（%.0f 倍）" % (bad["series"], good["series"], bad["series"] / good["series"]))
    print("  暴露体积  %.1f KiB -> %.1f KiB（%.0f 倍）" % (bad["bytes"] / 1024, good["bytes"] / 1024, bad["bytes"] / good["bytes"]))
    print("  生成耗时  %.2f ms -> %.2f ms" % (bad["gen_ms"], good["gen_ms"]))
    print("  单次抓取  %.1f ms -> %.1f ms" % (bad["scrape_ms"], good["scrape_ms"]))
    print("  峰值内存  %.1f MiB -> %.1f MiB" % (bad["peak_mb"], good["peak_mb"]))
    print()
    print("序列数是乘出来的：再加一个 100 种取值的标签，直接 ×100。")

    if args.json_out:
        d = os.path.dirname(os.path.abspath(args.json_out))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"rows": [{"n": a, "bad": b, "good": c} for a, b, c in rows]}, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
