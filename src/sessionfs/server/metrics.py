"""In-process Prometheus metrics registry (GitHub #90).

No third-party dependency: a small thread-safe registry of Counter / Gauge /
Histogram (fixed buckets) with label sets validated against a declared
allowlist, rendered as Prometheus text exposition format 0.0.4. Metrics are
per-process; operators scrape every replica and aggregate with ``sum()``.

Every label value is a bounded vocabulary chosen at the call site (route
template, outcome, size bucket, severity, reason) — never a raw path, user id,
project id or session id. A per-metric series cap drops observations past the
limit instead of growing memory without bound (cardinality guard).
"""

from __future__ import annotations

import threading

# Content-Type for the exposition format. Prometheus's text parser accepts
# the ``version=0.0.4`` parameter.
CONTENT_TYPE = "text/plain; version=0.0.4"

# Per-metric cap on the number of distinct label series. Every call site uses
# a bounded label vocabulary, so this is a backstop against an accidental
# unbounded label value, not a runtime knob operators are expected to tune.
DEFAULT_MAX_SERIES = 10_000


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text format (backslash, newline,
    quote)."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _float_text(value: float) -> str:
    """Render a float the way Prometheus expects: integral values keep a
    trailing ``.0`` so they stay type-stable when reparsed."""
    if value == int(value) and abs(value) < 1e16:
        return f"{int(value)}.0"
    return repr(float(value))


def status_class(status: int) -> str:
    """The ``status_class`` label value for a response status code."""
    return f"{status // 100}xx"


def size_bucket(size: int) -> str:
    """The ``size_bucket`` label value for a pushed payload size in bytes."""
    mb = 1024 * 1024
    if size < mb:
        return "<1MB"
    if size < 10 * mb:
        return "1-10MB"
    if size < 50 * mb:
        return "10-50MB"
    if size < 100 * mb:
        return "50-100MB"
    return ">100MB"


def push_outcome(status: int, detail: object = None) -> str:
    """Map a sync-push result to the bounded ``outcome`` label vocabulary."""
    if status in (200, 201):
        return "ok"
    if status == 409:
        return "conflict"
    if status == 413:
        return "rejected_quota"
    error = None
    if isinstance(detail, dict):
        error = detail.get("error") or detail.get("code")
    if error in ("dlp_blocked", "dlp_uninspectable"):
        return "rejected_dlp"
    return "error"


# HTTP methods the request-duration histogram may record verbatim. Anything
# else (PROPFIND, TRACE, CONNECT, …) collapses to "other" so an anonymous
# caller cannot mint a series for every arbitrary verb the server echoes.
_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def method_label(method: str) -> str:
    """The ``method`` label value: a known verb, else ``"other"``."""
    return method if method in _KNOWN_METHODS else "other"


# DLP severities and policy actions are bounded vocabularies. Severity in
# particular comes from tenant-defined custom-pattern text, so clamp it (and
# defensively the action) rather than echoing an unbounded value into a label.
_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
_ACTIONS = frozenset({"block", "redact", "warn"})


def severity_label(severity: str) -> str:
    """Clamp a DLP finding severity to the known set, else ``"other"``."""
    return severity if severity in _SEVERITIES else "other"


def action_label(action: str) -> str:
    """Clamp a DLP policy action to the known set, else ``"other"``."""
    return action if action in _ACTIONS else "other"


def push_size_bytes(bytes_in: int | None, content_length: str | None = None) -> int:
    """The payload size to bucket a sync push by.

    ``bytes_in`` is the number of body bytes actually read. When the body was
    never read (a 413 rejection from the Content-Length header alone), fall
    back to the declared header so the push still lands in the right size
    bucket instead of ``<1MB``.
    """
    if bytes_in is not None:
        return bytes_in
    if content_length:
        try:
            value = int(content_length)
        except ValueError:
            return 0
        return value if value >= 0 else 0
    return 0


