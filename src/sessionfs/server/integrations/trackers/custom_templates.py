"""Validation and shape for org-custom Jira story templates.

Custom templates live in ``jira_templates``/``jira_template_versions`` as a
JSON array of section dicts. This module validates that JSON against the fixed
source-field vocabulary and the documented limits, and turns a validated
sections array back into a ``Template`` that ``promote.py`` consumes exactly
like a built-in. The routes own persistence and DB lookups; this module is
pure (no DB, no HTTP) so it is unit-testable in isolation.

Field paths in ``TemplateValidationError`` match the request body (``slug``,
``name``, ``sections[i].heading``, ...) so the route can return a 422 that
names the offending field.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sessionfs.server.integrations.trackers.templates import (
    SOURCE_FIELDS,
    SOURCE_FIELDS_BY_MODE,
    Template,
    TemplateSection,
    get_template,
)

MAX_SECTIONS = 12
MAX_HEADING_CHARS = 80
MAX_GUIDANCE_CHARS = 500
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 1000
MAX_SLUG_CHARS = 50
MAX_TEMPLATES_PER_ORG = 50

# Custom slugs are lowercase letters/digits/hyphens; they must not collide
# with a built-in slug (checked separately against the built-in registry).
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
# Section keys are internal identifiers; keep them lower_snake_case so the
# publish body's unknown-key check stays predictable.
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class TemplateValidationError(ValueError):
    """A custom template input failed validation; ``field`` is the body path."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def _reject_control_chars(field: str, value: str, *, multiline: bool) -> None:
    """Refuse Unicode control (Cc) and format (Cf) characters.

    Format characters (bidi overrides, zero-width joiners, …) can make a
    heading or guidance render differently from what an admin reviewed, and
    control characters have no place in Jira text. Multi-line fields
    (guidance, description) may keep newlines and tabs.
    """
    allowed = {"\n", "\t"} if multiline else set()
    for ch in value:
        if ch in allowed:
            continue
        if unicodedata.category(ch) in ("Cc", "Cf"):
            raise TemplateValidationError(
                field, f"{field.rsplit('.', 1)[-1]} contains a control or format character"
            )


def validate_slug(slug: str | None) -> str:
    value = (slug or "").strip()
    if not value:
        raise TemplateValidationError("slug", "slug is required")
    if len(value) > MAX_SLUG_CHARS:
        raise TemplateValidationError(
            "slug", f"slug must be at most {MAX_SLUG_CHARS} characters"
        )
    if not _SLUG_RE.match(value):
        raise TemplateValidationError(
            "slug", "slug must be lowercase letters, digits and hyphens"
        )
    if get_template(value) is not None:
        raise TemplateValidationError(
            "slug", f"{value!r} is a built-in template slug"
        )
    return value


def validate_name(name: str | None) -> str:
    value = (name or "").strip()
    if not value:
        raise TemplateValidationError("name", "name is required")
    if len(value) > MAX_NAME_CHARS:
        raise TemplateValidationError(
            "name", f"name must be at most {MAX_NAME_CHARS} characters"
        )
    _reject_control_chars("name", value, multiline=False)
    return value


def validate_description(description: str | None) -> str | None:
    if description is None:
        return None
    if not isinstance(description, str):
        raise TemplateValidationError("description", "description must be a string")
    value = description.strip()
    if not value:
        return None
    if len(value) > MAX_DESCRIPTION_CHARS:
        raise TemplateValidationError(
            "description",
            f"description must be at most {MAX_DESCRIPTION_CHARS} characters",
        )
    _reject_control_chars("description", value, multiline=True)
    return value


