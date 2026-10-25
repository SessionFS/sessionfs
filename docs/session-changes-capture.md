# Session Changes — capture audit

How file changes are captured per converter/watcher, and how the Changes tab
derives per-file diffs from them. This documents the canonical `.sfs` shape
each source tool produces for the tool calls that touch files, so the
derivation service (`server/services/session_changes.py`) and its tests stay
grounded in what actually lands in `messages.jsonl`.

## Canonical `.sfs` tool_use block

Every file-touching action is a `tool_use` content block inside an assistant
message (or a `tool` message for Cursor/Codex, which emit the tool call inline).
The block shape is fixed by `spec/schemas/message.schema.json`:

```json
{
  "type": "tool_use",
  "tool_use_id": "toolu_abc123",
  "name": "Edit",
  "input": { "...": "..." }
}
```

`input` is an arbitrary object whose keys depend on the tool. The derivation
service reads `name` + `input` and ignores everything else.

## Allowlist

Only edit / create / delete / patch tools produce file-change entries. Read-only
tools are **not** on the allowlist and are ignored: Claude Code `Read` / `Grep` /
`Glob` / `LS`, Gemini `ReadFile` / `ReadFolder`, Cline/Roo/Kilo `read_file`,
Copilot `view`, Cursor `read_file` / `list_dir` / `search_files`, and any other
tool name not listed below. A session that only browsed the tree therefore shows
"no file changes".

## Per-tool input contract

The derivation service matches on the `name` string (normalized where a
converter rewrites it — see below) and reads the listed `input` keys, falling
back through each candidate in order.

| Source | `name` | `input` fields used | Diff source |
|---|---|---|---|
| Claude Code `Edit` | `Edit` | `file_path`→`path`, `old_string`→`old_str`, `new_string`→`new_str` | `difflib` unified diff (capped) |
| Claude Code `MultiEdit` | `MultiEdit` | `file_path`→`path`, `edits: [{old_string, new_string}]` | one unified diff per edit, merged in order |
| Claude Code `Write` | `Write` | `file_path`→`path`, `content` | created diff (`--- /dev/null` / `+++ b/<path>`, `+` per line) |
| Cursor `edit_file` / `str_replace` / `StrReplace` | `Edit` (normalized) | `file_path`, `old_string`, `new_string` | unified diff |
| Cursor `create_file` / `write_file` | `Write` (normalized) | `file_path`, `content` | created diff |
| Gemini `WriteFile` | `WriteFile` | `path`→`file_path`, `content` | created diff |
| Cline/Roo/Kilo edit (primary) | `replace_in_file` | `path`→`file_path`, `diff` (SEARCH/REPLACE block) | one unified diff per SEARCH/REPLACE pair (capped) |
| Cline/Roo/Kilo create (primary) | `write_to_file` | `path`→`file_path`, `content` | created diff |
| Cline/Roo/Kilo edit (ui_messages fallback) | `editedExistingFile` | `path`→`file_path`, `diff` | patch text verbatim if a unified diff, else `diff not available` |
| Cline/Roo/Kilo create (ui_messages fallback) | `newFileCreated` | `path`→`file_path`, `content` | created diff |
| Amp edit | `edit_file` | `path`→`file_path`, `old_str`→`old_string`, `new_str`→`new_string` | unified diff |
| Amp create | `create` | `path`→`file_path`, `content` | created diff |
| Copilot edit | `edit` | `path`→`file_path`, `old_string`, `new_string` | unified diff |
| Copilot create (unverified) | `create` | `path`→`file_path`, `content`→`file_text` | created diff |
| Antigravity create | `write_to_file` | `TargetFile`→`file_path`→`path`, `CodeContent`→`content` | created diff |
| Antigravity replace | `replace_file_content` | `TargetFile`, `OldString`→`old_string`, `NewString`→`new_string` | unified diff |
| Antigravity multi-replace | `multi_replace_file_content` | `TargetFile`, `Replacements`/`Edits: [{OldString, NewString}]` | one unified diff per replacement |
| Codex `apply_patch` (legacy) | `apply_patch` | `patch` (unified diff text), optional `file_path` | patch text verbatim |
| Codex `apply_patch` (custom) | `apply_patch` | `input` (raw `*** Begin Patch` string) | parsed per section |

### Cursor normalization

`converters/cursor_to_sfs.py` rewrites Cursor's native tool names to canonical
names before they reach the derivation service: `edit_file`/`str_replace`/
`StrReplace` → `Edit`, `create_file`/`write_file` → `Write`. The remaining
tools in the table above are passed through verbatim by their converters.

### Cline/Roo/Kilo capture paths

`converters/cline_to_sfs.py` (shared by the Cline, Roo and Kilo watchers) has two
capture paths:

