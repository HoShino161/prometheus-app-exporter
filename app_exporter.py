#!/usr/bin/env python3
"""给 Flask 应用写的 Prometheus exporter。

采应用的 /api/stats（队列深度、累计请求数），再自己请求一次首页测延迟，
全部暴露在 :9877/metrics 上等 Prometheus 来抓。

本地跑（不用装 Prometheus，仓库里带了假应用）：
    pip install prometheus_client requests
    python examples/mock_app.py &
    python app_exporter.py --debug
"""

import argparse
import logging
import os
import sys
import time

import requests
from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server

# 不走系统代理的会话。Windows 上开着代理软件时 requests 会自动用 HTTP_PROXY，
# 连采集 127.0.0.1 都被塞进代理，应用挂了报的是 502 而不是连接被拒绝，特别容易查错方向。
_session = requests.Session()


def _configure_session(use_env_proxy):
    _session.trust_env = bool(use_env_proxy)
    if not use_env_proxy:
        _session.proxies = {"http": None, "https": None}


# 指标定义。prometheus_client 会自己把这些注册进全局 REGISTRY
APP_QUEUE_DEPTH = Gauge("app_queue_depth", "当前任务队列深度", ["host"])
APP_REQUESTS = Counter("app_requests_total", "应用累计处理请求数（转发上游的增量）", ["host"])
APP_REQUEST_DURATION = Histogram(
    "app_request_duration_seconds",
    "访问应用首页的端到端耗时",
    ["host", "endpoint"],
    # 正常接口都在 10~50ms，桶在这段加几个才看得出 P99 抖动。
    # 别贪多，每个桶都是一条序列。
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
APP_UP = Gauge("app_up", "应用是否可达，1=可达 0=采不到", ["host"])
SCRAPE_ERRORS = Counter("app_exporter_scrape_errors_total", "采集失败次数", ["host"])
SCRAPE_DURATION = Histogram(
    "app_exporter_scrape_duration_seconds",
    "自己采集一次花多久",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 3.0),
)

# host -> 上一次读到的上游累计值
_upstream_baseline = {}


def init_series(cfg):
    """启动时先把要暴露的序列建出来。

    prometheus_client 是惰性的：带标签的指标在第一次 .labels() 之前，
    /metrics 里一条样本都没有。计数器一直是 0 的时候最明显——
    面板上直接显示 No data，看着像面板坏了，其实只是没序列。
    """
    for metric in (APP_QUEUE_DEPTH, APP_REQUESTS, APP_UP, SCRAPE_ERRORS):
        metric.labels(host=cfg.host_label)
    APP_REQUEST_DURATION.labels(host=cfg.host_label, endpoint=cfg.probe_path)


def forward_counter(counter, host, upstream_value):
    """把上游的累计值换算成增量再写进本地 Counter，返回这次加了多少。

    两种情况只重置基准、不计数：
      首次采集 —— 否则会把应用开机到现在的量一次性灌进来
      上游重启（值变小）—— 差值会算成负数，而 Counter 不允许减
    """
    prev = _upstream_baseline.get(host)
    if prev is None or upstream_value < prev:
        _upstream_baseline[host] = upstream_value
        return 0.0

    delta = upstream_value - prev
    if delta:
        counter.labels(host=host).inc(delta)
        _upstream_baseline[host] = upstream_value
    return delta


def collect_once(cfg):
    """拉一次 /api/stats 写进指标。成功返回 True。"""
    t0 = time.perf_counter()
    ok = True
    try:
        resp = _session.get(cfg.stats_url, timeout=cfg.timeout)
        resp.raise_for_status()
        data = resp.json()
        APP_QUEUE_DEPTH.labels(host=cfg.host_label).set(float(data.get("queue_depth", 0)))
        forward_counter(APP_REQUESTS, cfg.host_label, float(data.get("requests", 0)))
        APP_UP.labels(host=cfg.host_label).set(1)
    except (requests.RequestException, ValueError, TypeError) as exc:
        # 失败也要被看到。什么都不上报的话图上是一条平线，看着像一切正常
        ok = False
        SCRAPE_ERRORS.labels(host=cfg.host_label).inc()
        APP_UP.labels(host=cfg.host_label).set(0)
        logging.warning("采集失败 %s: %s", cfg.stats_url, exc)
    finally:
        SCRAPE_DURATION.observe(time.perf_counter() - t0)
    return ok


