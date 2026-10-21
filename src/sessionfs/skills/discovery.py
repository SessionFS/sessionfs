"""Marketplace discovery helpers (marketplace v2 §9): the category taxonomy,
search facets derived from a version's stored metadata, install-count display
rules, popularity buckets, README sanitizing, and package-image sniffing.

Pure functions with no server imports: the ORM models, migration 076's
backfill, the API and the CLI all use the same definitions.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Categories (§9.2: a fixed, curated taxonomy; a package picks up to three)
# ---------------------------------------------------------------------------

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("review", "Code review"),
    ("testing", "Testing"),
    ("debugging", "Debugging"),
    ("refactoring", "Refactoring"),
    ("docs", "Documentation"),
    ("security", "Security"),
    ("frontend", "Frontend"),
    ("backend", "Backend and APIs"),
    ("data", "Data and analytics"),
    ("infra", "Infrastructure"),
    ("deployment", "Deployment and release"),
    ("workflow", "Workflow and planning"),
    ("integrations", "Integrations"),
    ("ai-ml", "AI and ML"),
    ("productivity", "Productivity"),
)
CATEGORY_SLUGS = frozenset(slug for slug, _ in CATEGORIES)
MAX_CATEGORIES_PER_PACKAGE = 3

# ---------------------------------------------------------------------------
# Search facets
# ---------------------------------------------------------------------------

SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
# Capability classes a consumer can filter on, from the byte-derived
# risk_summary. "instructions_only" means none of the others.
CAPABILITY_CLASSES = ("scripts", "mcp", "hooks", "executables", "allowed_tools")
CAPABILITY_FILTERS = ("instructions_only", *CAPABILITY_CLASSES)
LICENSE_KEY_MAX = 100


def _obj(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def license_key(license_metadata: Any) -> str | None:
    """The declared license, normalized for an exact-match filter (casefolded,
    whitespace collapsed). None when undeclared."""
    meta = _obj(license_metadata)
    value = meta.get("value") if meta.get("declared") else None
    if not isinstance(value, str):
        return None
    normalized = normalize_license(value)
    return normalized or None


def normalize_license(value: str) -> str:
    return " ".join(value.split()).casefold()[:LICENSE_KEY_MAX]


def scan_severity_rank(scan_summary: Any) -> int:
    """Worst OPEN (unsuppressed) instruction-scan severity as a rank."""
    summary = _obj(scan_summary)
    severity = summary.get("open_max_severity")
    if severity is None:
        severity = summary.get("max_severity")
    return SEVERITY_RANK.get(str(severity or "none"), 0)


def capability_flags(risk_summary: Any) -> str:
    """Comma-delimited capability classes (",scripts,mcp,"), so a filter is a
    plain LIKE on both dialects; "" means instruction-only."""
    risk = _obj(risk_summary)
    present = []
    if risk.get("scripts"):
        present.append("scripts")
    if risk.get("mcp"):
        present.append("mcp")
    if risk.get("hooks"):
        present.append("hooks")
    if risk.get("executable_extensions"):
        present.append("executables")
    if risk.get("allowed_tools"):
        present.append("allowed_tools")
    return "," + ",".join(present) + "," if present else ""


def provenance_attested(signature_metadata: Any) -> bool:
    """Verified publication provenance: a trusted-publishing (GitHub Actions
    OIDC) publish, or a verified attestation once those exist."""
    signature = _obj(signature_metadata)
    return bool(signature.get("attested")) or (
        signature.get("kind") == "github_oidc" and signature.get("verification") == "oidc_token"
    )


# ---------------------------------------------------------------------------
# Install counts
# ---------------------------------------------------------------------------

INSTALL_EVENTS = ("install", "uninstall", "active", "translation_failure")
INSTALL_SOURCES = ("download", "reconcile", "native")
K_FLOOR = 10
# Service-key installs count separately and weigh this much in ranking.
SERVICE_INSTALL_WEIGHT = 0.1


def floored(count: int) -> dict[str, Any]:
    """A count under the display floor is shown as "<10" and its value
    withheld (§9.2, §9.5)."""
    count = max(0, int(count))
    if 0 < count < K_FLOOR:
        return {"count": None, "display": f"<{K_FLOOR}"}
    return {"count": count, "display": str(count)}


ANALYTICS_WINDOWS = (7, 30, 90)


def coarse_counter(cells: Iterable[int]):
    """One coarsening rule for every count a publisher can see.

    ``cells`` are the finest-grained counts behind a view (package × version
    × tool × day within the window). If ANY of them is hidden (1-9), every
    count derived from them is rounded down to a multiple of 10 and marked
    approximate ("40+"), so no hidden cell can be recovered by subtracting
    one displayed count from another (totals vs breakdowns, tool vs version,
    day vs window, listing vs analytics). Counts under 10 stay "<10" and
    zero stays "0". When no cell is hidden every count is exact."""
    hidden = any(0 < int(cell) < K_FLOOR for cell in cells)

    def display(count: int) -> dict[str, Any]:
        count = max(0, int(count))
        if not hidden or count < K_FLOOR:
            return floored(count)
        rounded = count - count % K_FLOOR
        return {"count": rounded, "display": f"{rounded}+", "approximate": True}

    return display


def floored_group(total: int, parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The displayed total of a count shown with a breakdown. When any part
    is hidden ("<10") or itself approximate, an exact total would let the
    hidden part be recovered by subtraction, so the total is rounded DOWN to
    a multiple of 10 and marked approximate ("40+")."""
    total = max(0, int(total))
    if not any(part.get("count") is None or part.get("approximate") for part in parts):
        return floored(total)
    if total < K_FLOOR:
        return {"count": None, "display": f"<{K_FLOOR}"}
    rounded = total - total % K_FLOOR
    return {"count": rounded, "display": f"{rounded}+", "approximate": True}


