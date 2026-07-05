"""The resident runner — a long-running client process.

R1: reviewer-only skeleton. Drives the work-queue heartbeat, calls the
operator's own LLM, and settles verdicts via the settle-path.
R2: living context — hydrates durable + private context before each review,
writes durable memory back after, and compacts periodically.
R3: implementer mode — processes implement/fix_findings directives, writes
code to a worktree, posts diff-refs, settles waiting_review.

Properties:
- Credential boundary: operator LLM key = LOCAL ONLY, never to SessionFS;
  SessionFS service key = SessionFS ONLY, never to the LLM provider.
- Fail-closed on LLM error: never settle a clean verdict when the adapter fails.
- Lease-fenced: passes directive_id + lease_epoch back exactly as received.
- Client NEVER sets author_persona or verdict_trusted — server derives both.
- Graceful shutdown on SIGINT/SIGTERM.
- No-op writeback discipline: a wake that learns nothing durable writes nothing
  to KB/wiki.
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
from sessionfs.resident.context import (
    LivingContext,
    hydrate_living_context,
)
from sessionfs.resident.implementer import run_implement_directive
from sessionfs.resident.llm_adapter import (
    ReviewLLM,
    ReviewContext,
    ReviewResult,
    OpenAICompatibleAdapter,
    ImplementLLM,
)
from sessionfs.resident.memory import (
    write_reasoning,
    writeback_durable_knowledge,
    compact_memory,
    summarize_for_digest,
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
    """Long-running resident that drives a work queue.

    Supports two modes (config.mode):
    - 'review': drives a review_until_clean queue; calls ReviewLLM.review().
    - 'implement': drives an implement_until_done queue; calls
      ImplementLLM.implement().

    Usage:
        config = ResidentConfig.from_toml("my-resident")
        runner = ResidentRunner(config)
        await runner.run()       # loop forever
        # or:
        await runner.run_once()  # single heartbeat (for testing)
    """

    def __init__(
        self,
        config: ResidentConfig,
        llm_adapter: ReviewLLM | None = None,
        implement_adapter: ImplementLLM | None = None,
    ) -> None:
        self._config = config
        self._running = False
        self._adapter = llm_adapter
        self._implement_adapter = implement_adapter

        # Auth state — resolved once on start.
        self._api_url: str = ""
        self._api_key: str = ""
        self._project_id: str = ""

        # R2 — living mind state.
        self._living_context: LivingContext | None = None
        self._wake_count: int = 0
        # Buffer of recent reasoning entries (as dicts) for compaction.
        self._reasoning_buffer: list[dict] = []

    # ── Public API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the main loop until SIGINT/SIGTERM."""
        self._setup_signals()
        self._resolve_auth()
        await self._resolve_project()
        self._init_adapter()

        # R2 — hydrate the living context on boot.
        await self._hydrate()

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

        # R2 — hydrate before the wake.
        await self._hydrate()

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

    # ── R2 — living mind: hydrate / writeback / compact ───────────────────

    async def _hydrate(self) -> None:
        """Hydrate the living context from durable SessionFS sources.
        Gracefully degrades if the memory endpoints are unreachable."""
        try:
            self._living_context = await hydrate_living_context(
                self._api_url, self._api_key, self._config
            )
        except Exception as exc:
            logger.warning(
                "Hydration failed — continuing with cold context: %s", exc
            )
            self._living_context = LivingContext()

    def _enrich_with_living_context(self, context: ReviewContext) -> ReviewContext:
        """Enrich a ReviewContext with the living mind fields.

        The LLM adapter formats these into the prompt so the reviewer
        reasons WITH its accumulated knowledge, not cold.
        """
        if self._living_context is None:
            return context

        lc = self._living_context
        if lc.memory_digest:
            context.ticket_description = (
                f"[Resident memory digest]\n{lc.memory_digest}\n\n"
                f"{context.ticket_description}"
            )
        if lc.recent_reasoning:
            context.ticket_description = (
                "[Recent reasoning]\n" + "\n".join(
                    f"- {r}" for r in lc.recent_reasoning[:5]
                ) + f"\n\n{context.ticket_description}"
            )
        if lc.prior_findings:
            context.ticket_description = (
                "[Prior findings by this reviewer]\n" + "\n".join(
                    f"- {f}" for f in lc.prior_findings[:5]
                ) + f"\n\n{context.ticket_description}"
            )
        if lc.review_playbook:
            context.ticket_description = (
                f"[Review playbook]\n{lc.review_playbook[:1000]}\n\n"
                f"{context.ticket_description}"
            )
        if lc.project_context_summary:
            context.ticket_description = (
                f"[Project context]\n{lc.project_context_summary}\n\n"
                f"{context.ticket_description}"
            )
        if lc.sections:
            # The compiled-context sections (architecture / conventions /
            # security …) are the resident's service-key-accessible project
            # context — include them or the reviewer prompt loses that context.
            rendered = "\n\n".join(
                f"## {slug}\n{body}" for slug, body in lc.sections.items() if body
            )
            if rendered:
                context.ticket_description = (
                    f"[Project context sections]\n{rendered}\n\n"
                    f"{context.ticket_description}"
                )
        return context

    async def _writeback_after_review(
        self,
        ticket_id: str,
        verdict_text: str,
        result: ReviewResult,
    ) -> None:
        """Write rolling reasoning + durable KB findings after a successful review.

        No-op discipline: if the review surfaced nothing durable, KB/wiki
        writes are skipped entirely. The verdict comment itself is NOT a
        writeback — it stays on the settle-path.
        """
        # 1. Rolling reasoning — always write a short conclusion.
        conclusion = _extract_conclusion(result, ticket_id)
        entry_id = await write_reasoning(
            self._api_url, self._api_key, self._config,
            ticket_id=ticket_id,
            conclusion=conclusion,
        )
        if entry_id:
            self._reasoning_buffer.append({
                "content": conclusion,
                "ticket_id": ticket_id,
                "id": entry_id,  # server id — superseded at compaction
            })
        else:
            # 429 from memory write — trigger compaction.
            logger.info("Reasoning write hit cap — triggering compaction.")
            await self._maybe_compact(force=True)

        # 2. Durable KB writeback — only if the review found something durable.
        durable = _extract_durable_findings(result)
        if durable:
            written = await writeback_durable_knowledge(
                self._api_url, self._api_key, self._config,
                findings=durable,
            )
            if written > 0:
                logger.info(
                    "Wrote %d durable finding(s) for ticket=%s.",
                    written, ticket_id,
                )

    async def _maybe_compact(self, force: bool = False) -> None:
        """Compact the resident's private memory if the wake count threshold
        has been reached, or if forced (e.g. after a 429 cap signal)."""
        lc = self._living_context
        hydrated_ids = list(lc.recent_reasoning_ids) if lc is not None else []

        # Nothing to compact only if the local buffer is empty AND (on a forced
        # compact) there are no hydrated live entries to supersede either. A
        # forced compact after a RESTART cap-hit has an empty buffer but live
        # server entries — it must still be able to free them.
        if not self._reasoning_buffer and not (force and hydrated_ids):
            logger.debug("No reasoning entries to compact.")
            return

        if not force and self._wake_count % self._config.compact_every_wakes != 0:
            return

        prior_digest = lc.memory_digest if lc is not None else ""
        # Digest content: prefer the buffered reasoning; on a forced compact
        # with an empty buffer (restart cap-hit) summarize the hydrated recent
        # reasoning so a digest is still produced.
        reasoning_for_digest = self._reasoning_buffer or [
            {"content": c} for c in (lc.recent_reasoning if lc is not None else [])
        ]
        digest = summarize_for_digest(reasoning_for_digest, prior_digest)

        # Supersede the reasoning entries this process wrote (ids captured at
        # write time) OR, when the buffer is empty (restart), the hydrated live
        # entries — otherwise the server's F6 cap (which counts non-superseded
        # entries) never frees. The server caps superseded_entry_ids at 200, so
        # reserve a slot for the prior digest and only supersede/CLEAR what fits;
        # any overflow stays in the buffer for the NEXT compaction rather than
        # leaking live on the server with its id lost.
        _SUPERSEDE_CAP = 200
        digest_id = (
            lc.memory_digest_id if (lc is not None and lc.memory_digest_id) else None
        )
        room = _SUPERSEDE_CAP - (1 if digest_id else 0)

        buffer_ids = [e["id"] for e in self._reasoning_buffer if e.get("id")]
        # On a FORCED compact (cap-hit), supersede the hydrated live entries too
        # — a near-cap restart plus one local write must free the cap in a
        # SINGLE compaction, not leave the server exactly at the cap. Dedupe
        # while preserving order. Periodic (non-forced) compaction only needs
        # this process's own buffered entries.
        if force:
            candidate_ids = list(dict.fromkeys(buffer_ids + hydrated_ids))
        else:
            candidate_ids = buffer_ids
        sent_reasoning_ids = candidate_ids[:room]

        superseded_ids: list[str] = list(sent_reasoning_ids)
        # ALSO supersede the prior digest — the new digest folds it in.
        if digest_id:
            superseded_ids.append(digest_id)

        ok = await compact_memory(
            self._api_url, self._api_key, self._config,
            digest_content=digest,
            superseded_entry_ids=superseded_ids,
        )
        if ok:
            # Clear ONLY the buffer entries whose ids were actually sent; keep
            # any overflow for the next compaction.
            if self._reasoning_buffer:
                _sent = set(sent_reasoning_ids)
                self._reasoning_buffer = [
                    e for e in self._reasoning_buffer if e.get("id") not in _sent
                ]
            # Re-hydrate to pick up the new digest.
            await self._hydrate()

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
        # Short-circuit if the mode's adapter was already INJECTED (offline
        # tests / a custom adapter with no provider key configured) — don't
        # require an LLM key or build the default adapter in that case.
        if self._config.mode == "implement":
            if self._implement_adapter is not None:
                return
        elif self._adapter is not None:
            return

        llm_key = self._config.resolved_llm_key
        if not llm_key:
            raise RuntimeError(
                "LLM API key is not configured. Set RESIDENT_LLM_API_KEY "
                "or configure [llm].api_key in the resident config TOML."
            )
        shared = OpenAICompatibleAdapter(self._config.llm)
        if self._config.mode == "implement":
            self._implement_adapter = shared
        else:
            self._adapter = shared

    async def _wake(self) -> list[dict]:
        """One heartbeat: step → for each directive → review → settle."""
        results: list[dict] = []
        self._wake_count += 1

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

        # Refresh the living context before reviewing so each wake sees the
        # latest KB/wiki/playbook/memory — another process may have updated the
        # playbook or KB, or this resident wrote durable findings on a prior
        # wake. (Skipped on idle wakes with no directives to avoid wasted calls.)
        if directives:
            await self._hydrate()

        for directive in directives:
            if not isinstance(directive, dict):
                continue

            intent = directive.get("intent", "")

            # R3: implement mode — route implement/fix_findings to the
            # implementer. The reviewer's fix_findings intent is the same
            # as implement (the implementer addresses findings from a
            # CHANGES_REQUESTED review). Both go to the implementer.
            if self._config.mode == "implement":
                if intent in ("implement", "fix_findings"):
                    assert self._implement_adapter is not None
                    result = await run_implement_directive(
                        directive,
                        api_url=self._api_url,
                        api_key=self._api_key,
                        config=self._config,
                        adapter=self._implement_adapter,
                    )
                    results.append(result)
                    continue
                else:
                    # Implementer received a non-implement directive — release.
                    logger.warning(
                        "Non-implement directive '%s' on an implementer runner "
                        "— releasing the lease.",
                        intent,
                    )
                    results.append(
                        await self._release_directive(
                            directive, reason=f"unsupported_intent:{intent}"
                        )
                    )
                    continue

            # Review mode: only post_review directives.
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

        # R2 — enrich with the living context (prior findings, memory digest,
        # review playbook, project context).
        if self._living_context is not None:
            context = self._enrich_with_living_context(context)

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
        else:
            # R2 — writeback: rolling reasoning + durable KB findings.
            await self._writeback_after_review(
                ticket_id=ticket_id,
                verdict_text=verdict_content,
                result=result,
            )

        # R2 — periodic compaction.
        if self._wake_count > 0 and self._wake_count % self._config.compact_every_wakes == 0:
            await self._maybe_compact()

        return {
            "ticket_id": ticket_id,
            "directive_id": directive_id,
            "settled": settle_resp.status_code in (200, 201),
            "verdict": result.verdict_phrase.split("\n")[0],
            "settle_status": settle_resp.status_code,
        }


