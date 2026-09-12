"""exporter 的单元测试。

测什么、为什么测这些：
1. 命名规范——指标名一旦暴露给 Prometheus 就不能随便改（改名=历史数据断档），
   所以在 CI/本地就把它钉死，防止后面手滑。
2. Counter 增量换算——这是整个 exporter 里唯一"错了但不会报错"的逻辑，
   错了只会让图上数字慢慢离谱，肉眼根本发现不了，必须有测试兜住。
3. 采集失败路径——监控组件最容易漏测的就是失败分支，
   而失败分支恰恰是它存在的意义。

跑法（在 exporter/ 目录下）：
    python -m pytest tests -v
"""

from __future__ import annotations

import os
import sys

import pytest
from prometheus_client import REGISTRY

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_exporter as ex  # noqa: E402


@pytest.fixture(autouse=True)
def clean_baseline():
    """每个用例前清掉增量基线，避免用例之间互相污染。"""
    ex._upstream_baseline.clear()
    yield
    ex._upstream_baseline.clear()


def value(name: str, labels: dict | None = None) -> float:
    """从默认注册表里读一个样本值，读不到返回 0。"""
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# --------------------------------------------------------------------------
# 1. 命名规范
# --------------------------------------------------------------------------

def test_metric_naming_conventions():
    """Counter 必须 _total 结尾、统一 app_ 前缀。

    坑：带标签的指标在没有 .labels(...) 实例化之前，/metrics 里【只有 # HELP/# TYPE，
    没有任何样本行】。这是 prometheus_client 的惰性设计——省内存，
    但会让"我明明定义了指标，怎么文本里搜不到"这种问题卡新人半天。
    所以测试里必须先造一个样本再断言。
    """
    from prometheus_client import generate_latest

    ex.APP_QUEUE_DEPTH.labels(host="app1").set(3)
    ex.APP_REQUESTS.labels(host="app1").inc(1)
    text = generate_latest().decode()

    names = {m.name for m in REGISTRY.collect()}
    assert "app_requests" in names, "Counter 内部名不带 _total，暴露时自动补"
    assert "app_request_duration_seconds" in names
    assert "app_queue_depth" in names
    assert "app_up" in names

    # 暴露出去的文本里 Counter 必须带 _total（PromQL 的 rate() 靠它区分语义）
    assert 'app_requests_total{host="app1"}' in text
    assert 'app_queue_depth{host="app1"} 3.0' in text


def test_histogram_has_buckets():
    from prometheus_client import generate_latest

    ex.APP_REQUEST_DURATION.labels(host="app1", endpoint="/").observe(0.037)
    text = generate_latest().decode()

    assert 'app_request_duration_seconds_bucket{endpoint="/",host="app1",le="0.005"}' in text
    # 累计桶：0.037 落在 0.05 的桶里，所以 le=0.05 应该是 1
    assert 'app_request_duration_seconds_bucket{endpoint="/",host="app1",le="0.05"} 1.0' in text
    # 没落进 0.025 的桶
    assert 'app_request_duration_seconds_bucket{endpoint="/",host="app1",le="0.025"} 0.0' in text
    assert 'app_request_duration_seconds_count{endpoint="/",host="app1"} 1.0' in text
    assert 'app_request_duration_seconds_sum{endpoint="/",host="app1"} 0.037' in text


# --------------------------------------------------------------------------
# 2. Counter 增量换算（核心逻辑）
# --------------------------------------------------------------------------

def test_first_scrape_records_baseline_only():
    """首次采集只记基线——不能把应用开机到现在的历史量一次性灌进来。"""
    before = value("app_requests_total", {"host": "app1"})
    delta = ex.forward_counter(ex.APP_REQUESTS, "app1", 100)
    assert delta == 0
    assert value("app_requests_total", {"host": "app1"}) == before
    assert ex._upstream_baseline["app1"] == 100


def test_second_scrape_forwards_delta():
    """第二次采集转发差值，而不是累计值。"""
    ex.forward_counter(ex.APP_REQUESTS, "app1", 100)
    before = value("app_requests_total", {"host": "app1"})

    ex.forward_counter(ex.APP_REQUESTS, "app1", 107)
    assert value("app_requests_total", {"host": "app1"}) == before + 7

    ex.forward_counter(ex.APP_REQUESTS, "app1", 110)
    assert value("app_requests_total", {"host": "app1"}) == before + 10

    # 关键断言：3 次采集、上游从 100 涨到 110，本地只应该 +10。
    # 如果是直接 inc(累计值)，这里会变成 100+107+110=317。
    assert value("app_requests_total", {"host": "app1"}) != before + 317


