"""Finds collectors: the built-in ones plus any listed in `METRICS_EXTRA_COLLECTORS`.

Extra collectors are named by import path (`package.module:ClassName`), so a show can
keep a private collector in its own package without changing the engine.
"""

import importlib

from core.config import settings
from metrics.collectors.base import Collector
from metrics.models import MetricSpec

# Import paths rather than imports, so a broken optional dependency in one collector
# can't stop the others from loading.
BUILTIN_COLLECTORS: dict[str, str] = {
    "op3": "metrics.collectors.op3:OP3Collector",
    "cloudflare": "metrics.collectors.cloudflare:CloudflareCollector",
    "x": "metrics.collectors.x:XCollector",
    "instagram": "metrics.collectors.instagram:InstagramCollector",
    "youtube": "metrics.collectors.youtube:YouTubeCollector",
    "devto": "metrics.collectors.devto:DevtoCollector",
    "substack": "metrics.collectors.substack:SubstackCollector",
}


def load_collector_class(import_path: str) -> type[Collector]:
    """Import `module:ClassName` and check it's a Collector subclass."""
    module_name, sep, class_name = import_path.partition(":")
    if not sep or not module_name or not class_name:
        raise ValueError(f"Collector path {import_path!r} must look like 'package.module:ClassName'.")
    cls = getattr(importlib.import_module(module_name), class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, Collector)):
        raise TypeError(f"{import_path} is not a metrics.collectors.base.Collector subclass.")
    return cls


def available_collectors() -> dict[str, type[Collector]]:
    """Every known collector class keyed by its `name` (built-ins first, then extras)."""
    found: dict[str, type[Collector]] = {}
    for import_path in [*BUILTIN_COLLECTORS.values(), *settings.metrics_extra_collectors]:
        cls = load_collector_class(import_path)
        found[cls.name] = cls
    return found


def select_collectors(names: list[str] | None = None) -> list[Collector]:
    """Instantiate the requested collectors: `names`, else `METRICS_SOURCES`, else all of them."""
    available = available_collectors()
    wanted = names or settings.metrics_sources or list(available)
    unknown = [n for n in wanted if n not in available]
    if unknown:
        raise ValueError(f"Unknown metrics source(s) {unknown}; available: {sorted(available)}")
    return [available[n]() for n in wanted]


def metric_specs() -> dict[tuple[str, str], MetricSpec]:
    """(source, metric) → presentation spec, gathered from every known collector."""
    return {
        (name, metric): spec
        for name, cls in available_collectors().items()
        for metric, spec in cls.metrics.items()
    }