def _extract_conclusion(result: ReviewResult, ticket_id: str) -> str:
    """Extract a short conclusion from a review result for the rolling
    reasoning entry. Bounded to _MAX_REASONING_LENGTH."""
    verdict = result.verdict_phrase.split("\n")[0].strip()
    reasoning = result.reasoning.strip()

    if reasoning:
        # Take the first paragraph of reasoning as the conclusion.
        first_para = reasoning.split("\n\n")[0].strip()
        if len(first_para) > 500:
            first_para = first_para[:497] + "..."
        return f"[{verdict}] {ticket_id}: {first_para}"
    else:
        return f"[{verdict}] {ticket_id}: review completed."


def _extract_durable_findings(result: ReviewResult) -> list[str]:
    """Extract durable findings from a review result that should be written
    to the shared KB. Only CHANGES_REQUESTED reviews with substantial
    findings produce durable entries.

    A finding is 'durable' when it describes a recurring pattern,
    convention, or fix pattern — not a one-off nit. We use simple
    heuristics: findings that mention 'pattern', 'convention',
    'should always', 'must always', 'anti-pattern', etc.

    Returns an empty list when nothing durable was found (no-op discipline).
    """
    verdict_first = result.verdict_phrase.split("\n")[0].strip()
    # Only extract durable findings from non-clean reviews.
    if re.sub(r"[\s_-]+", "_", verdict_first.upper()) == "VERIFIED_CLEAN":
        return []

    text = result.verdict_phrase + "\n" + result.reasoning
    durable_markers = [
        "pattern", "convention", "should always", "must always",
        "anti-pattern", "anti pattern", "best practice",
        "every handler", "all endpoints", "consistently",
    ]

    lines = text.split("\n")
    findings: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or len(stripped) < 20:
            continue
        lower = stripped.lower()
        if any(marker in lower for marker in durable_markers):
            findings.append(stripped)

    return findings[:5]  # cap at 5 durable findings per wake


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
