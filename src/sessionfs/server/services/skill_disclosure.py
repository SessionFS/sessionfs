"""Server side of install disclosure, update diffs and D2 re-approval.

Marketplace v2 §3 (tk_d8bc543da8344124). The manifest, the card, the diff
and the update classifier are :mod:`sessionfs.skills.disclosure` (shared
with the CLI and the daemon, so every surface decides the same way). This
module stores manifests on version rows, computes them lazily from the
stored canonical archive for versions published before migration 070, and
enforces re-approval on the org paths: an assignment or install approval for
one version never authorizes an update that widens access or changes
executable content.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    OrgSkillAssignment,
    SkillConsentReceipt,
    SkillInstallRequest,
    SkillPackageVersion,
)
from sessionfs.server.storage.base import BlobStore
from sessionfs.skills import disclosure
from sessionfs.skills.instruction_scan import RULESET_VERSION

logger = logging.getLogger("sessionfs.skill_disclosure")

CONSENT_RULE_VERSION = disclosure.CONSENT_RULE_VERSION

# Plan requirement -> server consent capability (hosted._REQUIREMENT_TO_SERVER_CAPABILITY).
_SERVER_CAPABILITY = {
    "skill-scripts": "scripts",
    "skill-hooks": "hooks",
    "executable-extensions": "hooks",
    "mcp": "mcp",
}

# The provenance flags a trust downgrade is computed from. They are written
# by verified publishers / trusted publishing (tk_a287f7d654f64b1b) into
# signature_metadata; until then every version reads as not verified.
_PROVENANCE_KEYS = ("publisher_verified", "attested")


def _json(raw: str | None, fallback: Any) -> Any:
    try:
        value = json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def grant_scope_key(scope: str, user_id: str | None, device_id: str | None) -> str:
    """The grant scope of an install request, normalized the way approvals
    group (project: one per project; user: per user; device: per user and
    device). Stored on the request: one ACTIVE approval per (project,
    package, grant scope) is enforced by a partial unique index."""
    if scope == "project":
        return "project"
    if scope == "user":
        return f"user:{user_id or ''}"
    return f"device:{user_id or ''}:{device_id or ''}"


async def supersede_active_requests(
    db: AsyncSession, row: SkillInstallRequest, package_id: str, now: Any
) -> list[str]:
    """Supersede every other ACTIVE (approved / applied) request of this
    package in the same project and grant scope: revoked (never granting
    again) with the superseding request recorded, and their consent
    receipts invalidated. Audit history is kept. Runs under the grant-scope
    lock, before the replacement is approved."""
    key = grant_scope_key(row.target_scope, row.target_user_id, row.target_device_id)
    # Lock order shared with moderation's _revoke_grants: request rows (here
    # in id order) BEFORE their consent receipts. The status predicate is
    # re-checked after the row lock, so a request a concurrent block already
    # revoked is left alone (its revocation is never rewritten as a
    # supersession).
    ids = list(
        (
            await db.execute(
                select(SkillInstallRequest.id)
                .where(
                    SkillInstallRequest.project_id == row.project_id,
                    SkillInstallRequest.package_id == package_id,
                    SkillInstallRequest.grant_scope_key == key,
                    SkillInstallRequest.status.in_(("approved", "applied")),
                    SkillInstallRequest.id != row.id,
                    SkillInstallRequest.deleted_at.is_(None),
                )
                .order_by(SkillInstallRequest.id)
                .with_for_update()
            )
        ).scalars().all()
    )
    if not ids:
        return []
    await db.execute(
        update(SkillInstallRequest)
        .where(
            SkillInstallRequest.id.in_(ids),
            SkillInstallRequest.status.in_(("approved", "applied")),
        )
        .values(status="revoked", superseded_at=now, superseded_by_request_id=row.id)
    )
    await db.execute(
        update(SkillConsentReceipt)
        .where(
            SkillConsentReceipt.install_request_id.in_(ids),
            SkillConsentReceipt.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return ids


def grant_scope_lock_key(
    project_id: str, package_id: str, scope: str, user_id: str | None, device_id: str | None
) -> int:
    """Stable signed 64-bit advisory-lock key for one approval scope. The
    dimensions follow the scope exactly as previous_request_version() groups
    grants: project = (project, package); user = + user; device = + user +
    device."""
    if scope == "project":
        user_id, device_id = None, None
    elif scope == "user":
        device_id = None
    raw = "\x1f".join(
        ["sessionfs-skill-grant", project_id, package_id, scope, user_id or "", device_id or ""]
    )
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big", signed=True)


async def lock_grant_scope(db: AsyncSession, key: int) -> None:
    """Serialize approvals of one (project, package, target scope/user/device)
    so two pending versions can never both classify against the same prior
    approval. PostgreSQL: a transaction advisory lock. SQLite: a zero-row
    write that takes the database write lock (the whole transaction is then
    serialized, the same protocol the marketplace uses elsewhere)."""
    connection = await db.connection()
    if connection.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    else:
        await db.execute(text("UPDATE skill_blocked_content_hashes SET id = id WHERE 1 = 0"))


def reject_incomplete_manifest(hosted: Any) -> None:
    manifest = getattr(hosted, "capability_manifest", None)
    if isinstance(manifest, dict) and manifest.get("incomplete"):
        raise HTTPException(
            422,
            detail={
                "code": "invalid_skill_package",
                "message": "The package could not be analyzed completely ("
                + ", ".join(str(item) for item in manifest["incomplete"][:5])
                + "); split very large files or reduce the scanner findings and publish again.",
                "reason": "manifest_incomplete",
            },
        )


def stage_capability_manifest(version: SkillPackageVersion, hosted: Any) -> None:
    """Store the publish-time manifest on a new version row (no flush)."""
    manifest = getattr(hosted, "capability_manifest", None)
    if not disclosure.manifest_known(manifest):
        return
    version.capability_manifest = json.dumps(manifest, sort_keys=True)
    version.capability_digest = disclosure.capability_digest(manifest)
    version.manifest_version = disclosure.MANIFEST_VERSION


def version_provenance(version: SkillPackageVersion) -> dict[str, bool]:
    signature = _json(version.signature_metadata, {})
    return {key: bool(signature.get(key)) for key in _PROVENANCE_KEYS}


def stored_manifest(version: SkillPackageVersion) -> dict[str, Any] | None:
    """The stored manifest when it is CURRENT: same derivation version and
    computed with the running scanner ruleset (persistence targets and
    hidden-character counts come from the scan). After a ruleset change it
    is recomputed lazily; grants bound to the old digest then no longer
    match and fail closed to an unknown baseline."""
    if version.manifest_version != disclosure.MANIFEST_VERSION:
        return None
    manifest = _json(version.capability_manifest, {})
    if not disclosure.manifest_known(manifest):
        return None
    if (manifest.get("scan") or {}).get("ruleset") != RULESET_VERSION:
        return None
    return manifest


def _extract(payload: bytes, root: Path) -> None:
    from sessionfs.skills.publish_validation import _extract_archive

    _extract_archive(payload, root)


def _manifest_from_archive(payload: bytes) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="sfs-skill-disclosure-") as temp:
        root = Path(temp)
        _extract(payload, root)
        return disclosure.compute_manifest(root)


async def version_manifest(
    db: AsyncSession, store: BlobStore, version: SkillPackageVersion, *, persist: bool = True
) -> dict[str, Any] | None:
    """The version's manifest; computed from the stored canonical archive
    (off the event loop) when the row predates migration 070 or an older
    manifest derivation, then persisted. None when the blob is unavailable
    (callers treat that as an unknown baseline)."""
    manifest = stored_manifest(version)
    if manifest is not None:
        return manifest
    payload = await store.get(version.blob_key)
    if payload is None:
        return None
    try:
        manifest = await asyncio.to_thread(_manifest_from_archive, payload)
    except Exception:  # noqa: BLE001 - an unreadable archive is an unknown baseline
        logger.warning("Could not compute the capability manifest of %s", version.id, exc_info=True)
        return None
    if persist:
        await persist_manifest(db, version, manifest)
    return manifest


async def persist_manifest(db: AsyncSession, version: SkillPackageVersion, manifest: dict[str, Any]) -> None:
    """Store a lazily computed manifest. Callers persist several versions
    only in version-id order (the marketplace's row lock order)."""
    if stored_manifest(version) is not None:
        return
    digest = disclosure.capability_digest(manifest)
    encoded = json.dumps(manifest, sort_keys=True)
    await db.execute(
        update(SkillPackageVersion)
        .where(SkillPackageVersion.id == version.id)
        .values(
            capability_manifest=encoded,
            capability_digest=digest,
            manifest_version=disclosure.MANIFEST_VERSION,
        )
    )
    version.capability_manifest = encoded
    version.capability_digest = digest
    version.manifest_version = disclosure.MANIFEST_VERSION


def _diff_trees(
    old_payload: bytes | None,
    new_payload: bytes | None,
    files: dict,
    old_files: dict[str, str] | None,
    new_files: dict[str, str] | None,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="sfs-skill-diff-") as temp:
        old_root = Path(temp) / "old"
        new_root = Path(temp) / "new"
        old_root.mkdir()
        new_root.mkdir()
        if old_payload is not None:
            _extract(old_payload, old_root)
        if new_payload is not None:
            _extract(new_payload, new_root)
        return disclosure.text_diffs(
            old_root if old_payload is not None else None,
            new_root if new_payload is not None else None,
            files,
            old_files=old_files,
            new_files=new_files,
        )


def _diff_unavailable(message: str) -> HTTPException:
    """503 diff_unavailable (structured envelope, short retry): the diff a
    consent decision binds can never be computed from a partial input — a
    missing archive or manifest is never shown as an empty side."""
    return HTTPException(
        503,
        detail={"code": "diff_unavailable", "message": message},
        headers={"Retry-After": "5"},
    )


async def version_diff(
    db: AsyncSession,
    store: BlobStore,
    old: SkillPackageVersion,
    new: SkillPackageVersion,
    *,
    include_text: bool = True,
) -> dict[str, Any]:
    old_manifest = await version_manifest(db, store, old, persist=False)
    new_manifest = await version_manifest(db, store, new, persist=False)
    if new_manifest is None:
        raise _diff_unavailable(
            "The target package's capability manifest is unavailable, so the update "
            "diff cannot be computed; retry shortly."
        )
    # Persist lazily computed manifests in version-id order only.
    for version, manifest in sorted(((old, old_manifest), (new, new_manifest)), key=lambda pair: pair[0].id):
        if manifest is not None:
            await persist_manifest(db, version, manifest)
    result = disclosure.build_diff(
        old_manifest,
        new_manifest,
        old_content_hash=old.content_hash,
        new_content_hash=new.content_hash,
        old_provenance=version_provenance(old),
        new_provenance=version_provenance(new),
        include_text=False,
    )
    if include_text:
        # A text diff needs BOTH archives, whether or not the manifests could
        # be computed: diffing with a missing side would show the absent side
        # as empty (fabricated additions/removals).
        old_payload = await store.get(old.blob_key)
        new_payload = await store.get(new.blob_key)
        if old_payload is None or new_payload is None:
            raise _diff_unavailable(
                "A package archive is unavailable, so the text diff cannot be computed; "
                "retry shortly."
            )
        if old_manifest is not None:
            try:
                result["text_diffs"] = await asyncio.to_thread(
                    _diff_trees,
                    old_payload,
                    new_payload,
                    result["files"],
                    old_manifest.get("files"),
                    new_manifest.get("files"),
                )
            except Exception:  # noqa: BLE001 - text diffs are display only
                logger.warning("Text diff failed for %s -> %s", old.id, new.id, exc_info=True)
                result["text_diffs"] = {"entries": [], "truncated": True, "unavailable": True}
    return result


async def classify_versions(
    db: AsyncSession, store: BlobStore, old: SkillPackageVersion, new: SkillPackageVersion
) -> dict[str, Any]:
    """D2 classification of old -> new (no text diff)."""
    return await version_diff(db, store, old, new, include_text=False)


async def require_manifest(
    db: AsyncSession, store: BlobStore, version: SkillPackageVersion
) -> dict[str, Any]:
    """A complete manifest and a verified digest, or 503: a grant is never
    created with an unbound digest (e.g. during a blob-store outage)."""
    manifest = await version_manifest(db, store, version)
    if (
        manifest is None
        or manifest.get("incomplete")
        or not version.capability_digest
        or version.capability_digest != disclosure.capability_digest(manifest)
    ):
        raise HTTPException(
            503,
            detail={
                "code": "disclosure_unavailable",
                "message": "The package's capability manifest could not be computed; retry shortly.",
            },
            headers={"Retry-After": "5"},
        )
    return manifest


def require_displayed_disclosure(
    version: SkillPackageVersion,
    digest: str | None,
    rule_version: int | None,
    *,
    disclosure_digest: str | None = None,
    manifest: dict[str, Any] | None = None,
    tools: list[str] | None = None,
    scope: str = "project",
) -> None:
    """When the caller says which disclosure it showed (capability digest
    and consent rule), it must be the version's CURRENT one (checked after
    require_manifest, under the caller's version / content lock). A
    disclosure computed under an older scanner ruleset or consent rule is
    refused with 409 disclosure_changed so the approver reloads it. Callers
    that send neither approve the access computed at decision time."""
    if digest is None and rule_version is None and disclosure_digest is None:
        return
    current_card = None
    if disclosure_digest is not None and manifest is not None:
        current_card = disclosure.build_disclosure(
            manifest,
            tools=tools or None,
            scope=scope,
            version_id=version.id,
            content_hash=version.content_hash,
            provenance=version_provenance(version),
        )["disclosure_digest"]
    if (
        (digest is not None or rule_version is not None)
        and (digest != version.capability_digest or rule_version != CONSENT_RULE_VERSION)
    ) or (disclosure_digest is not None and disclosure_digest != current_card):
        raise HTTPException(
            409,
            detail={
                "code": "disclosure_changed",
                "message": "The package's disclosure changed since it was shown; review it again.",
                "capability_digest": version.capability_digest,
                "consent_rule_version": CONSENT_RULE_VERSION,
                "disclosure_digest": current_card,
            },
        )


def grant_binding_valid(
    digest: str | None, rule_version: int | None, version: SkillPackageVersion
) -> bool:
    """A prior grant (approved request or assignment) is a usable D2 baseline
    only when it was bound under the CURRENT consent rule to the previous
    version's digest. Anything else is an unknown baseline (widening)."""
    return (
        bool(digest)
        and rule_version == CONSENT_RULE_VERSION
        and digest == version.capability_digest
    )


async def classify_grant_update(
    db: AsyncSession,
    store: BlobStore,
    old: SkillPackageVersion,
    new: SkillPackageVersion,
    *,
    digest: str | None,
    rule_version: int | None,
    persist: bool = True,
) -> dict[str, Any]:
    """Classify old -> new against the prior GRANT: with a stale or legacy
    binding the baseline is unknown, so the update is widening and nothing
    carries forward. The old version's manifest is never persisted here
    (its row is not locked, so writing it would break the version lock
    order). The TARGET manifest is always required — the full build_diff()
    result is what update_digest binds and what every surface renders, for
    known and unknown baselines alike."""
    bound = bool(digest) and rule_version == CONSENT_RULE_VERSION
    old_manifest = stored_manifest(old)
    if old_manifest is None and bound:
        # Stored with an older scanner ruleset: recompute IN MEMORY (never
        # persisted here, so the old row is not written out of lock order).
        # Nothing new detected -> the same digest -> the binding still holds;
        # a capability only the new ruleset sees changes the digest and the
        # grant fails closed below.
        old_manifest = await version_manifest(db, store, old, persist=False)
    new_manifest = await _target_manifest(db, store, new, persist=persist)
    if new_manifest is None:
        # Read-only paths only (grant paths raise in require_manifest first):
        # without the target manifest there is no diff to show, so the update
        # can never be acknowledged.
        raise _diff_unavailable(
            "The target package's capability manifest is unavailable, so the update "
            "diff cannot be computed; retry shortly."
        )
    if (
        bound
        and old_manifest is not None
        and not old_manifest.get("incomplete")
        and digest == disclosure.capability_digest(old_manifest)
    ):
        return disclosure.build_diff(
            old_manifest,
            new_manifest,
            old_content_hash=old.content_hash,
            new_content_hash=new.content_hash,
            old_provenance=version_provenance(old),
            new_provenance=version_provenance(new),
            include_text=False,
        )
    # Stale or legacy binding: the baseline is unknown (D2 fails closed to
    # widening), but the diff is still built against the target's real
    # manifest so the update_digest binds the scan, unicode and provenance of
    # exactly what is shown — a rescan or a provenance change to the same
    # target version invalidates a prior acknowledgement.
    return disclosure.build_diff(
        None,
        new_manifest,
        new_content_hash=new.content_hash,
        new_provenance=version_provenance(new),
        include_text=False,
    )


async def _target_manifest(
    db: AsyncSession, store: BlobStore, version: SkillPackageVersion, *, persist: bool
) -> dict[str, Any] | None:
    """The grant target's manifest. Grant paths (version locked) require a
    complete, persisted one; read-only listings compute it in memory and
    write nothing (no row lock is taken by a GET)."""
    if persist:
        return await require_manifest(db, store, version)
    manifest = await version_manifest(db, store, version, persist=False)
    if manifest is None or manifest.get("incomplete"):
        return None
    return manifest


def update_digest(
    *,
    prior_id: str | None,
    old: SkillPackageVersion,
    new: SkillPackageVersion,
    bound_digest: str | None,
    update: dict[str, Any],
) -> str:
    """Digest of exactly what a re-consent covers: the prior grant, the
    baseline version / hash / bound digest, the target, and the COMPLETE
    build_diff() result (classification, files, scan / unicode / provenance
    delta, consent). An acknowledgement is only valid for the same value,
    recomputed under the grant lock: an admin can never acknowledge an update
    without being cryptographically bound to the whole diff that was shown."""
    payload = {
        "prior_id": prior_id,
        "baseline": {"version_id": old.id, "content_hash": old.content_hash, "digest": bound_digest},
        "target": {"version_id": new.id, "content_hash": new.content_hash},
        "update": update,
    }
    return disclosure._sha(disclosure._canonical(payload))


def reapproval_error(
    update: dict[str, Any], *, widening_flag: str, code_flag: str, digest: str | None = None, changed: bool = False
) -> HTTPException:
    return HTTPException(
        409,
        detail={
            "code": "reapproval_required",
            "update_digest": digest,
            # The previously shown diff is stale: review this one instead.
            "diff_changed": changed,
            "message": (
                "This version widens access compared with the version already approved; "
                f"review the diff and confirm with {widening_flag}=true."
                if update.get("update_class") == "widening"
                else "This version changes executable content compared with the version already "
                f"approved; review the diff and confirm with {code_flag}=true."
                if update.get("update_class") == "code"
                else "This update changed since it was reviewed; review the updated diff and advance again."
            ),
            "update_class": update.get("update_class"),
            "baseline_known": update.get("baseline_known"),
            "widens": update.get("widens") or [],
            "narrows": update.get("narrows") or [],
            "code_changes": update.get("code_changes") or [],
            # The exact build_diff() object update_digest hashes, so every
            # surface renders precisely what an acknowledgement binds.
            "diff": update,
        },
    )


def enforce_reapproval(
    update: dict[str, Any],
    *,
    approve_widening: bool,
    approve_code_change: bool,
    trusted_code: bool,
    widening_flag: str,
    code_flag: str,
    expected_digest: str | None = None,
    given_digest: str | None = None,
    require_digest: bool = False,
) -> None:
    """D2 on an org path. Widening always needs explicit approval; a code
    change needs it unless the trusted-publisher hook says otherwise; text
    updates need nothing beyond the action itself. An acknowledgement must
    name the update_digest of the diff that was reviewed: if the baseline or
    the diff changed since (another approval superseded the prior grant, a
    rescan, ...), the new diff is returned and consent is asked again.

    ``require_digest`` (the assignment-advance path) also binds text and
    unchanged advances to the diff that was reviewed: no approval flag is
    needed, but a missing or mismatched update_digest still returns the
    current diff for a fresh review."""
    kind = update.get("update_class")
    needs = (kind == "widening") or (kind == "code" and not trusted_code)
    if not needs and not require_digest:
        return
    if needs:
        acknowledged = approve_widening if kind == "widening" else approve_code_change
        if not acknowledged:
            raise reapproval_error(update, widening_flag=widening_flag, code_flag=code_flag, digest=expected_digest)
    if expected_digest is not None and given_digest != expected_digest:
        raise reapproval_error(
            update, widening_flag=widening_flag, code_flag=code_flag, digest=expected_digest,
            changed=given_digest is not None,
        )


async def previous_assignment_version(
    db: AsyncSession,
    *,
    org_id: str,
    package_id: str,
    version_id: str,
    target_type: str,
    target_id: str,
    target_user_id: str | None,
) -> tuple[OrgSkillAssignment, SkillPackageVersion] | None:
    """The newest ACTIVE assignment of another version of the same package to
    the same target in this org: the version a new assignment updates."""
    rows = (
        await db.execute(
            select(OrgSkillAssignment, SkillPackageVersion)
            .join(SkillPackageVersion, SkillPackageVersion.id == OrgSkillAssignment.version_id)
            .where(
                OrgSkillAssignment.org_id == org_id,
                OrgSkillAssignment.revoked_at.is_(None),
                OrgSkillAssignment.target_type == target_type,
                OrgSkillAssignment.target_id == target_id,
                SkillPackageVersion.package_id == package_id,
                SkillPackageVersion.id != version_id,
            )
            .order_by(OrgSkillAssignment.created_at.desc(), OrgSkillAssignment.id.desc())
        )
    ).all()
    for assignment, version in rows:
        if target_type != "device" or assignment.target_user_id == target_user_id:
            return assignment, version
    return None


async def previous_request_version(
    db: AsyncSession, row: SkillInstallRequest, package_id: str
) -> tuple[SkillInstallRequest, SkillPackageVersion] | None:
    """The newest approved / applied request in the same project, for another
    version of the same package and the same target: the version this
    request updates."""
    candidates = (
        await db.execute(
            select(SkillInstallRequest, SkillPackageVersion)
            .join(SkillPackageVersion, SkillPackageVersion.id == SkillInstallRequest.version_id)
            .where(
                SkillInstallRequest.project_id == row.project_id,
                SkillInstallRequest.id != row.id,
                SkillInstallRequest.status.in_(("approved", "applied")),
                SkillInstallRequest.deleted_at.is_(None),
                SkillInstallRequest.target_scope == row.target_scope,
                SkillPackageVersion.package_id == package_id,
                SkillPackageVersion.id != row.version_id,
            )
            .order_by(SkillInstallRequest.decided_at.desc(), SkillInstallRequest.id.desc())
        )
    ).all()
    for request, version in candidates:
        if row.target_scope == "project":
            return request, version
        if request.target_user_id == row.target_user_id and (
            row.target_scope != "device" or request.target_device_id == row.target_device_id
        ):
            return request, version
    return None


async def carried_receipts(
    db: AsyncSession, previous: SkillInstallRequest, update: dict[str, Any]
) -> dict[str, list[SkillConsentReceipt]]:
    """Valid receipts of the previous approval (unrevoked, not archived, bound
    to that request, project and exact hash) for the capabilities that carry
    forward under D2: all of them for a text update, all but the changed
    capabilities for a code update, none for a widening update."""
    kind = update.get("update_class")
    if kind not in {"text", "unchanged", "code"} or not update.get("baseline_known", True):
        return {}
    bound = previous.approved_capability_digest
    if not bound or previous.approved_consent_rule_version != CONSENT_RULE_VERSION:
        return {}
    changed = {
        _SERVER_CAPABILITY[item]
        for item in update.get("changed_requirements") or []
        if item in _SERVER_CAPABILITY
    } if kind == "code" else set()
    rows = (
        await db.execute(
            select(SkillConsentReceipt).where(
                SkillConsentReceipt.install_request_id == previous.id,
                SkillConsentReceipt.project_id == previous.project_id,
                SkillConsentReceipt.content_hash == previous.content_hash,
                SkillConsentReceipt.revoked_at.is_(None),
                SkillConsentReceipt.deleted_at.is_(None),
                # Only receipts written under the current rule, for exactly
                # the access that approval bound.
                SkillConsentReceipt.consent_rule_version == CONSENT_RULE_VERSION,
                SkillConsentReceipt.capability_digest == bound,
            )
        )
    ).scalars().all()
    carried: dict[str, list[SkillConsentReceipt]] = {}
    for receipt in rows:
        if receipt.capability == "instructions" or receipt.capability in changed:
            continue
        carried.setdefault(receipt.capability, []).append(receipt)
    return carried


async def recheck_carried_sources(
    db: AsyncSession, carried: dict[str, list[SkillConsentReceipt]]
) -> None:
    """Lock the carried source receipts and refuse if any was revoked or
    archived since it was read (409 consent_changed; nothing is written).

    Lock order (shared with moderation's _revoke_grants and
    supersede_active_requests): the source approval's REQUEST row first,
    then its receipts. Locking the receipts first and the request later
    (when it is superseded) would invert a concurrent block's
    request-then-receipt order and could deadlock on PostgreSQL."""
    ids = sorted({receipt.id for receipts in carried.values() for receipt in receipts})
    if not ids:
        return
    request_ids = sorted(
        {
            receipt.install_request_id
            for receipts in carried.values()
            for receipt in receipts
            if receipt.install_request_id is not None
        }
    )
    active_requests = set(
        (
            await db.execute(
                select(SkillInstallRequest.id)
                .where(
                    SkillInstallRequest.id.in_(request_ids),
                    SkillInstallRequest.status.in_(("approved", "applied")),
                    SkillInstallRequest.deleted_at.is_(None),
                )
                .order_by(SkillInstallRequest.id)
                .with_for_update()
            )
        ).scalars().all()
    )
    if active_requests != set(request_ids):
        raise _consent_changed()
    valid = set(
        (
            await db.execute(
                select(SkillConsentReceipt.id)
                .where(
                    SkillConsentReceipt.id.in_(ids),
                    SkillConsentReceipt.revoked_at.is_(None),
                    SkillConsentReceipt.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars().all()
    )
    if valid != set(ids):
        raise _consent_changed()


def _consent_changed() -> HTTPException:
    return HTTPException(
        409,
        detail={
            "code": "consent_changed",
            "message": "The previous approval this update builds on was revoked; review and approve again.",
        },
    )


def carry_source(
    receipts: list[SkillConsentReceipt], target_tool: str | None, scope: str
) -> SkillConsentReceipt | None:
    """The receipt that covers (capability, target_tool, scope): the same
    tool, or an unrestricted (target_tool NULL) receipt, in the same scope.
    A receipt for a specific tool never covers an unrestricted grant or
    another tool. None when nothing covers it (never an arbitrary one)."""
    ordered = sorted(
        (item for item in receipts if item.scope == scope),
        key=lambda item: (item.target_tool is None, item.id),
    )
    for item in ordered:
        if target_tool is None:
            if item.target_tool is None:
                return item
        elif item.target_tool in (target_tool, None):
            return item
    return None


def receipt_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    """Columns a consent receipt records about what was consented to. Only
    called with a manifest from require_manifest (never unbound)."""
    if not disclosure.manifest_known(manifest):
        raise HTTPException(503, "The package's capability manifest is unavailable")
    return {
        "capability_digest": disclosure.capability_digest(manifest),
        "consented_manifest": json.dumps(disclosure.access_manifest(manifest), sort_keys=True),
        "consent_rule_version": disclosure.CONSENT_RULE_VERSION,
    }
