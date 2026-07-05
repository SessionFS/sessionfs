"""The resident reviewer runner — a long-running client process.

R1: reviewer-only skeleton. Drives the work-queue heartbeat, calls the
operator's own LLM, and settles verdicts via the settle-path.

Properties:
- Credential boundary: operator LLM key = LOCAL ONLY, never to SessionFS;
  SessionFS service key = SessionFS ONLY, never to the LLM provider.
- Fail-closed on LLM error: never settle a clean verdict when the adapter fails.
- Lease-fenced: passes directive_id + lease_epoch back exactly as received.
- Client NEVER sets author_persona or verdict_trusted — server derives both.
- Graceful shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal

from sessionfs.profiles import resolve_auth
from sessionfs.resident.client import (
    run_work_queue_step,
    complete_work_queue_step,
)
from sessionfs.resident.config import ResidentConfig
from sessionfs.resident.llm_adapter import (
    ReviewLLM,
    ReviewContext,
    ReviewResult,
    OpenAICompatibleAdapter,
)

logger = logging.getLogger("sessionfs.resident.runner")

# Keys/credentials that must NEVER appear in logs.
_SENSITIVE_KEYS = {"api_key", "authorization", "bearer", "token", "secret", "password"}

# The settle endpoint's verdict_content field is capped at 20000 chars; the
# runner truncates below that so a long review can't 422 and loop the item.
_MAX_VERDICT_CONTENT = 20000


def _sanitize_for_log(data: dict | None) -> dict:
    """Return a shallow copy with sensitive values redacted."""
    if data is None:
        return {}
    safe: dict = {}
    for k, v in data.items():
        if k.lower() in _SENSITIVE_KEYS:
            safe[k] = "***"
        elif isinstance(v, str) and len(v) > 200:
            safe[k] = v[:200] + "..."
        else:
            safe[k] = v
    return safe


class ResidentRunner:
    """Long-running resident that drives a review_until_clean work queue.

    Usage:
        config = ResidentConfig.from_toml("my-reviewer")
        runner = ResidentRunner(config)
        await runner.run()       # loop forever
        # or:
        await runner.run_once()  # single heartbeat (for testing)
    """

    def __init__(
        self,
        config: ResidentConfig,
        llm_adapter: ReviewLLM | None = None,
    ) -> None:
        self._config = config
        self._running = False
        self._adapter = llm_adapter

        # Auth state — resolved once on start.
        self._api_url: str = ""
        self._api_key: str = ""
        self._project_id: str = ""

    # ── Public API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the main loop until SIGINT/SIGTERM."""
        self._setup_signals()
        self._resolve_auth()
        await self._resolve_project()
        self._init_adapter()

        logger.info(
            "Resident %r starting — queue=%s project=%s poll=%ds",
            self._config.name,
            self._config.queue_id,
            self._project_id,
            self._config.poll_interval_seconds,
        )

        self._running = True
        try:
            while self._running:
                await self._wake()
                if self._running:
                    await asyncio.sleep(self._config.poll_interval_seconds)
        finally:
            logger.info("Resident %r stopped.", self._config.name)

    async def run_once(self) -> list[dict]:
        """Run a single heartbeat and return the settle results.
        Used for testing / ``--once`` mode."""
        self._resolve_auth()
        await self._resolve_project()
        self._init_adapter()

        logger.info(
            "Resident %r running once — queue=%s project=%s",
            self._config.name,
            self._config.queue_id,
            self._project_id,
        )
        return await self._wake()

    # ── Internal ────────────────────────────────────────────────────────

    def _setup_signals(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_shutdown)
            except NotImplementedError:
                # Windows — signal handlers not supported for SIGTERM
                pass

    def _handle_shutdown(self) -> None:
        logger.info("Shutting down (signal received)...")
        self._running = False

    def _resolve_auth(self) -> None:
        """Resolve the SessionFS service key from the named org profile.

        A resident MUST run under its bound service key (the trusted-reviewer
        boundary). resolve_auth() prefers an ambient SESSIONFS_API_KEY over
        SESSIONFS_PROFILE, so when an org_profile is configured we temporarily
        remove any ambient key to force the configured profile identity —
        otherwise a stray key in the operator's shell would silently run the
        resident under the wrong identity.

        If credentials are already set (e.g. injected for testing), skip.
        """
        if self._api_key and self._api_url:
            return

        if self._config.org_profile:
            saved_profile = os.environ.get("SESSIONFS_PROFILE")
            saved_env_key = os.environ.pop("SESSIONFS_API_KEY", None)
            os.environ["SESSIONFS_PROFILE"] = self._config.org_profile
            try:
                auth = resolve_auth()
            finally:
                # Restore BOTH env vars so the override never leaks into later
                # resolve_auth() calls in the same process.
                if saved_env_key is not None:
                    os.environ["SESSIONFS_API_KEY"] = saved_env_key
                if saved_profile is None:
                    os.environ.pop("SESSIONFS_PROFILE", None)
                else:
                    os.environ["SESSIONFS_PROFILE"] = saved_profile
        else:
            auth = resolve_auth()

        if not auth.api_key:
            raise RuntimeError(
                "No SessionFS API key found. Set up your org profile with "
                "`sfs auth login --profile <name>` and reference it in the "
                "resident config's `org_profile` field."
            )
        # Identity boundary: a resident MUST run under its EXACT configured
        # profile. resolve_auth() silently ignores an invalid SESSIONFS_PROFILE
        # (e.g. a typo) and falls back to the persisted/default profile — which
        # would post trusted reviews under the wrong identity. Refuse the
        # fallback rather than run as the wrong service key.
        if (
            self._config.org_profile
            and auth.profile_name != self._config.org_profile
        ):
            raise RuntimeError(
                f"Resident org_profile '{self._config.org_profile}' did not "
                f"resolve to that profile (got '{auth.profile_name}'). A "
                f"resident must run under its exact configured identity — "
                f"verify the profile exists with `sfs auth profiles`."
            )
        self._api_url = auth.api_url
        self._api_key = auth.api_key
        logger.info("Auth resolved — source=%s profile=%s", auth.source, auth.profile_name)

    async def _resolve_project(self) -> None:
        """Resolve the project identifier to a project_id.

        Residents authenticate with a SERVICE key. The work-queue routes
        address projects by id, and the only git-remote→id resolver
        (`GET /projects/{remote}`) is a USER-key route that rejects service
        keys — so a resident MUST be configured with a project_id (proj_...)
        directly. A git remote is rejected with clear guidance rather than
        failing obscurely on the first heartbeat."""
        if self._project_id:
            return
        project = self._config.project.strip()
        if not project:
            raise RuntimeError("resident.project is required in config")
        if project.startswith("proj_"):
            self._project_id = project
            return
        raise RuntimeError(
            f"resident.project must be a project_id (proj_...), not a git remote "
            f"('{project}'). Service-key residents cannot resolve git remotes; "
            f"find the id with `sfs project list` and set it in the config."
        )

    def _init_adapter(self) -> None:
        if self._adapter is not None:
            return
        llm_key = self._config.resolved_llm_key
        if not llm_key:
            raise RuntimeError(
                "LLM API key is not configured. Set RESIDENT_LLM_API_KEY "
                "or configure [llm].api_key in the resident config TOML."
            )
        self._adapter = OpenAICompatibleAdapter(self._config.llm)

    async def _wake(self) -> list[dict]:
        """One heartbeat: step → for each directive → review → settle."""
        results: list[dict] = []

        # 1. Call the heartbeat.
        step = await run_work_queue_step(
            self._api_url,
            self._api_key,
            self._project_id,
            self._config.queue_id,
            wake_source="resident",
            wake_ref=self._config.name,
        )

        logger.debug("Step response: status=%s", step.status_code)

        if step.status_code == 429:
            logger.warning("Rate-limited; sleeping.")
            return results
        if step.status_code >= 400 or not isinstance(step.body, dict):
            logger.error(
                "Step failed (HTTP %s): %s",
                step.status_code,
                _sanitize_for_log(step.body if isinstance(step.body, dict) else None),
            )
            return results

        status = step.body.get("status", "ok")

        if status == "idle":
            logger.debug("Idle — nothing to do (cadence).")
            return results
        if status == "stopped":
            reason = step.body.get("reason", "unknown")
            logger.info("Queue stopped: %s", reason)
            # Only TERMINAL queue states end the long-running process. A
            # `queue_empty` on an active selector/auto-adopt queue is temporary
            # — new matching tickets may appear later — so keep polling rather
            # than exiting permanently.
            if reason in ("paused", "completed", "cancelled"):
                self._running = False
            return results

        # 2. Process directives.
        directives = step.body.get("directives", [])
        if not isinstance(directives, list):
            logger.warning("Unexpected directives shape: %s", type(directives))
            return results

        for directive in directives:
            if not isinstance(directive, dict):
                continue

            intent = directive.get("intent", "")
            if intent != "post_review":
                # The item is already claimed (open_directive_id set). A bare
                # `continue` would leave the lease open and the queue would
                # re-emit the same item forever. Release it by settling failed.
                logger.warning(
                    "Non-review directive '%s' on a reviewer runner — releasing "
                    "the lease (this resident only handles post_review).",
                    intent,
                )
                results.append(
                    await self._release_directive(
                        directive, reason=f"unsupported_intent:{intent}"
                    )
                )
                continue

            result = await self._process_review_directive(directive)
            results.append(result)

        return results

    async def _release_directive(self, directive: dict, reason: str) -> dict:
        """Settle a directive the reviewer cannot handle so its lease is
        released (failed → the item backs off) instead of being re-emitted
        indefinitely."""
        settle_resp = await complete_work_queue_step(
            self._api_url,
            self._api_key,
            self._project_id,
            self._config.queue_id,
            item_id=directive.get("item_id", "?"),
            directive_id=directive.get("directive_id", "?"),
            ticket_id=directive.get("ticket_id", "?"),
            outcome="posted_review",
            ticket_lease_epoch=directive.get("ticket_lease_epoch"),
            failed=True,
            summary=f"Resident reviewer cannot handle directive: {reason}",
        )
        return {
            "ticket_id": directive.get("ticket_id"),
            "directive_id": directive.get("directive_id"),
            "settled": False,
            "released": reason,
            "settle_status": settle_resp.status_code,
        }

    async def _process_review_directive(self, directive: dict) -> dict:
        """Process one post_review directive: build context → review → settle."""
        ticket_id = directive.get("ticket_id", "?")
        directive_id = directive.get("directive_id", "?")
        item_id = directive.get("item_id", "?")
        lease_epoch = directive.get("ticket_lease_epoch")

        logger.info("Reviewing ticket=%s directive=%s", ticket_id, directive_id)

        # Build bounded review context from the directive payload.
        context = _build_review_context(directive)

        # Fail-closed: never review — and never settle a clean verdict over —
        # an empty payload. If the directive carried no new comments, there is
        # nothing to certify; skip the settle and wait for the next wake rather
        # than risk posting VERIFIED-CLEAN over nothing.
        if not context.new_comments:
            logger.info(
                "No new comments to review for ticket=%s directive=%s; "
                "skipping settle.",
                ticket_id,
                directive_id,
            )
            return {
                "ticket_id": ticket_id,
                "directive_id": directive_id,
                "settled": False,
                "skipped": "no_new_comments",
            }

        # Call the operator's OWN LLM (client-side, operator key).
        assert self._adapter is not None
        try:
            result = await self._adapter.review(context)
        except Exception as exc:
            # Fail-closed: a buggy adapter that raises instead of returning
            # a ReviewResult with error set must not crash the loop.
            logger.error(
                "LLM adapter raised for ticket=%s: %s",
                ticket_id,
                exc,
            )
            result = ReviewResult(error=f"LLM adapter exception: {exc}")

        if result.error:
            # Fail-closed: do NOT settle a clean verdict.
            logger.error(
                "LLM review failed for ticket=%s: %s",
                ticket_id,
                result.error,
            )
            # Settle as failed so the item backs off rather than sticking.
            settle_resp = await complete_work_queue_step(
                self._api_url,
                self._api_key,
                self._project_id,
                self._config.queue_id,
                item_id=item_id,
                directive_id=directive_id,
                ticket_id=ticket_id,
                outcome="posted_review",
                ticket_lease_epoch=lease_epoch,
                failed=True,
                summary=f"LLM adapter error: {result.error[:500]}",
            )
            return {
                "ticket_id": ticket_id,
                "directive_id": directive_id,
                "settled": False,
                "error": result.error,
                "settle_status": settle_resp.status_code,
            }

        # Assemble verdict_content — the full review text the server
        # will post as a verdict comment (stamping author_persona +
        # verdict_trusted server-side).
        verdict_content = _format_verdict_content(result, directive)

        # Settle via the settle-path. The client NEVER sets
        # author_persona or verdict_trusted — the server derives both.
        settle_resp = await complete_work_queue_step(
            self._api_url,
            self._api_key,
            self._project_id,
            self._config.queue_id,
            item_id=item_id,
            directive_id=directive_id,
            ticket_id=ticket_id,
            outcome="posted_review",
            ticket_lease_epoch=lease_epoch,
            verdict_content=verdict_content,
            verdict=result.verdict_phrase.split("\n")[0][:50],
        )

        if settle_resp.status_code == 409:
            logger.warning(
                "Stale lease on settle for ticket=%s directive=%s; "
                "directive will re-emit next wake.",
                ticket_id,
                directive_id,
            )
        elif settle_resp.status_code >= 400:
            logger.error(
                "Settle failed (HTTP %s) for ticket=%s: %s",
                settle_resp.status_code,
                ticket_id,
                _sanitize_for_log(
                    settle_resp.body if isinstance(settle_resp.body, dict) else None
                ),
            )

        return {
            "ticket_id": ticket_id,
            "directive_id": directive_id,
            "settled": settle_resp.status_code in (200, 201),
            "verdict": result.verdict_phrase.split("\n")[0],
            "settle_status": settle_resp.status_code,
        }


