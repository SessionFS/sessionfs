"""Evaluations v1 (GitHub #121): case liveness, acceptance, trust, baselines,
comparison and the family matrix.

Record-only (the workflow-templates rule): graders run in the customer's own
CI with the customer's keys and post results here. SessionFS computes the
summary and verdict from the stored rows and decides trust from the
credential alone:

* the bound credential must be an active ``trusted_reviewers`` row for the
  suite's ``grader.persona`` (persona taken from the pinned suite, never the
  request), checked at start and again at finish;
* the grader must be independent of whoever produced the subject: the
  artifact's creator, the minters of the key that registered it, the
  credential of ``produced_by_agent_run`` and, when the run names a ticket,
  that ticket's implementers. Keys count as every user who minted or rotated
  them (``review_state.load_key_minters``). Anything unresolvable makes the
  run untrusted (fail closed);
* ``human`` graders must use a user key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.eval_definition import Acceptance, GraderSpec
from sessionfs.server.db.models import (
    AgentRun,
    Artifact,
    EvalCase,
    EvalCaseResult,
    EvalRun,
    EvalSuite,
    Project,
    Ticket,
)

RUN_EXPIRY = timedelta(days=7)
MAX_SUMMARY_TAGS = 50
MAX_LISTED_REGRESSIONS = 100
MAX_COMPARE_ITEMS = 500


def live_clause(lineage_id: str, version: int):
    """Cases live in ``version`` of ``lineage_id``."""
    return and_(
        EvalCase.lineage_id == lineage_id,
        EvalCase.added_in_version <= version,
        or_(EvalCase.retired_in_version.is_(None), EvalCase.retired_in_version > version),
    )


def load_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def suite_grader(suite: EvalSuite) -> GraderSpec | None:
    try:
        return GraderSpec.model_validate(load_json(suite.grader, {}))
    except ValueError:
        return None


def suite_acceptance(suite: EvalSuite) -> Acceptance | None:
    try:
        return Acceptance.model_validate(load_json(suite.acceptance, {}))
    except ValueError:
        return None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def suite_view(row: EvalSuite, *, live_cases: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "lineage_id": row.lineage_id,
        "version": row.version,
        "supersedes_id": row.supersedes_id,
        "active": bool(row.active),
        "project_id": row.project_id,
        "slug": row.slug,
        "name": row.name,
        "description": row.description or "",
        "grader": load_json(row.grader, {}),
        "acceptance": load_json(row.acceptance, {}),
        "case_storage": row.case_storage,
        "case_bank_artifact_id": row.case_bank_artifact_id,
        "definition_sha256": row.definition_sha256,
        "created_by_user_id": row.created_by_user_id,
        "actor_type": row.actor_type,
        "created_at": _iso(row.created_at),
    }
    if live_cases is not None:
        out["live_cases"] = live_cases
    return out


def case_view(row: EvalCase, *, with_text: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "case_key": row.case_key,
        "name": row.name or "",
        "tags": load_json(row.tags, []),
        "content_sha256": row.content_sha256,
        "rubric_notes": row.rubric_notes or "",
        "added_in_version": row.added_in_version,
        "retired_in_version": row.retired_in_version,
    }
    if with_text:
        out["input"] = row.input
        out["expected"] = row.expected
    return out


def run_view(row: EvalRun) -> dict[str, Any]:
    trust_label = "trusted" if row.trusted else "unverified"
    return {
        "id": row.id,
        "project_id": row.project_id,
        "suite_id": row.suite_id,
        "suite_lineage_id": row.suite_lineage_id,
        "artifact_id": row.artifact_id,
        "subject_label": row.subject_label,
        "commit_sha": row.commit_sha,
        "ticket_id": row.ticket_id,
        "trigger_source": row.trigger_source,
        "trigger_ref": row.trigger_ref,
        "agent_run_id": row.agent_run_id,
        "grader_persona": row.grader_persona,
        "actor_type": row.actor_type,
        "service_key_name": row.service_key_name,
        "state": row.state,
        "verdict": row.verdict,
        "trusted": bool(row.trusted),
        "trust_label": trust_label if row.state == "finished" else None,
        "untrusted_reason": row.untrusted_reason,
        "summary": load_json(row.summary, None),
        "baseline_run_id": row.baseline_run_id,
        "voided_reason": row.voided_reason,
        "voided_at": _iso(row.voided_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
    }


def result_view(row: EvalCaseResult) -> dict[str, Any]:
    return {
        "id": row.id,
        "case_id": row.case_id,
        "case_key": row.case_key,
        "verdict": row.verdict,
        "score": float(row.score) if row.score is not None else None,
        "grader_kind": row.grader_kind,
        "notes": row.notes,
        "output": row.output,
        "output_sha256": row.output_sha256,
        "output_cleared": row.output_cleared_at is not None,
        "created_at": _iso(row.created_at),
    }


# ── Lifecycle ──


async def expire_if_stale(db: AsyncSession, run: EvalRun) -> bool:
    """Lazily mark a run ``expired`` after 7 days running (guarded UPDATE).
    Returns True when this call expired it (the caller commits)."""
    started = _utc(run.started_at)
    if run.state != "running" or started is None:
        return False
    if datetime.now(timezone.utc) - started < RUN_EXPIRY:
        return False
    res = await db.execute(
        update(EvalRun)
        .where(EvalRun.id == run.id, EvalRun.state == "running")
        .values(state="expired")
    )
    if res.rowcount == 1:
        if run.agent_run_id:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run.agent_run_id, AgentRun.status.in_(("queued", "running")))
                .values(status="cancelled", completed_at=datetime.now(timezone.utc))
            )
        run.state = "expired"
        return True
    return False


def same_credential(run: EvalRun, auth: Any) -> bool:
    """True when ``auth`` is exactly the credential bound at start."""
    if auth.key_kind == "service":
        return bool(auth.service_key_id) and run.grader_service_key_id == auth.service_key_id
    return run.grader_service_key_id is None and run.grader_user_id == auth.user.id


# ── Trust ──


@dataclass
class Trust:
    trusted: bool
    reason: str | None = None


async def evaluate_trust(
    db: AsyncSession, project: Project, run: EvalRun, grader: GraderSpec | None
) -> Trust:
    """Server-side trust for ``run`` from its bound credential (see module doc)."""
    from sessionfs.server.routes.tickets import is_registered_trusted_reviewer
    from sessionfs.server.services.review_state import (
        load_implementer_identities,
        load_key_minters,
    )

    if grader is None:
        return Trust(False, "suite_grader_unreadable")
    if grader.kind == "human" and run.grader_service_key_id:
        return Trust(False, "human_grader_needs_user_key")
    if not run.grader_user_id:
        return Trust(False, "grader_identity_unresolvable")
    registered = await is_registered_trusted_reviewer(
        db,
        project_id=project.id,
        org_id=project.org_id,
        user_id=run.grader_user_id,
        service_key_id=run.grader_service_key_id,
        claimed_persona=grader.persona,
    )
    if not registered:
        return Trust(False, "grader_not_registered")

    producers: list[tuple[str | None, str | None]] = []
    unresolvable = False
    if run.artifact_id:
        artifact = await db.get(Artifact, run.artifact_id)
        if artifact is None:
            unresolvable = True
        else:
            if artifact.service_key_id:
                producers.append((artifact.service_key_id, None))
            if artifact.created_by_user_id:
                producers.append((None, artifact.created_by_user_id))
            if not artifact.service_key_id and not artifact.created_by_user_id:
                unresolvable = True
            if artifact.produced_by_agent_run_id:
                producer_run = await db.get(AgentRun, artifact.produced_by_agent_run_id)
                if producer_run is None or producer_run.project_id != project.id:
                    unresolvable = True
                else:
                    if producer_run.service_key_id:
                        producers.append((producer_run.service_key_id, None))
                    if producer_run.triggered_by_user_id:
                        producers.append((None, producer_run.triggered_by_user_id))
                    if not producer_run.service_key_id and not producer_run.triggered_by_user_id:
                        unresolvable = True
    if run.ticket_id:
        ticket = await db.get(Ticket, run.ticket_id)
        if ticket is None or ticket.project_id != project.id:
            unresolvable = True
        else:
            producers.extend(
                (svc, usr)
                for svc, usr in (await load_implementer_identities(db, [ticket])).get(ticket.id, [])
                if svc or usr
            )
    if unresolvable:
        return Trust(False, "producer_identity_unresolvable")
    if not producers:
        # Commit-only / free-text subjects (no artifact, no ticket with a
        # resolvable implementer): nobody is known to have produced the
        # thing under test, so independence can't be shown. Fail closed.
        return Trust(False, "producer_identity_unknown")

    grader_key = run.grader_service_key_id
    pseudo = [{"verdict_trusted": True, "service_key_id": grader_key}] if grader_key else []
    minters = await load_key_minters(db, pseudo, producers)
    grader_users: set[str] = {run.grader_user_id}
    if grader_key:
        minted = minters.get(grader_key)
        if not minted:
            return Trust(False, "grader_identity_unresolvable")
        grader_users |= minted
    for svc, usr in producers:
        if usr and usr in grader_users:
            return Trust(False, "grader_not_independent")
        if svc:
            if grader_key and svc == grader_key:
                return Trust(False, "grader_not_independent")
            minted = minters.get(svc)
            if not minted:
                return Trust(False, "producer_identity_unresolvable")
            if minted & grader_users:
                return Trust(False, "grader_not_independent")
    return Trust(True, None)


# ── Summary and verdict ──


@dataclass
class LiveCase:
    id: str
    case_key: str
    tags: list[str]


async def load_live_cases(db: AsyncSession, suite: EvalSuite) -> list[LiveCase]:
    rows = (
        await db.execute(
            select(EvalCase.id, EvalCase.case_key, EvalCase.tags)
            .where(live_clause(suite.lineage_id, suite.version))
            .order_by(EvalCase.case_key)
        )
    ).all()
    return [LiveCase(r[0], r[1], [t for t in load_json(r[2], []) if isinstance(t, str)]) for r in rows]


async def load_results(db: AsyncSession, run_id: str) -> dict[str, tuple[str, str, Decimal | None]]:
    """case_id -> (case_key, verdict, score)."""
    rows = (
        await db.execute(
            select(
                EvalCaseResult.case_id,
                EvalCaseResult.case_key,
                EvalCaseResult.verdict,
                EvalCaseResult.score,
            ).where(EvalCaseResult.run_id == run_id)
        )
    ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def find_baseline(
    db: AsyncSession, run: EvalRun, acceptance: Acceptance
) -> tuple[EvalRun | None, str | None]:
    """The run to check regressions against, or (None, why) when there is
    none. An explicit baseline that is missing/voided/foreign fails closed
    (returns a reason the verdict reports); ``production`` with no
    production version or no trusted pass there means nothing to regress
    against (None, None)."""
    mode = acceptance.no_regression_vs
    if mode == "none":
        return None, None
    if mode == "production":
        if not run.artifact_id:
            return None, None
        subject = await db.get(Artifact, run.artifact_id)
        if subject is None:
            return None, None
        prod_ids = list(
            (
                await db.execute(
                    select(Artifact.id).where(
                        Artifact.project_id == subject.project_id,
                        Artifact.kind == subject.kind,
                        Artifact.name == subject.name,
                        Artifact.stage == "production",
                        Artifact.id != subject.id,
                    )
                )
            ).scalars().all()
        )
        if not prod_ids:
            return None, None
        base = (
            await db.execute(
                select(EvalRun)
                .where(
                    EvalRun.project_id == run.project_id,
                    EvalRun.suite_lineage_id == run.suite_lineage_id,
                    EvalRun.artifact_id.in_(prod_ids),
                    EvalRun.state == "finished",
                    EvalRun.verdict == "passed",
                    EvalRun.trusted.is_(True),
                    EvalRun.id != run.id,
                )
                .order_by(EvalRun.finished_at.desc(), EvalRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return base, None
    base = await db.get(EvalRun, mode)
    if (
        base is None
        or base.project_id != run.project_id
        or base.suite_lineage_id != run.suite_lineage_id
        or base.state != "finished"
        or base.id == run.id
    ):
        return None, "baseline_unavailable"
    return base, None


async def compute_summary(
    db: AsyncSession, run: EvalRun, suite: EvalSuite, acceptance: Acceptance
) -> tuple[dict[str, Any], str]:
    """(summary, verdict) from the stored rows and the acceptance criteria."""
    live = await load_live_cases(db, suite)
    results = await load_results(db, run.id)
    total = len(live)
    graded = passed = failed = errored = skipped = 0
    scores: list[float] = []
    by_tag: dict[str, dict[str, int]] = {}
    for case in live:
        res = results.get(case.id)
        if res is None:
            continue
        _key, verdict, score = res
        graded += 1
        if verdict == "pass":
            passed += 1
        elif verdict == "fail":
            failed += 1
        elif verdict == "error":
            errored += 1
        else:
            skipped += 1
        if score is not None:
            scores.append(float(score))
        for tag in case.tags:
            bucket = by_tag.setdefault(tag, {"cases": 0, "graded": 0, "passed": 0, "not_passed": 0})
            bucket["graded"] += 1
            if verdict == "pass":
                bucket["passed"] += 1
            else:
                bucket["not_passed"] += 1
    for case in live:
        for tag in case.tags:
            by_tag.setdefault(tag, {"cases": 0, "graded": 0, "passed": 0, "not_passed": 0})["cases"] += 1
    missing = total - graded
    pass_rate = (passed / graded) if graded else None
    mean_score = (sum(scores) / len(scores)) if scores else None

    reasons: list[str] = []
    verdict = "passed"
    if graded == 0:
        verdict = "incomplete"
        reasons.append("no case has a result")
    elif acceptance.require_all_cases and missing:
        verdict = "incomplete"
        reasons.append(f"{missing} of {total} cases have no result")

    regressions: list[str] = []
    baseline, baseline_problem = await find_baseline(db, run, acceptance)
    if verdict != "incomplete":
        thresholds_declared = any(
            (
                acceptance.min_pass_rate is not None,
                acceptance.min_mean_score is not None,
                bool(acceptance.min_pass_rate_by_tag),
                bool(acceptance.max_failures_by_tag),
            )
        )
        rate = pass_rate or 0.0
        if not thresholds_declared and passed < graded:
            reasons.append(f"{graded - passed} graded cases did not pass")
        if acceptance.min_pass_rate is not None and rate < acceptance.min_pass_rate:
            reasons.append(f"pass rate {rate:.4f} is below {acceptance.min_pass_rate}")
        if acceptance.min_mean_score is not None:
            if mean_score is None:
                reasons.append("no scores were reported, so the mean score cannot be checked")
            elif mean_score < acceptance.min_mean_score:
                reasons.append(f"mean score {mean_score:.4f} is below {acceptance.min_mean_score}")
        for tag, minimum in sorted(acceptance.min_pass_rate_by_tag.items()):
            bucket = by_tag.get(tag)
            if not bucket or not bucket["graded"]:
                reasons.append(f"no graded case is tagged {tag}")
                continue
            tag_rate = bucket["passed"] / bucket["graded"]
            if tag_rate < minimum:
                reasons.append(f"pass rate for {tag} {tag_rate:.4f} is below {minimum}")
        for tag, maximum in sorted(acceptance.max_failures_by_tag.items()):
            bucket = by_tag.get(tag)
            fails = bucket["not_passed"] if bucket else 0
            if fails > maximum:
                reasons.append(f"{fails} {tag} cases did not pass (at most {maximum})")
        if baseline_problem:
            reasons.append("the regression baseline run is not available")
        elif baseline is not None:
            base_results = await load_results(db, baseline.id)
            base_passed = {key for key, verdict_b, _s in base_results.values() if verdict_b == "pass"}
            reg_tags = set(acceptance.regression_tags)
            for case in live:
                if case.case_key not in base_passed or not reg_tags & set(case.tags):
                    continue
                res = results.get(case.id)
                if res is not None and res[1] != "pass":
                    regressions.append(case.case_key)
            if regressions:
                reasons.append(f"{len(regressions)} regressions against the baseline")
        if reasons:
            verdict = "failed"

    tags_out = {
        tag: {
            **b,
            "pass_rate": round(b["passed"] / b["graded"], 6) if b["graded"] else None,
        }
        for tag, b in sorted(by_tag.items())[:MAX_SUMMARY_TAGS]
    }
    summary = {
        "total_cases": total,
        "graded": graded,
        "passed": passed,
        "failed": failed,
        "error": errored,
        "skip": skipped,
        "missing": missing,
        "pass_rate": round(pass_rate, 6) if pass_rate is not None else None,
        "mean_score": round(mean_score, 6) if mean_score is not None else None,
        "by_tag": tags_out,
        "by_tag_truncated": len(by_tag) > MAX_SUMMARY_TAGS,
        "regressions": regressions[:MAX_LISTED_REGRESSIONS],
        "regression_count": len(regressions),
        "baseline_run_id": baseline.id if baseline is not None else None,
        "reasons": reasons,
    }
    return summary, verdict


# ── Comparison ──


async def compare_runs(
    db: AsyncSession, a: EvalRun, b: EvalRun
) -> dict[str, Any]:
    """Case-by-case comparison of two runs of one suite lineage (``a`` is the
    reference, ``b`` the candidate)."""
    suite_a = await db.get(EvalSuite, a.suite_id)
    suite_b = await db.get(EvalSuite, b.suite_id)
    live_a = {c.case_key: c for c in (await load_live_cases(db, suite_a))} if suite_a else {}
    live_b = {c.case_key: c for c in (await load_live_cases(db, suite_b))} if suite_b else {}
    res_a = {k: (v, s) for k, v, s in (await load_results(db, a.id)).values()}
    res_b = {k: (v, s) for k, v, s in (await load_results(db, b.id)).values()}

    regressions: list[str] = []
    fixes: list[str] = []
    unchanged = {"pass": 0, "not_pass": 0}
    score_deltas: list[dict[str, Any]] = []
    for key in sorted(set(live_a) & set(live_b)):
        va = res_a.get(key)
        vb = res_b.get(key)
        if va is None or vb is None:
            continue
        pa, pb = va[0] == "pass", vb[0] == "pass"
        if pa and not pb:
            regressions.append(key)
        elif pb and not pa:
            fixes.append(key)
        else:
            unchanged["pass" if pa else "not_pass"] += 1
        if va[1] is not None and vb[1] is not None and len(score_deltas) < MAX_COMPARE_ITEMS:
            score_deltas.append(
                {
                    "case_key": key,
                    "a": float(va[1]),
                    "b": float(vb[1]),
                    "delta": round(float(vb[1]) - float(va[1]), 6),
                }
            )

    def tag_rates(live: dict[str, LiveCase], res: dict[str, tuple[str, Any]]) -> dict[str, Any]:
        tags: dict[str, list[int]] = {}
        for key, case in live.items():
            r = res.get(key)
            if r is None:
                continue
            for tag in case.tags:
                bucket = tags.setdefault(tag, [0, 0])
                bucket[0] += 1
                bucket[1] += 1 if r[0] == "pass" else 0
        return {
            t: round(p / g, 6) if g else None
            for t, (g, p) in sorted(tags.items())[:MAX_SUMMARY_TAGS]
        }

    rates_a = tag_rates(live_a, res_a)
    rates_b = tag_rates(live_b, res_b)
    added = sorted(set(live_b) - set(live_a))
    removed = sorted(set(live_a) - set(live_b))
    warnings = []
    if not (a.trusted and b.trusted):
        warnings.append(
            "At least one run is unverified: its results were not posted by an "
            "independent trusted grader."
        )
    return {
        "warnings": warnings,
        "suite_lineage_id": a.suite_lineage_id,
        "a": {"run_id": a.id, "suite_version_id": a.suite_id, "verdict": a.verdict, "trusted": bool(a.trusted), "state": a.state},
        "b": {"run_id": b.id, "suite_version_id": b.suite_id, "verdict": b.verdict, "trusted": bool(b.trusted), "state": b.state},
        "regressions": regressions[:MAX_COMPARE_ITEMS],
        "regression_count": len(regressions),
        "fixes": fixes[:MAX_COMPARE_ITEMS],
        "fix_count": len(fixes),
        "unchanged": unchanged,
        "cases_added": added[:MAX_COMPARE_ITEMS],
        "cases_removed": removed[:MAX_COMPARE_ITEMS],
        "score_deltas": score_deltas,
        "pass_rate_by_tag": {
            tag: {"a": rates_a.get(tag), "b": rates_b.get(tag)}
            for tag in sorted(set(rates_a) | set(rates_b))
        },
    }
