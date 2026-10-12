"""High-level local package manager used by CLI and provenance capture."""

from __future__ import annotations

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
from sessionfs.skills.adapters import PROJECT_SKILL_ROOTS, USER_SKILL_ROOTS, plan_package
from sessionfs.skills.models import SUPPORTED_TOOLS, EmitPlan, PackageRecord, SkillError, SourceIdentity
from sessionfs.skills.package import import_package, load_package_record
from sessionfs.skills.package import local_package_id
from sessionfs.skills.path_safety import is_link_or_reparse
from sessionfs.skills.registry import RegistryStore
from sessionfs.skills.sources import materialize_source
from sessionfs.skills.state import SkillStateStore, interprocess_lock


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


class SkillManager:
    def __init__(
        self,
        *,
        project_root: Path | None = None,
        home_dir: Path | None = None,
        store_root: Path | None = None,
    ) -> None:
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
            )
        if scope == "user":
            roots = tuple(
                self.home_dir / value for value in sorted(set(USER_SKILL_ROOTS.values()))
            )
            return SkillStateStore(
                self.store_root,
                scope_root=self.home_dir,
                allowed_output_roots=roots,
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
            held[scope] += 1
            try:
                yield store
            finally:
                held[scope] -= 1
            return
        with interprocess_lock(store.root / "skills.operation.lock"):
            held[scope] = 1
            try:
                yield store
            finally:
                held[scope] = 0

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
    ) -> dict[str, Any]:
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
        with self._operation_lock(scope) as store:
            # The marketplace was resolved BEFORE this lock (CLI preview, update);
            # a registry removal may have landed in between. Never commit a row
            # that references a marketplace that no longer exists.
            self._assert_registry_still_registered(package)
            return store.apply(
                package,
                plans,
                consented_capabilities=self._consented_capabilities(plans),
                expected_current_hash=expected_current_hash,
                replace_package_id=replace_package_id,
                expected_revision=expected_revision,
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
    ) -> tuple[PackageRecord, list[EmitPlan], dict[str, Any]]:
        with self._operation_lock(scope):
            package = self.import_source(
                source, pin=pin, subdir=subdir, scope=scope, allow_dlp_findings=allow_dlp_findings
            )
            plans = self.plan(package, targets=targets, scope=scope, default_all=True)
            state = self.install_prepared(
                package,
                plans,
                allow_scripts=allow_scripts,
                enable_mcp=enable_mcp,
                allow_executable_extensions=allow_executable_extensions,
                allow_hooks=allow_hooks,
                allow_dlp_findings=allow_dlp_findings,
            )
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

    def remove(self, identifier: str, *, scope: str) -> dict[str, list[str]]:
        row = self.find_installed(identifier, scope=scope)
        with self._operation_lock(scope) as store:
            return store.remove(
                str(row["package_id"]),
                expected_current_hash=str(row.get("content_hash")),
            )

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
    ) -> tuple[PackageRecord, list[EmitPlan], dict[str, Any]]:
        package = self.load_installed_package(identifier, scope=scope)
        current = self.find_installed(identifier, scope=scope)
        selected = self.compile_targets(
            [str(item) for item in (current.get("targets") or [])], add=targets, drop=drop
        )
        plans = self.plan(package, targets=selected, scope=scope)
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
        if source.get("registry_id"):
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
            refreshed_registry: Any = registry
            registry_commit_before: str | None = registered.commit
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
        return UpdatePreparation(
            current=current,
            lock_row=lock_row if isinstance(lock_row, dict) else None,
            package=package,
            plans=plans,
            scope=scope,
            refreshed_registry=refreshed_registry,
            registry_commit_before=registry_commit_before,
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
    ) -> tuple[PackageRecord, list[EmitPlan], dict[str, Any]]:
        prepared = self.prepare_update(identifier, scope=scope, pin=pin)
        self._enforce_consent(
            prepared.plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            package=prepared.package,
        )
        state = self.apply_update(
            prepared,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
        )
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

    def sync(
        self,
        *,
        scope: str,
        allow_scripts: bool = False,
        enable_mcp: bool = False,
        allow_executable_extensions: bool = False,
        allow_hooks: bool = False,
        allow_dlp_findings: bool = False,
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
            for package_id, row in installed.items():
                if not isinstance(row, dict):
                    raise SkillError(
                        f"State entry for {package_id} is malformed; sync performed no changes"
                    )
            # ---- phase 1: validate everything, write nothing ----
            prepared: list[tuple[PackageRecord, list[EmitPlan], set[str]]] = []
            for package_id, row in desired["packages"].items():
                package = self._restore_locked_package(package_id, row, scope=scope)
                plans = self.plan(
                    package,
                    targets=self._validated_recorded_targets(
                        row.get("targets"), package_id=package_id, source="Lockfile"
                    ),
                    scope=scope,
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
                prepared.append((package, plans, self._consented_capabilities(plans)))
            removals = sorted(set(installed) - set(desired["packages"]))
            # ---- phase 2: one journaled transaction ----
            return state_store.reconcile(prepared, removals)

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
        self._enforce_consent(
            plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            package=package,
        )
        return self.install_prepared(
            package,
            plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            expected_current_hash=str(current.get("content_hash")),
            expected_revision=_row_revision(current),
            replace_package_id=package_id,
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
