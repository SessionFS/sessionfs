"""Evaluation suite and case definitions v1 (GitHub #121): the pydantic
models and validation shared by the server and the ``sfs eval`` CLI.

Server-independent on purpose (no SQLAlchemy, no server DB models) so the
CLI can refuse a bad suite or case file before sending any request. The
server re-validates every write with the same models.

Record-only: a suite describes a grader that runs in the customer's own CI
or orchestrator. SessionFS stores the definition and judges the results the
grader posts; it never runs the grader or calls a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DEFINITION_BYTES = 32 * 1024
MAX_RUBRIC_BYTES = 8 * 1024
MAX_DESCRIPTION = 4096
MAX_CASE_TEXT = 32 * 1024
MAX_RUBRIC_NOTES = 4096
MAX_CASE_TAGS = 16
MAX_TAG_RULES = 32
MAX_REGRESSION_TAGS = 16
MAX_LIVE_CASES = 5000
MAX_BATCH = 500

GRADER_KINDS = ("deterministic", "llm_judge", "human", "hybrid")
CASE_STORAGE = ("inline", "reference")
RESULT_VERDICTS = ("pass", "fail", "error", "skip")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_PERSONA_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^evr_[0-9a-f]{8,32}$")


class EvalDefinitionError(ValueError):
    pass


def _not_bool_number(v: Any) -> Any:
    if isinstance(v, bool):
        raise ValueError("must be a number, not a boolean")
    return v


def _check_key(v: str) -> str:
    v = (v or "").strip() if isinstance(v, str) else v
    if not isinstance(v, str) or not _KEY_RE.fullmatch(v):
        raise ValueError("case_key must be lowercase letters, digits, '_', '.' or '-' (1-64)")
    return v


def _check_sha(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip().lower()
    if v.startswith("sha256:"):
        v = v[len("sha256:"):]
    if not _SHA256_RE.fullmatch(v):
        raise ValueError("must be 64 lowercase hex characters")
    return v


def _check_tag(tag: str) -> str:
    if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
        raise ValueError(
            f"tag {tag!r} must be lowercase letters, digits, '_', '.', ':' or '-' (max 64)"
        )
    return tag


class GraderSpec(BaseModel):
    """Who grades and how. ``persona`` is the trusted-reviewer persona the
    grading credential must be registered as for a run to count."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["deterministic", "llm_judge", "human", "hybrid"]
    persona: str
    rubric: str = ""
    script_ref: str | None = Field(default=None, max_length=512)
    model_hint: str | None = Field(default=None, max_length=120)

    @field_validator("persona")
    @classmethod
    def _persona(cls, v: str) -> str:
        v = (v or "").strip()
        if not _PERSONA_RE.fullmatch(v):
            raise ValueError("grader.persona must be 1-50 letters, digits, '_' or '-'")
        return v

    @field_validator("rubric")
    @classmethod
    def _rubric(cls, v: str) -> str:
        if len((v or "").encode("utf-8")) > MAX_RUBRIC_BYTES:
            raise ValueError(f"grader.rubric must be at most {MAX_RUBRIC_BYTES} bytes")
        return v or ""


