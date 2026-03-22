#!/usr/bin/env python3
"""SFS Session Validator

Validates a .sfs session directory against the canonical JSON schemas.
Checks manifest.json, messages.jsonl, workspace.json, and tools.json.

Usage:
    python src/spec/validate.py path/to/session/
    python src/spec/validate.py src/spec/examples/minimal/
    python src/spec/validate.py src/spec/examples/complete/ src/spec/examples/subagent/

    # Validate all examples
    python src/spec/validate.py --all-examples

Requirements:
    pip install jsonschema
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import jsonschema
    from jsonschema import Draft202012Validator, ValidationError
except ImportError:
    print("Error: jsonschema package required. Install with: pip install jsonschema", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

SCHEMA_DIR = Path(__file__).parent / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    """Load a JSON schema file from the schemas directory."""
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"Schema not found: {path}")
    return json.loads(path.read_text())


def make_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Create a Draft 2020-12 validator for the given schema."""
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationResult:
    """Collects validation errors for a session."""

    def __init__(self, session_path: str):
        self.session_path = session_path
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.files_checked: list[str] = []

    def add_error(self, file: str, message: str, line: int | None = None) -> None:
        loc = f"{file}:{line}" if line else file
        self.errors.append(f"  ERROR {loc}: {message}")

    def add_warning(self, file: str, message: str) -> None:
        self.warnings.append(f"  WARN  {file}: {message}")

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def print_report(self) -> None:
        status = "VALID" if self.valid else "INVALID"
        print(f"\n{'='*60}")
        print(f"Session: {self.session_path}")
        print(f"Status:  {status}")
        print(f"Files:   {', '.join(self.files_checked) or 'none'}")

        if self.errors:
            print(f"Errors:  {len(self.errors)}")
            for err in self.errors:
                print(err)

        if self.warnings:
            print(f"Warnings: {len(self.warnings)}")
            for warn in self.warnings:
                print(warn)

        print(f"{'='*60}")


def _format_validation_error(err: ValidationError) -> str:
    """Format a jsonschema ValidationError into a readable string."""
    path = ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "(root)"
    # Truncate long messages
    msg = err.message
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return f"at {path}: {msg}"


def validate_json_file(
    result: ValidationResult,
    file_path: Path,
    schema: dict[str, Any],
    required: bool = True,
) -> dict[str, Any] | None:
    """Validate a JSON file against a schema. Returns parsed data or None."""
    if not file_path.exists():
        if required:
            result.add_error(file_path.name, "Required file not found")
        return None

    result.files_checked.append(file_path.name)

    try:
        data = json.loads(file_path.read_text())
    except json.JSONDecodeError as e:
        result.add_error(file_path.name, f"Invalid JSON: {e}")
        return None

    validator = make_validator(schema)
    for error in validator.iter_errors(data):
        result.add_error(file_path.name, _format_validation_error(error))

    return data


def validate_jsonl_file(
    result: ValidationResult,
    file_path: Path,
    schema: dict[str, Any],
    required: bool = True,
) -> list[dict[str, Any]]:
    """Validate each line of a JSONL file against a schema. Returns parsed lines."""
    if not file_path.exists():
        if required:
            result.add_error(file_path.name, "Required file not found")
        return []

    result.files_checked.append(file_path.name)
    validator = make_validator(schema)
    entries: list[dict[str, Any]] = []

    try:
        text = file_path.read_text()
    except OSError as e:
        result.add_error(file_path.name, f"Cannot read file: {e}")
        return []

    for line_num, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            result.add_error(file_path.name, f"Invalid JSON: {e}", line=line_num)
            continue

        for error in validator.iter_errors(data):
            result.add_error(
                file_path.name,
                _format_validation_error(error),
                line=line_num,
            )

        entries.append(data)

    return entries


