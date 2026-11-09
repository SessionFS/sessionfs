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


# --- Per-tenant labels (SFS_METRICS_TENANT_LABELS) ------------------------------
#
# A metric declared with ``tenant_labels`` takes optional ``org_id`` /
# ``project_id`` keyword labels. They are recorded only while the process-wide
# switch is on (a self-hosted opt-in, default off); otherwise they are dropped
# at the call so the default output aggregates across tenants. Only ids are
# ever accepted — never names — and a value that doesn't look like an id is
# dropped rather than echoed.

TENANT_LABEL_NAMES: tuple[str, ...] = ("org_id", "project_id")
_tenant_labels_enabled = False
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def set_tenant_labels(enabled: bool) -> None:
    """Turn per-tenant (org_id / project_id) labels on or off for this process."""
    global _tenant_labels_enabled
    _tenant_labels_enabled = bool(enabled)


def tenant_labels_enabled() -> bool:
    return _tenant_labels_enabled


def _tenant_value(value: object) -> str:
    """An id-shaped tenant label value, else ``""`` (rendered as absent)."""
    if not _tenant_labels_enabled or not isinstance(value, str):
        return ""
    if not value or len(value) > 64 or not set(value) <= _ID_CHARS:
        return ""
    return value


class _Labels:
    """Label allowlist + canonical key ordering for one metric."""

    def __init__(
        self,
        metric_name: str,
        label_names: tuple[str, ...],
        tenant_labels: bool = False,
    ) -> None:
        self.metric_name = metric_name
        self.base_names = label_names
        self.tenant = tenant_labels
        self.label_names = label_names + (TENANT_LABEL_NAMES if tenant_labels else ())

    def validate(self, labels: dict[str, str]) -> None:
        allowed = set(self.label_names)
        unknown = sorted(set(labels) - allowed)
        missing = sorted(set(self.base_names) - set(labels))
        if unknown or missing:
            raise ValueError(
                f"{self.metric_name}: expected labels {sorted(self.base_names)}"
                + (f" (+ optional {list(TENANT_LABEL_NAMES)})" if self.tenant else "")
                + f"; got unknown={unknown} missing={missing}"
            )

    def key(self, labels: dict[str, str]) -> tuple[str, ...]:
        self.validate(labels)
        base = tuple(str(labels[name]) for name in self.base_names)
        if not self.tenant:
            return base
        return base + tuple(_tenant_value(labels.get(name)) for name in TENANT_LABEL_NAMES)


class _Metric:
    def __init__(
        self,
        name: str,
        help: str,
        type_: str,
        label_names: tuple[str, ...] = (),
        max_series: int = DEFAULT_MAX_SERIES,
        tenant_labels: bool = False,
    ) -> None:
        self.name = name
        self.help = help
        self.type = type_
        self._labels = _Labels(name, label_names, tenant_labels)
        self._lock = threading.Lock()
        self._max_series = max_series
        self._dropped = 0

    @property
    def label_names(self) -> tuple[str, ...]:
        return self._labels.label_names

    @property
    def base_label_names(self) -> tuple[str, ...]:
        """The required (closed-vocabulary) labels, without tenant labels."""
        return self._labels.base_names

    @property
    def has_tenant_labels(self) -> bool:
        return self._labels.tenant

    def _format_labels(self, key: tuple[str, ...], extra: tuple[tuple[str, str], ...] = ()) -> str:
        tenant = set(TENANT_LABEL_NAMES) if self._labels.tenant else set()
        parts = [
            f'{name}="{_escape_label_value(value)}"'
            for name, value in zip(self.label_names, key)
            # An unset tenant label is absent, not an empty-string label.
            if not (name in tenant and value == "")
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
        tenant_labels: bool = False,
    ) -> None:
        super().__init__(name, help, "counter", label_names, max_series, tenant_labels)
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
        tenant_labels: bool = False,
    ) -> None:
        super().__init__(name, help, "gauge", label_names, max_series, tenant_labels)
        self._values: dict[tuple[str, ...], float] = {}

    def clear(self) -> None:
        """Drop every series (a DB-derived gauge rebuilt from scratch)."""
        with self._lock:
            self._values = {}

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
        tenant_labels: bool = False,
    ) -> None:
        super().__init__(name, help, "histogram", label_names, max_series, tenant_labels)
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


