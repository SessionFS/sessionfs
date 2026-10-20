"""Lookalike / confusable name detection for marketplace namespaces and names.

Marketplace v2 §4.5. Pure functions, no network, deterministic across server
and CLI. The confusables mapping is the full Unicode UTS #39 data file,
vendored and pinned (``data/uts39-confusables-16.0.0.txt``, Unicode license,
see THIRD_PARTY_NOTICES.md); its SHA-256 is checked when it is loaded.

The skeleton is the UTS #39 skeleton, extended for package identifiers:

1. NFKC (fullwidth, ligatures, mathematical alphanumerics, ...);
2. NFD; drop combining marks (``acmé`` -> ``acme``) and invisible /
   default-ignorable characters (zero-width joiners, soft hyphen, BOM, bidi
   controls);
3. map every code point through the UTS #39 table (so whole-script lookalikes
   such as Cherokee ``ᏟᎻᎪᎢᏀᏢᎢ`` reduce to ``CHATGPT``), casefold, map again,
   then a small supplement for small capitals and dash variants;
4. strip separators (``-``, ``_``, ``.``, whitespace);
5. fold leetspeak digits (``0 1 2 3 4 5 6 7 8 9 |`` ->
   ``o l z e a s b t b g l``);
6. fold multi-character lookalikes (``rn``->``m``, ``vv``->``w``, ``cl``->``d``).

Two names whose skeletons are equal are confusable. Display names are
additionally held to the UTS #39 "Moderately Restrictive" level
(:func:`restriction_level`); namespaces are ASCII-only, which is stricter
than "Highly Restrictive".
"""

from __future__ import annotations

import functools
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CONFUSABLES_VERSION = "16.0.0"
_CONFUSABLES_FILE = Path(__file__).with_name("data") / f"uts39-confusables-{CONFUSABLES_VERSION}.txt"
_CONFUSABLES_SHA256 = "95bd0aad6dced5ebc63436f459c06ab21a8d107cd842fb57f5c3a1e91bca8611"


@functools.lru_cache(maxsize=1)
def confusables_table() -> dict[str, str]:
    """source code point -> prototype string, from the vendored UTS #39 file.

    Raises RuntimeError when the file is missing or altered: lookalike
    blocking must never silently degrade to a partial table."""
    try:
        raw = _CONFUSABLES_FILE.read_bytes()
    except OSError as exc:  # pragma: no cover - packaging error
        raise RuntimeError(f"UTS #39 confusables data missing: {_CONFUSABLES_FILE}") from exc
    if hashlib.sha256(raw).hexdigest() != _CONFUSABLES_SHA256:
        raise RuntimeError("UTS #39 confusables data does not match the pinned checksum")
    table: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split(";")]
        if len(fields) < 2:
            continue
        source = "".join(chr(int(point, 16)) for point in fields[0].split())
        target = "".join(chr(int(point, 16)) for point in fields[1].split())
        if len(source) == 1:
            table[source] = target
    return table


# Applied after the UTS #39 table: small capitals and IPA letters the table
# maps TO (e.g. Cyrillic н -> ʜ), plus dash variants, to plain ASCII.
_SUPPLEMENT: dict[str, str] = {
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ɢ": "g", "ʜ": "h", "ɪ": "i",
    "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r",
    "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
    "ı": "i", "ȷ": "j", "ɑ": "a", "ɩ": "i", "ł": "l", "ƚ": "l", "ɫ": "l", "ð": "d",
    "đ": "d", "ħ": "h", "ø": "o", "œ": "oe", "æ": "ae", "ß": "ss", "ſ": "s",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
}

_SEPARATORS = re.compile(r"[\s\-_.]+")

_DIGIT_FOLDS = str.maketrans(
    {"0": "o", "1": "l", "2": "z", "3": "e", "4": "a", "5": "s", "6": "b", "7": "t",
     "8": "b", "9": "g", "|": "l"}
)

_MULTI_FOLDS: tuple[tuple[str, str], ...] = (("rn", "m"), ("vv", "w"), ("cl", "d"))

# A claimable public namespace: 3-39 chars, lowercase letters, digits and
# single hyphens, starting and ending with a letter or digit (no dots).
NAMESPACE_CLAIM_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,37})[a-z0-9]$")


def _ignorable(char: str) -> bool:
    category = unicodedata.category(char)
    return category in ("Mn", "Me", "Cf") or char in ("ᅟ", "ᅠ", "ㅤ", "ﾠ", "឴", "឵")


def _map(text: str) -> str:
    table = confusables_table()
    return "".join(table.get(char, char) for char in text)