class _Labels:
    """Label allowlist + canonical key ordering for one metric."""

    def __init__(self, metric_name: str, label_names: tuple[str, ...]) -> None:
        self.metric_name = metric_name
        self.label_names = label_names

    def validate(self, labels: dict[str, str]) -> None:
        unknown = sorted(set(labels) - set(self.label_names))
        missing = sorted(set(self.label_names) - set(labels))
        if unknown or missing:
            raise ValueError(
                f"{self.metric_name}: expected labels {sorted(self.label_names)}; "
                f"got unknown={unknown} missing={missing}"
            )

    def key(self, labels: dict[str, str]) -> tuple[str, ...]:
        self.validate(labels)
        return tuple(str(labels[name]) for name in self.label_names)


class _Metric:
    def __init__(
        self,
        name: str,
        help: str,
        type_: str,
        label_names: tuple[str, ...] = (),
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        self.name = name
        self.help = help
        self.type = type_
        self._labels = _Labels(name, label_names)
        self._lock = threading.Lock()
        self._max_series = max_series
        self._dropped = 0

    @property
    def label_names(self) -> tuple[str, ...]:
        return self._labels.label_names

    def _format_labels(self, key: tuple[str, ...], extra: tuple[tuple[str, str], ...] = ()) -> str:
        parts = [
            f'{name}="{_escape_label_value(value)}"'
            for name, value in zip(self.label_names, key)
        ]
        parts.extend(f'{name}="{_escape_label_value(value)}"' for name, value in extra)
        if not parts:
            return ""
        return "{" + ",".join(parts) + "}"

    def _accept_key(self, key: tuple[str, ...], existing: dict) -> bool:
        # O(1): dict membership + len, so the guard is cheap on the hot path.
        if key in existing or len(existing) < self._max_series:
            return True
        self._dropped += 1
        METRICS_SERIES_DROPPED.inc()
        return False


class Counter(_Metric):
    def __init__(
        self,
        name: str,
        help: str,
        label_names: tuple[str, ...] = (),
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        super().__init__(name, help, "counter", label_names, max_series)
        self._values: dict[tuple[str, ...], float] = {}
        if not label_names:
            # Unlabelled counters render 0 from process start (and after a
            # reset) so operators see them even before the first increment.
            self._values[()] = 0.0

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._labels.key(labels)
        with self._lock:
            if not self._accept_key(key, self._values):
                return
            self._values[key] = self._values.get(key, 0.0) + amount

    def _series(self) -> list[tuple[tuple[str, ...], float]]:
        with self._lock:
            return sorted(self._values.items())

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape_help(self.help)}", f"# TYPE {self.name} counter"]
        for key, value in self._series():
            lines.append(f"{self.name}{self._format_labels(key)} {_float_text(value)}")
        return lines


class Gauge(_Metric):
    def __init__(
        self,
        name: str,
        help: str,
        label_names: tuple[str, ...] = (),
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        super().__init__(name, help, "gauge", label_names, max_series)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = self._labels.key(labels)
        with self._lock:
            if not self._accept_key(key, self._values):
                return
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._labels.key(labels)
        with self._lock:
            if not self._accept_key(key, self._values):
                return
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def _series(self) -> list[tuple[tuple[str, ...], float]]:
        with self._lock:
            return sorted(self._values.items())

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape_help(self.help)}", f"# TYPE {self.name} gauge"]
        for key, value in self._series():
            lines.append(f"{self.name}{self._format_labels(key)} {_float_text(value)}")
        return lines


class Histogram(_Metric):
    def __init__(
        self,
        name: str,
        help: str,
        buckets: tuple[float, ...],
        label_names: tuple[str, ...] = (),
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        super().__init__(name, help, "histogram", label_names, max_series)
        if not buckets:
            raise ValueError(f"{name}: histogram needs at least one bucket")
        self._buckets = tuple(sorted(float(b) for b in buckets))
        self._counts: dict[tuple[str, ...], int] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        # Bucket cumulative counts, aligned with self._buckets (each is the
        # count of observations <= that upper bound).
        self._bucket_counts: dict[tuple[str, ...], list[int]] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._labels.key(labels)
        with self._lock:
            if not self._accept_key(key, self._counts):
                return
            self._counts[key] = self._counts.get(key, 0) + 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            counts = self._bucket_counts.setdefault(key, [0] * len(self._buckets))
            for i, upper in enumerate(self._buckets):
                if value <= upper:
                    counts[i] += 1

    def _series(self) -> list[tuple[tuple[str, ...], int, float, list[int]]]:
        with self._lock:
            out = []
            for key, count in self._counts.items():
                out.append((key, count, self._sums[key], self._bucket_counts[key]))
            return sorted(out)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape_help(self.help)}", f"# TYPE {self.name} histogram"]
        for key, count, total, bucket_counts in self._series():
            for upper, cumulative in zip(self._buckets, bucket_counts):
                lines.append(
                    f"{self.name}_bucket{self._format_labels(key, (('le', _float_text(upper)),))} "
                    f"{_float_text(float(cumulative))}"
                )
            lines.append(
                f"{self.name}_bucket{self._format_labels(key, (('le', '+Inf'),))} "
                f"{_float_text(float(count))}"
            )
            lines.append(f"{self.name}_sum{self._format_labels(key)} {_float_text(total)}")
            lines.append(f"{self.name}_count{self._format_labels(key)} {_float_text(float(count))}")
        return lines


# --- The process-wide registry -------------------------------------------------

_REGISTRY: list[_Metric] = []


def _register(metric: _Metric) -> _Metric:
    _REGISTRY.append(metric)
    return metric


def reset_registry() -> None:
    """Reset every metric's state (tests, or a fresh worker fork)."""
    for metric in _REGISTRY:
        with metric._lock:
            metric._dropped = 0
            if isinstance(metric, Histogram):
                metric._counts = {}
                metric._sums = {}
                metric._bucket_counts = {}
            else:
                metric._values = {}
                if isinstance(metric, Counter) and not metric.label_names:
                    metric._values[()] = 0.0


def render_metrics() -> str:
    """Render every registered metric as Prometheus text format 0.0.4."""
    lines: list[str] = []
    for metric in _REGISTRY:
        lines.extend(metric.render())
    return "\n".join(lines) + ("\n" if lines else "")


# --- Declared metrics ----------------------------------------------------------
#
# Label values are bounded vocabularies. Do NOT add a label whose value could
# be a raw path, id or other unbounded input.

HTTP_REQUEST_DURATION = _register(
    Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds.",
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        label_names=("method", "route", "status_class"),
    )
)