# --- Business metrics (0.30.0, GitHub #131) ------------------------------------
#
# Every label below is a CLOSED vocabulary. Values arrive through
# ``clamp_label`` so anything outside the vocabulary collapses to ``"other"``.
# Never add a user-typed name (persona, template / suite / policy slug, step
# name) or an id as a label here: ids are only ever the optional tenant labels,
# and only when SFS_METRICS_TENANT_LABELS is on. No dollar metric exists on
# purpose — tokens only.

OTHER = "other"


def clamp_label(value: object, vocabulary: tuple[str, ...]) -> str:
    """``value`` if it is in ``vocabulary``, else ``"other"``."""
    return value if isinstance(value, str) and value in vocabulary else OTHER


# Sweep route template → sweep label. Every route guarded by
# ``require_platform_sweeper`` is listed (a test fails on drift).
SWEEP_ROUTES: dict[str, str] = {
    "/api/v1/admin/sweeps/org-digest": "org-digest",
    "/api/v1/admin/sweeps/ticket-notifications": "ticket-notifications",
    "/api/v1/admin/sweeps/kb-compile": "kb-compile",
    "/api/v1/admin/sweeps/jira-sync": "jira-sync",
    "/api/v1/admin/sweeps/confluence-publish": "confluence-publish",
    "/api/v1/admin/sweeps/helper-retention": "helper-retention",
    "/api/v1/admin/sweeps/meeting-retention": "meeting-retention",
    "/api/v1/admin/sweeps/contact-retention": "contact-retention",
    "/api/v1/admin/sweeps/eval-outputs": "eval-outputs",
    "/api/v1/admin/sweeps/reminders-due": "reminders-due",
    "/api/v1/admin/sweeps/attention-hides": "attention-hides",
    "/api/v1/admin/sweeps/idempotency-keys": "idempotency-keys",
    "/api/v1/admin/github-webhook-deliveries/sweep": "github-webhook-deliveries",
    "/api/v1/admin/github-webhook-deliveries/purge": "github-webhook-deliveries",
    "/api/v1/admin/skills/verify": "skills-verify",
    "/api/v1/admin/skills/catalogs/regenerate": "skills-catalogs-regenerate",
    "/api/v1/admin/skills/rescan": "skills-rescan",
    "/api/v1/admin/marketplace/freshness/sweep": "marketplace-freshness",
    "/api/v1/admin/marketplace/publishers/recheck": "marketplace-publishers-recheck",
}
SWEEP_NAMES: tuple[str, ...] = tuple(sorted(set(SWEEP_ROUTES.values()))) + (OTHER,)
SWEEP_RESULTS: tuple[str, ...] = ("ok", "rejected", "error")
SWEEP_DENIAL_REASONS: tuple[str, ...] = (
    "unauthenticated",
    "rate_limited",
    "auth_rejected",
    "not_admin",
    "insufficient_scope",
    "owner_not_admin",
    "minter_not_admin",
    "minter_missing",
    OTHER,
)


def sweep_result(status: int | None) -> str:
    """Map an authorized sweep's response status (None = raised) to a result."""
    if status is None or status >= 500:
        return "error"
    if status >= 400:
        return "rejected"
    return "ok"


JIRA_DIRECTIONS: tuple[str, ...] = ("outbound", "inbound")
# outbound: the outbox delivery outcome; inbound: what the webhook did.
JIRA_SYNC_RESULTS: tuple[str, ...] = (
    "done",
    "skipped",
    "failed",
    "exhausted",
    "applied",
    "ignored",
    "duplicate",
    OTHER,
)

CONFLUENCE_RESULTS: tuple[str, ...] = (
    "published",
    "in_progress",
    "drifted",
    "conflict",
    "too_large",
    "timeout",
    "rejected",
    "error",
)

# Webhook route template → source label.
WEBHOOK_ROUTES: dict[str, str] = {
    "/webhooks/github": "github",
    "/webhooks/gitlab": "gitlab",
    "/api/v1/webhooks/jira/{org_id}": "jira",
    "/api/v1/webhooks/github/{org_id}": "delivery_github",
    "/api/v1/webhooks/gitlab/{org_id}": "delivery_gitlab",
    "/webhooks/stripe": "stripe",
}
WEBHOOK_SOURCES: tuple[str, ...] = tuple(sorted(set(WEBHOOK_ROUTES.values())))
WEBHOOK_RESULTS: tuple[str, ...] = (
    "ok",
    "unauthorized",
    "not_found",
    "too_large",
    "rejected",
    "unavailable",
    "error",
)


