"""Per-request phase timing for slow handlers (log line + Server-Timing).

Only phase names, durations and caller-supplied numbers/labels are
recorded: never request content.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager


class PhaseTimer:
    """Wall-clock seconds spent in named phases of one request.

    A phase entered several times accumulates. ``facts`` holds small
    non-content values (sizes, counts, a mode) that are logged with the
    phases.
    """

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self.phases: dict[str, float] = {}
        self.facts: dict[str, int | str] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - started)

    def add(self, name: str, seconds: float) -> None:
        self.phases[name] = self.phases.get(name, 0.0) + max(seconds, 0.0)

    def total(self) -> float:
        return time.perf_counter() - self._started

    def server_timing(self) -> str:
        """A ``Server-Timing`` header value (milliseconds), total last."""
        entries = [f"{name};dur={secs * 1000:.1f}" for name, secs in self.phases.items()]
        entries.append(f"total;dur={self.total() * 1000:.1f}")
        return ", ".join(entries)

    def log_fields(self) -> str:
        """``name_ms=N`` pairs for every phase, then the facts, then total."""
        fields = [f"{name}_ms={secs * 1000:.0f}" for name, secs in self.phases.items()]
        fields += [f"{key}={value}" for key, value in self.facts.items()]
        fields.append(f"total_ms={self.total() * 1000:.0f}")
        return " ".join(fields)
