"""LLM-spend bounding for resident runners (R4).

Per-wake + daily token ceilings with FAIL-CLOSED parking: when the daily budget
is exhausted the runner stops making LLM calls (parks) rather than spending
unbounded. Daily spend persists to a small state file so it survives a
restart within the same UTC day; it resets automatically at the UTC date change.

Token counts come from the LLM response's `usage` block (see
llm_adapter._extract_tokens). A budget of 0 means "unlimited".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("sessionfs.resident.budget")


class BudgetTracker:
    """Tracks per-wake + daily LLM token spend with fail-closed parking."""

    def __init__(
        self,
        name: str,
        *,
        daily_token_budget: int = 0,
        per_wake_token_budget: int = 0,
        per_call_estimate: int = 0,
        state_dir: Path | None = None,
    ) -> None:
        self._name = name or "resident"
        self._daily = max(0, int(daily_token_budget))
        self._per_wake = max(0, int(per_wake_token_budget))
        # Estimated cost of ONE full call — reserved before continuing, since a
        # call's actual spend is known only AFTER it returns.
        self._per_call = max(0, int(per_call_estimate))
        base = state_dir or (Path.home() / ".sessionfs" / "residents")
        # Sanitize the name for the filename (defense — config already validates).
        safe = "".join(c for c in self._name if c.isalnum() or c in ("-", "_")) or "resident"
        self._state_path = base / f"{safe}-budget.json"
        self._date = self._today()
        self._spent = 0
        self._wake_spent = 0  # cumulative spend for the CURRENT heartbeat
        # If persisting spend fails, the daily ceiling can't survive a restart —
        # fail closed (park) rather than silently stop enforcing the budget.
        self._persist_ok = True
        # Set if the current-day state file exists but can't be parsed.
        self._load_corrupt = False
        self._load()
        # PROBE writability up front (when a budget is set) so an unwritable
        # state dir fails closed from the FIRST call, not only after one save.
        if self._daily:  # only daily spend is persisted
            self._save()

    # ── Internal ────────────────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> None:
        if not self._state_path.exists():
            return  # first run — no prior spend to restore
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            date = data["date"]
            spent = int(data["spent"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            # The file EXISTS but is truncated/malformed — we can't trust the
            # prior spend, so FAIL CLOSED: treat today as already exhausted
            # (self-heals at the UTC date rollover) rather than resetting to 0
            # and forgetting spend across a same-day restart.
            logger.error(
                "Budget state unreadable (%s) — treating today as exhausted "
                "(fail closed).", exc,
            )
            self._spent = self._daily if self._daily else 0
            self._load_corrupt = True
            return
        # Only adopt persisted spend if it's for TODAY (else a new day → 0).
        if date == self._date:
            self._spent = max(0, spent)

    def _save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"date": self._date, "spent": self._spent}),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)
            self._persist_ok = True
        except OSError as exc:
            logger.error(
                "Could not persist budget state (%s) — the daily ceiling can no "
                "longer survive a restart; failing closed.", exc,
            )
            self._persist_ok = False

    def _roll_date(self) -> None:
        today = self._today()
        if today != self._date:
            self._date = today
            self._spent = 0
            self._wake_spent = 0
            # Yesterday's corrupt state must not block today — self-heal.
            self._load_corrupt = False

    # ── Public API ──────────────────────────────────────────────────────

    def begin_wake(self) -> None:
        """Reset the per-heartbeat spend counter. Call once at the START of each
        wake so per_wake_token_budget caps the WHOLE heartbeat (all directives),
        not a single LLM call."""
        self._roll_date()
        self._wake_spent = 0

    def can_wake(self) -> tuple[bool, str]:
        """Fail-closed check BEFORE an LLM call. Returns (ok, reason); ok=False
        means PARK. Checked before EACH directive so the per-heartbeat cap +
        daily ceiling both hold across a multi-directive wake.

        Reserves a FULL CALL's estimated cost before continuing — a call's
        actual spend is only known after it returns, so we must never START one
        that could push past a ceiling. If no per-call estimate is configured,
        a reserve of 1 makes the checks degrade to plain thresholds.
        """
        self._roll_date()
        # Fail closed if a budget is configured but its spend couldn't be
        # persisted — an un-persistable ceiling isn't enforceable across a
        # restart, so park rather than pretend it holds.
        if self._daily and not self._persist_ok:
            return False, (
                "budget state could not be persisted — failing closed (fix the "
                "state directory permissions to resume)"
            )
        if self._daily and self._load_corrupt:
            return False, (
                "budget state was unreadable — failing closed for today (remove "
                "the corrupt state file or wait for the UTC date rollover)"
            )
        reserve = self._per_call if self._per_call > 0 else 1
        if self._daily and self._spent + reserve > self._daily:
            return False, (
                f"insufficient daily budget for a full call "
                f"({self._spent}+{reserve} > {self._daily})"
            )
        if self._per_wake and self._wake_spent + reserve > self._per_wake:
            return False, (
                f"insufficient per-wake budget for a full call this heartbeat "
                f"({self._wake_spent}+{reserve} > {self._per_wake})"
            )
        return True, ""

    def record(self, tokens: int) -> None:
        """Record tokens consumed by an LLM call (daily + this heartbeat) +
        persist the daily total. Call this ONCE per completed LLM call."""
        self._roll_date()
        t = max(0, int(tokens))
        if t == 0 and self._per_call > 0:
            # A call was made but the provider returned no usable `usage` block.
            # Charge the conservative per-call ESTIMATE (fail closed) so the
            # budget still advances + eventually parks — missing usage must not
            # mean free, unbounded calls.
            logger.warning(
                "LLM call reported no token usage — charging the per-call "
                "estimate (%d) to keep the budget bounded.", self._per_call,
            )
            t = self._per_call
        self._spent += t
        self._wake_spent += t
        self._save()
        if self._per_wake and self._wake_spent > self._per_wake:
            logger.warning(
                "Heartbeat used %d tokens, over the per-wake budget of %d.",
                self._wake_spent, self._per_wake,
            )

    @property
    def spent_today(self) -> int:
        self._roll_date()
        return self._spent

    @property
    def wake_spent(self) -> int:
        """Tokens recorded during the CURRENT heartbeat (reset by begin_wake).
        > 0 means an LLM call actually ran this wake."""
        return self._wake_spent

    @property
    def daily_budget(self) -> int:
        return self._daily
