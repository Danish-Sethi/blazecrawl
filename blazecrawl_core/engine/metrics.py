"""Opt-in, process-local browser-pool metrics."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blazecrawl_core.engine.browser_pool import BrowserPool

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
except ModuleNotFoundError as exc:
    if exc.name != "prometheus_client":
        raise
    raise RuntimeError(
        "METRICS_ENABLED=true requires the metrics extra. Install with "
        '`pip install "blazecrawl-core[metrics]"` or `uv sync --extra metrics`.'
    ) from exc


class BrowserPoolMetrics:
    def __init__(self, pool: BrowserPool) -> None:
        self.registry = CollectorRegistry()
        Gauge(
            "blazecrawl_browser_pool_size",
            "Number of existing browser contexts (idle and active).",
            registry=self.registry,
        ).set_function(lambda: len(pool._all_contexts))
        Gauge(
            "blazecrawl_browser_pool_active_contexts",
            "Number of currently acquired browser contexts.",
            registry=self.registry,
        ).set_function(lambda: len(pool._active_contexts))
        self.errors = Counter(
            "blazecrawl_browser_pool_errors_total",
            "Browser-pool lifecycle errors; intentional cancellation is excluded.",
            registry=self.registry,
        )
        self.page_creation = Histogram(
            "blazecrawl_browser_pool_page_creation_seconds",
            "Page creation and setup latency, including failed and cancelled attempts.",
            registry=self.registry,
        )
