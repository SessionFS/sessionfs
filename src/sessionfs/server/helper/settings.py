"""Configurable session-helper settings and custom instructions.

Configuration lives in ``helper_configs`` (migration 095): one row per org and
one per user, apart from the key rows so deleting or rotating a key never
touches it. ``settings`` is JSON-as-Text holding only the keys the owner set
explicitly; ``effective_settings`` always returns a fully-resolved
``HelperModelSettings`` (defaults for anything unset).

The org row additionally carries two ceilings (``ceiling_max_output_tokens`` /
``ceiling_max_tool_steps``) that cap the org's own settings on the org-key
path. A caller's personal key is governed solely by their own settings. The
validation bounds below are the absolute hard caps and apply on every path.

Instructions: which blocks apply to a turn is decided by
``applicable_instructions`` (see its docstring). Instructions are delimited in
the system prompt by ``<org-instructions>`` / ``<personal-instructions>`` tags;
saving text that contains those tags is refused, and the prompt builder also
neutralises any tag-like sequence (defence in depth).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import HelperConfig, OrgMember, User
from sessionfs.server.helper.keys import ResolvedLLM

# Absolute hard caps (256..32000 output tokens, 1..20 tool rounds). They apply
# to every path — personal and org.
_HARD_MAX_OUTPUT_TOKENS = 32_000
_HARD_MAX_TOOL_STEPS = 20

MAX_ORG_INSTRUCTIONS_CHARS = 4000
MAX_PERSONAL_INSTRUCTIONS_CHARS = 2000

# Strict ints: a JSON ``true`` is not a token count.
OutputTokens = Annotated[int, Field(strict=True, ge=256, le=_HARD_MAX_OUTPUT_TOKENS)]
ToolSteps = Annotated[int, Field(strict=True, ge=1, le=_HARD_MAX_TOOL_STEPS)]
Temperature = Annotated[float, Field(ge=0, le=2)]
TopP = Annotated[float, Field(ge=0, le=1)]
ReasoningEffort = Literal["off", "low", "medium", "high"]
AnswerStyle = Literal["concise", "detailed"]

_CEILING_FIELDS = ("ceiling_max_output_tokens", "ceiling_max_tool_steps")


class HelperModelSettings(BaseModel):
    """Sampling / loop settings for one helper turn, fully resolved.

    ``temperature`` defaults to ``0`` (deterministic); ``None`` means "let the
    provider pick". ``top_p`` / ``reasoning_effort`` default to ``None`` (not
    sent). ``max_tool_steps`` counts tool ROUNDS: the model can always answer
    after its last allowed round.
    """

    model_config = ConfigDict(extra="ignore")

    max_output_tokens: OutputTokens = 4096
    temperature: Temperature | None = 0
    top_p: TopP | None = None
    max_tool_steps: ToolSteps = 8
    reasoning_effort: ReasoningEffort | None = None
    answer_style: AnswerStyle = "concise"


class OrgHelperSettings(HelperModelSettings):
    """Org helper settings: the model settings plus the org-only ceilings."""

    ceiling_max_output_tokens: OutputTokens | None = None
    ceiling_max_tool_steps: ToolSteps | None = None


class HelperSettingsPatch(BaseModel):
    """A partial settings update: omitted keys keep their stored value.

    An explicit ``null`` resets a key: ``max_output_tokens``,
    ``max_tool_steps`` and ``answer_style`` go back to their defaults; for
    ``temperature``, ``top_p`` and ``reasoning_effort`` ``null`` is itself the
    value (provider default / not sent).
    """

    model_config = ConfigDict(extra="forbid")

    max_output_tokens: OutputTokens | None = None
    temperature: Temperature | None = None
    top_p: TopP | None = None
    max_tool_steps: ToolSteps | None = None
    reasoning_effort: ReasoningEffort | None = None
    answer_style: AnswerStyle | None = None


class OrgHelperSettingsPatch(HelperSettingsPatch):
    """Org partial update; ``null`` on a ceiling removes the ceiling."""

    ceiling_max_output_tokens: OutputTokens | None = None
    ceiling_max_tool_steps: ToolSteps | None = None


# Keys whose ``None`` is a meaningful stored value (rather than "reset").
_NULLABLE_VALUE_KEYS = frozenset(
    {"temperature", "top_p", "reasoning_effort", *_CEILING_FIELDS}
)


def loads_settings(raw: str | None) -> dict[str, Any]:
    """The stored explicit-settings dict (malformed JSON reads as empty)."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def merge_settings(stored: dict[str, Any], patch: HelperSettingsPatch) -> dict[str, Any]:
    """Merge a partial update into the stored explicit settings, per key."""
    merged = dict(stored)
    for key in patch.model_fields_set:
        value = getattr(patch, key)
        if value is None and key not in _NULLABLE_VALUE_KEYS:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