def skeleton(name: str) -> str:
    """Reduce ``name`` to its confusable skeleton (see module docstring)."""
    text = unicodedata.normalize("NFKC", name or "")
    text = unicodedata.normalize("NFD", text)
    text = "".join(char for char in text if not _ignorable(char))
    # ASCII case is not a confusable distinction for identifiers: lowercase it
    # first so UTS #39's "I -> l" does not split ANTHROPIC from anthropic.
    # (Non-ASCII capitals, e.g. Cherokee, go through the table as they are.)
    text = "".join(char.lower() if "A" <= char <= "Z" else char for char in text)
    text = _map(text)
    text = unicodedata.normalize("NFD", text)
    text = "".join(char for char in text if not _ignorable(char))
    text = text.casefold()
    text = _map(text)
    text = "".join(_SUPPLEMENT.get(char, char) for char in text)
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _SEPARATORS.sub("", text)
    text = text.translate(_DIGIT_FOLDS)
    for source, target in _MULTI_FOLDS:
        text = text.replace(source, target)
    return text


def loose_skeleton(name: str) -> str:
    """The skeleton with ``i`` folded into ``l`` as well (``1``/``l``/``i``
    all collapse). Too aggressive for every name (``mail`` / ``mall``), so it
    is applied only against strong protected names (reserved, verified)."""
    return skeleton(name).replace("i", "l")


_HAN_LIKE = {"CJK", "HIRAGANA", "KATAKANA", "HANGUL", "BOPOMOFO", "IDEOGRAPHIC"}


def _scripts(text: str) -> set[str]:
    """Approximate script set: the first word of each letter's Unicode name
    (``LATIN``, ``CYRILLIC``, ``CHEROKEE``, ``CJK`` ...). Digits, punctuation
    and spaces are script-neutral (UTS #24 Common)."""
    scripts: set[str] = set()
    for char in unicodedata.normalize("NFKC", text or ""):
        if not char.isalpha():
            continue
        try:
            scripts.add(unicodedata.name(char).split(" ", 1)[0])
        except ValueError:
            scripts.add("UNKNOWN")
    return scripts


def restriction_level(text: str) -> str:
    """UTS #39 §5.2 restriction level of ``text``:
    ``ascii`` < ``single_script`` < ``highly_restrictive`` <
    ``moderately_restrictive`` < ``minimally_restrictive``."""
    if all(ord(char) < 128 for char in text or ""):
        return "ascii"
    scripts = _scripts(text)
    if len(scripts) <= 1:
        return "single_script"
    if "LATIN" in scripts:
        others = scripts - {"LATIN"}
        if others <= _HAN_LIKE and not ({"HANGUL", "BOPOMOFO"} <= others):
            return "highly_restrictive"
        if len(others) == 1 and not (others & {"CYRILLIC", "GREEK", "CHEROKEE", "UNKNOWN"}):
            return "moderately_restrictive"
    return "minimally_restrictive"


def has_mixed_scripts(text: str) -> bool:
    """True when ``text`` is not acceptable as a display name: it is below
    the UTS #39 Moderately Restrictive level (e.g. Latin mixed with Cyrillic,
    Greek or Cherokee, or three unrelated scripts)."""
    return restriction_level(text) == "minimally_restrictive"