def displayed_rate(numerator: dict[str, Any], denominator: dict[str, Any]) -> float | None:
    """A rate computed from the DISPLAYED counts, never the exact ones:
    shown only when both clear the floor, and rounded to two decimals, so it
    cannot reverse a rounded or hidden count."""
    top, bottom = numerator.get("count"), denominator.get("count")
    if top is None or bottom is None or bottom < K_FLOOR or top < K_FLOOR:
        return None
    return round(top / bottom, 2)


def popularity_rank(user_installs: int, service_installs: int = 0) -> int:
    """floor(log2(1 + weighted installs over 30 days)): the ranking's
    install term, coarse enough that small gaming moves nothing."""
    weighted = max(0, user_installs) + SERVICE_INSTALL_WEIGHT * max(0, service_installs)
    return int(math.floor(math.log2(1 + weighted)))


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

README_MAX_BYTES = 256 * 1024
README_FLAG_LIMIT = 200
README_NAMES = ("readme.md", "readme.markdown", "readme")

# Same classes as the instruction scanner's hidden-Unicode rules
# (skills/instruction_scan.py), plus C0/C1 controls except TAB/LF/CR.
_BIDI = "\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c"
_TAGS = "\U000e0000-\U000e007f"
_VARIATION = "\ufe00-\ufe0f\U000e0100-\U000e01ef"
# ZWNJ / ZWJ (U+200C / U+200D) are left alone: they join emoji and Indic
# scripts in ordinary prose (the scanner exempts them the same way).
_ZERO_WIDTH = (
    "\u00ad\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b"
    "\u2060-\u2065\u206a-\u206f\u3164\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0080-\U000e00ff\U000e01f0-\U000e0fff"
)
_CONTROL = "\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
# A single variation selector is normal (emoji presentation); a run of three
# or more can carry hidden data (the scanner's variation_selector_run rule).
_HIDDEN = re.compile(f"[{_BIDI}{_TAGS}{_ZERO_WIDTH}{_CONTROL}]|[{_VARIATION}]{{3,}}")


def has_hidden_characters(value: str) -> bool:
    """Any control, invisible, tag, bidi or variation-selector-run character."""
    return bool(_HIDDEN.search(value))


_TERMINAL_UNSAFE = re.compile(
    f"[{_BIDI}{_TAGS}{_ZERO_WIDTH}{_VARIATION}\x00-\x08\x0b-\x1f\x7f-\x9f\u200c\u200d]"
)


def terminal_safe(value: object) -> str:
    """Server-supplied text made safe to print in a terminal: every control
    (except newline and tab), escape, zero-width, bidi, tag and variation
    character is dropped, so a package's metadata cannot move the cursor,
    recolour the screen or reorder what is shown."""
    return _TERMINAL_UNSAFE.sub("", "" if value is None else str(value))