async def load_org_config(db: AsyncSession, org_id: str | None) -> HelperConfig | None:
    if not org_id:
        return None
    return (
        await db.execute(select(HelperConfig).where(HelperConfig.org_id == org_id))
    ).scalar_one_or_none()


async def load_user_config(db: AsyncSession, user_id: str) -> HelperConfig | None:
    return (
        await db.execute(select(HelperConfig).where(HelperConfig.user_id == user_id))
    ).scalar_one_or_none()


def _settings_of(config: HelperConfig | None) -> dict[str, Any]:
    return loads_settings(config.settings if config is not None else None)


def org_settings_view(config: HelperConfig | None) -> OrgHelperSettings:
    """The org's stored settings with defaults filled in."""
    return OrgHelperSettings.model_validate(_settings_of(config))


def user_settings_view(config: HelperConfig | None) -> HelperModelSettings:
    """The caller's stored settings with defaults filled in."""
    return HelperModelSettings.model_validate(_settings_of(config))


def org_effective(config: HelperConfig | None) -> HelperModelSettings:
    """Org settings capped by the org's own ceilings."""
    org = org_settings_view(config)
    if org.ceiling_max_output_tokens is not None:
        org.max_output_tokens = min(org.max_output_tokens, org.ceiling_max_output_tokens)
    if org.ceiling_max_tool_steps is not None:
        org.max_tool_steps = min(org.max_tool_steps, org.ceiling_max_tool_steps)
    return HelperModelSettings(**org.model_dump(exclude=set(_CEILING_FIELDS)))


def effective_settings(
    resolved_key: ResolvedLLM,
    org_config: HelperConfig | None,
    user_config: HelperConfig | None,
) -> HelperModelSettings:
    """Resolve the effective helper settings for a turn.

    A user-key turn uses the caller's personal settings. An org-key turn uses
    the key org's settings, capped by that org's ceilings. The validation
    bounds are the absolute hard caps on every path.
    """
    if resolved_key.key_source == "user":
        return user_settings_view(user_config)
    return org_effective(org_config)


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------

# Anything that looks like one of the prompt's delimiter tags, opening or
# closing, with any spacing / case / attributes.
_DELIMITER_RE = re.compile(
    r"<\s*/?\s*(?:org|personal)[\s_-]*instructions\b[^>]*>", re.IGNORECASE
)


def contains_delimiter(text: str) -> bool:
    return bool(_DELIMITER_RE.search(text))


def neutralize_delimiters(text: str) -> str:
    """Defang delimiter-like tags so instruction text can't close its block."""
    return _DELIMITER_RE.sub("[tag removed]", text)


@dataclass(frozen=True)
class ApplicableInstructions:
    org: tuple[str, ...] = ()
    personal: str | None = None


async def _is_member(db: AsyncSession, user_id: str, org_id: str) -> bool:
    row = (
        await db.execute(
            select(OrgMember.id).where(
                OrgMember.org_id == org_id, OrgMember.user_id == user_id
            )
        )
    ).first()
    return row is not None


async def applicable_instructions(
    db: AsyncSession,
    user: User,
    resolved: ResolvedLLM,
    scope_org_id: str | None,
    user_config: HelperConfig | None = None,
) -> ApplicableInstructions:
    """Which custom instructions apply to a turn.

    - Org instructions apply from the org whose key the turn uses, and from
      the org that owns the conversation's scope (its project, or the
      sessions' projects), whichever key is used. The same org is applied
      once.
    - Instructions from an org the caller is not an active member of are
      never applied.
    - The caller's personal instructions always apply, after the org block.
    """
    candidates: list[str] = []
    if resolved.key_source == "org" and resolved.key_org_id:
        candidates.append(resolved.key_org_id)
    if scope_org_id and scope_org_id not in candidates:
        candidates.append(scope_org_id)

    org_texts: list[str] = []
    for org_id in candidates:
        if not await _is_member(db, user.id, org_id):
            continue
        config = await load_org_config(db, org_id)
        if config is not None and config.instructions:
            org_texts.append(config.instructions)

    if user_config is None:
        user_config = await load_user_config(db, user.id)
    personal = user_config.instructions if user_config is not None else None
    return ApplicableInstructions(org=tuple(org_texts), personal=personal or None)
