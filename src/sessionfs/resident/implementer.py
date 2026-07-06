"""Implementer resident — propose-only code writer (R3).

Security invariants (non-negotiable, §3.7, C6/C7/C8):
1. NEVER send code contents to SessionFS — only diff-refs (branch/SHA/
   changed-path list). The server is out of the code-custody blast radius (C7).
2. NEVER self-close — settle only to waiting_review (outcome='posted_progress').
   The server's F1 gate closes only on an INDEPENDENT trusted VERIFIED-CLEAN.
3. NEVER push/merge — git ops confined to resident/<queue>/<ticket> branch.
   Refuse if HEAD is on a protected branch.
4. Identity separation — persona defaults to 'atlas', not a reviewer.
5. Data-not-instructions — ticket/finding content is untrusted input.
6. Fail-closed — on LLM error / unparseable changes / apply failure, settle
   failed (backoff) rather than present a non-change as a proposal.
7. Bounded blast radius — file writes only inside the worktree.
8. Credential boundary — operator LLM key local-only, service key SessionFS-only.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from sessionfs.resident.budget import BudgetTracker

from sessionfs.resident.client import (
    _api_request,
    add_ticket_comment,
    complete_work_queue_step,
)
from sessionfs.resident.config import ResidentConfig
from sessionfs.resident.llm_adapter import (
    FileChange,
    ImplementContext,
    ImplementLLM,
    ImplementResult,
)

logger = logging.getLogger("sessionfs.resident.implementer")

# Branches the implementer MUST NOT operate on (the authoritative protected set).
_PROTECTED_BRANCHES = {"main", "master", "develop", "prod", "production"}
_PROTECTED_PREFIXES = ("release/", "prod/", "staging/")

# Maximum changed-path count in a diff-ref comment.
_MAX_CHANGED_PATHS = 50

# Default persona for the implementer (NOT a reviewer persona).
_DEFAULT_IMPLEMENTER_PERSONA = "atlas"


@dataclass
class GitState:
    """Snapshot of git state after a commit."""

    branch: str
    commit_sha: str
    changed_paths: list[str]
    # True when this state surfaces an EXISTING commit (retry-resume) rather
    # than a fresh commit — the diff-ref may already be posted, so re-posting is
    # gated on a not-already-present check to avoid staling a clean verdict.
    is_resume: bool = False


# Matches the reviewer's verdict HEADER ("… review on <ticket>: <VERDICT>"),
# as emitted by the reviewer runner's _format_verdict_content.
_VERDICT_HEADER_RE = re.compile(
    r"review on \S+:\s*(VERIFIED[-_]CLEAN|CHANGES[-_]REQUESTED)",
    re.IGNORECASE,
)


def _latest_review_verdict(directive: dict) -> str | None:
    """The most recent TRUSTED reviewer verdict in the comment delta:
    ``"clean"`` (VERIFIED-CLEAN), ``"changes"`` (CHANGES_REQUESTED), or ``None``
    (no trusted verdict yet — the item is simply AWAITING review).

    Real `implement_until_done` directives do NOT populate `review_state` (that
    is a review-queue field), so `open_findings` is always empty and cannot
    distinguish these; the authoritative signal is the server-stamped
    `verdict_trusted` verdict in the comments. The three states matter: a
    change request with no new work is no-progress (fail), but an item merely
    awaiting review must NOT be failed.
    """
    verdict: str | None = None
    for c in directive.get("comment_delta") or []:
        if not isinstance(c, dict) or not c.get("verdict_trusted"):
            continue
        # Parse the verdict from the reviewer's HEADER line
        # ("… review on <ticket>: <VERDICT>") rather than substring-anywhere, so
        # prose like "this is not VERIFIED-CLEAN yet" can't false-match.
        m = _VERDICT_HEADER_RE.search(str(c.get("content", "")))
        if not m:
            continue
        token = m.group(1).upper().replace("_", "-")
        if token == "VERIFIED-CLEAN":
            verdict = "clean"
        elif token == "CHANGES-REQUESTED":
            verdict = "changes"
    return verdict


# ── Public API ──────────────────────────────────────────────────────────────


async def run_implement_directive(
    directive: dict,
    *,
    api_url: str,
    api_key: str,
    config: ResidentConfig,
    adapter: ImplementLLM,
    budget: "BudgetTracker | None" = None,
) -> dict:
    """Process one implement or fix_findings directive.

    Flow:
    1. Hydrate context: ticket + findings + worktree file contents.
    2. Call adapter.implement() → proposed changes.
    3. Apply changes to worktree (write files).
    4. Commit to resident branch.
    5. Post diff-ref comment (branch/SHA/paths, NO code).
    6. Settle directive → waiting_review (NOT done).

    Returns a result dict for the runner's wake summary.
    On any failure, settles failed (backoff) — never presents a non-change
    as a proposal (invariant 6).
    """
    ticket_id = directive.get("ticket_id", "?")
    directive_id = directive.get("directive_id", "?")
    item_id = directive.get("item_id", "?")
    lease_epoch = directive.get("ticket_lease_epoch")
    intent = directive.get("intent", "implement")

    worktree = _resolve_worktree(config)
    if worktree is None:
        return await _settle_failed(
            api_url, api_key, config,
            item_id=item_id, directive_id=directive_id,
            ticket_id=ticket_id, lease_epoch=lease_epoch,
            reason="No worktree_path configured for implement mode",
        )

    # (0) Verify we're not on a protected branch BEFORE doing anything costly
    #     (ticket fetch, LLM call). Never operate against a protected HEAD.
    branch_ok, branch_err = _check_branch_safety(worktree)
    if not branch_ok:
        return await _settle_failed(
            api_url, api_key, config,
            item_id=item_id, directive_id=directive_id,
            ticket_id=ticket_id, lease_epoch=lease_epoch,
            reason=branch_err,
        )

    # (0.5) Check out the resident branch (from the clean base) BEFORE
    #       hydrating files, so the LLM reads the TARGET branch's state — not a
    #       prior ticket branch's, which it could otherwise copy in and
    #       contaminate this ticket's proposal (clean-base isolation).
    try:
        _prepare_resident_branch(worktree, config, ticket_id)
    except Exception as exc:
        logger.error("Failed to prepare resident branch: %s", exc)
        return await _settle_failed(
            api_url, api_key, config,
            item_id=item_id, directive_id=directive_id,
            ticket_id=ticket_id, lease_epoch=lease_epoch,
            reason=f"Branch prepare error: {exc}",
        )

    # (1) Build implement context (fetches the full ticket + reads the worktree
    #     on the now-checked-out resident branch).
    try:
        context = await _build_implement_context(
            directive, worktree, api_url, api_key, config
        )
    except Exception as exc:
        logger.error("Failed to build implement context: %s", exc)
        return await _settle_failed(
            api_url, api_key, config,
            item_id=item_id, directive_id=directive_id,
            ticket_id=ticket_id, lease_epoch=lease_epoch,
            reason=f"Context build error: {exc}",
        )

    # (2) Call the operator's OWN LLM.
    try:
        result = await adapter.implement(context)
    except Exception as exc:
        logger.error("LLM adapter raised for ticket=%s: %s", ticket_id, exc)
        result = ImplementResult(error=f"LLM adapter exception: {exc}")

    # R4 — record LLM spend for cost bounding (fail-closed park happens in the
    # runner BEFORE the wake). Recorded regardless of the outcome below.
    if budget is not None:
        budget.record(result.tokens_used)

    if result.error:
        logger.error(
            "LLM implement failed for ticket=%s: %s", ticket_id, result.error
        )
        return await _settle_failed(
            api_url, api_key, config,
            item_id=item_id, directive_id=directive_id,
            ticket_id=ticket_id, lease_epoch=lease_epoch,
            reason=f"LLM error: {result.error[:500]}",
            llm_invoked=True,
        )

    # Refuse a full-file rewrite of a file that was TOO LARGE to fully include
    # in context — the LLM saw only a placeholder, so its `new_content` would
    # silently delete the unseen tail (data loss). Fail closed. Compare
    # NORMALIZED paths (Path collapses "./" and "//") so `./src/x.py` can't slip
    # past a `src/x.py` entry; FileChange already rejects "..".
    _truncated = {Path(p) for p in context.truncated_files}
    _clobbered = sorted(
        {c.path for c in result.changes if Path(c.path) in _truncated}
    )
    if _clobbered:
        logger.error(
            "Refusing full-file rewrite of truncated file(s) for ticket=%s: %s",
            ticket_id, _clobbered,
        )
        return await _settle_failed(
            api_url, api_key, config,
            item_id=item_id, directive_id=directive_id,
            ticket_id=ticket_id, lease_epoch=lease_epoch,
            reason=f"Refused full-file rewrite of oversized file(s): {_clobbered}",
            llm_invoked=True,
        )

    # Refuse a BLIND rewrite: a change to an existing file that was NEVER
    # hydrated into context (e.g. a path only in the ticket prose, or missed by
    # the extractor) means the LLM produced full `new_content` without seeing
    # the original — a full-file replacement that silently drops unseen code.
    # New files (absent on disk) are fine; hydrated files (incl. the truncated
    # placeholder) were shown. Runs after _prepare_resident_branch so existence
    # is checked against the resident branch. Fail closed.
    _hydrated = {Path(p) for p in context.current_files}
    _blind_set: set[str] = set()
    for c in result.changes:
        if Path(c.path) in _hydrated:
            continue
        # Use the already-RESOLVED worktree (from _resolve_worktree) — not a
        # re-derived config.worktree_path, which may be unexpanded/relative.
        target = worktree / c.path
        if not target.is_file():
            continue  # a NEW file — nothing to clobber
        # Existing + un-hydrated: only a hazard if the content actually DIFFERS.
        # A byte-identical rewrite loses nothing (and is what enables a
        # retry-resume where the LLM re-emits the same change it already made).
        try:
            _existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            _existing = None
        if _existing is None or _existing != c.new_content:
            _blind_set.add(c.path)
    _blind = sorted(_blind_set)
    if _blind:
        logger.error(
            "Refusing blind rewrite of un-hydrated existing file(s) for "
            "ticket=%s: %s", ticket_id, _blind,
        )
        return await _settle_failed(
            api_url, api_key, config,
            item_id=item_id, directive_id=directive_id,
            ticket_id=ticket_id, lease_epoch=lease_epoch,
            reason=f"Refused blind rewrite of un-hydrated existing file(s): {_blind}",
            llm_invoked=True,
        )

    if not result.changes:
        # If a prior wake already committed to this branch, "no changes" means
        # the work is done and the retry just couldn't surface the diff-ref —
        # surface the EXISTING commit rather than orphaning it. Only a genuinely
        # empty branch fails closed (never present empty work as a proposal).
        if _branch_ahead_of_base(worktree, config) <= 0:
            logger.warning(
                "LLM returned no changes for ticket=%s — settling failed.",
                ticket_id,
            )
            return await _settle_failed(
                api_url, api_key, config,
                item_id=item_id, directive_id=directive_id,
                ticket_id=ticket_id, lease_epoch=lease_epoch,
                reason="LLM returned no changes",
                llm_invoked=True,
            )
        # Branch ahead → a prior wake committed. Surface the existing commit;
        # whether we re-post the diff-ref or fail depends on whether it's
        # already on the ticket + whether the review is clean (handled by the
        # shared post gate below — a legitimate fix_findings retry whose
        # diff-ref never posted must still be surfaced).
        logger.info(
            "LLM returned no changes for ticket=%s but the resident branch is "
            "ahead of base — surfacing the existing commit (retry-resume).",
            ticket_id,
        )
        git_state = _head_git_state(worktree, config, ticket_id)
    else:
        # (4) Apply changes + commit.
        try:
            git_state = _apply_and_commit(
                worktree,
                result.changes,
                config=config,
                ticket_id=ticket_id,
                summary=result.summary,
            )
        except Exception as exc:
            logger.error("Failed to apply/commit changes: %s", exc)
            return await _settle_failed(
                api_url, api_key, config,
                item_id=item_id, directive_id=directive_id,
                ticket_id=ticket_id, lease_epoch=lease_epoch,
                reason=f"Apply/commit error: {exc}",
                llm_invoked=True,
            )

    # Retry-resume post gate. When we're surfacing an EXISTING commit whose
    # diff-ref is ALREADY on the ticket, we must not blindly re-post:
    #   - Review is CLEAN (no open findings): the reviewer already saw this
    #     commit and cleared it. A fresh implementer comment would POSTDATE (and
    #     stale) that trusted verdict so the item never closes. Settle WITHOUT a
    #     comment.
    #   - Findings STILL OPEN on this same commit: the reviewer has seen it and
    #     the LLM produced nothing new — no progress. Fail closed (backoff /
    #     eventual escalation) rather than spam the same diff-ref forever.
    # A resume whose diff-ref is NOT yet posted (e.g. a prior post failed) falls
    # through and posts it — so a legitimate fix_findings retry is surfaced.
    if git_state.is_resume and await _diff_ref_already_posted(
        api_url, api_key, config, ticket_id, git_state.commit_sha
    ):
        _verdict = _latest_review_verdict(directive)
        if _verdict == "changes":
            # The reviewer REJECTED this already-surfaced commit and the LLM
            # produced nothing new — genuine no-progress. Fail closed (backoff /
            # eventual escalation) rather than silently ACK the directive in a
            # loop.
            logger.warning(
                "Diff-ref for %s already on ticket=%s, reviewer requested "
                "changes, and the LLM made no new change — settling failed "
                "(no progress).",
                git_state.commit_sha[:12], ticket_id,
            )
            return await _settle_failed(
                api_url, api_key, config,
                item_id=item_id, directive_id=directive_id,
                ticket_id=ticket_id, lease_epoch=lease_epoch,
                reason="No progress: reviewer requested changes on an already-surfaced commit",
                llm_invoked=True,
            )
        # verdict == "clean" (item closing) OR None (still AWAITING review):
        # settle WITHOUT a new comment — don't stale a clean verdict, and don't
        # FAIL an item that is legitimately waiting for the reviewer.
        logger.info(
            "Diff-ref for %s already on ticket=%s (verdict=%s) — settling "
            "without a new comment.",
            git_state.commit_sha[:12], ticket_id, _verdict or "awaiting-review",
        )
        settle_resp = await complete_work_queue_step(
            api_url, api_key, config.project, config.queue_id,
            item_id=item_id, directive_id=directive_id, ticket_id=ticket_id,
            outcome="posted_progress", ticket_lease_epoch=lease_epoch,
        )
        return {
            "ticket_id": ticket_id,
            "directive_id": directive_id,
            "settled": settle_resp.status_code in (200, 201),
            "llm_invoked": True,
            "intent": intent,
            "branch": git_state.branch,
            "commit": git_state.commit_sha,
            "resumed_noop": True,
            "settle_status": settle_resp.status_code,
        }

    # (5) Post diff-ref comment (branch/SHA/paths — NEVER code contents, C7).
    diff_ref = _format_diff_ref(git_state, ticket_id)
    comment_resp = await add_ticket_comment(
        api_url,
        api_key,
        config.project,
        ticket_id,
        content=diff_ref,
        author_persona=config.persona,
        lease_epoch=lease_epoch,
    )
    comment_id: str | None = None
    if comment_resp.status_code in (200, 201) and isinstance(comment_resp.body, dict):
        comment_id = comment_resp.body.get("id")

    if comment_id is None:
        logger.error(
            "Failed to post diff-ref comment for ticket=%s (HTTP %s) — leaving "
            "the directive UNSETTLED so the next heartbeat re-emits it. The "
            "commit is already on the resident branch; the retry re-posts the "
            "diff-ref.",
            ticket_id,
            comment_resp.status_code,
        )
        # Do NOT settle failed here: a transient comment-post failure would
        # otherwise burn a retry toward the failed-after-max giveup AND advance
        # the item past a change the reviewer never got a diff-ref for. Leaving
        # the lease open is the retryable path (heartbeat re-emit).
        return {
            "ticket_id": ticket_id,
            "directive_id": directive_id,
            "settled": False,
            "llm_invoked": True,
            "intent": intent,
            "retryable": True,
            "error": f"diff-ref comment post failed (HTTP {comment_resp.status_code})",
        }

    # (6) Settle → waiting_review (NOT done, NEVER a verdict).
    settle_resp = await complete_work_queue_step(
        api_url,
        api_key,
        config.project,
        config.queue_id,
        item_id=item_id,
        directive_id=directive_id,
        ticket_id=ticket_id,
        outcome="posted_progress",
        ticket_lease_epoch=lease_epoch,
        comment_id=comment_id,
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
            "Settle failed (HTTP %s) for ticket=%s.",
            settle_resp.status_code,
            ticket_id,
        )

    return {
        "ticket_id": ticket_id,
        "directive_id": directive_id,
        "settled": settle_resp.status_code in (200, 201),
        "llm_invoked": True,
        "intent": intent,
        "branch": git_state.branch,
        "commit": git_state.commit_sha,
        "changed_paths": len(git_state.changed_paths),
        "settle_status": settle_resp.status_code,
    }


# ── Context building ────────────────────────────────────────────────────────


async def _build_implement_context(
    directive: dict,
    worktree: Path,
    api_url: str,
    api_key: str,
    config: ResidentConfig,
) -> ImplementContext:
    """Assemble the ImplementContext from the directive + worktree state.

    The directive's ``ticket`` carries only BOUNDED metadata (id/title/status);
    the description + acceptance criteria live on the ticket itself, so we fetch
    the full ticket (tickets:read) — otherwise the implementer LLM sees only a
    title + comment delta and implements the wrong thing.
    """
    ticket_meta = directive.get("ticket") or {}
    ticket_id = directive.get("ticket_id", "") or ticket_meta.get("id", "")
    ticket_title = ticket_meta.get("title", "")

    # Fetch the FULL ticket (the directive omits description / acceptance
    # criteria for token control).
    ticket_description = ""
    acceptance_criteria: list[str] = []
    fetched_file_refs: list[str] = []
    if ticket_id and config.project:
        tresp = await _api_request(
            "GET", api_url, api_key,
            f"/api/v1/projects/{config.project}/tickets/{ticket_id}",
        )
        # Fail closed: if the full ticket can't be fetched, do NOT implement
        # from cold context (title + comment delta only) — that produces wrong
        # or no-op work. Raise so the caller settles the directive failed.
        if tresp.status_code != 200 or not isinstance(tresp.body, dict):
            raise RuntimeError(
                f"Ticket hydration failed for {ticket_id} "
                f"(HTTP {tresp.status_code}) — refusing to implement from "
                f"incomplete context."
            )
        ticket_description = str(tresp.body.get("description") or "")
        _ac = tresp.body.get("acceptance_criteria")
        if isinstance(_ac, list):
            acceptance_criteria = [str(x) for x in _ac]
        _fr = tresp.body.get("file_refs")
        if isinstance(_fr, list):
            fetched_file_refs = [str(x) for x in _fr]
        if not ticket_title:
            ticket_title = str(tresp.body.get("title") or "")
    # Fall back to any acceptance criteria the directive happened to include.
    if not acceptance_criteria:
        _dac = ticket_meta.get("acceptance_criteria")
        if isinstance(_dac, list):
            acceptance_criteria = [str(x) for x in _dac]

    # Extract open findings from review_state (if present — for fix_findings).
    review_state = directive.get("review_state") or {}
    open_findings: list[dict] = []
    if isinstance(review_state, dict):
        raw = review_state.get("open_findings")
        if isinstance(raw, list):
            for f in raw:
                if isinstance(f, dict):
                    open_findings.append(f)

    # The comment_delta contains the reviewer's latest verdict / comments.
    comment_delta = directive.get("comment_delta") or []
    review_verdict = ""
    directive_notes_parts: list[str] = []
    for c in (comment_delta if isinstance(comment_delta, list) else []):
        if not isinstance(c, dict):
            continue
        content = str(c.get("content", ""))
        author = c.get("author_persona") or ""
        if "codex" in author.lower() or "review" in author.lower():
            review_verdict += content + "\n"
        else:
            directive_notes_parts.append(content)

    # Read files from the worktree. We look for files referenced in:
    # - The ticket's file_refs (if any)
    # - Path-like strings in findings text
    # - The expand_hints (if they look like file paths)
    files_to_read: set[str] = set()
    # file_refs come from the FETCHED ticket (the real directive omits them);
    # the directive's ticket metadata is a fallback for older servers.
    for ref in [*fetched_file_refs, *(ticket_meta.get("file_refs") or [])]:
        if isinstance(ref, str) and ref and not ref.startswith("http"):
            files_to_read.add(ref)

    # Scan the ticket description + acceptance criteria for path-like strings —
    # a file named only there would otherwise go un-hydrated, and the
    # blind-rewrite guard would then block the LLM from editing it.
    _collect_file_refs(ticket_description, files_to_read)
    for _crit in acceptance_criteria:
        _collect_file_refs(str(_crit), files_to_read)

    # Also scan findings text for file paths.
    for f in open_findings:
        text = f.get("text", "")
        _collect_file_refs(text, files_to_read)

    for c in (comment_delta if isinstance(comment_delta, list) else []):
        if isinstance(c, dict):
            _collect_file_refs(str(c.get("content", "")), files_to_read)

    # Read discovered files (bounded).
    current_files: dict[str, str] = {}
    truncated_files: list[str] = []

    # First filter to SAFE relative paths — file_refs / finding-text paths are
    # UNTRUSTED (they come from the ticket). Reject the same way the write path
    # does: no absolute paths, no traversal, no `.git` control dir (reading
    # .git/config etc. into the LLM prompt is info disclosure). Do this BEFORE
    # touching git/the filesystem (a `../x` path would even error check-ignore).
    safe_refs: list[str] = []
    for rel_path in sorted(files_to_read):
        parts = rel_path.replace("\\", "/").split("/")
        if rel_path.startswith("/") or ".." in parts or ".git" in parts:
            continue
        safe_refs.append(rel_path)

    # Skip GITIGNORED referenced files — they are likely local secrets (.env,
    # credentials) and must NOT be read into the operator's LLM prompt. Batch
    # `git check-ignore` (rc 0 = some ignored, 1 = none) over the safe set.
    ignored_refs: set[str] = set()
    if safe_refs:
        ck = _git_run(
            ["check-ignore", "--", *safe_refs],
            worktree, ok_returncodes={0, 1},
        )
        ignored_refs = {ln.strip() for ln in ck.stdout.splitlines() if ln.strip()}

    worktree_resolved = worktree.resolve()
    for rel_path in safe_refs:
        if len(current_files) >= 20:  # cap at 20 files
            break
        if rel_path in ignored_refs:
            continue
        abs_path = (worktree / rel_path).resolve()
        # Bounded blast radius: only within the worktree (also rejects symlink
        # escape, since resolve() follows links before the containment check).
        try:
            abs_path.relative_to(worktree_resolved)
        except ValueError:
            continue
        if abs_path.is_file():
            try:
                content = abs_path.read_text(encoding="utf-8")
                if len(content) > 8000:
                    # The LLM would only see a prefix — mark the file so a
                    # full-file rewrite of it is refused (it can't reproduce the
                    # unseen tail). Show a placeholder, not partial code.
                    truncated_files.append(rel_path)
                    current_files[rel_path] = (
                        f"[FILE TOO LARGE TO INCLUDE ({len(content)} bytes) — "
                        f"do NOT return a full-file rewrite of this path; it "
                        f"cannot be reconstructed from a partial view.]"
                    )
                else:
                    current_files[rel_path] = content
            except (OSError, UnicodeDecodeError):
                continue

    return ImplementContext(
        ticket_id=ticket_id,
        ticket_title=ticket_title,
        ticket_description=ticket_description,
        acceptance_criteria=acceptance_criteria,
        open_findings=open_findings,
        current_files=current_files,
        review_verdict=review_verdict[:2000],
        directive_notes="\n".join(directive_notes_parts)[:2000],
        truncated_files=truncated_files,
    )


def _collect_file_refs(text: str, files: set[str]) -> None:
    """Extract plausible file paths from text."""
    import re as _re

    for m in _re.finditer(
        r'(?:^|\s|["\'`])((?:src|tests?|lib|docs|deploy)/[\w/\-_.]+\.\w+)',
        text,
    ):
        files.add(m.group(1))


# ── Git helpers ─────────────────────────────────────────────────────────────


def _resolve_worktree(config: ResidentConfig) -> Path | None:
    """Return the configured worktree path, or None if not set."""
    if not config.worktree_path:
        return None
    p = Path(config.worktree_path).expanduser().resolve()
    if not p.is_dir():
        logger.error("Worktree path does not exist: %s", p)
        return None
    return p


def _check_branch_safety(worktree: Path) -> tuple[bool, str]:
    """Verify HEAD is not on a protected branch.

    Returns (ok, error_reason). ok=True means safe to proceed.
    Invariant 3: NEVER operate on main/develop/master/release/*.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(worktree),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git rev-parse failed: {exc}"

    if result.returncode != 0:
        return False, f"git rev-parse failed: {result.stderr.strip()}"

    branch = result.stdout.strip()
    if branch in _PROTECTED_BRANCHES:
        return False, (
            f"HEAD is on protected branch '{branch}'. "
            f"Implementer must operate on a resident branch, "
            f"not a protected branch (invariant 3)."
        )
    if branch.startswith(_PROTECTED_PREFIXES):
        return False, (
            f"HEAD is on protected branch '{branch}' (release prefix). "
            f"Implementer must operate on a resident branch (invariant 3)."
        )
    if branch == "HEAD":
        return False, "HEAD is detached — implementer needs a branch to commit to."

    return True, ""


def _prepare_resident_branch(
    worktree: Path,
    config: ResidentConfig,
    ticket_id: str,
) -> str:
    """Check out (or create from the CLEAN base) the resident branch for this
    ticket; return its name. This runs BEFORE context hydration so the LLM reads
    the TARGET branch's files — never a prior ticket branch's, which would let
    it copy another ticket's changes in and break clean-base isolation.
    Idempotent (safe to call again from _apply_and_commit).
    """
    branch_name = _resident_branch(config, ticket_id)
    # The configured prefix must not land the resident branch in a PROTECTED
    # namespace (e.g. resident_branch_prefix='release' → 'release/<q>/<t>'),
    # which would let the implementer commit on a protected-prefixed branch and
    # bypass invariant 3 (the startup HEAD check passed on a different branch).
    if branch_name in _PROTECTED_BRANCHES or branch_name.startswith(_PROTECTED_PREFIXES):
        raise RuntimeError(
            f"Resident branch '{branch_name}' falls in a protected namespace — "
            f"refusing (invariant 3). Fix resident.resident_branch_prefix."
        )

    # Create or CONTINUE on the resident branch. On a later fix_findings round
    # the branch already exists — switch to it and keep its prior commits (never
    # reset/-B, which would drop earlier work). Otherwise create it FROM THE
    # CLEAN BASE (not the current HEAD, which could be a prior resident branch).
    exists = _git_run(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        worktree, ok_returncodes={0, 1},
    ).returncode == 0
    if exists:
        co = _git_run(["checkout", branch_name], worktree, ok_returncodes={0})
    else:
        base = config.base_branch
        base_exists = _git_run(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{base}"],
            worktree, ok_returncodes={0, 1},
        ).returncode == 0
        if not base_exists:
            # Fail closed: cutting from the current HEAD instead could inherit a
            # PRIOR resident branch's commits into this ticket's proposal.
            raise RuntimeError(
                f"Clean base branch '{base}' not found in the worktree — "
                f"refusing to cut '{branch_name}' from an unknown base. Set "
                f"resident.base_branch to a branch that exists in the checkout."
            )
        co = _git_run(
            ["checkout", "-b", branch_name, base], worktree, ok_returncodes={0}
        )
    if co.returncode != 0:
        raise RuntimeError(
            f"Failed to switch to resident branch '{branch_name}': "
            f"{co.stderr.strip()}"
        )
    current = _current_branch(worktree)
    if current != branch_name:
        raise RuntimeError(
            f"Not on resident branch '{branch_name}' after checkout (on '{current}')."
        )
    return branch_name


def _apply_and_commit(
    worktree: Path,
    changes: list[FileChange],
    *,
    config: ResidentConfig,
    ticket_id: str,
    summary: str,
) -> GitState:
    """Apply file changes to the worktree and commit to the resident branch
    (already checked out by _prepare_resident_branch).

    All file writes are confined to the worktree (invariant 7).
    """
    # The resident branch must already be checked out (see
    # _prepare_resident_branch, called BEFORE context hydration). This call is
    # idempotent — it confirms/re-checks out the same branch.
    branch_name = _prepare_resident_branch(worktree, config, ticket_id)

    # (2) Refuse to overwrite target files that already exist with UNCOMMITTED
    #     state — the operator's local work must not be silently clobbered.
    #     `git status --porcelain -- <paths>` emits a line for any target path
    #     that is tracked-modified OR untracked-but-present; a truly NEW file
    #     (nonexistent) produces NO line and is allowed.
    change_paths = [c.path for c in changes]
    # `--ignored` is REQUIRED: a gitignored file (e.g. a local `.env` holding
    # secrets) is invisible to plain `--porcelain`, so without it the
    # implementer would clobber exactly the files most likely to hold local
    # secrets. With `--ignored`, an existing ignored target surfaces as "!!".
    status = _git_run(
        ["status", "--porcelain", "--ignored", "--", *change_paths],
        worktree, ok_returncodes={0},
    )
    dirty = [
        # Strip the 2-char status code + separator ("XY path" / "?? path" /
        # "!! path"). Any line means the target already exists with uncommitted
        # state (modified / untracked / ignored); a clean tracked file or a
        # nonexistent path produces no line.
        line[3:].strip()
        for line in status.stdout.splitlines()
        if line.strip()
    ]
    if dirty:
        raise RuntimeError(
            "Refusing to overwrite target files that already exist with "
            f"uncommitted local content: {', '.join(sorted(dirty))}. "
            "Commit, stash, or remove them first."
        )

    # (3) Write changed files inside the worktree.
    worktree_resolved = worktree.resolve()
    for change in changes:
        abs_path = (worktree / change.path).resolve()
        # Bounded blast radius (invariant 7).
        try:
            abs_path.relative_to(worktree_resolved)
        except ValueError:
            raise ValueError(
                f"FileChange path '{change.path}' resolves outside "
                f"the worktree — rejected (invariant 7)."
            )
        # Create parent dirs if needed.
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(change.new_content, encoding="utf-8")
        logger.info("Wrote %s (%d bytes)", change.path, len(change.new_content))

    # (3) Stage ONLY the paths the implementer wrote (not `git add .`, which
    #     would sweep any pre-existing dirty state / stray secrets in the
    #     worktree into the resident commit).
    _git_run(["add", "--", *[c.path for c in changes]], worktree, ok_returncodes={0})

    # Fail-closed on a no-op (invariant 6): if the LLM's changes were
    # byte-identical to the existing files, nothing is staged. Do NOT
    # --allow-empty a proposal — commit refuses (nothing to commit) and we
    # settle failed rather than present a non-change as a reviewable proposal.
    commit_msg = _format_commit_message(ticket_id, summary, changes)
    # Pathspec-limit the commit to ONLY the implementer's files — a bare
    # `git commit` would also commit any changes pre-staged in the index (not
    # written by this run) into the resident branch.
    commit_result = _git_run(
        ["commit", "-m", commit_msg, "--", *[c.path for c in changes]],
        worktree, ok_returncodes={0, 1},
    )
    resumed = False
    if commit_result.returncode != 0:
        # A non-zero commit is only a RETRY-RESUME candidate if it was a genuine
        # no-op: nothing staged AND the branch already ahead of base (a prior
        # wake's commit). Distinguish that from a REAL failure — e.g. a
        # rejecting pre-commit/commit-msg hook, where our changes are STILL
        # staged but uncommitted. In that case surfacing the older HEAD would
        # advance the queue with a diff-ref that omits the attempted fix, so we
        # must fail closed. `git diff --cached --quiet` → rc 0 (nothing staged)
        # / rc 1 (staged diff present).
        staged = _git_run(
            ["diff", "--cached", "--quiet", "--", *[c.path for c in changes]],
            worktree, ok_returncodes={0, 1},
        )
        has_staged_diff = staged.returncode == 1
        if has_staged_diff or _branch_ahead_of_base(worktree, config) <= 0:
            detail = (commit_result.stdout + commit_result.stderr).strip()
            raise RuntimeError(
                f"git commit produced no proposal (no-op, staged-but-uncommitted, "
                f"or error): {detail}"
            )
        logger.info(
            "Commit was a genuine no-op but the resident branch is ahead of "
            "base '%s' — surfacing the existing commit for review "
            "(retry-resume).",
            config.base_branch,
        )
        resumed = True

    # (4) Get the commit SHA and changed paths.
    sha_result = _git_run(
        ["rev-parse", "HEAD"], worktree, ok_returncodes={0},
    )
    commit_sha = sha_result.stdout.strip()[:40]

    changed_paths = _get_changed_paths(worktree)

    return GitState(
        branch=branch_name,
        commit_sha=commit_sha,
        changed_paths=changed_paths,
        is_resume=resumed,
    )


def _branch_ahead_of_base(worktree: Path, config: ResidentConfig) -> int:
    """How many commits HEAD is ahead of the configured clean base (0 if the
    base is unknown or HEAD is not ahead). Used to detect a prior wake's commit
    on a no-op retry so it can be surfaced rather than orphaned."""
    base = config.base_branch
    base_exists = _git_run(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{base}"],
        worktree, ok_returncodes={0, 1},
    ).returncode == 0
    if not base_exists:
        return 0
    cnt = _git_run(
        ["rev-list", "--count", f"{base}..HEAD"], worktree, ok_returncodes={0, 1}
    )
    try:
        return int(cnt.stdout.strip())
    except ValueError:
        return 0


async def _diff_ref_already_posted(
    api_url: str,
    api_key: str,
    config: ResidentConfig,
    ticket_id: str,
    commit_sha: str,
) -> bool:
    """True if a comment referencing this commit SHA is already on the ticket.
    Used so a retry-resume doesn't re-post a diff-ref and postdate/stale a
    trusted clean verdict. PAGES through the full (oldest-first) comment thread
    via the since/since_id cursor so the diff-ref isn't missed on a long thread.
    Fails OPEN (False → post) when comments can't be listed, so a genuinely
    missing diff-ref is still surfaced."""
    needle = commit_sha[:12]
    if not needle:
        return False
    _page = 500
    since: str | None = None
    since_id: str | None = None
    base = f"/api/v1/projects/{config.project}/tickets/{ticket_id}/comments"
    for _ in range(40):  # cap: 40 * 500 = 20000 comments
        path = f"{base}?limit={_page}"
        if since and since_id:
            path += f"&since={quote(since, safe='')}&since_id={quote(since_id, safe='')}"
        resp = await _api_request("GET", api_url, api_key, path)
        if resp.status_code != 200:
            return False
        body = resp.body
        if isinstance(body, dict):
            comments = body.get("comments") or body.get("items") or []
        elif isinstance(body, list):
            comments = body
        else:
            comments = []
        if not comments:
            return False
        for c in comments:
            if isinstance(c, dict) and needle in str(c.get("content", "")):
                return True
        if len(comments) < _page:
            return False  # last page
        last = comments[-1]
        if not isinstance(last, dict):
            return False
        since = last.get("created_at")
        since_id = last.get("id")
        if not since or not since_id:
            return False  # can't advance the cursor → stop (fail open)
    return False


def _head_git_state(
    worktree: Path, config: ResidentConfig, ticket_id: str
) -> GitState:
    """GitState for the current HEAD of the resident branch — used to surface an
    already-existing commit (retry-resume) without regenerating it."""
    branch = _resident_branch(config, ticket_id)
    sha = _git_run(
        ["rev-parse", "HEAD"], worktree, ok_returncodes={0}
    ).stdout.strip()[:40]
    return GitState(
        branch=branch,
        commit_sha=sha,
        changed_paths=_get_changed_paths(worktree),
        is_resume=True,
    )


def _git_run(
    args: list[str],
    cwd: Path,
    ok_returncodes: set[int] | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command. Raises RuntimeError on unexpected failure."""
    if ok_returncodes is None:
        ok_returncodes = {0}
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=30,
            cwd=str(cwd),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode not in ok_returncodes:
        raise RuntimeError(
            f"git {' '.join(args)} returned {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result


def _current_branch(worktree: Path) -> str:
    """Get the current branch name."""
    result = _git_run(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        worktree, ok_returncodes={0},
    )
    return result.stdout.strip()


def _resident_branch(config: ResidentConfig, ticket_id: str) -> str:
    """Build the resident branch name: <prefix>/<queue_short>/<ticket_short>."""
    prefix = config.resident_branch_prefix or "resident"
    queue_short = config.queue_id[-12:] if len(config.queue_id) > 12 else config.queue_id
    ticket_short = ticket_id[-16:] if len(ticket_id) > 16 else ticket_id
    return f"{prefix}/{queue_short}/{ticket_short}"


def _get_changed_paths(worktree: Path) -> list[str]:
    """Get the list of paths changed in the most recent commit."""
    result = _git_run(
        ["diff", "--name-only", "HEAD~1", "HEAD"],
        worktree, ok_returncodes={0, 128},  # 128 = no parent commit (first commit)
    )
    if result.returncode == 128:
        # First commit on this branch — diff against the merge base.
        result = _git_run(
            ["diff", "--name-only", "HEAD", "--", "."],
            worktree, ok_returncodes={0},
        )
        if not result.stdout.strip():
            # Show staged files instead.
            result = _git_run(
                ["diff", "--name-only", "--cached"],
                worktree, ok_returncodes={0},
            )
    paths = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
    return paths[:_MAX_CHANGED_PATHS]


def _format_commit_message(
    ticket_id: str,
    summary: str,
    changes: list[FileChange],
) -> str:
    """Format a commit message from the implement result."""
    msg = f"feat(resident): {summary[:72]}"
    msg += f"\n\nTicket: {ticket_id}"
    msg += "\nChanged files:"
    for c in changes[:20]:
        msg += f"\n- {c.path}"
    if len(changes) > 20:
        msg += f"\n- ... and {len(changes) - 20} more"
    return msg


def _format_diff_ref(git_state: GitState, ticket_id: str) -> str:
    """Format a diff-ref comment — METADATA ONLY, never code contents (C7).

    Contains ONLY server-safe refs: branch name, commit SHA, and the
    changed-path LIST. The LLM's free-text summary is DELIBERATELY excluded —
    C7 enumerates the allowed cross-server set as branch/SHA/paths/PR-URL, and a
    prompt-injected model could smuggle code or secrets it read out through a
    free-text field. Reviewers read the actual change from the branch locally.
    """
    lines: list[str] = [
        "## Resident Implementer — Proposed Changes",
        "",
        f"**Ticket:** `{ticket_id}`",
        f"**Branch:** `{git_state.branch}`",
        f"**Commit:** `{git_state.commit_sha}`",
        "",
        "### Changed Files",
    ]
    for p in git_state.changed_paths:
        lines.append(f"- `{p}`")
    if not git_state.changed_paths:
        lines.append("- (no files changed)")
    lines.append("")
    lines.append(
        "> ℹ️ This is a diff-ref (metadata only). "
        "Review the branch/commit on the operator host; "
        "code is NEVER transported through SessionFS (C7)."
    )
    return "\n".join(lines)


# ── Settle helpers ──────────────────────────────────────────────────────────


async def _settle_failed(
    api_url: str,
    api_key: str,
    config: ResidentConfig,
    *,
    item_id: str,
    directive_id: str,
    ticket_id: str,
    lease_epoch: int | None,
    reason: str,
    llm_invoked: bool = False,
) -> dict:
    """Settle a directive as failed (backoff). Invariant 6: never present
    a non-change as a proposal. `llm_invoked` records whether the LLM had
    already been called before this failure (for the runner's --cold signal)."""
    # C7: `reason` can carry LLM output / exception text / file paths — log it
    # LOCALLY for the operator, but send only a FIXED, server-safe string. A
    # free-text failure reason would be another cross-server channel a
    # prompt-injected model could smuggle content through.
    logger.warning(
        "Implementer settling FAILED for ticket=%s directive=%s: %s",
        ticket_id, directive_id, reason,
    )
    settle_resp = await complete_work_queue_step(
        api_url,
        api_key,
        config.project,
        config.queue_id,
        item_id=item_id,
        directive_id=directive_id,
        ticket_id=ticket_id,
        outcome="posted_progress",
        ticket_lease_epoch=lease_epoch,
        failed=True,
        summary="Resident implementer could not complete this directive "
                "(details in the operator's local logs).",
    )
    return {
        "ticket_id": ticket_id,
        "directive_id": directive_id,
        "settled": False,
        "error": reason,
        "settle_status": settle_resp.status_code,
        "llm_invoked": llm_invoked,
    }