def _hidden_kind(char: str) -> str:
    if re.match(f"[{_BIDI}]", char):
        return "bidi"
    if re.match(f"[{_TAGS}]", char):
        return "tag"
    if re.match(f"[{_VARIATION}]", char):
        return "variation_selector"
    if re.match(f"[{_CONTROL}]", char):
        return "control"
    return "zero_width"


@dataclass(frozen=True)
class SafeReadme:
    content: str
    hidden_unicode: list[dict[str, Any]]
    truncated: bool


def sanitize_readme(raw: bytes | str) -> SafeReadme:
    """README text safe to hand to a markdown renderer that never renders raw
    HTML: decoded as UTF-8 (invalid bytes replaced), capped, with every hidden
    or bidirectional character replaced by a visible ⟦U+XXXX⟧ marker and
    reported with its line and column (the same notation as the install
    disclosure). The renderer still treats the text as untrusted."""
    truncated = False
    if isinstance(raw, bytes):
        if len(raw) > README_MAX_BYTES:
            raw = raw[:README_MAX_BYTES]
            truncated = True
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
        if len(text.encode("utf-8")) > README_MAX_BYTES:
            text = text.encode("utf-8")[:README_MAX_BYTES].decode("utf-8", errors="ignore")
            truncated = True
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    flags: list[dict[str, Any]] = []
    out: list[str] = []
    for line_no, line in enumerate(text.split("\n"), start=1):
        def _mark(match: re.Match[str], _line: int = line_no) -> str:
            marked = []
            for offset, char in enumerate(match.group(0)):
                code = f"U+{ord(char):04X}"
                if len(flags) < README_FLAG_LIMIT:
                    flags.append(
                        {"line": _line, "column": match.start() + offset + 1,
                         "code_point": code, "kind": _hidden_kind(char)}
                    )
                marked.append(f"⟦{code}⟧")
            return "".join(marked)

        out.append(_HIDDEN.sub(_mark, line))
    return SafeReadme(content="\n".join(out), hidden_unicode=flags, truncated=truncated)


def first_skill_body(instruction_bundle: Iterable[dict]) -> tuple[str, str] | None:
    """(path, body) of the first SKILL.md in an instruction bundle, without its
    YAML frontmatter: the README fallback (§9.2)."""
    for item in instruction_bundle:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if path.rsplit("/", 1)[-1].casefold() != "skill.md":
            continue
        content = str(item.get("content") or "")
        if content.startswith("---\n"):
            end = content.find("\n---", 4)
            if end != -1:
                newline = content.find("\n", end + 4)
                content = content[newline + 1:] if newline != -1 else ""
        return path, content.lstrip("\n")
    return None


_SAFE_PATH = re.compile(r"^[A-Za-z0-9._\-][A-Za-z0-9._\-/ ]{0,299}$")


def valid_package_path(path: str) -> bool:
    """A relative path inside a package: no absolute, parent or empty
    segments, no backslashes or control characters."""
    if not path or len(path) > 300 or not _SAFE_PATH.fullmatch(path):
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") for part in parts)


# ---------------------------------------------------------------------------
# Package images (README images load only from the package itself)
# ---------------------------------------------------------------------------

IMAGE_MAX_BYTES = 1024 * 1024
_IMAGE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".gif": "image/gif", ".webp": "image/webp"}


def image_media_type(path: str, payload: bytes) -> str | None:
    """The media type of a raster image whose extension AND magic bytes
    agree; None otherwise. SVG is never served (it can carry script)."""
    dot = path.rfind(".")
    expected = _IMAGE_EXTENSIONS.get(path[dot:].casefold()) if dot != -1 else None
    if expected is None or len(payload) > IMAGE_MAX_BYTES:
        return None
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        sniffed = "image/png"
    elif payload.startswith(b"\xff\xd8\xff"):
        sniffed = "image/jpeg"
    elif payload[:6] in (b"GIF87a", b"GIF89a"):
        sniffed = "image/gif"
    elif payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        sniffed = "image/webp"
    else:
        return None
    return sniffed if sniffed == expected else None


# ---------------------------------------------------------------------------
# Freshness (§9.2)
# ---------------------------------------------------------------------------

STALE_AFTER_DAYS = 180
DEDUPE_RETENTION_DAYS = 30
VERIFY_STATUSES = ("unknown", "passing", "failing", "regressed")