def _build_review_context(directive: dict) -> ReviewContext:
    """Extract a bounded ReviewContext from the directive payload.

    Maps the ACTUAL server directive schema (services/work_queues.py
    `_build_directive`): ticket metadata is under ``ticket`` (id/title/status/
    kind/priority/assigned_to — no description) and the comments to review are
    under ``comment_delta`` (the bounded new-comment delta), NOT the
    ``ticket_title`` / ``new_comments`` fields an earlier draft assumed. Getting
    this wrong sent the LLM an empty payload while still allowing a clean
    verdict. All fields are pointers/metadata, never code contents (C7).
    """
    ticket_meta = directive.get("ticket") or {}
    ctx = ReviewContext(
        ticket_id=directive.get("ticket_id", "") or ticket_meta.get("id", ""),
        ticket_title=ticket_meta.get("title", ""),
        directive_id=directive.get("directive_id", ""),
        item_id=directive.get("item_id", ""),
        review_state=directive.get("review_state") or {},
        new_comments=directive.get("comment_delta") or [],
        # The server directive carries minimal ticket metadata (no description /
        # acceptance criteria); the review substance is the comment_delta and
        # review_state. `expand_hints` is the server's on-demand tool menu
        # (get_ticket, list_ticket_comments, …), NOT changed files — mapping it
        # into file_refs would render tool names under "Files:" and mislead the
        # reviewer about what changed. The directive carries no changed-path
        # metadata, so file_refs stays empty.
        ticket_description="",
        acceptance_criteria=[],
        file_refs=[],
    )
    return ctx