def validate_referential_integrity(
    result: ValidationResult,
    messages: list[dict[str, Any]],
) -> None:
    """Check referential integrity of message parent_msg_id references."""
    msg_ids = {m.get("msg_id") for m in messages}

    for i, msg in enumerate(messages, start=1):
        parent = msg.get("parent_msg_id")
        if parent is not None and parent not in msg_ids:
            result.add_error(
                "messages.jsonl",
                f"parent_msg_id '{parent}' references non-existent message",
                line=i,
            )

    # Check for duplicate msg_ids
    seen: set[str] = set()
    for i, msg in enumerate(messages, start=1):
        msg_id = msg.get("msg_id", "")
        if msg_id in seen:
            result.add_error(
                "messages.jsonl",
                f"Duplicate msg_id: '{msg_id}'",
                line=i,
            )
        seen.add(msg_id)


def validate_manifest_stats(
    result: ValidationResult,
    manifest: dict[str, Any],
    messages: list[dict[str, Any]],
) -> None:
    """Warn if manifest stats don't match actual message data."""
    stats = manifest.get("stats")
    if not stats:
        return

    declared_count = stats.get("message_count", 0)
    actual_count = len(messages)
    if declared_count != actual_count:
        result.add_warning(
            "manifest.json",
            f"stats.message_count={declared_count} but messages.jsonl has {actual_count} entries",
        )

    # Check tool_use_count
    declared_tools = stats.get("tool_use_count", 0)
    actual_tools = sum(
        1
        for msg in messages
        for block in msg.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    )
    if declared_tools != actual_tools:
        result.add_warning(
            "manifest.json",
            f"stats.tool_use_count={declared_tools} but messages have {actual_tools} tool_use blocks",
        )


def validate_session(session_dir: Path) -> ValidationResult:
    """Validate a complete .sfs session directory."""
    result = ValidationResult(str(session_dir))

    if not session_dir.is_dir():
        result.add_error("(directory)", f"Not a directory: {session_dir}")
        return result

    # Load schemas
    manifest_schema = load_schema("manifest")
    message_schema = load_schema("message")
    workspace_schema = load_schema("workspace")
    tools_schema = load_schema("tools")

    # Validate manifest.json (required)
    manifest = validate_json_file(
        result,
        session_dir / "manifest.json",
        manifest_schema,
        required=True,
    )

    # Validate messages.jsonl (required)
    messages = validate_jsonl_file(
        result,
        session_dir / "messages.jsonl",
        message_schema,
        required=True,
    )

    if not messages:
        result.add_warning("messages.jsonl", "No messages found")

    # Validate workspace.json (optional)
    validate_json_file(
        result,
        session_dir / "workspace.json",
        workspace_schema,
        required=False,
    )

    # Validate tools.json (optional)
    validate_json_file(
        result,
        session_dir / "tools.json",
        tools_schema,
        required=False,
    )

    # Referential integrity checks
    if messages:
        validate_referential_integrity(result, messages)

    # Manifest-message consistency checks
    if manifest and messages:
        validate_manifest_stats(result, manifest, messages)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate .sfs session directories against JSON schemas.",
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        help="Path(s) to .sfs session directories to validate.",
    )
    parser.add_argument(
        "--all-examples",
        action="store_true",
        help="Validate all example sessions in src/spec/examples/.",
    )

    args = parser.parse_args()

    session_dirs: list[Path] = []

    if args.all_examples:
        examples_dir = Path(__file__).parent / "examples"
        if examples_dir.is_dir():
            session_dirs.extend(
                d for d in sorted(examples_dir.iterdir()) if d.is_dir()
            )
        else:
            print(f"Examples directory not found: {examples_dir}", file=sys.stderr)
            sys.exit(1)

    for s in args.sessions:
        session_dirs.append(Path(s))

    if not session_dirs:
        parser.print_help()
        sys.exit(1)

    all_valid = True
    for session_dir in session_dirs:
        result = validate_session(session_dir)
        result.print_report()
        if not result.valid:
            all_valid = False

    print(f"\n{'='*60}")
    total = len(session_dirs)
    if all_valid:
        print(f"All {total} session(s) are valid.")
    else:
        invalid = sum(
            1 for d in session_dirs if not validate_session(d).valid
        )
        print(f"{invalid}/{total} session(s) have validation errors.")

    sys.exit(0 if all_valid else 1)


if __name__ == "__main__":
    main()
