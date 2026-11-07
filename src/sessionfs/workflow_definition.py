"""Workflow template definition v1 (GitHub #120): the pydantic model and
validation shared by the server and the ``sfs workflow`` CLI.

Server-independent on purpose (no SQLAlchemy, no server DB models) so the
CLI can refuse a bad template before sending any request. The server
re-validates every write with the same model.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from sessionfs.server.sanitize import sanitize_archive_string

MAX_STEPS = 30
MAX_FAN_OUT = 10
MAX_BRIEF_BYTES = 8 * 1024
MAX_DEFINITION_BYTES = 32 * 1024

STEP_ROLES = ("implementer", "reviewer", "verifier", "documenter", "dispatcher")
# Gates that check a review verdict: only steps that take review rounds can
# carry them (an implementer step's own tickets are reviewed by the reviewer
# step that depends on it, so a review gate there could never be met).
REVIEW_GATES = frozenset({"trusted_clean"})
# Which report kinds each role may make.
WORKER_ROLES = frozenset({"implementer", "documenter", "dispatcher", "verifier"})
ROUND_ROLES = frozenset({"reviewer", "verifier"})

_STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_AGENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
_POLICY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class WorkflowDefinitionError(ValueError):
    pass


# ── Text hygiene ──


def clean_multiline(value: str) -> str:
    """Strip NULs / lone surrogates and redact secrets, keeping line breaks.

    Brief templates are markdown, so whitespace is preserved (unlike the
    loop checklist sanitizer, which collapses it). Fails closed: when the
    secret scan itself fails, raise ``WorkflowDefinitionError`` (a 422 on
    the server) instead of storing unscanned text.
    """
    value, _ = sanitize_archive_string(value or "")
    try:
        from sessionfs.server.dlp import redact_text, scan_dlp

        findings = scan_dlp(value, categories=["secrets"])
        if findings:
            value = redact_text(value, findings)
    except Exception as exc:
        raise WorkflowDefinitionError(
            "text could not be scanned for secrets, so it was not accepted"
        ) from exc
    return value


# ── Definition v1 ──


class StepDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=1, max_length=200)
    role: Literal["implementer", "reviewer", "verifier", "documenter", "dispatcher"]
    agent: str
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    fan_out: StrictInt = Field(1, ge=1, le=MAX_FAN_OUT)
    gate: str = "none"
    brief_template: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not isinstance(v, str) or not _STEP_ID_RE.match(v):
            raise ValueError(
                "step id must be a lowercase slug (a-z, 0-9, '-'), 1-64 characters"
            )
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = " ".join(clean_multiline(v).split())
        if not v:
            raise ValueError("step title is required")
        return v

    @field_validator("agent")
    @classmethod
    def _agent(cls, v: str) -> str:
        v = (v or "").strip()
        if not _AGENT_RE.match(v):
            raise ValueError(
                "agent must be a persona name (letters, digits, '_' or '-', 1-50 characters)"
            )
        return v

    @field_validator("gate")
    @classmethod
    def _gate(cls, v: str) -> str:
        v = (v or "").strip()
        if v in ("none", "trusted_clean"):
            return v
        if v.startswith("approval_policy:") and _POLICY_ID_RE.match(
            v[len("approval_policy:"):]
        ):
            return v
        raise ValueError(
            "gate must be 'none', 'trusted_clean' or 'approval_policy:<id>'"
        )

    @field_validator("brief_template")
    @classmethod
    def _brief(cls, v: str) -> str:
        if len((v or "").encode("utf-8")) > MAX_BRIEF_BYTES:
            raise ValueError(f"brief_template must be at most {MAX_BRIEF_BYTES} bytes")
        return clean_multiline(v)


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    steps: list[StepDefinition] = Field(min_length=1, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def _graph(self) -> WorkflowDefinition:
        ids = [s.id for s in self.steps]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate step ids: {dupes}")
        known = set(ids)
        for step in self.steps:
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError(f"step {step.id!r} lists a dependency twice")
            if step.id in step.depends_on:
                raise ValueError(f"step {step.id!r} depends on itself")
            unknown = [d for d in step.depends_on if d not in known]
            if unknown:
                raise ValueError(f"step {step.id!r} depends on unknown steps: {unknown}")
            if step.gate in REVIEW_GATES and step.role not in ROUND_ROLES:
                raise ValueError(
                    f"step {step.id!r}: gate {step.gate!r} checks a review, so it "
                    "belongs on a reviewer or verifier step (this step has role "
                    f"{step.role!r}); add a reviewer step that depends on this one "
                    "and put the gate there"
                )
            if step.role == "reviewer" and step.depends_on and step.fan_out != 1:
                raise ValueError(
                    f"step {step.id!r}: a reviewer step reviews its dependencies' "
                    "tickets and takes no fan_out"
                )
        cycle = _find_cycle({s.id: list(s.depends_on) for s in self.steps})
        if cycle:
            raise ValueError("steps form a cycle: " + " -> ".join(cycle))
        return self

    def step(self, step_id: str) -> StepDefinition | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Iterative DFS; returns one cycle as a path (first node repeated)."""
    white, grey, black = 0, 1, 2
    color = {n: white for n in graph}
    for root in graph:
        if color[root] != white:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        while stack:
            node, i = stack.pop()
            if i == 0:
                color[node] = grey
                path.append(node)
            deps = graph[node]
            if i < len(deps):
                stack.append((node, i + 1))
                nxt = deps[i]
                if color[nxt] == grey:
                    return path[path.index(nxt):] + [nxt]
                if color[nxt] == white:
                    stack.append((nxt, 0))
            else:
                color[node] = black
                path.pop()
    return None


def parse_definition(raw: Any) -> WorkflowDefinition:
    """Validate a definition (dict or stored JSON text); raise
    ``WorkflowDefinitionError`` with a readable message."""
    from pydantic import ValidationError

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise WorkflowDefinitionError("definition is not valid JSON") from exc
    try:
        definition = WorkflowDefinition.model_validate(raw)
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = str(err.get("msg", "invalid"))
            parts.append(f"{loc}: {msg}" if loc else msg)
        raise WorkflowDefinitionError("; ".join(parts)) from exc
    if len(dump_definition(definition).encode("utf-8")) > MAX_DEFINITION_BYTES:
        raise WorkflowDefinitionError(
            f"definition must be at most {MAX_DEFINITION_BYTES} bytes"
        )
    return definition


def dump_definition(definition: WorkflowDefinition) -> str:
    return json.dumps(definition.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