def webhook_result(status: int | None) -> str:
    """Map a webhook response status (None = the handler raised) to a result."""
    if status is None:
        return "error"
    if status < 400:
        return "ok"
    if status in (401, 403):
        return "unauthorized"
    if status == 404:
        return "not_found"
    if status == 413:
        return "too_large"
    if status == 503:
        return "unavailable"
    if status < 500:
        return "rejected"
    return "error"


TOKEN_KINDS: tuple[str, ...] = ("input", "output", "cache_read", "cache_write")


def _provider_ids() -> tuple[str, ...]:
    from sessionfs.judge.provider_registry import PROVIDERS

    return tuple(p.id for p in PROVIDERS)


MODEL_FAMILIES: tuple[str, ...] = _provider_ids() + (OTHER,)


def model_family(provider: object, model: object) -> str:
    """Bucket a run's provider/model into the provider registry's ids.

    The stored ``provider`` wins when it is a registry id; otherwise the model
    name is matched against the registry's detection prefixes (a
    ``vendor/model`` name counts as that vendor when the vendor is a registry
    id). Anything else is ``"other"`` — never the raw provider or model text.
    """
    from sessionfs.judge.provider_registry import PROVIDER_BY_ID, PROVIDERS

    if isinstance(provider, str):
        pid = provider.strip().lower()
        if pid in PROVIDER_BY_ID:
            return pid
    if isinstance(model, str) and model.strip():
        name = model.strip().lower()
        if "/" in name:
            vendor = name.split("/", 1)[0]
            return vendor if vendor in PROVIDER_BY_ID else OTHER
        for p in PROVIDERS:
            if any(name.startswith(prefix) for prefix in p.detect_prefixes):
                return p.id
    return OTHER


AGENT_RUN_STATUSES: tuple[str, ...] = ("passed", "failed", "errored", "cancelled")
AGENT_ROLES: tuple[str, ...] = (
    "implementer",
    "ui-implementer",
    "reviewer",
    "design-reviewer",
    "security-reviewer",
    "release",
    "grader",
    "none",
    OTHER,
)

APPROVAL_STATES: tuple[str, ...] = ("pending", "approved", "rejected", "expired", "superseded")
APPROVAL_DECISIONS: tuple[str, ...] = ("approved", "rejected")
REVIEW_LOOP_ITEM_STATUSES: tuple[str, ...] = (
    "pending",
    "active",
    "waiting",
    "done",
    "failed",
    "needs_human",
)

# Long-running jobs: sweeps and sync lags span seconds to hours.
_LONG_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)
_LAG_BUCKETS = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, 4 * 3600.0, 24 * 3600.0)
_DECISION_BUCKETS = (
    60.0, 300.0, 900.0, 3600.0, 4 * 3600.0, 24 * 3600.0, 3 * 86400.0, 7 * 86400.0, 14 * 86400.0,
)

SWEEP_RUNS_TOTAL = _register(
    Counter(
        "sessionfs_sweep_runs_total",
        "Authorized sweep runs by sweep and result (ok / rejected / error).",
        label_names=("sweep", "result"),
    )
)

SWEEP_DURATION = _register(
    Histogram(
        "sessionfs_sweep_duration_seconds",
        "Wall-clock duration of authorized sweep runs.",
        buckets=_LONG_BUCKETS,
        label_names=("sweep",),
    )
)

SWEEP_LAG = _register(
    Gauge(
        "sessionfs_sweep_lag_seconds",
        "Age of the oldest due item when the sweep last ran on this replica (0 = nothing due).",
        label_names=("sweep",),
    )
)

SWEEP_DENIED_TOTAL = _register(
    Counter(
        "sessionfs_sweep_denied_total",
        "Sweep calls refused before the sweep ran, by bounded reason.",
        label_names=("sweep", "reason"),
    )
)

JIRA_SYNC_TOTAL = _register(
    Counter(
        "sessionfs_jira_sync_total",
        "Jira two-way sync events by direction and result.",
        label_names=("direction", "result"),
        tenant_labels=True,
    )
)

JIRA_SYNC_LAG = _register(
    Histogram(
        "sessionfs_jira_sync_lag_seconds",
        "Jira sync lag: outbound = queued to delivered; inbound = Jira event time to applied.",
        buckets=_LAG_BUCKETS,
        label_names=("direction",),
        tenant_labels=True,
    )
)

CONFLUENCE_PUBLISH_TOTAL = _register(
    Counter(
        "sessionfs_confluence_publish_total",
        "Confluence page publishes (manual and swept) by result.",
        label_names=("result",),
        tenant_labels=True,
    )
)

WEBHOOK_INGEST_TOTAL = _register(
    Counter(
        "sessionfs_webhook_ingest_total",
        "Inbound webhook deliveries by source and result.",
        label_names=("source", "result"),
    )
)