def test_upstream_restart_resets_baseline():
    """上游重启后计数归零：重置基线，绝不能让本地 Counter 回退（会抛异常）。"""
    ex.forward_counter(ex.APP_REQUESTS, "app1", 500)
    before = value("app_requests_total", {"host": "app1"})

    delta = ex.forward_counter(ex.APP_REQUESTS, "app1", 3)  # 应用重启了
    assert delta == 0
    assert value("app_requests_total", {"host": "app1"}) == before
    assert ex._upstream_baseline["app1"] == 3

    # 重启之后再正常涨，应该从新基线算增量
    ex.forward_counter(ex.APP_REQUESTS, "app1", 9)
    assert value("app_requests_total", {"host": "app1"}) == before + 6


# --------------------------------------------------------------------------
# 3. 采集成功 / 失败路径
# --------------------------------------------------------------------------

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


def test_collect_success_sets_metrics(monkeypatch):
    import requests

    monkeypatch.setattr(
        ex._session, "get", lambda *a, **k: FakeResp({"requests": 42, "queue_depth": 7})
    )
    cfg = ex.Config(host_label="app1", stats_url="http://x/api/stats", interval=1)
    # 先喂一个基线，再喂正常值，才能看到增量
    ex.collect_once(cfg)
    assert ex.collect_once(cfg) is True
    assert value("app_queue_depth", {"host": "app1"}) == 7
    assert value("app_up", {"host": "app1"}) == 1


def test_collect_failure_counts_error_and_sets_down(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ex._session, "get", boom)
    cfg = ex.Config(host_label="app1", stats_url="http://x/api/stats", interval=1)

    before = value("app_exporter_scrape_errors_total", {"host": "app1"})
    assert ex.collect_once(cfg) is False
    assert value("app_exporter_scrape_errors_total", {"host": "app1"}) == before + 1
    assert value("app_up", {"host": "app1"}) == 0


def test_collect_records_scrape_duration(monkeypatch):
    monkeypatch.setattr(
        ex._session, "get", lambda *a, **k: FakeResp({"requests": 1, "queue_depth": 0})
    )
    cfg = ex.Config(host_label="app1", stats_url="http://x/api/stats", interval=1)
    before = value("app_exporter_scrape_duration_seconds_count")
    ex.collect_once(cfg)
    assert value("app_exporter_scrape_duration_seconds_count") == before + 1


def test_probe_failure_sets_app_down(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(ex._session, "get", boom)
    cfg = ex.Config(host_label="app1", probe_base="http://x", probe_path="/", interval=1)
    before = value("app_exporter_scrape_errors_total", {"host": "app1"})
    ex.probe_once(cfg)
    assert value("app_exporter_scrape_errors_total", {"host": "app1"}) == before + 1
    assert value("app_up", {"host": "app1"}) == 0


# --------------------------------------------------------------------------
# 3.5 代理隔离（Windows 系统代理真踩过：采集 127.0.0.1 被劫持成 502）
# --------------------------------------------------------------------------

def test_session_ignores_env_proxy_by_default():
    ex._configure_session(False)
    assert ex._session.trust_env is False
    assert ex._session.proxies == {"http": None, "https": None}


def test_session_can_opt_into_env_proxy():
    try:
        ex._configure_session(True)
        assert ex._session.trust_env is True
    finally:
        ex._configure_session(False)  # 别把状态漏给后面的用例


# --------------------------------------------------------------------------
# 4. 配置优先级
# --------------------------------------------------------------------------

def test_env_var_defaults(monkeypatch):
    monkeypatch.setenv("APP_EXPORTER_PORT", "9999")
    monkeypatch.setenv("APP_EXPORTER_HOST_LABEL", "app9")
    cfg = ex.Config()
    assert cfg.port == 9999
    assert cfg.host_label == "app9"


def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("APP_EXPORTER_PORT", "9999")
    cfg = ex.Config(port=1234)
    assert cfg.port == 1234


# --------------------------------------------------------------------------
# 5. 序列初始化
# --------------------------------------------------------------------------

def test_init_series_exposes_zero_counters():
    """计数器一直是 0 的时候也得有序列。

    这个 bug 是跑起来才发现的：采集从来没失败过，
    app_exporter_scrape_errors_total 一条序列都没有，Grafana 面板上
    显示成 No data，看着像面板坏了。
    """
    from prometheus_client import generate_latest

    ex.init_series(ex.Config(host_label="app1", probe_path="/"))
    text = generate_latest().decode()

    assert 'app_exporter_scrape_errors_total{host="app1"}' in text
    assert 'app_up{host="app1"}' in text
    assert 'app_request_duration_seconds_count{endpoint="/",host="app1"}' in text