def _validate_section(section: object, index: int, seen: set[str]) -> dict:
    base = f"sections[{index}]"
    if not isinstance(section, dict):
        raise TemplateValidationError(base, "each section must be an object")

    key = section.get("key")
    if not isinstance(key, str) or not key.strip():
        raise TemplateValidationError(f"{base}.key", "key is required")
    key = key.strip()
    if not _KEY_RE.match(key):
        raise TemplateValidationError(f"{base}.key", "key must be lower_snake_case")
    if key in seen:
        raise TemplateValidationError(f"{base}.key", f"duplicate section key {key!r}")
    seen.add(key)

    heading = section.get("heading")
    if not isinstance(heading, str) or not heading.strip():
        raise TemplateValidationError(f"{base}.heading", "heading is required")
    heading = heading.strip()
    if len(heading) > MAX_HEADING_CHARS:
        raise TemplateValidationError(
            f"{base}.heading", f"heading must be at most {MAX_HEADING_CHARS} characters"
        )
    _reject_control_chars(f"{base}.heading", heading, multiline=False)

    mode = section.get("mode")
    if mode not in ("verbatim", "rewrite"):
        raise TemplateValidationError(
            f"{base}.mode", "mode must be 'verbatim' or 'rewrite'"
        )

    source_fields = section.get("source_fields")
    if not isinstance(source_fields, list) or not source_fields:
        raise TemplateValidationError(
            f"{base}.source_fields", "source_fields must be a non-empty list"
        )
    allowed = SOURCE_FIELDS_BY_MODE[mode]
    for j, field in enumerate(source_fields):
        path = f"{base}.source_fields[{j}]"
        if not isinstance(field, str) or field not in SOURCE_FIELDS:
            raise TemplateValidationError(path, f"unknown source field {field!r}")
        if field not in allowed:
            # promote.py has no rendering for this field in this mode; refuse
            # it rather than publishing a section that is silently empty.
            raise TemplateValidationError(
                path,
                f"source field {field!r} can't be used in a {mode} section "
                f"(allowed: {', '.join(allowed)})",
            )
    if len(source_fields) != len(set(source_fields)):
        raise TemplateValidationError(
            f"{base}.source_fields", "source_fields must not repeat fields"
        )
    source_fields = tuple(source_fields)

    guidance = section.get("guidance")
    if guidance is not None:
        if mode != "rewrite":
            # Guidance must never steer a verbatim section (its text is copied
            # from the source and never touches the model).
            raise TemplateValidationError(
                f"{base}.guidance", "guidance is only allowed on rewrite sections"
            )
        if not isinstance(guidance, str):
            raise TemplateValidationError(f"{base}.guidance", "guidance must be a string")
        guidance = guidance.strip()
        if len(guidance) > MAX_GUIDANCE_CHARS:
            raise TemplateValidationError(
                f"{base}.guidance",
                f"guidance must be at most {MAX_GUIDANCE_CHARS} characters",
            )
        _reject_control_chars(f"{base}.guidance", guidance, multiline=True)
        if not guidance:
            guidance = None

    # Unknown keys are dropped (forward-compatible) so an older server can read
    # a template written by a newer one without losing it on write.
    return {
        "key": key,
        "heading": heading,
        "mode": mode,
        "source_fields": list(source_fields),
        "guidance": guidance,
    }


def validate_sections(sections: object) -> list[dict]:
    """Validate and normalize a raw sections JSON array.

    Returns a list of normalized section dicts with exactly the keys the
    persistence layer stores (``key``, ``heading``, ``mode``, ``source_fields``,
    ``guidance``). Raises ``TemplateValidationError`` on the first failure.
    """
    if not isinstance(sections, list) or not sections:
        raise TemplateValidationError(
            "sections", "sections must be a non-empty list"
        )
    if len(sections) > MAX_SECTIONS:
        raise TemplateValidationError(
            "sections", f"a template can have at most {MAX_SECTIONS} sections"
        )
    seen: set[str] = set()
    return [_validate_section(section, i, seen) for i, section in enumerate(sections)]


def sections_to_template(slug: str, name: str, sections: list[dict]) -> Template:
    """Rebuild a ``Template`` from validated/normalized section dicts."""
    return Template(
        slug=slug,
        name=name,
        sections=tuple(
            TemplateSection(
                key=s["key"],
                heading=s["heading"],
                mode=s["mode"],
                source_fields=tuple(s["source_fields"]),
                guidance=s.get("guidance"),
            )
            for s in sections
        ),
    )


@dataclass(frozen=True)
class ResolvedTemplate:
    """A template resolved for one ticket, with provenance for publish storage.

    ``slug``/``name``/``version`` are what a link stores (version is None for
    built-ins); ``builtin`` distinguishes the two. ``template`` is the object
    ``promote.py`` consumes.
    """

    slug: str
    name: str
    version: int | None
    builtin: bool
    template: Template


def builtin_resolved(slug: str) -> ResolvedTemplate:
    """Resolve a built-in slug (assumes the caller has checked it exists)."""
    template = get_template(slug)
    assert template is not None, f"not a built-in template: {slug}"
    return ResolvedTemplate(
        slug=slug, name=template.name, version=None, builtin=True, template=template
    )