AGENT_TOKENS_TOTAL = _register(
    Counter(
        "sessionfs_agent_tokens_total",
        "Agent tokens reported on agent runs, by token kind and model family.",
        label_names=("token_kind", "model_family"),
        tenant_labels=True,
    )
)

AGENT_RUNS_TOTAL = _register(
    Counter(
        "sessionfs_agent_runs_total",
        "Agent runs that reached a terminal status, by status and role.",
        label_names=("status", "role"),
        tenant_labels=True,
    )
)

APPROVAL_REQUESTS = _register(
    Gauge(
        "sessionfs_approval_requests",
        "Approval requests by state (DB-derived, cached 30 s).",
        label_names=("status",),
        tenant_labels=True,
    )
)

APPROVAL_DECISION_SECONDS = _register(
    Histogram(
        "sessionfs_approval_decision_seconds",
        "Time from an approval request being raised to it being approved or rejected.",
        buckets=_DECISION_BUCKETS,
        label_names=("decision",),
        tenant_labels=True,
    )
)

REVIEW_LOOP_ITEMS = _register(
    Gauge(
        "sessionfs_review_loop_items",
        "Review-loop work-queue items by status (DB-derived, cached 30 s).",
        label_names=("status",),
        tenant_labels=True,
    )
)

# Route classes capped per org (services/org_concurrency.py).
CONCURRENCY_ROUTE_CLASSES: tuple[str, ...] = ("extraction", "helper", "compile", "export")

ORG_CONCURRENCY_REJECTED_TOTAL = _register(
    Counter(
        "sessionfs_org_concurrency_rejected_total",
        "Requests refused with 429 org_concurrency_limit, by route class.",
        label_names=("route_class",),
        tenant_labels=True,
    )
)

# Meeting / chat extraction: the server's own share of an extraction, i.e.
# wall time minus the time spent waiting on the provider call. Measured so an
# SLO can hold the server to account without a slow model eating the budget.
EXTRACTION_KINDS: tuple[str, ...] = ("meeting", "chat", OTHER)
EXTRACTION_RESULTS: tuple[str, ...] = ("done", "failed", OTHER)
_SERVER_WORK_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

EXTRACTION_SERVER_SECONDS = _register(
    Histogram(
        "sessionfs_extraction_server_seconds",
        "Meeting/chat extraction time spent in the server, excluding the provider call.",
        buckets=_SERVER_WORK_BUCKETS,
        label_names=("kind", "result"),
    )
)

# Closed vocabulary per label of each business family. The cardinality test
# multiplies these (tenant labels excluded: they are off by default).
BUSINESS_LABEL_VOCABULARIES: dict[str, dict[str, tuple[str, ...]]] = {
    "sessionfs_sweep_runs_total": {"sweep": SWEEP_NAMES, "result": SWEEP_RESULTS},
    "sessionfs_sweep_duration_seconds": {"sweep": SWEEP_NAMES},
    "sessionfs_sweep_lag_seconds": {"sweep": SWEEP_NAMES},
    "sessionfs_sweep_denied_total": {"sweep": SWEEP_NAMES, "reason": SWEEP_DENIAL_REASONS},
    "sessionfs_jira_sync_total": {"direction": JIRA_DIRECTIONS, "result": JIRA_SYNC_RESULTS},
    "sessionfs_jira_sync_lag_seconds": {"direction": JIRA_DIRECTIONS},
    "sessionfs_confluence_publish_total": {"result": CONFLUENCE_RESULTS},
    "sessionfs_webhook_ingest_total": {"source": WEBHOOK_SOURCES, "result": WEBHOOK_RESULTS},
    "sessionfs_agent_tokens_total": {"token_kind": TOKEN_KINDS, "model_family": MODEL_FAMILIES},
    "sessionfs_agent_runs_total": {"status": AGENT_RUN_STATUSES, "role": AGENT_ROLES},
    "sessionfs_approval_requests": {"status": APPROVAL_STATES},
    "sessionfs_approval_decision_seconds": {"decision": APPROVAL_DECISIONS},
    "sessionfs_review_loop_items": {"status": REVIEW_LOOP_ITEM_STATUSES},
    "sessionfs_org_concurrency_rejected_total": {"route_class": CONCURRENCY_ROUTE_CLASSES},
    "sessionfs_extraction_server_seconds": {
        "kind": EXTRACTION_KINDS,
        "result": EXTRACTION_RESULTS,
    },
}
