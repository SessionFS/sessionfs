"""Multiple scoped external marketplace registries."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from sessionfs.skills.models import MarketplaceRegistry, SkillError, clean_untrusted_text
from sessionfs.skills.package import _exact_entry
from sessionfs.skills.path_safety import is_link_or_reparse
from sessionfs.skills.sources import materialize_source, normalize_git_url
from sessionfs.skills.state import atomic_json, interprocess_lock, read_json

_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_GITHUB_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _atomic_json(path: Path, value: Any) -> None:
    atomic_json(path, value)  # bounded, no-follow, temp+rename (skills.state)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return read_json(path, default)
    except SkillError as exc:
        raise SkillError(f"Cannot read marketplace registry state: {exc}") from exc


def _safe_relative(value: str) -> str:
    clean = value.strip()
    if (
        clean.startswith(("/", "\\"))
        or "\\" in clean
        or any(
            char in '<>:"|?*' or ord(char) < 32 or ord(char) == 127
            for char in clean
        )
    ):
        raise SkillError(f"Unsafe marketplace package path: {value}")
    clean = clean.removeprefix("./").rstrip("/")
    if not clean:
        return ""
    path = PurePosixPath(clean)
    if path.is_absolute() or ".." in path.parts:
        raise SkillError(f"Unsafe marketplace package path: {value}")
    return path.as_posix()


def _safe_text(value: Any, *, label: str, limit: int) -> str:
    """Untrusted marketplace text: escape sequences and control characters are
    STRIPPED at ingestion (never stored, never rendered); length is bounded."""
    text = str(value or "")
    if len(text) > limit:
        raise SkillError(f"{label} is too long (limit {limit} characters)")
    return clean_untrusted_text(text, limit=limit)


MARKETPLACE_MANIFEST_PATHS = (
    ".claude-plugin/marketplace.json",
    ".codex-plugin/marketplace.json",
    "marketplace.json",
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_marketplace_full(root: Path) -> dict[str, Any]:
    """Parse a marketplace manifest into supported packages, unsupported
    entries (recorded with a reason, never aborting registration) and renames."""
    manifest = None
    for item in MARKETPLACE_MANIFEST_PATHS:
        # Exact-spelling probe (host-independent): `Marketplace.json` is not
        # `marketplace.json` even where the filesystem would resolve it.
        directory = root
        found: Path | None = root
        for part in item.split("/"):
            found = _exact_entry(directory, part) if found is not None else None
            if found is None:
                break
            directory = found
        if found is not None and found.is_file():
            manifest = found
            break
    if manifest is None:
        raise SkillError("Marketplace source has no supported marketplace.json manifest")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError(f"Invalid marketplace manifest: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("plugins"), list):
        raise SkillError("Marketplace manifest must contain a plugins array")
    packages: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value["plugins"]):
        if not isinstance(raw, dict):
            unsupported.append({"name": f"#{index}", "reason": "plugin entry is not an object"})
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not _PACKAGE_RE.fullmatch(name):
            unsupported.append({"name": str(name)[:100], "reason": "plugin name is not a safe identifier"})
            continue
        if name in seen:
            unsupported.append({"name": name, "reason": "duplicate plugin name"})
            continue
        try:
            subdir, external, external_ref, external_sha = _parse_plugin_source(name, raw.get("source"))
            description = _safe_text(
                raw.get("description"), label=f"Marketplace plugin '{name}' description", limit=4096
            )
        except SkillError as exc:
            unsupported.append({"name": name, "reason": str(exc)[:300]})
            continue
        seen.add(name)
        packages.append(
            {
                "name": name,
                "description": description,
                "subdir": subdir,
                "external_source": external,
                "external_ref": external_ref,
                "external_sha": external_sha,
            }
        )
    renames: dict[str, str] = {}
    raw_renames = value.get("renames")
    if isinstance(raw_renames, dict):
        for old, new in raw_renames.items():
            if (
                isinstance(old, str)
                and isinstance(new, str)
                and _PACKAGE_RE.fullmatch(old)
                and _PACKAGE_RE.fullmatch(new)
            ):
                renames[old] = new
    return {"packages": packages, "unsupported": unsupported, "renames": renames}


def parse_marketplace(root: Path) -> list[dict[str, Any]]:
    """Supported packages only (see parse_marketplace_full)."""
    return parse_marketplace_full(root)["packages"]


_SOURCE_KINDS_URL = {"url", "git", None}
_SOURCE_KINDS_SUBDIR = {"git-subdir", "git-subdirectory"}


def _parse_plugin_source(
    name: str, source: Any
) -> tuple[str, str | None, str | None, str | None]:
    """Interpret a Claude Code marketplace `source` (string or object).

    Real-world shapes (anthropics/claude-plugins-official):
      "./plugins/<name>"                                   relative subdir
      {"source":"url","url":"https://…/repo.git","sha":…}  external repo
      {"source":"git-subdir","url":…,"path":…,"ref":…,"sha":…}
    plus the documented {"source":"github","repo":"owner/repo"} and "git".
    `sha` (40-hex) IS the pin when present, else `ref`, else HEAD.
    Returns (subdir, external_url, pin, sha).
    """
    if isinstance(source, str):
        return _safe_relative(source), None, None, None
    if not isinstance(source, dict):
        raise SkillError(f"Marketplace plugin '{name}' has no supported source")
    kind = source.get("source")
    external_raw: str | None = None
    subdir_raw = source.get("path") or source.get("subdir")
    if kind == "github":
        slug = source.get("repo")
        if not isinstance(slug, str) or not _GITHUB_SLUG_RE.fullmatch(slug) or ".." in slug.split("/"):
            raise SkillError(f"Marketplace plugin '{name}' has an invalid github repo slug")
        external_raw = f"https://github.com/{slug}"
    elif kind in _SOURCE_KINDS_URL or kind in _SOURCE_KINDS_SUBDIR:
        candidate = source.get("url") or source.get("repo")
        if isinstance(candidate, str):
            external_raw = candidate
        if kind in _SOURCE_KINDS_SUBDIR and not isinstance(subdir_raw, str):
            raise SkillError(f"Marketplace plugin '{name}' {kind} source requires a path")
    else:
        raise SkillError(f"Marketplace plugin '{name}' has an unsupported source type: {kind!r}")
    subdir = _safe_relative(subdir_raw) if isinstance(subdir_raw, str) else ""
    external = normalize_git_url(external_raw) if isinstance(external_raw, str) else None
    if external is not None and not external.startswith("https://"):
        raise SkillError(f"Marketplace plugin '{name}' external sources must use HTTPS")
    if not subdir and not external:
        raise SkillError(f"Marketplace plugin '{name}' has no supported source")
    sha_raw = source.get("sha")
    ref_raw = source.get("ref")
    external_sha: str | None = None
    if sha_raw is not None:
        if not isinstance(sha_raw, str) or not _SHA_RE.fullmatch(sha_raw):
            raise SkillError(f"Marketplace plugin '{name}' has an invalid sha")
        external_sha = sha_raw
    if ref_raw is not None and (
        not isinstance(ref_raw, str) or not _REF_RE.fullmatch(ref_raw) or ".." in ref_raw
    ):
        raise SkillError(f"Marketplace plugin '{name}' has an invalid ref")
    if (external_sha or ref_raw) and external is None:
        raise SkillError(f"Marketplace plugin '{name}' ref/sha only applies to external sources")
    pin = external_sha or (ref_raw if isinstance(ref_raw, str) else None)
    return subdir, external, pin, external_sha


class RegistryStore:
    def __init__(self, project_root: Path, home_dir: Path) -> None:
        self.project_root = project_root
        self.home_dir = home_dir

    def _path(self, scope: str) -> Path:
        if scope == "project":
            root = self.project_root
        elif scope == "user":
            root = self.home_dir
        else:
            raise SkillError("Marketplace scope must be 'project' or 'user'")
        path = root / ".sessionfs" / "marketplaces.json"
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current /= part
            if is_link_or_reparse(current):
                raise SkillError(f"Marketplace registry path traverses a symlink: {current}")
            if current.exists() and not current.is_dir():
                raise SkillError(f"Marketplace registry parent is not a directory: {current}")
        if is_link_or_reparse(path):
            raise SkillError(f"Marketplace registry cannot be a symlink: {path}")
        return path

    def _mutex_path(self, scope: str) -> Path:
        return self._path(scope).with_name("marketplaces.lock")

    @staticmethod
    def _parse_registry(row: dict[str, Any]) -> MarketplaceRegistry:
        try:
            registry = MarketplaceRegistry(**row)
        except TypeError as exc:
            raise SkillError("Marketplace registry state contains invalid fields") from exc
        if (
            not registry.registry_id.startswith("mkt_")
            or not _ALIAS_RE.fullmatch(registry.alias)
            or registry.scope not in {"project", "user"}
            or normalize_git_url(registry.source) != registry.source
            or not _COMMIT_RE.fullmatch(registry.commit)
            or not isinstance(registry.packages, list)
        ):
            raise SkillError("Marketplace registry state contains an invalid registry")
        for package in registry.packages:
            if not isinstance(package, dict):
                raise SkillError("Marketplace registry package rows must be objects")
            name = package.get("name")
            if not isinstance(name, str) or not _PACKAGE_RE.fullmatch(name):
                raise SkillError("Marketplace registry contains an invalid package name")
            _safe_text(
                package.get("description"),
                label=f"Marketplace plugin '{name}' description",
                limit=4096,
            )
            subdir = package.get("subdir")
            if not isinstance(subdir, str) or _safe_relative(subdir) != subdir:
                raise SkillError("Marketplace registry contains an invalid package path")
            external = package.get("external_source")
            if external is not None and (
                not isinstance(external, str) or normalize_git_url(external) != external
            ):
                raise SkillError("Marketplace registry contains an invalid external source")
            external_ref = package.get("external_ref")
            if external_ref is not None and (
                not isinstance(external_ref, str) or not _REF_RE.fullmatch(external_ref)
            ):
                raise SkillError("Marketplace registry contains an invalid external ref")
            external_sha = package.get("external_sha")
            if external_sha is not None and (
                not isinstance(external_sha, str) or not _SHA_RE.fullmatch(external_sha)
            ):
                raise SkillError("Marketplace registry contains an invalid external sha")
        if not isinstance(registry.unsupported, list) or not isinstance(registry.renames, dict):
            raise SkillError("Marketplace registry state contains invalid metadata")
        return registry

    def list(self, scope: str | None = None) -> list[MarketplaceRegistry]:
        scopes = (scope,) if scope else ("project", "user")
        result: list[MarketplaceRegistry] = []
        for item_scope in scopes:
            value = _read_json(self._path(item_scope), {"registries": []})
            rows = value.get("registries") if isinstance(value, dict) else None
            if not isinstance(rows, list):
                raise SkillError("Marketplace registry state must contain a registries array")
            for row in rows:
                if isinstance(row, dict):
                    registry = self._parse_registry(row)
                    if registry.scope != item_scope:
                        raise SkillError("Marketplace registry scope does not match its state file")
                    result.append(registry)
        return result

    def add(self, alias: str, source: str, *, scope: str, pin: str | None = None) -> MarketplaceRegistry:
        # Validate the scope FIRST - before URL normalization and before any
        # network fetch. `remove`/`refresh` already fail fast; `add` used to
        # reach _path() only after materialize_source() had already cloned the
        # caller-supplied remote, so an unsupported --scope spent a git fetch
        # against an untrusted URL and then reported the remote's error
        # instead of the real one.
        self._path(scope)
        alias = alias.strip().lower()
        if not _ALIAS_RE.fullmatch(alias):
            raise SkillError("Marketplace alias must use lowercase letters, digits, and hyphens")
        normalized = normalize_git_url(source)
        # Only the manifest candidates are materialized: a marketplace repo can
        # hold hundreds of plugins and registering it must not fetch them all.
        with materialize_source(
            normalized,
            pin=pin,
            source_type="marketplace",
            kind="git",
            paths=MARKETPLACE_MANIFEST_PATHS,
        ) as materialized:
            parsed = parse_marketplace_full(materialized.path)
            commit = materialized.identity.commit
        if not commit:
            raise SkillError("Marketplace source did not resolve to a commit")
        registry_id = "mkt_" + hashlib.sha256(
            f"{scope}\0{normalized}".encode("utf-8")
        ).hexdigest()[:24]
        registry = MarketplaceRegistry(
            registry_id=registry_id,
            alias=alias,
            scope=scope,
            source=normalized,
            commit=commit,
            packages=parsed["packages"],
            unsupported=parsed["unsupported"],
            renames=parsed["renames"],
        )
        path = self._path(scope)
        with interprocess_lock(self._mutex_path(scope)):
            existing = self.list(scope)
            if any(item.alias == alias for item in existing):
                raise SkillError(f"Marketplace alias already exists in {scope} scope: {alias}")
            # registry_id is derived from (scope, normalized source): a second
            # alias for the same source would share that id and make every
            # registry_id-keyed lookup ambiguous.
            duplicate = next(
                (
                    item
                    for item in existing
                    if item.source == normalized or item.registry_id == registry_id
                ),
                None,
            )
            if duplicate is not None:
                raise SkillError(
                    f"Marketplace source is already registered in {scope} scope as "
                    f"'{duplicate.alias}': {normalized}"
                )
            _atomic_json(
                path,
                {
                    "version": 1,
                    "registries": [
                        *[item.as_dict() for item in existing],
                        registry.as_dict(),
                    ],
                },
            )
        return registry

    def remove(self, alias: str, *, scope: str) -> None:
        with interprocess_lock(self._mutex_path(scope)):
            existing = self.list(scope)
            remaining = [item for item in existing if item.alias != alias]
            if len(remaining) == len(existing):
                raise SkillError(f"Marketplace not found in {scope} scope: {alias}")
            _atomic_json(
                self._path(scope),
                {"version": 1, "registries": [item.as_dict() for item in remaining]},
            )

    def refresh(self, alias: str, *, scope: str) -> MarketplaceRegistry:
        current, refreshed = self.fetch_refreshed(alias, scope=scope)
        return self.persist_refreshed(refreshed, expected_commit=current.commit)

    def fetch_refreshed(
        self, alias: str, *, scope: str
    ) -> tuple[MarketplaceRegistry, MarketplaceRegistry]:
        """Fetch the marketplace's current catalog WITHOUT persisting it.

        Returns (registered snapshot, refreshed snapshot). `persist_refreshed`
        writes the refreshed one; a caller that only previews (update prepare)
        must leave marketplaces.json untouched until the user confirms.
        """
        current = next((item for item in self.list(scope) if item.alias == alias), None)
        if current is None:
            raise SkillError(f"Marketplace not found in {scope} scope: {alias}")
        with materialize_source(
            current.source,
            source_type="marketplace",
            kind="git",
            paths=MARKETPLACE_MANIFEST_PATHS,
        ) as materialized:
            parsed = parse_marketplace_full(materialized.path)
            commit = materialized.identity.commit
        if not commit:
            raise SkillError("Marketplace source did not resolve to a commit")
        refreshed = MarketplaceRegistry(
            registry_id=current.registry_id,
            alias=current.alias,
            scope=current.scope,
            source=current.source,
            commit=commit,
            packages=parsed["packages"],
            unsupported=parsed["unsupported"],
            renames=parsed["renames"],
        )
        return current, refreshed

    def persist_refreshed(
        self, refreshed: MarketplaceRegistry, *, expected_commit: str
    ) -> MarketplaceRegistry:
        """Write a snapshot from `fetch_refreshed`, fenced on the commit the
        snapshot was taken against (another refresh landing meanwhile is an
        error, unless it already persisted this exact snapshot)."""
        scope = refreshed.scope
        with interprocess_lock(self._mutex_path(scope)):
            existing = self.list(scope)
            live = next((item for item in existing if item.alias == refreshed.alias), None)
            if live is None:
                raise SkillError(
                    f"Marketplace '{refreshed.alias}' ({scope} scope) is no longer registered"
                )
            if live.registry_id != refreshed.registry_id or live.source != refreshed.source:
                raise SkillError("Marketplace changed while it was being refreshed; retry")
            if live == refreshed:
                return refreshed
            if live.commit != expected_commit:  # another refresh landed meanwhile
                raise SkillError("Marketplace changed while it was being refreshed; retry")
            rows = [
                refreshed if item.registry_id == refreshed.registry_id else item
                for item in existing
            ]
            _atomic_json(
                self._path(scope),
                {"version": 1, "registries": [item.as_dict() for item in rows]},
            )
        return refreshed

    def listing_scopes(self, locator: str) -> list[str]:
        """Scopes in which `<alias>/<package>` names a listing (supported or
        not). Empty when the locator is not marketplace-shaped or unknown;
        never raises for a missing listing."""
        if ":" in locator.split("/", 1)[0] or "/" not in locator:
            return []
        alias, package_name = locator.split("/", 1)
        scopes: list[str] = []
        for registry in self.list():
            if registry.alias != alias:
                continue
            wanted = registry.renames.get(package_name, package_name)
            listed = any(item.get("name") == wanted for item in registry.packages) or any(
                item.get("name") == wanted for item in registry.unsupported
            )
            if listed and registry.scope not in scopes:
                scopes.append(registry.scope)
        return scopes

    def resolve(self, locator: str) -> tuple[MarketplaceRegistry, dict[str, Any]]:
        requested_scope: str | None = None
        if ":" in locator.split("/", 1)[0]:
            requested_scope, locator = locator.split(":", 1)
            if requested_scope not in {"project", "user"}:
                raise SkillError("Marketplace qualifier must be 'project:' or 'user:'")
        if "/" not in locator:
            raise SkillError("Marketplace package must use <marketplace>/<package>")
        alias, package_name = locator.split("/", 1)
        matches: list[tuple[MarketplaceRegistry, dict[str, Any]]] = []
        unsupported_reason: str | None = None
        for registry in self.list():
            if requested_scope and registry.scope != requested_scope:
                continue
            if registry.alias != alias:
                continue
            # Top-level `renames`: old name -> new name (single hop).
            wanted = registry.renames.get(package_name, package_name)
            for package in registry.packages:
                if package.get("name") == wanted:
                    matches.append((registry, package))
            for item in registry.unsupported:
                if item.get("name") == wanted:
                    unsupported_reason = str(item.get("reason") or "unsupported source")
        if not matches:
            if unsupported_reason:
                raise SkillError(
                    f"Marketplace package is listed but unsupported: {locator} ({unsupported_reason})"
                )
            raise SkillError(f"Marketplace package not found: {locator}")
        if len(matches) > 1:
            scopes = ", ".join(sorted(item[0].scope for item in matches))
            raise SkillError(f"Marketplace package is ambiguous across scopes ({scopes}); qualify the scope")
        return matches[0]