class Acceptance(BaseModel):
    """When a run passes. Only these keys are accepted. With no threshold
    declared, every graded case must pass."""

    model_config = ConfigDict(extra="forbid")

    require_all_cases: bool = True
    min_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    min_mean_score: float | None = Field(default=None, ge=0.0, le=1.0)
    min_pass_rate_by_tag: dict[str, float] = Field(default_factory=dict)
    max_failures_by_tag: dict[str, int] = Field(default_factory=dict)
    no_regression_vs: str = "none"
    regression_tags: list[str] = Field(default_factory=lambda: ["critical"])

    _no_bool = field_validator("min_pass_rate", "min_mean_score", mode="before")(
        classmethod(lambda cls, v: _not_bool_number(v))
    )

    @field_validator("require_all_cases", mode="before")
    @classmethod
    def _strict_bool(cls, v: Any) -> Any:
        if not isinstance(v, bool):
            raise ValueError("require_all_cases must be true or false")
        return v

    @field_validator("min_pass_rate_by_tag", mode="before")
    @classmethod
    def _rate_map(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError("min_pass_rate_by_tag must be a mapping of tag to rate")
        if len(v) > MAX_TAG_RULES:
            raise ValueError(f"min_pass_rate_by_tag allows at most {MAX_TAG_RULES} tags")
        out: dict[str, float] = {}
        for tag, rate in v.items():
            _check_tag(tag)
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise ValueError(f"min_pass_rate_by_tag[{tag!r}] must be a number")
            if not 0.0 <= float(rate) <= 1.0:
                raise ValueError(f"min_pass_rate_by_tag[{tag!r}] must be between 0 and 1")
            out[tag] = float(rate)
        return out

    @field_validator("max_failures_by_tag", mode="before")
    @classmethod
    def _fail_map(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError("max_failures_by_tag must be a mapping of tag to count")
        if len(v) > MAX_TAG_RULES:
            raise ValueError(f"max_failures_by_tag allows at most {MAX_TAG_RULES} tags")
        out: dict[str, int] = {}
        for tag, count in v.items():
            _check_tag(tag)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"max_failures_by_tag[{tag!r}] must be an integer >= 0")
            out[tag] = count
        return out

    @field_validator("no_regression_vs")
    @classmethod
    def _baseline(cls, v: str) -> str:
        v = (v or "").strip()
        if v in ("none", "production") or _RUN_ID_RE.fullmatch(v):
            return v
        raise ValueError("no_regression_vs must be 'none', 'production' or an eval run id (evr_...)")

    @field_validator("regression_tags")
    @classmethod
    def _reg_tags(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_REGRESSION_TAGS:
            raise ValueError(f"regression_tags allows at most {MAX_REGRESSION_TAGS} tags")
        return sorted({_check_tag(t) for t in v})


class SuiteDefinition(BaseModel):
    """One suite version's definition (everything except its cases)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    grader: GraderSpec
    acceptance: Acceptance = Field(default_factory=Acceptance)
    case_storage: Literal["inline", "reference"] = "inline"
    case_bank_artifact_id: str | None = Field(default=None, max_length=64)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = (v or "").strip()
        if not _SLUG_RE.fullmatch(v):
            raise ValueError("slug must be lowercase letters, digits or '-' (1-64, starting with a letter or digit)")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join((v or "").split())
        if not v:
            raise ValueError("name is required")
        return v

    @model_validator(mode="after")
    def _size(self) -> "SuiteDefinition":
        if len(dump_definition(self).encode("utf-8")) > MAX_DEFINITION_BYTES:
            raise ValueError(f"the suite definition must be at most {MAX_DEFINITION_BYTES} bytes")
        if self.case_bank_artifact_id and self.case_storage != "reference":
            raise ValueError("case_bank_artifact_id is only used with case_storage: reference")
        return self


class CaseSpec(BaseModel):
    """One case as submitted. In ``reference`` storage the text stays with
    the customer: send ``content_sha256`` and no ``input`` / ``expected``."""

    model_config = ConfigDict(extra="forbid")

    case_key: str
    name: str = Field(default="", max_length=200)
    input: str | None = Field(default=None)
    expected: str | None = Field(default=None)
    rubric_notes: str = Field(default="", max_length=MAX_RUBRIC_NOTES)
    tags: list[str] = Field(default_factory=list)
    content_sha256: str | None = Field(default=None)

    @field_validator("case_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _check_key(v)

    @field_validator("input", "expected")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        if v is not None and len(v.encode("utf-8")) > MAX_CASE_TEXT:
            raise ValueError(f"case text must be at most {MAX_CASE_TEXT} bytes")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_CASE_TAGS:
            raise ValueError(f"a case has at most {MAX_CASE_TAGS} tags")
        return sorted({_check_tag(t) for t in v})

    @field_validator("content_sha256")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        return _check_sha(v)


class ResultSpec(BaseModel):
    """One per-case result posted by a grader."""

    model_config = ConfigDict(extra="forbid")

    case_key: str
    verdict: Literal["pass", "fail", "error", "skip"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    notes: str | None = Field(default=None, max_length=4096)
    output: str | None = Field(default=None)
    output_sha256: str | None = Field(default=None)
    content_sha256: str | None = Field(default=None)
    grader_kind: Literal["deterministic", "llm_judge", "human", "hybrid"] | None = Field(default=None)

    _no_bool = field_validator("score", mode="before")(
        classmethod(lambda cls, v: _not_bool_number(v))
    )

    @field_validator("case_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _check_key(v)

    @field_validator("output")
    @classmethod
    def _output(cls, v: str | None) -> str | None:
        if v is not None and len(v.encode("utf-8")) > 16 * 1024:
            raise ValueError("output must be at most 16384 bytes")
        return v

    @field_validator("output_sha256", "content_sha256")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        return _check_sha(v)


# ── Helpers ──


def parse_definition(raw: dict[str, Any] | str) -> SuiteDefinition:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise EvalDefinitionError(f"definition is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise EvalDefinitionError("a suite definition must be a mapping")
    try:
        return SuiteDefinition.model_validate(raw)
    except ValueError as exc:
        raise EvalDefinitionError(_first_error(exc)) from exc


def parse_case(raw: Any) -> CaseSpec:
    if not isinstance(raw, dict):
        raise EvalDefinitionError("each case must be a mapping")
    try:
        return CaseSpec.model_validate(raw)
    except ValueError as exc:
        raise EvalDefinitionError(_first_error(exc)) from exc


def parse_result(raw: Any) -> ResultSpec:
    if not isinstance(raw, dict):
        raise EvalDefinitionError("each result must be a mapping")
    try:
        return ResultSpec.model_validate(raw)
    except ValueError as exc:
        raise EvalDefinitionError(_first_error(exc)) from exc


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:
            items = []
        if items:
            first = items[0]
            loc = ".".join(str(p) for p in first.get("loc", ()) if p != "__root__")
            msg = str(first.get("msg", "invalid value")).removeprefix("Value error, ")
            return f"{loc}: {msg}" if loc else msg
    return str(exc)


def dump_definition(definition: SuiteDefinition) -> str:
    return json.dumps(definition.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def definition_sha256(definition: SuiteDefinition) -> str:
    return hashlib.sha256(dump_definition(definition).encode("utf-8")).hexdigest()


def case_content_sha256(input_text: str | None, expected: str | None) -> str:
    """The hash that proves which case was graded.

    sha256 over the canonical JSON ``{"expected": ..., "input": ...}`` (sorted
    keys, no spaces, UTF-8). A grader holding the case text locally computes
    the same value, so reference-mode suites can prove the case without the
    text ever reaching SessionFS.
    """
    payload = json.dumps(
        {"expected": expected, "input": input_text},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "Acceptance",
    "CaseSpec",
    "EvalDefinitionError",
    "GraderSpec",
    "ResultSpec",
    "SuiteDefinition",
    "case_content_sha256",
    "definition_sha256",
    "dump_definition",
    "parse_case",
    "parse_definition",
    "parse_result",
    "text_sha256",
]