- **Primary** — `api_conversation_history.json` passes the model's tool names
  verbatim: `write_to_file` (`path`, `content`) for creates and `replace_in_file`
  (`path`, `diff` in SEARCH/REPLACE block format) for edits. `replace_in_file`
  is split into `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` pairs and each
  pair is diffed like an `Edit`.
- **Fallback** — `ui_messages.json` emits the older `editedExistingFile` /
  `newFileCreated` names instead. `editedExistingFile`'s `diff` is rendered
  verbatim **only when it is a unified diff**; otherwise the changes tab shows
  `diff not available`.

`write_to_file` is also Antigravity's create name but with different key names
(`TargetFile` / `CodeContent`); the derivation service accepts both shapes.

### Copilot (unverified)

Copilot's `create` capture is **unverified**: it may emit `file_text` instead of
`content` for the body. Both keys are accepted (`content` → `file_text`).

### Codex `apply_patch`

Codex's `custom_tool_call` captures `apply_patch` as a single raw string in
`input` (`{"input": "*** Begin Patch ..."}`), which the Codex watcher preserves
verbatim. The derivation service parses that format:

```
*** Begin Patch
*** Add File: /path/to/file          → created (body lines are `+`-prefixed)
*** Update File: /path/to/file       → edited (hunks rendered as a unified diff)
@@
-old
+new
*** Delete File: path/to/file        → deleted
*** Move File: path/to/old
*** Move to: path/to/new             → recorded as a rename diff, operation `unknown`
*** End Patch
```

The legacy `function_call` path emits `apply_patch` with a `patch` unified-diff
field instead, attributed to `input.file_path` if present, else the patch's
`+++ b/<path>` header.

The Codex watcher also stamps each captured `tool_use` block with
`codex_item_type` (`custom_tool_call`, `function_call`, or `local_shell_call`) so
resume-into-Codex (`converters/sfs_to_codex.py`) can round-trip the original
response_item type — `custom_tool_call` items are written back with their raw
`input` string rather than as a `function_call` with JSON-encoded arguments.

## Path resolution

- Edit / create tools read `file_path` then `path` (and `TargetFile` for
  Antigravity), in that order.
- `apply_patch` (legacy) reads `file_path` first, else `+++ b/<path>`.
- Absolute paths are relativized against `workspace.root_path` when they live
  under it (Claude Code and Codex both capture absolute paths; Gemini/Cursor/
  Cline/Amp/Copilot capture workspace-relative paths). Paths outside the
  workspace root are kept as-is.

## Operation determination

| Signal | Result |
|---|---|
| `workspace.files` role `deleted` | `deleted` |
| first touch was a create (`Write`/`WriteFile`/`newFileCreated`/`create`/… or a `*** Add File` section) | `created` (even if a later Edit follows) |
| a delete op (e.g. `*** Delete File`) and no create | `deleted` |
| an edit / patch op | `edited` |
| a move (`*** Move File` / `*** Move to`) | `unknown` (rename diff) |
| `workspace.files` role `created` / `edited` / `written` fallback | `created` / `edited` |
| no signal | `unknown` |

`deleted` usually has no structured tool call in the non-Codex tools — deletion
happens via `Bash`/shell, so it is recovered from `workspace.json`'s
`files[].role`. Note: no converter currently emits the `deleted` role; it is an
enum value in `spec/schemas/workspace.schema.json` reserved for future tools and
consumed by the derivation service as a fallback.

## Merge and cap rules

- Edits to the same path are merged in message order (hunks concatenated;
  `added_lines` / `removed_lines` summed).
- `message_indexes` are 0-based indexes into the raw archive message list,
  matching the audit convention in `judge/evidence.py` (`enumerate(messages)`).
- Caps:
  - 200 files — later files are dropped and the top-level `truncated` flag is set.
  - 1 MB of diff text counted in **UTF-8 bytes** across all files.
  - Per-edit difflib guard: if either side exceeds 2,000 lines or 200 KB, the
    `difflib` diff is skipped and a short `diff too large: -N/+M lines`
    placeholder is emitted instead (difflib is quadratic and would block the
    event loop). The per-file `truncated` flag is set.
  - Session-wide difflib work budget: at most 5 MB of difflib **input** bytes
    (old + new, UTF-8) and 1,000 edits actually diffed per session. Once either
    is spent, every remaining edit emits the `diff too large` placeholder (still
    counted with its message index) and the top-level `truncated` flag is set.
    This bounds total CPU for sessions with thousands of large edits.
  - `Write`/create content is capped at 2,000 lines.

## Redaction parity

Messages are redacted at push time (`server/dlp.py` `redact_and_repack`
rewrites every `.json`/`.jsonl` member value). The derivation service reads the
stored, already-redacted messages — it never touches the original workspace
files — so a secret in an `Edit`/`Write` input can never surface in the changes
output. This is proven by the redaction test in
`tests/server/integration/test_session_changes_api.py`.
