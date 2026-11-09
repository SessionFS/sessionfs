"""Committed OpenAPI snapshot of the SessionFS API, plus a breaking-change check.

Usage::

    python -m sessionfs.server.openapi_snapshot            # write docs/api/openapi.json
    python -m sessionfs.server.openapi_snapshot --check    # exit 1 if it is stale
    python -m sessionfs.server.openapi_snapshot --accept-breaking   # write despite breaking changes

The snapshot is normalised so it only changes when the API does: sorted keys,
two-space indent, no ``info.version``. ``tests/unit/test_openapi_snapshot.py``
regenerates it and fails when it differs from the committed file. A change
that removes a path or method, removes or renames a request or response field,
changes a field's type, or adds a required request field is BREAKING: ship a
deprecation first (see :mod:`sessionfs.server.api_contract` and the "API
contract and versioning" docs page), or update the snapshot deliberately with
``--accept-breaking`` (or ``SFS_OPENAPI_ACCEPT_BREAKING=1``).
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH = REPO_ROOT / "docs" / "api" / "openapi.json"
ACCEPT_BREAKING_ENV = "SFS_OPENAPI_ACCEPT_BREAKING"
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def build_openapi() -> dict[str, Any]:
    """The live OpenAPI document, normalised (no version string)."""
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    app = create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))
    doc = json.loads(json.dumps(app.openapi()))
    info = doc.get("info") or {}
    info.pop("version", None)
    doc["info"] = info
    return doc


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Change classification
# ---------------------------------------------------------------------------


@dataclass
class ContractDiff:
    breaking: list[str] = field(default_factory=list)
    additive: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.breaking or self.additive)


class _Resolver:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.schemas = ((doc.get("components") or {}).get("schemas")) or {}

    def resolve(self, schema: Any, seen: frozenset[str] = frozenset()) -> tuple[Any, frozenset[str]]:
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            name = ref.rsplit("/", 1)[-1]
            if name in seen:
                return {}, seen
            seen = seen | {name}
            schema = self.schemas.get(name, {})
        return schema, seen


def _non_null_variants(schema: dict[str, Any]) -> list[Any]:
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return [s for s in schema[key] if not (isinstance(s, dict) and s.get("type") == "null")]
    return []


def _type_set(schema: Any, resolver: _Resolver) -> frozenset[str]:
    schema, _ = resolver.resolve(schema)
    if not isinstance(schema, dict):
        return frozenset()
    if "anyOf" in schema or "oneOf" in schema:
        out: set[str] = set()
        for sub in schema.get("anyOf", []) + schema.get("oneOf", []):
            out |= _type_set(sub, resolver)
        return frozenset(out)
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return _type_set(schema["allOf"][0], resolver)
    t = schema.get("type")
    if isinstance(t, list):
        return frozenset(t)
    if isinstance(t, str):
        return frozenset({t})
    if "properties" in schema:
        return frozenset({"object"})
    return frozenset()


def _object_schema(schema: Any, resolver: _Resolver, seen: frozenset[str]) -> tuple[dict[str, Any], frozenset[str]]:
    schema, seen = resolver.resolve(schema, seen)
    if not isinstance(schema, dict):
        return {}, seen
    variants = _non_null_variants(schema)
    if len(variants) == 1:
        return _object_schema(variants[0], resolver, seen)
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return _object_schema(schema["allOf"][0], resolver, seen)
    return schema, seen


def _compare_schema(
    old: Any,
    new: Any,
    where: str,
    *,
    request: bool,
    old_r: _Resolver,
    new_r: _Resolver,
    diff: ContractDiff,
    seen_old: frozenset[str] = frozenset(),
    seen_new: frozenset[str] = frozenset(),
    depth: int = 0,
) -> None:
    if depth > 8:
        return
    old_types = _type_set(old, old_r)
    new_types = _type_set(new, new_r)
    if old_types and new_types and old_types != new_types:
        # A request field may widen (accept more); a response field may narrow.
        widened_ok = request and old_types <= new_types
        narrowed_ok = (not request) and new_types <= old_types
        msg = f"{where}: type {sorted(old_types)} -> {sorted(new_types)}"
        if widened_ok or narrowed_ok:
            diff.additive.append(msg)
        else:
            diff.breaking.append(msg)
            return

    old_s, seen_old = _object_schema(old, old_r, seen_old)
    new_s, seen_new = _object_schema(new, new_r, seen_new)
    old_props = old_s.get("properties") or {}
    new_props = new_s.get("properties") or {}
    old_req = set(old_s.get("required") or [])
    new_req = set(new_s.get("required") or [])
    kind = "request" if request else "response"

    for name in sorted(set(old_props) - set(new_props)):
        diff.breaking.append(f"{where}: {kind} field '{name}' removed or renamed")
    for name in sorted(set(new_props) - set(old_props)):
        if request and name in new_req:
            diff.breaking.append(f"{where}: new required request field '{name}'")
        else:
            diff.additive.append(f"{where}: {kind} field '{name}' added")
    if request:
        for name in sorted((new_req - old_req) & set(old_props)):
            diff.breaking.append(f"{where}: request field '{name}' became required")
    for name in sorted(set(old_props) & set(new_props)):
        _compare_schema(
            old_props[name], new_props[name], f"{where}.{name}",
            request=request, old_r=old_r, new_r=new_r, diff=diff,
            seen_old=seen_old, seen_new=seen_new, depth=depth + 1,
        )
    old_items = old_s.get("items")
    new_items = new_s.get("items")
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        _compare_schema(
            old_items, new_items, f"{where}[]",
            request=request, old_r=old_r, new_r=new_r, diff=diff,
            seen_old=seen_old, seen_new=seen_new, depth=depth + 1,
        )


def _json_schema(container: Any) -> Any:
    if not isinstance(container, dict):
        return None
    content = container.get("content") or {}
    media = content.get("application/json")
    if media is None and content:
        media = content[sorted(content)[0]]
    return (media or {}).get("schema")


def _params(op: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(p.get("in", ""), p.get("name", "")): p for p in op.get("parameters") or []}


def classify(old_doc: dict[str, Any], new_doc: dict[str, Any]) -> ContractDiff:
    """Classify every contract change between two OpenAPI documents."""
    diff = ContractDiff()
    old_r, new_r = _Resolver(old_doc), _Resolver(new_doc)
    old_paths = old_doc.get("paths") or {}
    new_paths = new_doc.get("paths") or {}

    for path in sorted(set(old_paths) | set(new_paths)):
        old_ops = {m: o for m, o in (old_paths.get(path) or {}).items() if m in _HTTP_METHODS}
        new_ops = {m: o for m, o in (new_paths.get(path) or {}).items() if m in _HTTP_METHODS}
        for method in sorted(set(old_ops) - set(new_ops)):
            diff.breaking.append(f"{method.upper()} {path}: removed")
        for method in sorted(set(new_ops) - set(old_ops)):
            diff.additive.append(f"{method.upper()} {path}: added")
        for method in sorted(set(old_ops) & set(new_ops)):
            old_op, new_op = old_ops[method], new_ops[method]
            where = f"{method.upper()} {path}"

            old_p, new_p = _params(old_op), _params(new_op)
            for key in sorted(set(old_p) - set(new_p)):
                diff.breaking.append(f"{where}: {key[0]} parameter '{key[1]}' removed")
            for key in sorted(set(new_p) - set(old_p)):
                if new_p[key].get("required"):
                    diff.breaking.append(f"{where}: new required {key[0]} parameter '{key[1]}'")
                else:
                    diff.additive.append(f"{where}: {key[0]} parameter '{key[1]}' added")
            for key in sorted(set(old_p) & set(new_p)):
                if new_p[key].get("required") and not old_p[key].get("required"):
                    diff.breaking.append(f"{where}: {key[0]} parameter '{key[1]}' became required")
                _compare_schema(
                    old_p[key].get("schema"), new_p[key].get("schema"),
                    f"{where} {key[0]}:{key[1]}", request=True,
                    old_r=old_r, new_r=new_r, diff=diff,
                )

            old_body = _json_schema(old_op.get("requestBody"))
            new_body = _json_schema(new_op.get("requestBody"))
            if old_body is None and new_body is not None:
                if (new_op.get("requestBody") or {}).get("required"):
                    diff.breaking.append(f"{where}: new required request body")
                else:
                    diff.additive.append(f"{where}: request body added")
            elif old_body is not None and new_body is not None:
                _compare_schema(
                    old_body, new_body, f"{where} body", request=True,
                    old_r=old_r, new_r=new_r, diff=diff,
                )

            old_resp = old_op.get("responses") or {}
            new_resp = new_op.get("responses") or {}
            for status in sorted(old_resp):
                if not status.startswith("2"):
                    continue
                if status not in new_resp:
                    diff.breaking.append(f"{where}: response {status} removed")
                    continue
                old_schema = _json_schema(old_resp[status])
                new_schema = _json_schema(new_resp[status])
                if old_schema is not None and new_schema is not None:
                    _compare_schema(
                        old_schema, new_schema, f"{where} -> {status}", request=False,
                        old_r=old_r, new_r=new_r, diff=diff,
                    )
    return diff


def unified_diff(old_text: str, new_text: str, limit: int = 200) -> str:
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile="docs/api/openapi.json (committed)",
            tofile="docs/api/openapi.json (generated)", lineterm="", n=2,
        )
    )
    if len(lines) > limit:
        lines = lines[:limit] + [f"... ({len(lines) - limit} more diff lines)"]
    return "\n".join(lines)


def report(diff: ContractDiff) -> str:
    out: list[str] = []
    if diff.breaking:
        out.append(f"BREAKING changes ({len(diff.breaking)}):")
        out += [f"  - {line}" for line in diff.breaking]
    if diff.additive:
        out.append(f"Additive / compatible changes ({len(diff.additive)}):")
        out += [f"  - {line}" for line in diff.additive[:100]]
        if len(diff.additive) > 100:
            out.append(f"  ... and {len(diff.additive) - 100} more")
    return "\n".join(out)


HOW_TO_FIX = (
    "Additive change: regenerate the snapshot with\n"
    "    PYTHONPATH=src python -m sessionfs.server.openapi_snapshot\n"
    "and commit docs/api/openapi.json.\n"
    "BREAKING change: /api/v1 promises no breaking change without a deprecation "
    "window. Keep the old field/route working and mark it deprecated "
    "(sessionfs.server.api_contract.mark_deprecated / mark_field_deprecated / "
    "deprecated_route, which send Deprecation + Sunset headers) for at least one "
    "minor release. If the break is deliberate and approved, update the snapshot with\n"
    "    PYTHONPATH=src python -m sessionfs.server.openapi_snapshot --accept-breaking\n"
    f"(or {ACCEPT_BREAKING_ENV}=1)."
)


def _accept_breaking_env() -> bool:
    return os.environ.get(ACCEPT_BREAKING_ENV, "").strip().lower() in ("1", "true", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed snapshot is stale")
    parser.add_argument("--accept-breaking", action="store_true", help="write even if the change is breaking")
    parser.add_argument("--output", type=Path, default=SNAPSHOT_PATH)
    args = parser.parse_args(argv)

    new_doc = build_openapi()
    new_text = render(new_doc)
    old_text = args.output.read_text() if args.output.exists() else ""
    if old_text == new_text:
        print(f"{args.output} is up to date.")
        return 0

    diff = classify(json.loads(old_text), new_doc) if old_text else ContractDiff()
    if diff.changed:
        print(report(diff))
    if args.check:
        print(f"\n{args.output} is stale.\n{HOW_TO_FIX}")
        return 1
    if diff.breaking and not (args.accept_breaking or _accept_breaking_env()):
        print(f"\nRefusing to write: the API change is BREAKING.\n{HOW_TO_FIX}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(new_text)
    print(f"Wrote {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