# The server writes verdict_content VERBATIM into the trusted verdict comment,
# and the review-state oracle (services/review_state.py `_HEADER_RE`) only
# counts a comment as a verdict when its first line matches
# `Codex R{N} review on tk_X: <verdict>`. The runner MUST emit exactly that
# header or a clean resident verdict is silently ignored and never closes the
# review_until_clean loop.
_PRIOR_ROUND_RE = re.compile(
    r"Codex\s+R(\d+)\s+(?:doc\s+)?review\s+on\s+tk_", re.IGNORECASE
)


def _next_review_round(comment_delta: list) -> int:
    """The next review round number = 1 + the highest prior Codex round seen in
    the visible comment delta (so the resident's verdict sorts as the latest
    round in the server oracle). Defaults to 1 when no prior round is visible."""
    highest = 0
    for c in comment_delta:
        if not isinstance(c, dict):
            continue
        m = _PRIOR_ROUND_RE.search(str(c.get("content", "")))
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def _format_verdict_content(result: ReviewResult, directive: dict) -> str:
    """Format the verdict comment body the server posts verbatim.

    The FIRST line must match the review-state parser's header
    (`Codex R{N} review on tk_X: <verdict>`) or the verdict is ignored.
    """
    ticket_meta = directive.get("ticket") or {}
    ticket_id = directive.get("ticket_id", "") or ticket_meta.get("id", "")
    # Prefer the server-computed review_round (derived over the FULL thread);
    # the bounded comment_delta usually omits prior rounds, so deriving from it
    # would repeat R1 and never let a later clean round close the findings. Fall
    # back to the delta only for older servers that don't send review_round.
    round_n = directive.get("review_round")
    if not isinstance(round_n, int) or round_n < 1:
        round_n = _next_review_round(directive.get("comment_delta") or [])
    verdict_lines = result.verdict_phrase.split("\n")
    verdict_first = verdict_lines[0].strip() if verdict_lines else ""
    is_clean = (
        re.sub(r"[\s_-]+", "_", verdict_first.upper()) == "VERIFIED_CLEAN"
    )

    # Parseable header line (round + ticket + verdict).
    parts: list[str] = [f"Codex R{round_n} review on {ticket_id}: {verdict_first}"]

    if is_clean:
        # Isolate free-form reasoning behind a findings-end marker
        # (`_FINDINGS_END_RE` in review_state.py matches "no change needed"), so
        # a stray "- LOW: …" line in the reasoning is NOT parsed as a NEW open
        # finding on the clean round — which would block the loop from closing
        # despite VERIFIED-CLEAN. The findings-scan region (header→marker) is
        # kept empty for a clean verdict.
        parts.append("")
        parts.append("Verified clean — no change needed.")
        if result.reasoning:
            parts.append("")
            parts.append(result.reasoning)
    else:
        # CHANGES: the finding detail lines ARE the findings (scanned by the
        # oracle); reasoning follows.
        extra = [ln for ln in verdict_lines[1:] if ln.strip()]
        if extra:
            parts.append("")
            parts.extend(extra)
        if result.reasoning:
            parts.append("")
            parts.append(result.reasoning)

    content = "\n".join(parts)
    # The settle endpoint caps verdict_content at 20000 chars; truncate to stay
    # under it. A 422 would leave the directive lease open and loop the item.
    # The parseable header (line 0) is always preserved.
    if len(content) > _MAX_VERDICT_CONTENT:
        content = (
            content[: _MAX_VERDICT_CONTENT - 60].rstrip()
            + "\n\n… (verdict truncated to fit the length limit)"
        )
    return content