def probe_once(cfg):
    """自己请求一次首页，把耗时打进直方图。

    应用里没埋点，所以在外面测，拿到的是用户实际感受到的耗时（含反代那一段）。
    探针是串行的，看趋势可以，别当压测数据用。
    """
    url = cfg.probe_base + cfg.probe_path
    t0 = time.perf_counter()
    try:
        resp = _session.get(url, timeout=cfg.timeout)
        resp.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        SCRAPE_ERRORS.labels(host=cfg.host_label).inc()
        APP_UP.labels(host=cfg.host_label).set(0)
        logging.warning("探测失败 %s: %s", url, exc)
        return
    APP_REQUEST_DURATION.labels(host=cfg.host_label, endpoint=cfg.probe_path).observe(
        time.perf_counter() - t0
    )


class Config:
    """优先级：命令行参数 > 环境变量 > 默认值。

    支持环境变量是为了 systemd 那边好改，不用动 unit 文件。
    """

    def __init__(self, **kw):
        self.host_label = kw.get("host_label") or os.getenv("APP_EXPORTER_HOST_LABEL", "app1")
        self.stats_url = kw.get("stats_url") or os.getenv(
            "APP_EXPORTER_STATS_URL", "http://127.0.0.1/api/stats"
        )
        self.probe_base = kw.get("probe_base") or os.getenv(
            "APP_EXPORTER_PROBE_BASE", "http://127.0.0.1"
        )
        self.probe_path = kw.get("probe_path") or os.getenv("APP_EXPORTER_PROBE_PATH", "/")
        self.port = int(kw.get("port") or os.getenv("APP_EXPORTER_PORT", "9877"))
        self.interval = float(kw.get("interval") or os.getenv("APP_EXPORTER_INTERVAL", "5"))
        self.timeout = float(kw.get("timeout") or os.getenv("APP_EXPORTER_TIMEOUT", "3"))
        self.no_probe = bool(kw.get("no_probe"))
        self.use_env_proxy = bool(kw.get("use_env_proxy"))


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="应用 exporter",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host-label", help="写进指标的 host 标签值")
    p.add_argument("--stats-url", help="应用指标接口地址")
    p.add_argument("--probe-base", help="探测用基地址")
    p.add_argument("--probe-path", help="探测路径")
    p.add_argument("--port", type=int, help="/metrics 端口")
    p.add_argument("--interval", type=float, help="采集间隔（秒）")
    p.add_argument("--timeout", type=float, help="请求超时（秒）")
    p.add_argument("--no-probe", action="store_true", help="只采 /api/stats，不做延迟探测")
    p.add_argument("--use-env-proxy", action="store_true", help="允许走环境变量里的代理")
    p.add_argument("--debug", action="store_true", help="打印每次采集的明细")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = Config(
        host_label=args.host_label,
        stats_url=args.stats_url,
        probe_base=args.probe_base,
        probe_path=args.probe_path,
        port=args.port,
        interval=args.interval,
        timeout=args.timeout,
        no_probe=args.no_probe,
        use_env_proxy=args.use_env_proxy,
    )
    _configure_session(cfg.use_env_proxy)
    if cfg.use_env_proxy:
        logging.warning("允许走系统代理，内网采集报 502 或超时先怀疑这里")

    # 先把端口起起来再进采集循环，不然 Prometheus 第一次抓会连接被拒，
    # 在 up 上留下一个假的 0
    init_series(cfg)
    start_http_server(cfg.port)
    logging.info("监听 :%d/metrics，采集 %s，间隔 %.1fs", cfg.port, cfg.stats_url, cfg.interval)

    while True:
        ok = collect_once(cfg)
        if not cfg.no_probe:
            probe_once(cfg)
        if args.debug:
            logging.debug("ok=%s 队列深度=%s", ok, _snapshot("app_queue_depth"))
        time.sleep(cfg.interval)


def _snapshot(name):
    """调试用，把某个指标当前的值拼成一行。"""
    out = []
    for metric in REGISTRY.collect():
        if metric.name != name:
            continue
        for s in metric.samples:
            out.append("%s=%s" % (s.labels, s.value))
    return " ".join(out) or "无"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("已停止")
        sys.exit(0)

# TODO: 现在只能采一个实例，host 标签是写死的。
#       多实例要改成读配置文件，而且一个实例连不上不能拖累别的
