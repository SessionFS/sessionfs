"""High-level local package manager used by CLI and provenance capture."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sessionfs.project_root import resolve_project_root
from sessionfs.skills import disclosure
from sessionfs.skills.adapters import PROJECT_SKILL_ROOTS, USER_SKILL_ROOTS, plan_package
from sessionfs.skills.models import (
    SUPPORTED_TOOLS,
    EmitPlan,
    PackageChangedConcurrently,
    PackageRecord,
    SkillError,
    SourceIdentity,
)
from sessionfs.skills.package import import_package, load_package_record
from sessionfs.skills.package import local_package_id
from sessionfs.skills.path_safety import is_link_or_reparse
from sessionfs.skills.registry import RegistryStore
from sessionfs.skills.sources import materialize_source
from sessionfs.skills.state import (
    TRASH_RETENTION_SECONDS,
    SkillStateStore,
    interprocess_lock,
    ordered_lock,
    park_stamp,
    prune_trash,
    safe_component,
    trash_root,
    trash_usage,
)


def _row_revision(row: dict[str, Any]) -> int:
    """State-row revision a prepared operation planned against (legacy rows: 0)."""
    value = row.get("revision", 0)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


_CONTENT_DIGEST_RE = re.compile(r"^(?:sha256:)?(?P<digest>[0-9a-fA-F]{64})$")
_REGISTRY_QUALIFIED_RE = re.compile(r"^(?:project|user):")

# An import staging directory (`<digest>.tmp-<pid>-<uuid>`) older than this
# belongs to no live import; younger ones may be another process's in-flight
# copy and are never touched by doctor.
STALE_STAGING_SECONDS = 60 * 60


def _is_explicit_path(source: str) -> bool:
    """`./x`, `../x`, `~/x`, absolute, `.`/`..`: unambiguously a filesystem path."""
    return (
        source in {".", ".."}
        or source.startswith(("./", "../", "~", "/", ".\\", "..\\"))
        or Path(source).is_absolute()
    )


@dataclass(frozen=True)
class UpdatePreparation:
    """A fetched-but-not-applied update, ready for review and confirmation."""

    current: dict[str, Any]
    lock_row: dict[str, Any] | None
    package: PackageRecord
    plans: list[EmitPlan]
    scope: str
    # Marketplace catalog fetched during prepare, persisted ONLY by apply_update
    # (declining the update must leave marketplaces.json untouched), fenced on
    # the commit that was registered when it was fetched.
    refreshed_registry: Any = None
    registry_commit_before: str | None = None
    # Marketplace v2 §3.4: the installed baseline (the consented access
    # manifest, or one recomputed from the cached version; None = unknown),
    # the new version's manifest, and the D2 classification + diff.
    old_manifest: dict[str, Any] | None = None
    new_manifest: dict[str, Any] | None = None
    diff: dict[str, Any] | None = None

    @property
    def update_class(self) -> str:
        return str((self.diff or {}).get("update_class") or "widening")

    @property
    def widenings(self) -> list[dict[str, str]]:
        return list((self.diff or {}).get("widens") or [])

    @property
    def consented_requirements(self) -> set[str]:
        """Consent requirements the installed version was consented with."""
        receipts = self.current.get("consent_receipts") or []
        return {
            str(item.get("capability"))
            for item in receipts
            if isinstance(item, dict) and item.get("capability")
        }

    @property
    def current_capabilities(self) -> tuple[str, ...]:
        row = self.lock_row or {}
        declared = row.get("required_capabilities")
        if isinstance(declared, list):
            return tuple(sorted({str(item) for item in declared}))
        receipts = self.current.get("consent_receipts") or []
        return tuple(
            sorted(
                {
                    str(item.get("capability"))
                    for item in receipts
                    if isinstance(item, dict) and item.get("capability")
                }
            )
        )

    @property
    def new_capabilities(self) -> tuple[str, ...]:
        return tuple(
            sorted({requirement for plan in self.plans for requirement in plan.consent_requirements})
        )

    @property
    def escalations(self) -> tuple[str, ...]:
        return tuple(item for item in self.new_capabilities if item not in self.current_capabilities)

    @property
    def dropped_capabilities(self) -> tuple[str, ...]:
        return tuple(item for item in self.current_capabilities if item not in self.new_capabilities)


def _expands_installed(installed_row: dict[str, Any] | None, plans: list[EmitPlan]) -> bool:
    """Would applying ``plans`` differ from what the installed row recorded:
    a target tool it does not have, a (capability, tool, scope) it has no
    consent receipt for, or - for a tool it keeps - a plan whose hash is not
    the recorded ``plan_hashes[tool]`` (an adapter update or a newly
    resolved placeholder would write new or changed outputs)? Dropping
    targets is allowed when every kept plan matches exactly. No installed
    row, or no recorded hash, means it is not an exact re-apply."""
    if not isinstance(installed_row, dict):
        return True
    recorded = installed_row.get("plan_hashes")
    if not isinstance(recorded, dict):
        return True
    for plan in plans:
        if not plan.plan_hash or recorded.get(plan.tool) != plan.plan_hash:
            return True
    targets = {str(tool) for tool in installed_row.get("targets") or []}
    receipts = {
        (item.get("capability"), item.get("tool"), item.get("scope"))
        for item in installed_row.get("consent_receipts") or []
        if isinstance(item, dict)
    }
    for plan in plans:
        if plan.tool not in targets:
            return True
        for capability in {"instructions", *plan.consent_requirements}:
            if (capability, plan.tool, plan.scope) not in receipts:
                return True
    return False


class SkillManager:
    def __init__(
        self,
        *,
        project_root: Path | None = None,
        home_dir: Path | None = None,
        store_root: Path | None = None,
        hosted_client: Any = None,
    ) -> None:
        # Hosted marketplace client (sessionfs:<ns>/<name>); resolved lazily
        # from the active CLI identity when not injected.
        self._hosted_client = hosted_client
        # Project scope is the REPOSITORY root (git toplevel), not the cwd:
        # `sfs skill add` from `repo/src` must land in `repo/.sessionfs` and
        # `repo/.codex/skills`, where provenance and the tools look.
        self.project_root = (project_root or resolve_project_root()).resolve()
        self.home_dir = (home_dir or Path.home()).resolve()
        self.store_root = Path(
            os.path.abspath(store_root or self.home_dir / ".sessionfs" / "skills")
        )
        self.registries = RegistryStore(self.project_root, self.home_dir)
        self._held_locks = threading.local()

    def _validate_store_root(self) -> None:
        try:
            relative = self.store_root.relative_to(self.home_dir)
        except ValueError as exc:
            raise SkillError("The skill content store must be inside the user's home directory") from exc
        current = self.home_dir
        for part in relative.parts:
            current /= part
            if is_link_or_reparse(current):
                raise SkillError(f"Skill content store traverses a symlink: {current}")
            if current.exists() and current != self.store_root and not current.is_dir():
                raise SkillError(f"Skill content store parent is not a directory: {current}")

    def state_store(self, scope: str) -> SkillStateStore:
        if scope == "project":
            roots = tuple(
                self.project_root / value for value in sorted(set(PROJECT_SKILL_ROOTS.values()))
            )
            return SkillStateStore(
                self.project_root / ".sessionfs",
                scope_root=self.project_root,
                allowed_output_roots=roots,
                store_root=self.store_root,
            )
        if scope == "user":
            roots = tuple(
                self.home_dir / value for value in sorted(set(USER_SKILL_ROOTS.values()))
            )
            return SkillStateStore(
                self.store_root,
                scope_root=self.home_dir,
                allowed_output_roots=roots,
                store_root=self.store_root,
            )
        raise SkillError("Skill scope must be 'project' or 'user'")

    @contextmanager
    def _operation_lock(self, scope: str):
        """Per-scope interprocess operation lock, re-entrant within a thread so
        an outer holder (e.g. add() spanning resolve + import + install, or
        remove_registry spanning scan + removal) can call locked helpers."""
        store = self.state_store(scope)
        store.validate_root()
        held: dict[str, int] = getattr(self._held_locks, "counts", None) or {}
        self._held_locks.counts = held
        if held.get(scope, 0) > 0:
            held[scope] += 1  # re-entrant within this thread: nothing acquired
            try:
                yield store
            finally:
                held[scope] -= 1
            return
        # Lock order: scope lock -> operation lock -> hosted-state lock.
        with ordered_lock("operation"):
            with interprocess_lock(store.root / "skills.operation.lock"):
                held[scope] = 1
                try:
                    yield store
                finally:
                    held[scope] = 0

    @contextmanager
    def explicit_change(self, scope: str):
        """Locks for a USER-initiated install/remove that also updates pull
        bookkeeping, in the one global order: scope lock -> operation lock
        (the hosted-state lock is taken inside by the bookkeeping)."""
        from sessionfs.skills import hosted

        with hosted._scope_lock(self, scope), self._operation_lock(scope) as store:
            yield store

    def hosted_client(self) -> Any:
        """The hosted marketplace client (signed-in identity required)."""
        from sessionfs.skills import hosted

        if self._hosted_client is None:
            self._hosted_client = hosted.client_from_auth()
        return hosted.require_client(self._hosted_client)

    def hosted_client_for(self, origin: str) -> Any:
        """A client for the server a hosted package is BOUND to.

        Update, locked sync and re-downloads must talk to the server the
        package came from, never to whichever server the active profile
        happens to point at: the active client is used only when it is that
        server, otherwise a configured profile for that server; with none, a
        clear refusal (a different server must never pass the hash checks for
        a package id it did not publish).
        """
        from sessionfs.skills import hosted

        candidates = []
        if self._hosted_client is not None:
            candidates.append(self._hosted_client)
        else:
            active = hosted.client_from_auth()
            if active is not None:
                candidates.append(active)
        for candidate in candidates:
            if hosted.canonical_origin(candidate.api_url) == origin:
                return candidate
        for client in hosted.clients_from_profiles():
            if hosted.canonical_origin(client.api_url) == origin:
                return client
        raise SkillError(
            f"This package was installed from {origin}, but no signed-in profile is configured "
            f"for that server. Sign in to it (sfs auth login --url {origin} --profile <name>) "
            "and retry; packages are only ever re-downloaded from the server they came from"
        )

    def _hosted_origin_for(self, source: dict[str, Any], *, rebind_origin: bool) -> str:
        """Server a hosted row is bound to; a legacy unbound row is refused
        unless the caller explicitly re-binds it to the ACTIVE server."""
        from sessionfs.skills import hosted

        origin = hosted.hosted_origin(source)
        if origin is not None:
            return origin
        active = self.hosted_client()
        active_origin = hosted.canonical_origin(active.api_url)
        if not rebind_origin:
            raise SkillError(
                "This hosted package was installed before SessionFS recorded which server it "
                f"came from. Re-run with --rebind-origin to bind it to {active_origin} (only if "
                "that is the server you installed it from)"
            )
        return active_origin

    def _installed_hash(self, scope: str, package_id: str) -> str | None:
        try:
            row = self.state_store(scope).state()["packages"].get(package_id)
        except SkillError:
            return None
        return str(row.get("content_hash")) if isinstance(row, dict) else None

    def _assert_not_revoked(
        self,
        package: PackageRecord,
        *,
        installed_hash: str | None,
        installed_row: dict[str, Any] | None = None,
        plans: list[EmitPlan] | None = None,
        register: bool = True,
    ) -> None:
        """Blocked content is never installed; yanked content only stays if it
        is already the installed version (compile / sync of an existing row).
        Judged with the install's own provenance, or - for a re-apply of the
        same content - the installed row's. ``plans`` let a quarantined
        version's re-apply be checked for expansion beyond the installed
        row's recorded targets and consented capabilities. `register=False`
        skips the online identity registration (the caller did it before
        taking the revocation-state lock)."""
        from sessionfs.skills import hosted

        if (
            package.provenance is None
            and isinstance(installed_row, dict)
            and installed_row.get("content_hash") == package.content_hash
            and isinstance(installed_row.get("provenance"), dict)
        ):
            package = replace(package, provenance=dict(installed_row["provenance"]))
        package_id = hosted.hosted_package_id(package.source)
        # For a HOSTED package (which already involves its server), a
        # credential not known yet (e.g. a rotated key) is mapped to its
        # namespace first when online; otherwise - local packages, offline -
        # the last-user fallback applies without any network access.
        if package_id is not None and register:
            hosted.ensure_identity_registered(self)
        fingerprint, active_origin = hosted.current_identity(self)
        hosted_state = hosted.HostedState(self.store_root).read()
        hosted.assert_install_allowed(
            self.store_root,
            content_hash=package.content_hash,
            version_id=package.source.commit if package_id else None,
            package_id=package_id,
            installed_hash=installed_hash,
            fingerprint=fingerprint,
            # Revocations of the server the package is bound to.
            origin=hosted.hosted_origin(package.source) or active_origin,
            # Org-scoped entries only reach the org's own NON-PUBLIC copy.
            owner_org_id=hosted.package_owner(hosted_state, package.source, package.provenance),
            visibility=hosted.package_visibility(hosted_state, package.source, package.provenance),
            expands=plans is not None and _expands_installed(installed_row, plans),
        )

    def _import_hosted(
        self,
        source: str,
        *,
        pin: str | None,
        subdir: str,
        store_root: Path,
    ) -> PackageRecord:
        from sessionfs.skills import hosted

        if subdir:
            raise SkillError("--subdir is not applicable to a hosted marketplace package")
        ref = hosted.parse_hosted_ref(source)
        if pin:
            if ref.version and ref.version != pin:
                raise SkillError("Give the hosted version either as @version or --pin, not both")
            ref = hosted.parse_hosted_ref(f"{ref.display.split('@', 1)[0]}@{pin}")
        client = self.hosted_client()
        return hosted.import_hosted_version(client, client.resolve(ref), store_root)

    def import_source(
        self,
        source: str,
        *,
        pin: str | None = None,
        subdir: str = "",
        scope: str | None = None,
        allow_dlp_findings: bool = False,
        _store_root: Path | None = None,
    ) -> PackageRecord:
        target_store = _store_root or self.store_root
        if _store_root is None:
            self._validate_store_root()
        if source.startswith("sessionfs:"):
            # Hosted SessionFS Marketplace: resolve + hash-verified download.
            return self._import_hosted(source, pin=pin, subdir=subdir, store_root=target_store)
        # Explicit remote locators are Git BEFORE any filesystem check: a repo
        # can ship a directory literally named "https:/example.com/repo"; a
        # scope-qualified locator (`project:alias/pkg`) is registry-only.
        is_remote = source.startswith(("https://", "ssh://", "git@"))
        registry_only = bool(_REGISTRY_QUALIFIED_RE.match(source))
        local = Path(source).expanduser()
        if is_remote or (not registry_only and local.exists()):
            if not is_remote and not _is_explicit_path(source):
                # A local directory literally named `<alias>/<package>` must
                # not silently shadow a registered marketplace listing.
                scopes = self.registries.listing_scopes(source)
                if scopes:
                    qualified = " or ".join(f"'{scope}:{source}'" for scope in scopes)
                    raise SkillError(
                        f"'{source}' is both a local directory and a marketplace listing "
                        f"({', '.join(scopes)} scope); use './{source}' for the local "
                        f"directory or {qualified} for the marketplace package"
                    )
            kind = "git" if is_remote else "local"
            with materialize_source(source, pin=pin, subdir=subdir, kind=kind) as materialized:
                identity = materialized.identity
                if identity.source_type == "local" and scope == "project":
                    try:
                        relative = Path(identity.locator).relative_to(self.project_root)
                    except ValueError as exc:
                        raise SkillError(
                            "Project-scoped local packages must live inside the project root"
                        ) from exc
                    identity = replace(
                        identity,
                        source_type="local-project",
                        locator=relative.as_posix() or ".",
                    )
                return import_package(
                    materialized.path,
                    target_store,
                    identity,
                    allow_dlp_findings=allow_dlp_findings,
                )
        registry, package = self.registries.resolve(source)
        package_source = package.get("external_source") or registry.source
        package_subdir = str(package.get("subdir") or "")
        if package.get("external_source"):
            # External git listing: an explicit --pin wins, else the catalog's
            # declared ref/sha, else the listing's HEAD.
            package_pin = pin or package.get("external_ref") or None
        else:
            if pin:
                raise SkillError(
                    "--pin is not applicable to a marketplace listing that lives inside the "
                    "marketplace repository; refresh the marketplace to move its commit"
                )
            package_pin = registry.commit
        with materialize_source(
            package_source,
            pin=package_pin,
            subdir=package_subdir,
            source_type="marketplace",
            registry_scope=registry.scope,
            registry_id=registry.registry_id,
            registry_alias=registry.alias,
            listing_name=str(package.get("name")),
        ) as materialized:
            return import_package(materialized.path, target_store, materialized.identity)

    def inspect_source(
        self,
        source: str,
        *,
        pin: str | None = None,
        subdir: str = "",
        scope: str = "project",
    ) -> PackageRecord:
        """Validate a source without mutating the persistent content store."""
        with tempfile.TemporaryDirectory(prefix="sfs-skill-inspect-") as tmp:
            return self.import_source(
                source,
                pin=pin,
                subdir=subdir,
                scope=scope,
                _store_root=Path(tmp),
            )

    @staticmethod
    def _validated_recorded_targets(
        targets: object, *, package_id: str, source: str
    ) -> list[str]:
        """Validate a target list that was READ BACK from the lockfile/state.

        An empty or malformed `targets` must never fall through to the
        "no --for given" all-tools default: `sync --locked` is the CI
        reproducibility contract and the lockfile is a committed,
        merge-conflict-prone file, so a row that lost its targets would
        silently fan a one-tool install out across all nine tool roots.
        """
        if not isinstance(targets, list) or not targets:
            raise SkillError(
                f"{source} entry for {package_id} has no targets "
                f"(expected a non-empty list of: {', '.join(SUPPORTED_TOOLS)}); "
                "performed no changes"
            )
        unknown = sorted({str(item) for item in targets} - set(SUPPORTED_TOOLS))
        if unknown:
            raise SkillError(
                f"{source} entry for {package_id} names unknown target(s): "
                f"{', '.join(unknown)}; supported: {', '.join(SUPPORTED_TOOLS)}; "
                "performed no changes"
            )
        return [str(item) for item in targets]

    def plan(
        self,
        package: PackageRecord,
        *,
        targets: list[str] | tuple[str, ...],
        scope: str,
        default_all: bool = False,
    ) -> list[EmitPlan]:
        """Plan emissions for `targets`.

        `default_all` is the "`--for` was omitted, install everywhere" default
        and belongs ONLY to `add`. Every other caller passes a target set that
        was resolved (`compile_targets`) or read back from durable state, where
        an empty list means the row is broken, not "all nine tools".
        """
        unique_targets = list(dict.fromkeys(targets))
        if not unique_targets:
            if not default_all:
                raise SkillError(
                    "Refusing to plan an empty target set; "
                    f"expected one or more of: {', '.join(SUPPORTED_TOOLS)}"
                )
            unique_targets = list(SUPPORTED_TOOLS)
        return [
            plan_package(
                package,
                tool=target,
                scope=scope,
                project_root=self.project_root,
                home_dir=self.home_dir,
            )
            for target in unique_targets
        ]

    @staticmethod
    def _enforce_consent(
        plans: list[EmitPlan],
        *,
        allow_scripts: bool,
        enable_mcp: bool,
        allow_executable_extensions: bool,
        allow_hooks: bool = False,
        package: PackageRecord | None = None,
        allow_dlp_findings: bool = False,
    ) -> None:
        requirements = {item for plan in plans for item in plan.consent_requirements}
        if (
            enable_mcp
            and "mcp" not in requirements
            and any(
                omission.component == "portable-mcp"
                for plan in plans
                for omission in plan.omissions
            )
        ):
            raise SkillError(
                "MCP activation is not implemented by the local adapter; the config remains omitted"
            )
        if (
            allow_executable_extensions
            and "executable-extensions" not in requirements
            and any(
                omission.component == "executable-extensions"
                for plan in plans
                for omission in plan.omissions
            )
        ):
            raise SkillError(
                "Executable extension activation is not implemented; extensions remain omitted"
            )
        missing: list[str] = []
        if "skill-scripts" in requirements and not allow_scripts:
            missing.append("--allow-scripts")
        if "skill-hooks" in requirements and not allow_hooks:
            missing.append("--allow-hooks")
        if "mcp" in requirements and not enable_mcp:
            missing.append("--enable-mcp")
        if "executable-extensions" in requirements and not allow_executable_extensions:
            missing.append("--allow-executable-extensions")
        if "dlp-findings" in requirements and not allow_dlp_findings:
            missing.append("--allow-dlp-findings")
        if missing:
            detail = ""
            if package is not None and "--allow-dlp-findings" in missing and package.dlp_findings:
                detail += "; " + SkillManager.describe_dlp_findings(package)
            if package is not None and "--allow-hooks" in missing:
                detail += "; " + "; ".join(
                    SkillManager._describe_hooks(skill)
                    for skill in package.skills
                    if skill.has_hooks
                )
            raise SkillError(
                "Package requires explicit capability consent: " + ", ".join(missing) + detail
            )

    @staticmethod
    def describe_dlp_findings(package: PackageRecord) -> str:
        """Count + per-file/category summary of REDACTED findings (never the match)."""
        by_file: dict[str, list[str]] = {}
        for finding in package.dlp_findings:
            by_file.setdefault(str(finding.get("path")), []).append(
                f"{finding.get('category')}/{finding.get('pattern')}@{finding.get('line')}"
            )
        parts = [f"{path} [{', '.join(items)}]" for path, items in sorted(by_file.items())]
        return (
            f"{len(package.dlp_findings)} DLP finding(s) in {len(by_file)} file(s): "
            + "; ".join(parts[:10])
            + (" ..." if len(parts) > 10 else "")
        )

    @staticmethod
    def _describe_hooks(skill: Any) -> str:
        parts: list[str] = []
        if skill.hook_events:
            parts.append("hooks " + ", ".join(skill.hook_events))
        if skill.allowed_tools:
            parts.append("tool pre-approval " + ", ".join(skill.allowed_tools))
        return f"skill '{skill.name}' declares " + " and ".join(parts)

    @staticmethod
    def _consented_capabilities(plans: list[EmitPlan]) -> set[str]:
        return {
            "instructions",
            *(requirement for plan in plans for requirement in plan.consent_requirements),
        }

    def install_prepared(
        self,
        package: PackageRecord,
        plans: list[EmitPlan],
        *,
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        expected_current_hash: str | None = None,
        replace_package_id: str | None = None,
        expected_revision: int | None = None,
        consent_context: dict[str, Any] | None = None,
        rotate_install_id: bool = False,
        provenance_mode: str = "keep",
    ) -> dict[str, Any]:
        """`provenance_mode`: "keep" (the package's own provenance, else the
        row's for the same content) or "omit" (the row gets none)."""
        if provenance_mode not in ("keep", "omit"):
            raise SkillError(f"Unknown provenance mode: {provenance_mode}")
        self._enforce_consent(
            plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            package=package,
        )
        scope = plans[0].scope if plans else "project"
        context = dict(consent_context or {})
        if not isinstance(context.get("capability_manifest"), dict):
            # The consented capability set is recorded with every install so
            # the next update can be classified against what was approved.
            context.update(self.consent_snapshot(package))
        with self._operation_lock(scope) as store:
            # The marketplace was resolved BEFORE this lock (CLI preview, update);
            # a registry removal may have landed in between. Never commit a row
            # that references a marketplace that no longer exists.
            self._assert_registry_still_registered(package)
            from sessionfs.skills import hosted

            if hosted.hosted_package_id(package.source) is not None:
                hosted.ensure_identity_registered(self)
            # The final revocation check and the commit run under the same
            # lock revocation-state updates take: a block merged concurrently
            # lands either before the check (refused) or after the commit
            # (enforced like any installed blocked package).
            with hosted.HostedState(self.store_root).locked():
                current_row = store.state()["packages"].get(
                    replace_package_id or package.package_id
                )
                self._assert_not_revoked(
                    package,
                    installed_hash=(
                        str(current_row.get("content_hash"))
                        if isinstance(current_row, dict) else None
                    ),
                    installed_row=current_row if isinstance(current_row, dict) else None,
                    plans=plans,
                    register=False,
                )
                return store.apply(
                    package,
                    plans,
                    consented_capabilities=self._consented_capabilities(plans),
                    expected_current_hash=expected_current_hash,
                    replace_package_id=replace_package_id,
                    expected_revision=expected_revision,
                    consent_context=context,
                    rotate_install_id=rotate_install_id,
                    omit_provenance=provenance_mode == "omit",
                )

    # -- marketplace v2 §3: disclosure, capability baseline, D2 re-consent ----

    @staticmethod
    def capability_manifest(package: PackageRecord) -> dict[str, Any]:
        return disclosure.compute_manifest(package.package_root)

    @staticmethod
    def consent_snapshot(
        package: PackageRecord, manifest: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        manifest = manifest if manifest is not None else disclosure.compute_manifest(package.package_root)
        return {
            "capability_manifest": disclosure.access_manifest(manifest),
            "capability_digest": disclosure.capability_digest(manifest),
            "consent_rule_version": disclosure.CONSENT_RULE_VERSION,
        }

    def disclose(
        self,
        package: PackageRecord,
        *,
        targets: list[str] | tuple[str, ...] = (),
        scope: str = "project",
        manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The install disclosure card for `package` (the same JSON the server
        and the dashboard render)."""
        manifest = manifest if manifest is not None else self.capability_manifest(package)
        return disclosure.build_disclosure(
            manifest,
            tools=list(targets) or None,
            scope=scope,
            version_id=(package.source.commit if package.source.source_type == "hosted" else None),
            content_hash=package.content_hash,
            provenance=package.provenance or {},
        )

    def installed_baseline(
        self, row: dict[str, Any], *, scope: str
    ) -> tuple[dict[str, Any] | None, Path | None]:
        """(consented access manifest, cached package root) of an installed
        row. The manifest recorded at consent time is trusted only while a
        CURRENT digest binds it: the row must have been consented under the
        current rule version and its stored capability_digest must still equal
        the digest recomputed from the recorded manifest. A stale or unbound
        (and any pre-disclosure) row is an unknown baseline, so an update
        from it is widening (fail closed)."""
        recorded = row.get("capability_manifest")
        root: Path | None = None
        computed: dict[str, Any] | None = None
        try:
            package = self.load_installed_package(str(row["package_id"]), scope=scope)
            root = package.package_root
            computed = disclosure.compute_manifest(root)
        except (SkillError, KeyError, OSError):
            root = None
        stored_digest = row.get("capability_digest")
        bound = (
            disclosure.manifest_known(recorded)
            and row.get("consent_rule_version") == disclosure.CONSENT_RULE_VERSION
            and isinstance(stored_digest, str)
            and stored_digest
            and disclosure.capability_digest(recorded) == stored_digest
        )
        if bound:
            assert isinstance(recorded, dict)
            baseline = dict(recorded)
            if computed is not None:
                # Display-only parts (file list, scan, Unicode) for the diff.
                for key in ("files", "unicode_findings", "scan", "mcp_config_digest"):
                    baseline.setdefault(key, computed.get(key))
            return baseline, root
        return None, root

    def classify_update(
        self,
        current: dict[str, Any],
        package: PackageRecord,
        *,
        scope: str,
        new_manifest: dict[str, Any] | None = None,
        include_text: bool = True,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        """(old manifest, new manifest, diff) for replacing `current` by `package`."""
        old_manifest, old_root = self.installed_baseline(current, scope=scope)
        new_manifest = new_manifest if new_manifest is not None else self.capability_manifest(package)
        provenance = current.get("provenance")
        diff = disclosure.build_diff(
            old_manifest,
            new_manifest,
            old_root=old_root,
            new_root=package.package_root,
            old_content_hash=str(current.get("content_hash") or ""),
            new_content_hash=package.content_hash,
            old_provenance=provenance if isinstance(provenance, dict) else None,
            new_provenance=package.provenance,
            include_text=include_text,
        )
        return old_manifest, new_manifest, diff

    @staticmethod
    def enforce_hidden_unicode(manifest: dict[str, Any], *, allow_hidden_unicode: bool) -> None:
        """Critical hidden-Unicode / control-character findings (the classes
        the publish gate refuses) need explicit consent for a local install."""
        if allow_hidden_unicode or not disclosure.hidden_unicode_blocks(manifest):
            return
        counts = manifest.get("unicode_counts") or {}
        critical = [
            item for item in manifest.get("unicode_findings") or []
            if item.get("severity") == "critical"
        ]
        total = int(counts.get("critical") or len(critical))
        shown = "; ".join(
            f"{item.get('path')}:{item.get('line')}:{item.get('column')} "
            f"{item.get('visible') or item.get('class')}"
            for item in critical[:5]
        )
        if not total:
            raise SkillError(
                "The package scan was truncated, so hidden or control characters cannot be ruled "
                "out; review the package and pass --allow-hidden-unicode to install anyway"
            )
        raise SkillError(
            f"Package contains {total} hidden or control character(s) that can make "
            f"text read differently than the agent sees it ({shown}); review them and pass "
            "--allow-hidden-unicode to install anyway"
        )

    def update_consent(
        self,
        prepared: UpdatePreparation,
        *,
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        approve_widening: bool = False,
    ) -> tuple[dict[str, bool], dict[str, Any]]:
        """Apply the D2 re-consent table to a prepared update.

        * text / unchanged: capabilities consented for the installed version
          carry forward; one confirmation is enough;
        * code: the CHANGED capabilities need their flag again (never carried);
        * widening: refused unless `approve_widening` (explicit, never implied
          by --yes or an existing receipt), and nothing carries forward.

        Returns (effective consent flags, consent context for the install).
        Raises SkillError when consent is missing.
        """
        return self.transition_consent(
            current=prepared.current,
            package=prepared.package,
            plans=prepared.plans,
            diff=prepared.diff,
            new_manifest=prepared.new_manifest,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            approve_widening=approve_widening,
        )

    def transition_consent(
        self,
        *,
        current: dict[str, Any],
        package: PackageRecord,
        plans: list[EmitPlan],
        diff: dict[str, Any] | None,
        new_manifest: dict[str, Any] | None,
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        approve_widening: bool = False,
    ) -> tuple[dict[str, bool], dict[str, Any]]:
        """D2 consent for replacing the installed `current` row by `package`
        (update, rollback and locked sync all go through here)."""
        diff = diff or disclosure.classify_update(None, new_manifest or {})
        if diff.get("update_class") == "widening" and not approve_widening:
            items = ", ".join(
                f"{entry.get('kind')}: {entry.get('item')}" for entry in diff.get("widens") or []
            )
            raise SkillError(
                "This update asks for more access than you approved and needs explicit "
                f"re-consent ({items or 'unknown baseline'}); review the diff and pass "
                "--approve-widening"
            )
        explicit = {
            "allow_scripts": allow_scripts,
            "enable_mcp": enable_mcp,
            "allow_executable_extensions": allow_executable_extensions,
            "allow_hooks": allow_hooks,
            "allow_dlp_findings": allow_dlp_findings,
        }
        receipts = current.get("consent_receipts") or []
        # Consent is enforced per plan (tool): carried consent comes only from
        # receipts bound to that tool and scope.
        flags = dict(explicit)
        carried: set[str] = set()
        for plan in plans:
            tool_carried = disclosure.carried_for_tool(
                diff, receipts, tool=plan.tool, scope=plan.scope,
                content_hash=str(current.get("content_hash") or ""),
            )
            tool_flags = disclosure.flags_for_tool(explicit, tool_carried)
            try:
                self._enforce_consent([plan], package=package, **tool_flags)
            except SkillError as exc:
                detail = f" for {plan.tool}"
                if diff.get("update_class") == "code":
                    detail += "; executable content changed (" + ", ".join(diff.get("changed_requirements") or []) + ")"
                raise SkillError(f"{exc}{detail}") from exc
            carried |= tool_carried
            for name, value in tool_flags.items():
                flags[name] = flags.get(name, False) or value
        context = {
            **self.consent_snapshot(package, new_manifest),
            "update_class": diff.get("update_class"),
            "carried_requirements": sorted(carried),
            # Carry sources must be valid for the replaced content.
            "baseline_content_hash": current.get("content_hash"),
            "baseline_digest": diff.get("baseline_digest"),
        }
        return flags, context

    @staticmethod
    def _source_key(row: dict[str, Any]) -> tuple | None:
        source = row.get("source")
        if not isinstance(source, dict):
            return None
        return (
            row.get("name"),
            source.get("source_type"),
            source.get("locator"),
            source.get("subdir") or "",
            source.get("registry_id"),
        )

    @classmethod
    def _sync_lineage(
        cls,
        desired: dict[str, Any],
        installed: dict[str, Any],
        removable: set[str],
    ) -> dict[str, tuple[dict[str, Any] | None, bool]]:
        """For each lock row with no installed row of its own id: the single
        removed installed row with the same source identity (one-to-one), or
        (None, True) when the lineage is ambiguous."""
        candidates: dict[str, list[str]] = {}
        for package_id, row in desired.items():
            if isinstance(installed.get(package_id), dict) or not isinstance(row, dict):
                continue
            key = cls._source_key(row)
            candidates[package_id] = sorted(
                other for other in removable
                if key is not None
                and isinstance(installed.get(other), dict)
                and cls._source_key(installed[other]) == key
            )
        claims: dict[str, int] = {}
        for options in candidates.values():
            for other in options:
                claims[other] = claims.get(other, 0) + 1
        result: dict[str, tuple[dict[str, Any] | None, bool]] = {}
        for package_id, options in candidates.items():
            if len(options) == 1 and claims[options[0]] == 1:
                result[package_id] = (installed[options[0]], False)
            else:
                result[package_id] = (None, len(options) > 0)
        return result

    @staticmethod
    def expands_targets(current: dict[str, Any], plans: list[EmitPlan]) -> bool:
        installed = {str(tool) for tool in current.get("targets") or []}
        return any(plan.tool not in installed for plan in plans)

    def _replacement_consent(
        self,
        current: dict[str, Any],
        package: PackageRecord,
        plans: list[EmitPlan],
        *,
        scope: str,
        flags: dict[str, bool],
        approve_widening: bool,
        allow_hidden_unicode: bool,
    ) -> tuple[dict[str, bool], dict[str, Any] | None]:
        """Classify an installed row -> target version transition BEFORE any
        change and require the same approvals as `update`. Same content keeps
        the strict per-capability flags."""
        if current.get("content_hash") == package.content_hash:
            # Same content: explicit per-capability flags, and adding a tool
            # to it goes through the hidden-character gate like an install.
            self._enforce_consent(plans, package=package, **flags)
            if self.expands_targets(current, plans):
                self.enforce_hidden_unicode(
                    self.capability_manifest(package), allow_hidden_unicode=allow_hidden_unicode
                )
            # A current, digest-bound receipt is minted for this consent.
            return flags, self.consent_snapshot(package)
        _old, new_manifest, diff = self.classify_update(current, package, scope=scope, include_text=False)
        self.enforce_hidden_unicode(new_manifest, allow_hidden_unicode=allow_hidden_unicode)
        return self.transition_consent(
            current=current,
            package=package,
            plans=plans,
            diff=diff,
            new_manifest=new_manifest,
            approve_widening=approve_widening,
            **flags,
        )

    def _assert_registry_still_registered(self, package: PackageRecord) -> None:
        source = package.source
        if not source.registry_id:
            return
        live = next(
            (
                item
                for item in self.registries.list(source.registry_scope)
                if item.registry_id == source.registry_id
                and (not source.registry_alias or item.alias == source.registry_alias)
            ),
            None,
        )
        if live is None:
            raise SkillError(
                f"Marketplace '{source.registry_alias or source.registry_id}' "
                f"({source.registry_scope} scope) is no longer registered; it was removed "
                f"after {package.name} was resolved. Nothing was installed - re-add the "
                "marketplace and retry"
            )

    def add(
        self,
        source: str,
        *,
        targets: list[str] | tuple[str, ...] = (),
        scope: str = "project",
        pin: str | None = None,
        subdir: str = "",
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        allow_hidden_unicode: bool = False,
    ) -> tuple[PackageRecord, list[EmitPlan], dict[str, Any]]:
        from sessionfs.skills import hosted

        # scope lock -> operation lock: the install and the retirement of
        # pull's authority over it happen as one mutation.
        with self.explicit_change(scope):
            package = self.import_source(
                source, pin=pin, subdir=subdir, scope=scope, allow_dlp_findings=allow_dlp_findings
            )
            manifest = self.capability_manifest(package)
            self.enforce_hidden_unicode(manifest, allow_hidden_unicode=allow_hidden_unicode)
            plans = self.plan(package, targets=targets, scope=scope, default_all=True)
            state = self.install_prepared(
                package,
                plans,
                allow_scripts=allow_scripts,
                enable_mcp=enable_mcp,
                allow_executable_extensions=allow_executable_extensions,
                allow_hooks=allow_hooks,
                allow_dlp_findings=allow_dlp_findings,
                consent_context=self.consent_snapshot(package, manifest),
                # A new installation id in the SAME commit: earlier pull
                # records lose their authority even if the cleanup below
                # never runs (crash).
                rotate_install_id=True,
            )
            # An explicit add is the user's choice: pull may no longer
            # uninstall it.
            hosted.retire_pull_authority(self, scope, package.package_id)
            hosted.clear_opt_outs(self, scope, local_package_id=package.package_id)
        return package, plans, state

    def list_installed(self, scope: str | None = None) -> list[dict[str, Any]]:
        scopes = (scope,) if scope else ("project", "user")
        result: list[dict[str, Any]] = []
        for item_scope in scopes:
            state = self.state_store(item_scope).state()
            for row in state["packages"].values():
                if isinstance(row, dict):
                    result.append(row)
        return result

    def find_installed(self, identifier: str, *, scope: str | None = None) -> dict[str, Any]:
        matches = [
            row for row in self.list_installed(scope)
            if row.get("package_id") == identifier or row.get("name") == identifier
        ]
        if not matches:
            raise SkillError(f"Installed package not found: {identifier}")
        if len(matches) > 1:
            raise SkillError(f"Installed package name is ambiguous: {identifier}; use package_id")
        return matches[0]

    @staticmethod
    def _row_source(row: dict[str, Any]) -> SourceIdentity | None:
        """The state/lock row's source is authoritative provenance."""
        raw = row.get("source")
        if not isinstance(raw, dict):
            return None
        try:
            return SourceIdentity(**raw)
        except TypeError:
            return None

    def load_installed_package(self, identifier: str, *, scope: str) -> PackageRecord:
        self._validate_store_root()
        row = self.find_installed(identifier, scope=scope)
        package_id = str(row["package_id"])
        digest = str(row["content_hash"]).removeprefix("sha256:")
        package = load_package_record(
            self.store_root / "packages" / package_id / digest, source=self._row_source(row)
        )
        if package.package_id != package_id or package.content_hash != row.get("content_hash"):
            raise SkillError(f"Installed package metadata mismatch: {package_id}")
        return package

    def remove(
        self,
        identifier: str,
        *,
        scope: str,
        quarantine_dir: Path | None = None,
        expected_hash: str | None = None,
        expected_revision: int | None = None,
        recheck: Any = None,
        expected_install_id: str | None = None,
    ) -> dict[str, Any]:
        """Remove a package; with `quarantine_dir` (blocked content) every
        output, modified or not, is moved there instead of staying active.

        The installed row is read UNDER the scope's operation lock. When the
        caller decided on a specific row (`expected_hash` / `expected_revision`)
        a different locked row raises PackageChangedConcurrently, and
        `recheck(row)` re-evaluates the decision against the locked row: if it
        no longer holds, nothing is removed (`{"skipped": True}`).
        """
        with self._operation_lock(scope) as store:
            row = self.find_installed(identifier, scope=scope)
            revision = _row_revision(row)
            if (
                (expected_hash is not None and row.get("content_hash") != expected_hash)
                or (expected_revision is not None and revision != expected_revision)
                # A remove + re-add resets hash/revision to the same values:
                # only the installation id tells the reinstall apart.
                or (
                    expected_install_id is not None
                    and row.get("install_id") != expected_install_id
                )
            ):
                raise PackageChangedConcurrently(
                    f"Installed package changed concurrently: {row.get('package_id')}"
                )
            if recheck is not None and not recheck(row):
                return {"removed": [], "dirty": [], "skipped": True}
            self._validate_store_root()
            with contextlib.suppress(OSError, SkillError):
                prune_trash(self.store_root)  # entries past the retention window
            package_id = str(row["package_id"])
            return store.remove(
                package_id,
                expected_current_hash=str(row.get("content_hash")),
                quarantine_dir=quarantine_dir,
                expected_revision=revision if expected_revision is not None else None,
                # Clean outputs are moved (never deleted) into the store's trash.
                trash_dir=trash_root(self.store_root) / safe_component(package_id) / park_stamp(),
                park_root=self.store_root,
            )

    def trash_usage(self) -> dict[str, Any]:
        """Files and bytes held in the removal trash (for `sfs skill doctor`)."""
        files, size = trash_usage(self.store_root)
        return {
            "path": str(trash_root(self.store_root)),
            "files": files,
            "bytes": size,
            "retention_days": TRASH_RETENTION_SECONDS // 86400,
        }

    @staticmethod
    def compile_targets(
        current: list[str] | tuple[str, ...],
        *,
        add: list[str] | tuple[str, ...] = (),
        drop: list[str] | tuple[str, ...] = (),
    ) -> list[str]:
        """Target set after a compile: `add` (--for) is ADDITIVE - the installed
        targets are always kept - and `drop` (--drop) is the only way to shrink
        it, so a re-apply for one tool can never silently unlink the outputs of
        every other tool."""
        overlap = sorted(set(add) & set(drop))
        if overlap:
            raise SkillError(f"Cannot both add and drop the same target(s): {', '.join(overlap)}")
        selected = list(dict.fromkeys([*current, *add]))
        missing = [tool for tool in dict.fromkeys(drop) if tool not in selected]
        if missing:
            raise SkillError(
                f"Cannot drop target(s) not installed for this package: {', '.join(missing)}; "
                f"current targets: {', '.join(selected) or '-'}"
            )
        selected = [tool for tool in selected if tool not in set(drop)]
        if not selected:
            raise SkillError(
                "Dropping every target would leave nothing installed; use `sfs skill remove` instead"
            )
        return selected

    @staticmethod
    def outputs_dropped_by(current: dict[str, Any], plans: list[EmitPlan]) -> list[str]:
        """Managed outputs of the installed row that the new plan set no longer
        produces (the files a compile would unlink)."""
        planned = {emission.destination for plan in plans for emission in plan.emissions}
        return [
            str(output["path"])
            for output in current.get("outputs") or []
            if isinstance(output, dict)
            and isinstance(output.get("path"), str)
            and output["path"] not in planned
        ]

    def compile(
        self,
        identifier: str,
        *,
        scope: str,
        targets: list[str] | tuple[str, ...] = (),
        drop: list[str] | tuple[str, ...] = (),
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        allow_hidden_unicode: bool = False,
        pull_op: str | None = None,
    ) -> tuple[PackageRecord, list[EmitPlan], dict[str, Any]]:
        package = self.load_installed_package(identifier, scope=scope)
        if pull_op is not None:
            # A pull operation's retarget: stamped onto the row in the same
            # commit (its crash recovery acts only on stamped rows).
            package = replace(package, pull_op=pull_op)
        current = self.find_installed(identifier, scope=scope)
        selected = self.compile_targets(
            [str(item) for item in (current.get("targets") or [])], add=targets, drop=drop
        )
        plans = self.plan(package, targets=selected, scope=scope)
        if self.expands_targets(current, plans):
            # Adding a tool writes the package somewhere new: same gate as add.
            self.enforce_hidden_unicode(
                self.capability_manifest(package), allow_hidden_unicode=allow_hidden_unicode
            )
        self._enforce_consent(
            plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            package=package,
        )
        state = self.install_prepared(
            package,
            plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            expected_current_hash=str(current.get("content_hash")),
            expected_revision=_row_revision(current),
        )
        return package, plans, state

    def prepare_update(
        self,
        identifier: str,
        *,
        scope: str,
        pin: str | None = None,
        rebind_origin: bool = False,
    ) -> UpdatePreparation:
        """Resolve, fetch and plan the new version WITHOUT applying it.

        Nothing under the adapter roots or the state files changes; the caller
        reviews the result (old hash -> new hash, commit, per-tool plan,
        capability escalations, degradations), enforces consent, and only then
        applies it with `apply_update`.
        """
        current = self.find_installed(identifier, scope=scope)
        source = current.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("locator"), str):
            raise SkillError("Installed package has no reusable source identity")
        if source.get("source_type") == "hosted":
            from sessionfs.skills import hosted

            hosted_id = hosted.hosted_package_id(source)
            if hosted_id is None:
                raise SkillError("Installed hosted package has an invalid source identity")
            # No --pin: the package's CURRENT hosted version; --pin selects a
            # version label or version id of the same package.
            reference = f"sessionfs:{hosted_id}" + (f"@{pin}" if pin else "")
            # Only ever the server the package is bound to (legacy rows need
            # an explicit --rebind-origin to the active server).
            client = self.hosted_client_for(
                self._hosted_origin_for(source, rebind_origin=rebind_origin)
            )
            package = hosted.import_hosted_version(
                client, client.resolve(hosted.parse_hosted_ref(reference)), self.store_root
            )
            refreshed_registry: Any = None
            registry_commit_before: str | None = None
        elif source.get("registry_id"):
            registry = next(
                (
                    item
                    for item in self.registries.list()
                    if item.registry_id == source.get("registry_id")
                ),
                None,
            )
            if registry is None:
                raise SkillError("The package marketplace is no longer registered")
            # Refresh INTO MEMORY only: marketplaces.json is rewritten by
            # apply_update, never by a preview the user may decline.
            registered, registry = self.registries.fetch_refreshed(
                registry.alias, scope=registry.scope
            )
            refreshed_registry = registry
            registry_commit_before = registered.commit
            # The catalog entry selected at install time (its listing name may
            # differ from plugin.json's name) is the thing to re-resolve. A
            # top-level `renames` entry published since install is honored the
            # same way RegistryStore.resolve honors it, and the new name is
            # what gets persisted with the updated row.
            stored_name = str(source.get("listing_name") or current.get("name"))
            listing_name = registry.renames.get(stored_name, stored_name)
            listing = next(
                (item for item in registry.packages if item.get("name") == listing_name),
                None,
            )
            if listing is None:
                raise SkillError("The package is no longer listed by its marketplace")
            package_source = listing.get("external_source") or registry.source
            if listing.get("external_source"):
                package_pin = pin or listing.get("external_ref") or None
            else:
                if pin:
                    raise SkillError(
                        "--pin is not applicable to a marketplace listing that lives inside the "
                        "marketplace repository; refresh the marketplace to move its commit"
                    )
                package_pin = registry.commit
            with materialize_source(
                package_source,
                pin=package_pin,
                subdir=str(listing.get("subdir") or ""),
                source_type="marketplace",
                registry_scope=registry.scope,
                registry_id=registry.registry_id,
                registry_alias=registry.alias,
                listing_name=listing_name,
            ) as materialized:
                package = import_package(
                    materialized.path, self.store_root, materialized.identity
                )
        else:
            refreshed_registry = None
            registry_commit_before = None
            locator = source["locator"]
            if source.get("source_type") == "local-project":
                locator = str(self.project_root / locator)
            package = self.import_source(
                locator,
                pin=pin,
                subdir=str(source.get("subdir") or ""),
                scope=scope,
            )
        plans = self.plan(
            package,
            targets=self._validated_recorded_targets(
                current.get("targets"),
                package_id=str(current.get("package_id")),
                source="State",
            ),
            scope=scope,
        )
        lock_row = self.state_store(scope).desired()["packages"].get(str(current["package_id"]))
        old_manifest, new_manifest, diff = self.classify_update(current, package, scope=scope)
        return UpdatePreparation(
            current=current,
            lock_row=lock_row if isinstance(lock_row, dict) else None,
            package=package,
            plans=plans,
            scope=scope,
            refreshed_registry=refreshed_registry,
            registry_commit_before=registry_commit_before,
            old_manifest=old_manifest,
            new_manifest=new_manifest,
            diff=diff,
        )

    def apply_update(
        self,
        prepared: UpdatePreparation,
        *,
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        consent_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._operation_lock(prepared.scope):
            # Same lock the install takes (re-entrant): the marketplace must
            # still be registered, then the catalog fetched during prepare is
            # persisted, then the package is applied.
            self._assert_registry_still_registered(prepared.package)
            if prepared.refreshed_registry is not None:
                self.registries.persist_refreshed(
                    prepared.refreshed_registry,
                    expected_commit=str(prepared.registry_commit_before or ""),
                )
            return self.install_prepared(
                prepared.package,
                prepared.plans,
                allow_scripts=allow_scripts,
                enable_mcp=enable_mcp,
                allow_executable_extensions=allow_executable_extensions,
                allow_hooks=allow_hooks,
                allow_dlp_findings=allow_dlp_findings,
                expected_current_hash=str(prepared.current.get("content_hash")),
                expected_revision=_row_revision(prepared.current),
                # Content-bound local ids change with new bytes, and a marketplace
                # listing whose external URL / subdir moved yields a new id too:
                # migrate the installed row (and the outputs it owns) instead of
                # failing as "disappeared concurrently".
                replace_package_id=str(prepared.current["package_id"]),
                consent_context=consent_context,
            )

    def update(
        self,
        identifier: str,
        *,
        scope: str,
        pin: str | None = None,
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        rebind_origin: bool = False,
        approve_widening: bool = False,
        allow_hidden_unicode: bool = False,
    ) -> tuple[PackageRecord, list[EmitPlan], dict[str, Any]]:
        prepared = self.prepare_update(
            identifier, scope=scope, pin=pin, rebind_origin=rebind_origin
        )
        self.enforce_hidden_unicode(
            prepared.new_manifest or {}, allow_hidden_unicode=allow_hidden_unicode
        )
        flags, context = self.update_consent(
            prepared,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            approve_widening=approve_widening,
        )
        state = self.apply_update(prepared, consent_context=context, **flags)
        return prepared.package, prepared.plans, state

    def _restore_locked_package(
        self,
        package_id: str,
        row: dict[str, Any],
        *,
        scope: str,
    ) -> PackageRecord:
        expected_hash = str(row.get("content_hash", ""))
        digest = expected_hash.removeprefix("sha256:")
        version_dir = self.store_root / "packages" / package_id / digest
        if version_dir.is_dir():
            package = load_package_record(version_dir, source=self._row_source(row))
        else:
            source = row.get("source")
            if not isinstance(source, dict) or not isinstance(source.get("locator"), str):
                raise SkillError(f"Locked package has no reusable source: {package_id}")
            source_type = source.get("source_type")
            if source_type == "hosted":
                return self._restore_hosted_package(package_id, row, source, scope=scope)
            if source_type in {"local", "local-project"}:
                local_dir = (
                    self.project_root / source["locator"]
                    if source_type == "local-project"
                    else Path(source["locator"])
                )
                if not local_dir.is_dir():
                    raise SkillError(
                        f"Locked package source directory does not exist: {local_dir}"
                    )
            if source_type == "local-project":
                package = self.import_source(
                    str(self.project_root / source["locator"]),
                    pin=source.get("commit"),
                    subdir=str(source.get("subdir") or ""),
                    scope=scope,
                )
            elif source_type == "local":
                package = self.import_source(
                    source["locator"],
                    subdir=str(source.get("subdir") or ""),
                    scope=scope,
                )
            else:
                with materialize_source(
                    source["locator"],
                    pin=source.get("commit"),
                    subdir=str(source.get("subdir") or ""),
                    source_type=str(source_type or "git"),
                    registry_scope=source.get("registry_scope"),
                    registry_id=source.get("registry_id"),
                    registry_alias=source.get("registry_alias"),
                    listing_name=source.get("listing_name"),
                ) as materialized:
                    package = import_package(
                        materialized.path, self.store_root, materialized.identity
                    )
        if package.package_id != package_id or package.content_hash != expected_hash:
            raise SkillError(
                f"Locked package identity mismatch: expected {package_id}@{expected_hash}, "
                f"got {package.package_id}@{package.content_hash}"
            )
        return package

    def _restore_hosted_package(
        self,
        package_id: str,
        row: dict[str, Any],
        source: dict[str, Any],
        *,
        scope: str,
    ) -> PackageRecord:
        """Re-download the EXACT locked hosted version (the lock's version id
        and content hash), never whatever is current on the server."""
        from sessionfs.skills import hosted

        expected_hash = str(row.get("content_hash", ""))
        version_id = source.get("commit")
        hosted_id = hosted.hosted_package_id(source)
        if not isinstance(version_id, str) or hosted_id is None:
            raise SkillError(f"Locked hosted package has no version id: {package_id}")
        origin = hosted.hosted_origin(source)
        if origin is None:
            # A rebind would change the package id the lockfile pins: do it
            # deliberately with `sfs skill update --rebind-origin`, not here.
            raise SkillError(
                f"Locked hosted package {package_id} does not record which server it came "
                "from; run `sfs skill update <name> --rebind-origin` against that server, "
                "commit the new lockfile, then sync"
            )
        client = self.hosted_client_for(origin)
        item = client.version_metadata(version_id)
        if item.get("package_id") != hosted_id or item.get("version_id") != version_id:
            raise SkillError(
                f"Locked hosted package identity mismatch for {package_id}: the server "
                f"returned a different package/version for {version_id}"
            )
        package = hosted.import_hosted_version(
            client,
            item,
            self.store_root,
            expected_hash=expected_hash,
            installed_hash=self._installed_hash(scope, package_id),
        )
        if package.package_id != package_id or package.content_hash != expected_hash:
            raise SkillError(
                f"Locked package identity mismatch: expected {package_id}@{expected_hash}, "
                f"got {package.package_id}@{package.content_hash}"
            )
        return package

    def sync(
        self,
        *,
        scope: str,
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        approve_widening: bool = False,
        allow_hidden_unicode: bool = False,
    ) -> list[dict[str, Any]]:
        """Reconcile to the lockfile's exact desired state in two phases.

        Phase 1 (zero writes to managed outputs): every lock row is validated
        (a malformed row is a hard error), resolved, materialized, planned and
        consent-checked. Phase 2 hands ALL removals and installs to
        SkillStateStore.reconcile, which preflights every planned output across
        the whole set and applies everything under one journal entry, so a
        failure or crash anywhere rolls the entire reconcile back.
        """
        with self._operation_lock(scope) as state_store:
            desired = state_store.desired()
            installed = state_store.state()["packages"]
            for package_id, row in desired["packages"].items():
                if not isinstance(row, dict):
                    raise SkillError(
                        f"Lockfile entry for {package_id} is malformed "
                        f"(expected an object, got {type(row).__name__}); sync performed no changes"
                    )
            # A BLOCKED package whose quarantine is still pending (some output
            # could not leave the tool directories) is left exactly as it is:
            # never reinstalled and never "removed" (which would put a
            # modified output back and drop the row that tracks it). Only the
            # quarantine itself (`sfs skill pull`) finishes it.
            pending = {
                package_id for package_id, row in desired["packages"].items()
                if row.get("quarantine_pending") is True
            } | {
                package_id for package_id, row in installed.items()
                if isinstance(row, dict) and row.get("quarantine_pending") is True
            }
            desired = {**desired, "packages": {
                key: value for key, value in desired["packages"].items() if key not in pending
            }}
            installed = {key: value for key, value in installed.items() if key not in pending}
            for package_id, row in installed.items():
                if not isinstance(row, dict):
                    raise SkillError(
                        f"State entry for {package_id} is malformed; sync performed no changes"
                    )
            # ---- phase 1: validate everything, write nothing ----
            prepared: list[tuple[PackageRecord, list[EmitPlan], set[str]]] = []
            contexts: dict[str, dict[str, Any]] = {}
            base_flags = {
                "allow_scripts": allow_scripts,
                "enable_mcp": enable_mcp,
                "allow_executable_extensions": allow_executable_extensions,
                "allow_hooks": allow_hooks,
                "allow_dlp_findings": allow_dlp_findings,
            }
            removable = set(installed) - set(desired["packages"])
            lineage = self._sync_lineage(desired["packages"], installed, removable)
            for package_id, row in desired["packages"].items():
                package = self._restore_locked_package(package_id, row, scope=scope)
                installed_row = installed.get(package_id)
                plans = self.plan(
                    package,
                    targets=self._validated_recorded_targets(
                        row.get("targets"), package_id=package_id, source="Lockfile"
                    ),
                    scope=scope,
                )
                self._assert_not_revoked(
                    package,
                    installed_hash=(
                        str(installed_row.get("content_hash"))
                        if isinstance(installed_row, dict)
                        else None
                    ),
                    installed_row=installed_row if isinstance(installed_row, dict) else None,
                    plans=plans,
                )
                # The installed version this lock row replaces: the same id, or
                # (content-bound local ids change with the bytes) the ONE
                # removed row with the same source identity. Never a mere
                # name match; an ambiguous lineage is an unknown baseline.
                predecessor, ambiguous = lineage.get(package_id, (None, False))
                current = installed_row if isinstance(installed_row, dict) else predecessor
                if ambiguous and not approve_widening:
                    raise SkillError(
                        f"Cannot tell which installed package {package.name} replaces (several share "
                        "its source); its access is treated as new: pass --approve-widening; "
                        "sync performed no changes"
                    )
                if current is None:
                    manifest = self.capability_manifest(package)
                    self.enforce_hidden_unicode(manifest, allow_hidden_unicode=allow_hidden_unicode)
                    self._enforce_consent(plans, package=package, **base_flags)
                    contexts[package.package_id] = self.consent_snapshot(package, manifest)
                else:
                    # Every transition is classified and approved BEFORE any
                    # write: one refused transition refuses the whole sync.
                    _flags, context = self._replacement_consent(
                        current,
                        package,
                        plans,
                        scope=scope,
                        flags=base_flags,
                        approve_widening=approve_widening,
                        allow_hidden_unicode=allow_hidden_unicode,
                    )
                    if context is not None:
                        if not isinstance(installed_row, dict):
                            # The predecessor's receipts form the carried chain.
                            context["predecessor_receipts"] = list(current.get("consent_receipts") or [])
                        contexts[package.package_id] = context
                prepared.append((package, plans, self._consented_capabilities(plans)))
            removals = sorted(set(installed) - set(desired["packages"]))
            # ---- phase 2: one journaled transaction ----
            from sessionfs.skills import hosted

            # Re-checked and committed under the revocation-state lock (see
            # install_prepared): no block merged in between can be missed.
            with hosted.HostedState(self.store_root).locked():
                for package, package_plans, _caps in prepared:
                    installed_row = installed.get(package.package_id)
                    self._assert_not_revoked(
                        package,
                        installed_hash=(
                            str(installed_row.get("content_hash"))
                            if isinstance(installed_row, dict) else None
                        ),
                        installed_row=installed_row if isinstance(installed_row, dict) else None,
                        plans=package_plans,
                        register=False,
                    )
                return state_store.reconcile(prepared, removals, consent_contexts=contexts)

    def versions(self, package_id: str) -> list[str]:
        self._validate_store_root()
        root = self.store_root / "packages" / package_id
        if not root.is_dir():
            return []
        return sorted(
            item.name for item in root.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        )

    def rollback(
        self,
        identifier: str,
        content_hash: str,
        *,
        scope: str,
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
        approve_widening: bool = False,
        allow_hidden_unicode: bool = False,
    ) -> dict[str, Any]:
        # The requested version is an EXACT digest, validated before any path
        # work: `--to ../x` or an absolute path must never select a
        # cache-shaped directory outside the content store.
        digest = self._validated_digest(content_hash)
        current = self.find_installed(identifier, scope=scope)
        package_id = str(current["package_id"])
        version_dir = self._cached_version_dir(current, digest)
        # The installed row's source describes the CURRENT version only; the
        # selected cached version carries its own provenance (the commit it
        # was imported at), which is what the lock must record after rollback.
        package = load_package_record(version_dir)
        plans = self.plan(
            package,
            targets=self._validated_recorded_targets(
                current.get("targets"), package_id=package_id, source="State"
            ),
            scope=scope,
        )
        # A rollback replaces the installed version like an update does: the
        # transition is classified against what was consented to (D2).
        flags, context = self._replacement_consent(
            current,
            package,
            plans,
            scope=scope,
            flags={
                "allow_scripts": allow_scripts,
                "enable_mcp": enable_mcp,
                "allow_executable_extensions": allow_executable_extensions,
                "allow_hooks": allow_hooks,
                "allow_dlp_findings": allow_dlp_findings,
            },
            approve_widening=approve_widening,
            allow_hidden_unicode=allow_hidden_unicode,
        )
        return self.install_prepared(
            package,
            plans,
            **flags,
            expected_current_hash=str(current.get("content_hash")),
            expected_revision=_row_revision(current),
            replace_package_id=package_id,
            consent_context=context,
        )

    @staticmethod
    def _validated_digest(content_hash: str) -> str:
        """`sha256:<64 hex>` or bare 64-hex, nothing else (no paths, no prefixes)."""
        match = _CONTENT_DIGEST_RE.fullmatch(str(content_hash).strip())
        if match is None:
            raise SkillError(
                "Content hash must be sha256:<64 hex digits> (or the bare 64-hex digest)"
            )
        return match.group("digest").lower()

    def _assert_inside_store(self, candidate: Path) -> Path:
        resolved = Path(os.path.abspath(candidate))
        store = Path(os.path.abspath(self.store_root / "packages"))
        try:
            resolved.relative_to(store)
        except ValueError as exc:
            raise SkillError(f"Cached version path escapes the content store: {candidate}") from exc
        return resolved

    @staticmethod
    def _known_package_ids(row: dict[str, Any]) -> list[str]:
        """Current id first, then every id the row was migrated from."""
        ids = [str(row["package_id"])]
        for item in row.get("previous_package_ids") or []:
            if isinstance(item, str) and item and item not in ids:
                ids.append(item)
        return ids

    def _cached_version_dir(self, row: dict[str, Any], digest: str) -> Path:
        """Locate a cached version for an installed row.

        A marketplace source move or a catalog rename changes the package id;
        versions imported before that stay cached under the OLD id, so the
        current id is probed first and then each previous id. Content-bound
        local ids derive from the requested bytes instead.
        """
        digest = self._validated_digest(digest)
        packages_root = self.store_root / "packages"
        candidates = [packages_root / pid / digest for pid in self._known_package_ids(row)]
        source_raw = row.get("source")
        source: dict[str, Any] = source_raw if isinstance(source_raw, dict) else {}
        if source.get("source_type") == "local":
            candidates.append(
                packages_root
                / local_package_id(str(row.get("name")), "sha256:" + digest)
                / digest
            )
        # Package ids come from state rows; a tampered id must not escape either.
        candidates = [self._assert_inside_store(candidate) for candidate in candidates]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return candidates[0]

    def doctor(self, scope: str | None = None, *, repair_cache: bool = True) -> list[dict[str, str]]:
        scopes = (scope,) if scope else ("project", "user")
        drift: list[dict[str, str]] = []
        for item_scope in scopes:
            for item in self.state_store(item_scope).drift():
                item["scope"] = item_scope
                drift.append(item)
        drift.extend(self.doctor_cache(repair=repair_cache))
        return drift

    def doctor_cache(self, *, repair: bool = True) -> list[dict[str, str]]:
        """Report (and by default repair) stale or invalid content-store entries.

        A stale envelope is rewritten from its verified payload; an entry whose
        integrity does not verify is evicted so the next sync/add re-materializes
        it from the locked source.
        """
        from sessionfs.skills.package import envelope_is_stale, evict_version, refresh_envelope

        findings: list[dict[str, str]] = []
        packages_root = self.store_root / "packages"
        if not packages_root.is_dir():
            return findings
        self._validate_store_root()
        # Versions cached under an id a row was migrated from belong to that
        # row's CURRENT id; report them as such rather than as strangers.
        current_id_by_dir: dict[str, str] = {}
        for state_scope in ("project", "user"):
            try:
                rows = self.state_store(state_scope).state()["packages"]
            except SkillError:
                continue
            for row in rows.values():
                if isinstance(row, dict) and isinstance(row.get("package_id"), str):
                    for known in self._known_package_ids(row):
                        current_id_by_dir.setdefault(known, str(row["package_id"]))
        for package_dir in sorted(packages_root.iterdir()):
            if not package_dir.is_dir() or is_link_or_reparse(package_dir):
                continue
            reported_id = current_id_by_dir.get(package_dir.name, package_dir.name)
            for version_dir in sorted(package_dir.iterdir()):
                if not version_dir.is_dir() or is_link_or_reparse(version_dir) or version_dir.name.startswith("."):
                    continue
                if ".tmp-" in version_dir.name:
                    staging_finding = self._doctor_staging_dir(
                        version_dir, reported_id, repair=repair, evict=evict_version
                    )
                    if staging_finding is not None:
                        findings.append(staging_finding)
                    continue
                try:
                    load_package_record(version_dir)
                    stale = envelope_is_stale(version_dir)
                except SkillError as exc:
                    if repair:
                        evict_version(version_dir)
                    findings.append(
                        {
                            "package_id": reported_id,
                            "path": str(version_dir),
                            "expected": "valid-cache",
                            "actual": f"invalid-cache ({str(exc)[:80]})",
                            "repaired": "evicted" if repair else "no",
                            "scope": "store",
                        }
                    )
                    continue
                if stale:
                    if repair:
                        refresh_envelope(version_dir)
                    findings.append(
                        {
                            "package_id": reported_id,
                            "path": str(version_dir),
                            "expected": "current-envelope",
                            "actual": "stale-envelope",
                            "repaired": "rewritten" if repair else "no",
                            "scope": "store",
                        }
                    )
        return findings

    @staticmethod
    def _doctor_staging_dir(
        staging: Path, reported_id: str, *, repair: bool, evict: Any
    ) -> dict[str, str] | None:
        """An import staging dir is another process's in-flight copy unless it
        is older than STALE_STAGING_SECONDS: young ones are skipped untouched,
        stale ones are reported (and evicted when repairing)."""
        try:
            age = time.time() - staging.lstat().st_mtime
        except OSError:
            return None
        if age < STALE_STAGING_SECONDS:
            return None
        if repair:
            evict(staging)
        return {
            "package_id": reported_id,
            "path": str(staging),
            "expected": "no-staging-dir",
            "actual": f"stale-staging-dir ({int(age // 60)} min old, abandoned import)",
            "repaired": "evicted" if repair else "no",
            "scope": "store",
        }

    def remove_registry(self, alias: str, *, scope: str) -> None:
        # Hold BOTH scopes' operation locks (project, then user — the same
        # order add() takes them) across the reference scan and the removal so
        # an in-flight add that resolved this registry cannot slip between.
        with self._operation_lock("project"), self._operation_lock("user"):
            registry = next(
                (item for item in self.registries.list(scope) if item.alias == alias),
                None,
            )
            if registry is None:
                raise SkillError(f"Marketplace not found in {scope} scope: {alias}")
            for state_scope in ("project", "user"):
                desired = self.state_store(state_scope).desired()
                for row in desired["packages"].values():
                    source = row.get("source") if isinstance(row, dict) else None
                    if isinstance(source, dict) and source.get("registry_id") == registry.registry_id:
                        raise SkillError(
                            f"Marketplace '{alias}' is still referenced by locked package {row.get('name')}"
                        )
            self.registries.remove(alias, scope=scope)