PUSH_TOTAL = _register(
    Counter(
        "sessionfs_push_total",
        "Session pushes by outcome and size bucket.",
        label_names=("outcome", "size_bucket"),
    )
)

SYNC_PULL_TOTAL = _register(
    Counter("sessionfs_sync_pull_total", "Session pulls.")
)

HANDOFF_CREATE_TOTAL = _register(
    Counter("sessionfs_handoff_create_total", "Handoffs created.")
)

HANDOFF_CLAIM_TOTAL = _register(
    Counter("sessionfs_handoff_claim_total", "Handoffs claimed.")
)

DLP_FINDINGS_TOTAL = _register(
    Counter(
        "sessionfs_dlp_findings_total",
        "DLP findings by severity and policy action.",
        label_names=("severity", "action"),
    )
)

AUTH_FAILURES_TOTAL = _register(
    Counter(
        "sessionfs_auth_failures_total",
        "Authentication failures by reason.",
        label_names=("reason",),
    )
)

# Samples dropped by the per-metric series cap, across every metric. Unlabelled
# so it renders 0 from process start.
METRICS_SERIES_DROPPED = _register(
    Counter(
        "sessionfs_metrics_series_dropped_total",
        "Samples dropped by the per-metric series cap (cardinality guard).",
    )
)

# DB-derived gauges — set at scrape time (see routes/metrics.py).
ACTIVE_USERS = _register(
    Gauge(
        "sessionfs_active_users",
        "Active users (distinct users with a sync in the window).",
        label_names=("window",),
    )
)

SESSIONS = _register(
    Gauge("sessionfs_sessions", "Total non-deleted sessions.")
)

DB_POOL_SIZE = _register(
    Gauge("sessionfs_db_pool_size", "Configured database connection pool size.")
)

DB_POOL_CHECKED_OUT = _register(
    Gauge("sessionfs_db_pool_checked_out", "Database connections currently checked out.")
)

DB_POOL_OVERFLOW = _register(
    Gauge("sessionfs_db_pool_overflow", "Database connection pool overflow connections.")
)