def damerau_levenshtein(a: str, b: str, limit: int = 3) -> int:
    """Optimal-string-alignment distance, capped at ``limit + 1``."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        row_min = current[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                value = min(value, previous2[j - 2] + 1)
            current[j] = value
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous2, previous = previous, current
    return min(previous[len(b)], limit + 1)


# Affixes squatters bolt onto a brand (§4.5 rule 4), as skeletons.
AFFIX_SUFFIXES: frozenset[str] = frozenset(
    skeleton(word)
    for word in (
        "official", "team", "inc", "ai", "dev", "devs", "hq", "labs", "lab", "plugin",
        "plugins", "skill", "skills", "mcp", "app", "apps", "io", "sdk", "cli", "tools",
        "org", "corp", "co", "security", "support", "agent", "agents", "verified",
    )
)
AFFIX_PREFIXES: frozenset[str] = frozenset(
    skeleton(word) for word in ("official", "real", "the", "get", "my", "team", "verified", "true")
)

# Brand-protected and structural names (§4.2). Value = the domain whose
# verification unlocks the claim, or None when only a platform admin may
# assign it. The table ``skill_reserved_names`` adds admin-managed entries.
BRAND_RESERVED: dict[str, str | None] = {
    # SessionFS itself and structural names used as default namespaces
    "sessionfs": None, "sfs": None, "personal": None, "org": None, "orgs": None,
    "user": None, "users": None, "public": None, "private": None, "system": None,
    "default": None, "local": None, "admin": None, "administrator": None, "root": None,
    "official": None, "verified": None, "marketplace": None, "security": None,
    "support": None, "help": None, "staff": None, "moderator": None, "api": None,
    # AI tool vendors and products
    "anthropic": "anthropic.com", "claude": "anthropic.com", "openai": "openai.com",
    "chatgpt": "openai.com", "codex": "openai.com", "google": "google.com",
    "gemini": "google.com", "deepmind": "google.com", "github": "github.com",
    "copilot": "github.com", "microsoft": "microsoft.com", "cursor": "cursor.com",
    "anysphere": "anysphere.inc", "windsurf": "windsurf.com", "codeium": "codeium.com",
    "sourcegraph": "sourcegraph.com", "ampcode": "ampcode.com", "cline": "cline.bot",
    "roocode": "roocode.com", "kilocode": "kilocode.ai", "mistral": "mistral.ai",
    "deepseek": "deepseek.com", "huggingface": "huggingface.co", "meta": "meta.com",
    "llama": "meta.com", "xai": "x.ai", "grok": "x.ai", "perplexity": "perplexity.ai",
    "jetbrains": "jetbrains.com", "vscode": "microsoft.com", "antigravity": "google.com",
    # Clouds and platforms
    "amazon": "amazon.com", "aws": "amazon.com", "azure": "microsoft.com",
    "gcp": "google.com", "googlecloud": "google.com", "cloudflare": "cloudflare.com",
    "vercel": "vercel.com", "netlify": "netlify.com", "heroku": "heroku.com",
    "digitalocean": "digitalocean.com", "oracle": "oracle.com", "ibm": "ibm.com",
    "nvidia": "nvidia.com", "apple": "apple.com", "gitlab": "gitlab.com",
    "bitbucket": "atlassian.com", "atlassian": "atlassian.com", "docker": "docker.com",
    "kubernetes": "kubernetes.io", "hashicorp": "hashicorp.com", "terraform": "hashicorp.com",
    "stripe": "stripe.com", "npm": "npmjs.com", "pypi": "pypi.org", "python": "python.org",
    "supabase": "supabase.com", "mongodb": "mongodb.com", "slack": "slack.com",
    "salesforce": "salesforce.com", "shopify": "shopify.com", "linear": "linear.app",
    "notion": "notion.so", "figma": "figma.com", "sentry": "sentry.io",
}

RESERVED_SKELETONS: dict[str, tuple[str, str | None]] = {
    skeleton(name): (name, domain) for name, domain in BRAND_RESERVED.items()
}


@dataclass(frozen=True)
class ProtectedName:
    """A name new claims must not imitate.

    ``owner_key`` identifies who may use it ("org:<id>", "user:<id>",
    "system", or "" for unowned reserved names); ``strong`` names (reserved,
    verified publishers, the platform) additionally get the distance-1 and
    affix checks, weak names (any other claimed namespace) only the exact
    skeleton check."""

    name: str
    skeleton: str
    owner_key: str
    kind: str  # 'reserved' | 'publisher' | 'tombstone'
    strong: bool


@dataclass(frozen=True)
class ConfusableVerdict:
    blocked: bool
    rule: str  # 'exact' | 'distance' | 'affix' | 'review'
    conflict: str
    conflict_kind: str


def check_confusable(
    candidate: str, protected: Iterable[ProtectedName], *, owner_key: str
) -> ConfusableVerdict | None:
    """Return the first blocking verdict for ``candidate``, else the first
    review flag (distance 2), else None. Names owned by ``owner_key`` are
    exempt (an owner may hold ``acme`` and ``acme-labs``)."""
    skel = skeleton(candidate)
    loose = skel.replace("i", "l")
    review: ConfusableVerdict | None = None
    for item in protected:
        if item.owner_key and item.owner_key == owner_key:
            continue
        if not item.skeleton:
            continue
        if skel == item.skeleton:
            return ConfusableVerdict(True, "exact", item.name, item.kind)
        if not item.strong:
            continue
        if loose == item.skeleton.replace("i", "l"):
            return ConfusableVerdict(True, "exact", item.name, item.kind)
        if _is_affix_squat(skel, item.skeleton):
            return ConfusableVerdict(True, "affix", item.name, item.kind)
        if len(skel) >= 5 and len(item.skeleton) >= 5:
            distance = damerau_levenshtein(skel, item.skeleton, limit=2)
            if distance <= 1:
                return ConfusableVerdict(True, "distance", item.name, item.kind)
            # Long protected names: two edits still read as the brand
            # ("anthrpoic-x"), so block rather than flag.
            if distance == 2 and len(item.skeleton) >= 8:
                return ConfusableVerdict(True, "distance", item.name, item.kind)
            if distance == 2 and review is None:
                review = ConfusableVerdict(False, "review", item.name, item.kind)
    return review


def _is_affix_squat(candidate: str, protected: str) -> bool:
    if len(protected) < 3 or candidate == protected:
        return False
    if candidate.startswith(protected) and candidate[len(protected):] in AFFIX_SUFFIXES:
        return True
    if candidate.endswith(protected) and candidate[: -len(protected)] in AFFIX_PREFIXES:
        return True
    return False


# Unicode general categories never allowed in a publisher identifier or display
# name: format (incl. bidi controls such as U+202E, zero-width joiners),
# control, surrogate, private use, unassigned. A bidi override can make
# "\u202eIAnepO" RENDER as "OpenAI" while its skeleton is something else.
FORBIDDEN_CATEGORIES = frozenset({"Cf", "Cc", "Cs", "Co", "Cn"})


def forbidden_code_points(text: str) -> list[str]:
    """``U+XXXX`` for every code point in a forbidden category (bidi and
    format controls included); empty when the text is clean."""
    return [
        f"U+{ord(char):04X}"
        for char in text or ""
        if unicodedata.category(char) in FORBIDDEN_CATEGORIES
    ]
