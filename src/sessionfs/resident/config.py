"""Resident configuration — loaded from a local TOML file + env overrides.

The operator LLM key lives ONLY in local config/env — it is NEVER sent to
the SessionFS server, never logged, never embedded in the service key.
The SessionFS service key (from the org-profile via profiles.py) is used
ONLY for SessionFS API auth. Two single-purpose credentials that never cross.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class LLMConfig:
    """Operator's own LLM endpoint settings. The api_key is resolved from
    an env-var NAME or a raw inline value — the raw key is NEVER logged."""

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.1"
    api_key: str = field(default="", repr=False)  # raw key or env-var name (resolved at load time)
    api_key_is_env: bool = False  # True → api_key is an env-var name to resolve
    request_timeout_seconds: int = 120
    max_tokens: int = 4096

    def resolve_api_key(self) -> str:
        """Return the actual API key, resolving env-var references."""
        if self.api_key_is_env:
            return os.environ.get(self.api_key, "")
        return self.api_key


def _sessionfs_dir() -> Path:
    return Path.home() / ".sessionfs"


def _residents_dir() -> Path:
    return _sessionfs_dir() / "residents"


@dataclass
class ResidentConfig:
    """Configuration for one resident process.

    Loaded from ``~/.sessionfs/residents/<name>.toml``, with env-var overrides
    for secrets (LLM key, service profile).
    """

    # Identity
    name: str = ""  # the config file stem
    queue_id: str = ""
    project: str = ""  # project_id (proj_...); service-key residents cannot use git remotes

    # SessionFS auth — which named profile provides the service key
    org_profile: str = ""

    # R2 — resident-memory identity (required for the memory endpoints)
    resident_id: str = ""  # res_<hex> — the server-registered resident identity
    org_id: str = ""  # org_<hex> — the org this resident belongs to
    # The resident's REGISTERED persona — used for KB writeback + persona-filtered
    # hydration/de-dup. Must match the persona the resident was registered under
    # (server validates persona_name against project personas).
    persona: str = "codex-reviewer"

    # R3 — implementer settings.
    # The resident mode: 'review' (review_until_clean) or 'implement'
    # (implement_until_done). Default 'review' for back-compat. The runner
    # refuses to process directives that don't match its mode.
    mode: str = "review"  # 'review' | 'implement'
    # Path to the git worktree where the implementer writes code.
    # Required when mode='implement'. Must exist + be a git checkout.
    worktree_path: str = ""
    # Prefix for resident branches (default 'resident').
    resident_branch_prefix: str = "resident"
    # The CLEAN base a new resident branch is cut from (so a ticket's proposal
    # never inherits a prior resident branch's commits). Default 'main'.
    base_branch: str = "main"

    # Polling
    poll_interval_seconds: int = 30  # clamped [10, 300]

    # R2 — mind bounding
    mind_token_budget: int = 8000  # client-side cap for the warm digest
    compact_every_wakes: int = 10  # compact every N wakes

    # R4 — LLM cost bounding (0 = unlimited). Fail-closed parking when exhausted.
    daily_token_budget: int = 0  # daily LLM-token ceiling across restarts
    per_wake_token_budget: int = 0  # per-wake reserve (don't start a wake we can't afford)

    # LLM
    llm: LLMConfig = field(default_factory=LLMConfig)

    # Derived — resolved at load time
    _resolved_llm_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        """Auto-switch persona default based on mode (R3 identity separation)."""
        if self.mode == "implement" and self.persona == "codex-reviewer":
            self.persona = "atlas"

    @classmethod
    def from_toml(cls, name: str) -> ResidentConfig:
        """Load a resident config from ``~/.sessionfs/residents/<name>.toml``.

        Env overrides:
        - ``RESIDENT_LLM_API_KEY`` — inline LLM key (takes precedence over TOML)
        - ``RESIDENT_LLM_API_KEY_ENV`` — name of env var holding the LLM key
        """
        path = _residents_dir() / f"{name}.toml"
        if not path.exists():
            raise FileNotFoundError(
                f"Resident config not found: {path}\n"
                f"Create it with a [resident] section + [llm] section."
            )

        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Could not parse resident config {path}: {exc}") from exc

        resident_raw = raw.get("resident", {}) if isinstance(raw, dict) else {}

        # LLM section
        llm_raw = raw.get("llm", {}) if isinstance(raw, dict) else {}
        llm = LLMConfig(
            base_url=str(llm_raw.get("base_url", "https://api.openai.com/v1")),
            model=str(llm_raw.get("model", "gpt-5.1")),
            api_key=str(llm_raw.get("api_key", "")),
            api_key_is_env=bool(llm_raw.get("api_key_is_env", False)),
            request_timeout_seconds=int(llm_raw.get("request_timeout_seconds", 120)),
            max_tokens=int(llm_raw.get("max_tokens", 4096)),
        )

        poll = int(resident_raw.get("poll_interval_seconds", 30))
        poll = max(10, min(300, poll))

        # R3: only set persona if explicitly in the TOML; otherwise let the
        # dataclass default + __post_init__ handle mode-based default.
        toml_persona = str(resident_raw.get("persona", ""))
        cfg = cls(
            name=name,
            queue_id=str(resident_raw.get("queue_id", "")),
            project=str(resident_raw.get("project", "")),
            org_profile=str(resident_raw.get("org_profile", "")),
            resident_id=str(resident_raw.get("resident_id", "")),
            org_id=str(resident_raw.get("org_id", "")),
            persona=toml_persona if toml_persona else "codex-reviewer",
            poll_interval_seconds=poll,
            mind_token_budget=int(resident_raw.get("mind_token_budget", 8000)),
            daily_token_budget=int(resident_raw.get("daily_token_budget", 0)),
            per_wake_token_budget=int(resident_raw.get("per_wake_token_budget", 0)),
            compact_every_wakes=int(resident_raw.get("compact_every_wakes", 10)),
            llm=llm,
            mode=str(resident_raw.get("mode", "review")),
            worktree_path=str(resident_raw.get("worktree_path", "")),
            base_branch=str(resident_raw.get("base_branch", "main")),
            resident_branch_prefix=str(
                resident_raw.get("resident_branch_prefix", "resident")
            ),
        )
        cfg.resolve_llm_key()
        return cfg

    def resolve_llm_key(self) -> None:
        """Apply the RESIDENT_LLM_API_KEY / RESIDENT_LLM_API_KEY_ENV env
        overrides to self.llm and set self._resolved_llm_key. Shared by
        from_toml AND the ad-hoc CLI path so env-var config works in BOTH modes
        (previously the no-config path skipped this and always failed on the
        missing-key check even with the env var set)."""
        env_key = os.environ.get("RESIDENT_LLM_API_KEY")
        if env_key:
            self.llm.api_key = env_key
            self.llm.api_key_is_env = False
        else:
            env_key_name = os.environ.get("RESIDENT_LLM_API_KEY_ENV")
            if env_key_name:
                self.llm.api_key = env_key_name
                self.llm.api_key_is_env = True
        self._resolved_llm_key = self.llm.resolve_api_key()

    def estimated_call_tokens(self) -> int:
        """A conservative CONFIG-TIME estimate of one full LLM call's charged
        tokens (completion cap + prompt). Used to validate the per-wake reserve
        and to reserve headroom before each call.

        This is deliberately a fixed heuristic, not a per-directive measurement:
        the actual directive payload (ticket text, comment delta, findings) is
        only known at runtime, and completion tokens can't be known before the
        call. The budget is the SAFETY NET, not a precise meter — it fails
        CLOSED (parks) whenever the reserve can't be guaranteed, and record()
        charges the estimate when a provider omits usage, so real overspend is
        bounded to roughly one call's worth. Operators size the budget with
        headroom accordingly. In implement mode the prompt also carries up to
        ~20 hydrated file bodies (~8000 chars ≈ 2000 tokens each)."""
        prompt_est = self.mind_token_budget
        # A fixed allowance for the DIRECTIVE payload beyond the mind digest —
        # ticket text, comment delta, and review verdict/findings.
        prompt_est += 4000
        if self.mode == "implement":
            prompt_est += 20 * 2000
        return self.llm.max_tokens + prompt_est

    def validate(self) -> list[str]:
        """Validate required fields. Returns a list of error messages (empty = valid)."""
        errors: list[str] = []
        if not self.queue_id:
            errors.append("resident.queue_id is required")
        if not self.project:
            errors.append("resident.project is required (a project_id, proj_...)")
        elif not self.project.startswith("proj_"):
            errors.append(
                f"resident.project must be a project_id (proj_...), not "
                f"'{self.project}' — service-key residents cannot resolve git "
                f"remotes. Find the id with `sfs project list`."
            )
        if not self.org_profile:
            errors.append("resident.org_profile is required (named profile for service key)")
        if not self._resolved_llm_key:
            errors.append(
                "LLM API key is not set. Set it via the [llm] section's api_key field, "
                "or the RESIDENT_LLM_API_KEY env var, or RESIDENT_LLM_API_KEY_ENV env var."
            )
        # R2: resident_id + org_id enable the private-memory endpoints, but they
        # are OPTIONAL — without them R2 memory is simply disabled (hydrate uses
        # cold context, reasoning writes skip) and the R1 reviewer loop still
        # works. Validate FORMAT when present; require BOTH together (memory
        # needs both) — but never hard-require them.
        if self.resident_id and not self.resident_id.startswith("res_"):
            errors.append(
                f"resident.resident_id must start with 'res_', got '{self.resident_id}'"
            )
        if self.org_id and not self.org_id.startswith("org_"):
            errors.append(
                f"resident.org_id must start with 'org_', got '{self.org_id}'"
            )
        if bool(self.resident_id) != bool(self.org_id):
            errors.append(
                "resident.resident_id and resident.org_id must be set together "
                "(both are required to enable R2 private memory)"
            )
        if self.compact_every_wakes < 1:
            errors.append("resident.compact_every_wakes must be >= 1")
        if self.mind_token_budget < 500:
            errors.append("resident.mind_token_budget must be >= 500")
        if self.daily_token_budget < 0:
            errors.append("resident.daily_token_budget must be >= 0 (0 = unlimited)")
        if self.per_wake_token_budget < 0:
            errors.append("resident.per_wake_token_budget must be >= 0 (0 = unlimited)")
        if (
            self.daily_token_budget
            and self.per_wake_token_budget
            and self.per_wake_token_budget > self.daily_token_budget
        ):
            errors.append(
                "resident.per_wake_token_budget must not exceed daily_token_budget"
            )
        if self.daily_token_budget and not self.per_wake_token_budget:
            # Without a per-wake cap, a daily ceiling can be overshot by one full
            # unrestricted LLM call before the next wake parks — so the per-wake
            # bound is REQUIRED whenever a daily budget is set.
            errors.append(
                "resident.per_wake_token_budget is required when "
                "daily_token_budget is set (it bounds a single wake's spend so "
                "the daily ceiling can't be overshot by one unrestricted call)."
            )
        if self.per_wake_token_budget:
            # The per-wake reserve must cover a whole CHARGED call (completion +
            # full prompt), else one call overshoots the ceiling before record()
            # notices. Same estimate the runner reserves before each call.
            _min_call = self.estimated_call_tokens()
            if self.per_wake_token_budget < _min_call:
                errors.append(
                    f"resident.per_wake_token_budget "
                    f"({self.per_wake_token_budget}) must be at least an "
                    f"estimated full-call cost (llm.max_tokens + prompt ≈ "
                    f"{_min_call}) in {self.mode} mode, so the per-wake reserve "
                    f"covers a whole charged LLM call."
                )
        # R3: implementer validation.
        if self.mode not in ("review", "implement"):
            errors.append(
                f"resident.mode must be 'review' or 'implement', got '{self.mode}'"
            )
        if self.mode == "implement":
            if not self.worktree_path:
                errors.append(
                    "resident.worktree_path is required when mode='implement'"
                )
            else:
                worktree = Path(self.worktree_path).expanduser()
                if not worktree.is_dir():
                    errors.append(
                        f"resident.worktree_path does not exist: {worktree}"
                    )
                elif not (worktree / ".git").exists():
                    # Accept BOTH a normal repo (.git is a directory) AND a
                    # linked worktree from `git worktree add` (.git is a FILE
                    # pointing at the common git dir) — operators commonly
                    # provide the latter.
                    errors.append(
                        f"resident.worktree_path is not a git checkout: "
                        f"{worktree}"
                    )
            if self.persona == "codex-reviewer":
                # The implementer must not use the reviewer persona (identity
                # separation, invariant 4). Default to 'atlas' unless the
                # operator explicitly configured a different non-reviewer persona.
                errors.append(
                    "resident.persona should not be 'codex-reviewer' in "
                    "implement mode — use a non-reviewer persona like 'atlas' "
                    "(identity separation, invariant 4)."
                )
        if self.resident_branch_prefix and "/" in self.resident_branch_prefix.strip("/"):
            errors.append(
                "resident.resident_branch_prefix must be a single path segment "
                "(no intermediate '/' — the full branch is prefix/queue/ticket)."
            )
        return errors

    @property
    def resolved_llm_key(self) -> str:
        return self._resolved_llm_key
