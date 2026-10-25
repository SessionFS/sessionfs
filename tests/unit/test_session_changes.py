"""Tests for the session-changes derivation service."""

from __future__ import annotations

import time

from sessionfs.server.services.session_changes import (
    MAX_DIFF_BYTES,
    MAX_FILES,
    _DIFF_NOT_AVAILABLE,
    _DIFF_TOO_LARGE_PREFIX,
    _TRUNCATION_NOTE,
    _sanitize,
    _truncate_utf8,
    derive_session_changes,
)


def _tool_use(name: str, input: dict) -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": input}]}


class TestEdit:
    def test_unified_diff_and_counts(self):
        changes = derive_session_changes(
            [
                _tool_use(
                    "Edit",
                    {
                        "file_path": "src/a.py",
                        "old_string": "foo\nbar\nbaz",
                        "new_string": "foo\nqux\nbaz",
                    },
                )
            ],
            None,
        )
        assert len(changes["files"]) == 1
        f = changes["files"][0]
        assert f["operation"] == "edited"
        assert "-bar" in f["diff"]
        assert "+qux" in f["diff"]
        assert f["added_lines"] == 1
        assert f["removed_lines"] == 1
        assert f["message_indexes"] == [0]

    def test_merge_edits_to_same_file_in_order(self):
        changes = derive_session_changes(
            [
                _tool_use("Edit", {"file_path": "a.py", "old_string": "a", "new_string": "b"}),
                _tool_use("Edit", {"file_path": "a.py", "old_string": "c", "new_string": "d"}),
            ],
            None,
        )
        assert len(changes["files"]) == 1
        f = changes["files"][0]
        assert f["message_indexes"] == [0, 1]
        assert f["added_lines"] == 2
        assert f["removed_lines"] == 2
        # Both hunks present, first before second
        assert f["diff"].index("+b") < f["diff"].index("+d")


class TestMultiEdit:
    def test_merges_each_edit(self):
        changes = derive_session_changes(
            [
                _tool_use(
                    "MultiEdit",
                    {
                        "file_path": "a.py",
                        "edits": [
                            {"old_string": "x", "new_string": "y"},
                            {"old_string": "p", "new_string": "q"},
                        ],
                    },
                )
            ],
            None,
        )
        f = changes["files"][0]
        assert f["operation"] == "edited"
        assert f["added_lines"] == 2
        assert f["removed_lines"] == 2
        assert "+y" in f["diff"] and "+q" in f["diff"]


class TestWrite:
    def test_full_content_and_created(self):
        changes = derive_session_changes(
            [_tool_use("Write", {"file_path": "new.txt", "content": "line1\nline2\nline3"})],
            None,
        )
        f = changes["files"][0]
        assert f["operation"] == "created"
        assert f["diff"] == "--- /dev/null\n+++ b/new.txt\n+line1\n+line2\n+line3"
        assert f["added_lines"] == 3
        assert f["removed_lines"] == 0

    def test_truncates_to_2000_lines(self):
        content = "\n".join(f"line{i}" for i in range(3000))
        changes = derive_session_changes(
            [_tool_use("Write", {"file_path": "big.txt", "content": content})],
            None,
        )
        f = changes["files"][0]
        assert f["truncated"] is True
        assert f["added_lines"] == 2000
        assert "line1999" in f["diff"]
        assert "line2000" not in f["diff"]


class TestApplyPatch:
    def test_patch_text_verbatim(self):
        patch = "--- a/c.py\n+++ b/c.py\n@@ -1 +1 @@\n-old\n+new"
        changes = derive_session_changes(
            [_tool_use("apply_patch", {"patch": patch})],
            None,
        )
        f = changes["files"][0]
        assert f["path"] == "c.py"
        assert f["operation"] == "edited"
        assert f["diff"] == patch
        assert f["added_lines"] == 1
        assert f["removed_lines"] == 1

    def test_explicit_file_path_wins(self):
        patch = "--- a/ignored.py\n+++ b/ignored.py\n@@ -1 +1 @@\n-old\n+new"
        changes = derive_session_changes(
            [_tool_use("apply_patch", {"file_path": "target.py", "patch": patch})],
            None,
        )
        assert changes["files"][0]["path"] == "target.py"


class TestOperationResolution:
    def test_write_then_edit_stays_created(self):
        changes = derive_session_changes(
            [
                _tool_use("Write", {"file_path": "a.py", "content": "one"}),
                _tool_use("Edit", {"file_path": "a.py", "old_string": "one", "new_string": "two"}),
            ],
            None,
        )
        assert changes["files"][0]["operation"] == "created"

    def test_deleted_from_workspace_role(self):
        workspace = {"files": [{"path": "gone.py", "role": "deleted"}]}
        changes = derive_session_changes([], workspace)
        assert len(changes["files"]) == 1
        f = changes["files"][0]
        assert f["path"] == "gone.py"
        assert f["operation"] == "deleted"
        assert f["diff"] == ""


class TestCaps:
    def test_file_cap(self):
        msgs = [
            _tool_use("Write", {"file_path": f"f{i}.py", "content": "x"})
            for i in range(MAX_FILES + 5)
        ]
        changes = derive_session_changes(msgs, None)
        assert len(changes["files"]) == MAX_FILES
        assert changes["truncated"] is True

    def test_diff_byte_cap(self):
        # One apply_patch with a patch larger than the 1 MB byte budget.
        patch = "--- a/big.py\n+++ b/big.py\n@@ -1 +1 @@\n" + "\n".join(
            f"+{i:020d}" for i in range(60000)
        )
        changes = derive_session_changes(
            [_tool_use("apply_patch", {"patch": patch})],
            None,
        )
        f = changes["files"][0]
        assert changes["truncated"] is True
        assert f["truncated"] is True
        assert len(f["diff"].encode("utf-8")) <= MAX_DIFF_BYTES + len(_TRUNCATION_NOTE.encode("utf-8"))


class TestGitRef:
    def test_branch_and_sha(self):
        changes = derive_session_changes(
            [], {"git": {"branch": "main", "commit_sha": "abc123"}}
        )
        assert changes["git_ref"] == "main@abc123"

    def test_branch_only(self):
        changes = derive_session_changes([], {"git": {"branch": "main"}})
        assert changes["git_ref"] == "main"

    def test_no_workspace(self):
        assert derive_session_changes([], None)["git_ref"] is None


class TestEmpty:
    def test_no_tool_uses(self):
        changes = derive_session_changes([{"role": "user", "content": [{"type": "text", "text": "hi"}]}], None)
        assert changes["files"] == []
        assert changes["truncated"] is False


# --- Finding 1: oversized edits must not run quadratic difflib ---------------


class TestOversizedDiff:
    def test_huge_edit_skips_difflib_quickly(self):
        old = "\n".join(f"line{i}" for i in range(20000))
        new = "\n".join(f"line{i}" for i in range(20000)) + "\nextra"
        start = time.monotonic()
        changes = derive_session_changes(
            [_tool_use("Edit", {"file_path": "big.py", "old_string": old, "new_string": new})],
            None,
        )
        elapsed = time.monotonic() - start
        f = changes["files"][0]
        assert f["truncated"] is True
        assert f["diff"].startswith(f"{_DIFF_TOO_LARGE_PREFIX}: -20000/+20001 lines")
        # The unfixed quadratic difflib path takes ~100s on this input; the cap
        # keeps it well under 2s.
        assert elapsed < 2.0

    def test_huge_single_line_skips_difflib(self):
        # One line, but over the 200 KB per-side byte cap.
        old = "x" * 300_000
        new = "y" * 300_000
        changes = derive_session_changes(
            [_tool_use("Edit", {"file_path": "big.py", "old_string": old, "new_string": new})],
            None,
        )
        f = changes["files"][0]
        assert f["truncated"] is True
        assert f["diff"].startswith(f"{_DIFF_TOO_LARGE_PREFIX}: -1/+1 lines")


# --- Finding 2: read-only tools must not appear as changed files ------------


class TestReadOnlyToolsIgnored:
    def test_read_grep_glob_ls_ignored(self):
        changes = derive_session_changes(
            [
                _tool_use("Read", {"file_path": "a.py"}),
                _tool_use("Grep", {"pattern": "foo"}),
                _tool_use("Glob", {"pattern": "**/*.py"}),
                _tool_use("LS", {"path": "src"}),
            ],
            None,
        )
        assert changes["files"] == []

    def test_other_tool_read_equivalents_ignored(self):
        changes = derive_session_changes(
            [
                _tool_use("ReadFile", {"path": "a.py"}),
                _tool_use("read_file", {"path": "a.py"}),
                _tool_use("view", {"path": "a.py"}),
                _tool_use("list_dir", {"path": "src"}),
            ],
            None,
        )
        assert changes["files"] == []


# --- Finding 3: Codex apply_patch `*** Begin Patch` -------------------------


class TestCodexBeginPatch:
    def test_add_update_delete(self):
        raw = (
            "*** Begin Patch\n"
            "*** Add File: /root/src/new.py\n"
            "+def hi():\n"
            "+    return 1\n"
            "*** Update File: /root/src/app.py\n"
            "@@\n"
            "-return 1\n"
            "+return 2\n"
            "*** Delete File: /root/gone.py\n"
            "*** End Patch\n"
        )
        changes = derive_session_changes(
            [_tool_use("apply_patch", {"input": raw})],
            {"root_path": "/root"},
        )
        by_path = {f["path"]: f for f in changes["files"]}
        assert set(by_path) == {"src/new.py", "src/app.py", "gone.py"}

        created = by_path["src/new.py"]
        assert created["operation"] == "created"
        assert created["diff"] == "--- /dev/null\n+++ b/src/new.py\n+def hi():\n+    return 1"

        edited = by_path["src/app.py"]
        assert edited["operation"] == "edited"
        assert "-return 1" in edited["diff"] and "+return 2" in edited["diff"]
        assert edited["diff"].startswith("--- a/src/app.py\n+++ b/src/app.py")

        deleted = by_path["gone.py"]
        assert deleted["operation"] == "deleted"

    def test_move_recorded(self):
        raw = (
            "*** Begin Patch\n"
            "*** Move File: /root/old.py\n"
            "*** Move to: renamed.py\n"
            "*** End Patch\n"
        )
        changes = derive_session_changes(
            [_tool_use("apply_patch", {"input": raw})],
            {"root_path": "/root"},
        )
        assert len(changes["files"]) == 1
        f = changes["files"][0]
        assert f["path"] == "renamed.py"
        assert f["operation"] == "unknown"
        assert f["diff"] == "--- a/old.py\n+++ b/renamed.py"


# --- Finding 4: absolute paths relativized + per-tool mappings --------------


class TestToolMappings:
    def test_claude_code_absolute_path_relativized(self):
        changes = derive_session_changes(
            [
                _tool_use(
                    "Edit",
                    {"file_path": "/home/user/project/src/a.py", "old_string": "x", "new_string": "y"},
                )
            ],
            {"root_path": "/home/user/project"},
        )
        assert changes["files"][0]["path"] == "src/a.py"

    def test_gemini_write_file(self):
        changes = derive_session_changes(
            [_tool_use("WriteFile", {"path": "new.md", "content": "# Title"})],
            None,
        )
        f = changes["files"][0]
        assert f["operation"] == "created"
        assert f["diff"] == "--- /dev/null\n+++ b/new.md\n+# Title"

    def test_cline_edited_existing_file_and_new_file(self):
        changes = derive_session_changes(
            [
                _tool_use(
                    "editedExistingFile",
                    {"path": "a.py", "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new"},
                ),
                _tool_use("newFileCreated", {"path": "b.py", "content": "print(1)"}),
            ],
            None,
        )
        by_path = {f["path"]: f for f in changes["files"]}
        assert by_path["a.py"]["operation"] == "edited"
        assert "-old" in by_path["a.py"]["diff"]
        assert by_path["b.py"]["operation"] == "created"
        assert by_path["b.py"]["diff"] == "--- /dev/null\n+++ b/b.py\n+print(1)"

    def test_amp_edit_and_create(self):
        changes = derive_session_changes(
            [
                _tool_use("edit_file", {"path": "a.py", "old_str": "old", "new_str": "new"}),
                _tool_use("create", {"path": "b.py", "content": "x = 1"}),
            ],
            None,
        )
        by_path = {f["path"]: f for f in changes["files"]}
        assert by_path["a.py"]["operation"] == "edited"
        assert by_path["b.py"]["operation"] == "created"

    def test_copilot_edit_and_create(self):
        changes = derive_session_changes(
            [
                _tool_use("edit", {"path": "a.py", "old_string": "1", "new_string": "2"}),
                _tool_use("create", {"path": "c.py", "content": "y = 2"}),
            ],
            None,
        )
        by_path = {f["path"]: f for f in changes["files"]}
        assert by_path["a.py"]["operation"] == "edited"
        assert by_path["c.py"]["operation"] == "created"

    def test_antigravity_write_and_replace(self):
        changes = derive_session_changes(
            [
                _tool_use(
                    "write_to_file",
                    {"TargetFile": "new.py", "CodeContent": "print(1)"},
                ),
                _tool_use(
                    "replace_file_content",
                    {"TargetFile": "a.py", "OldString": "old", "NewString": "new"},
                ),
            ],
            None,
        )
        by_path = {f["path"]: f for f in changes["files"]}
        assert by_path["new.py"]["operation"] == "created"
        assert by_path["a.py"]["operation"] == "edited"
        assert "-old" in by_path["a.py"]["diff"] and "+new" in by_path["a.py"]["diff"]


# --- Finding 8: the 1 MB cap counts UTF-8 bytes, not characters -------------


class TestByteBudget:
    def test_multibyte_content_counted_in_bytes(self):
        # 600k "é" chars = 1.2 MB of UTF-8 bytes but under 1 MB of characters,
        # so a char-based cap would not truncate it.
        content = "é" * 600_000
        changes = derive_session_changes(
            [_tool_use("Write", {"file_path": "unicode.txt", "content": content})],
            None,
        )
        f = changes["files"][0]
        assert changes["truncated"] is True
        assert f["truncated"] is True
        assert len(f["diff"].encode("utf-8")) <= MAX_DIFF_BYTES + len(_TRUNCATION_NOTE.encode("utf-8"))

    def test_truncate_utf8_never_splits_a_character(self):
        text = "é" * 5  # 10 bytes
        out = _truncate_utf8(text, 5)
        # 5 bytes fits exactly two 2-byte characters, never a half-char.
        assert out == "éé"
        assert len(out.encode("utf-8")) <= 5


# --- Session-wide difflib work budget ---------------------------------------


class TestDiffWorkBudget:
    def test_caps_edits_diffed(self):
        # 1,100 single-line edits to one file: only the first 1,000 are diffed,
        # the remaining 100 become placeholders.
        msgs = [
            _tool_use("Edit", {"file_path": "a.py", "old_string": f"x{i}", "new_string": f"y{i}"})
            for i in range(1_100)
        ]
        changes = derive_session_changes(msgs, None)
        f = changes["files"][0]
        assert changes["truncated"] is True
        assert f["truncated"] is True
        assert "+y999" in f["diff"]  # 1,000th edit (index 999) is diffed
        assert "+y1000" not in f["diff"]  # 1,001st edit (index 1000) is a placeholder
        assert _DIFF_TOO_LARGE_PREFIX in f["diff"]

    def test_reviewer_scenario_completes_quickly(self):
        # 2,000 copies of a 2,000-line edit changing a single line. Each diff is
        # tiny (one changed hunk), so the 1 MB output budget never trips — but
        # every copy still feeds 2,000 lines into quadratic difflib. The 5 MB
        # input budget must cap total CPU well under 2s.
        lines = [f"line{i:04d}" for i in range(2000)]
        old = "\n".join(lines)
        changed = list(lines)
        changed[1000] = "CHANGED"
        new = "\n".join(changed)
        msgs = [
            _tool_use("Edit", {"file_path": "big.py", "old_string": old, "new_string": new})
            for _ in range(2_000)
        ]
        start = time.monotonic()
        changes = derive_session_changes(msgs, None)
        elapsed = time.monotonic() - start
        f = changes["files"][0]
        assert changes["truncated"] is True
        assert f["truncated"] is True
        assert _DIFF_TOO_LARGE_PREFIX in f["diff"]
        # Every edit is still accounted for via its message index.
        assert len(f["message_indexes"]) == 2_000
        assert elapsed < 2.0


# --- Codex patch markers must not swallow hunk context lines ----------------

class TestCodexMarkerMatching:
    def test_context_lines_with_leading_space_are_hunk_body(self):
        raw = (
            "*** Begin Patch\n"
            "*** Update File: /root/src/app.py\n"
            "@@\n"
            " *** End Patch\n"
            " *** Delete File: b.py\n"
            "-old\n"
            "+new\n"
            "*** End Patch\n"
        )
        changes = derive_session_changes(
            [_tool_use("apply_patch", {"input": raw})],
            {"root_path": "/root"},
        )
        by_path = {f["path"]: f for f in changes["files"]}
        # The indented markers must not be parsed as section markers/deletes.
        assert set(by_path) == {"src/app.py"}
        f = by_path["src/app.py"]
        assert f["operation"] == "edited"
        assert " *** End Patch" in f["diff"]
        assert " *** Delete File: b.py" in f["diff"]


# --- Cline/Roo/Kilo replace_in_file + ui_messages fallback ------------------

class TestClineReplaceInFile:
    def test_single_search_replace_pair(self):
        diff = (
            "<<<<<<< SEARCH\n"
            "def greet():\n"
            "    return 'hello'\n"
            "=======\n"
            "def greet():\n"
            "    return 'goodbye'\n"
            ">>>>>>> REPLACE\n"
        )
        changes = derive_session_changes(
            [_tool_use("replace_in_file", {"path": "src/util.py", "diff": diff})],
            None,
        )
        f = changes["files"][0]
        assert f["path"] == "src/util.py"
        assert f["operation"] == "edited"
        assert "-    return 'hello'" in f["diff"]
        assert "+    return 'goodbye'" in f["diff"]

    def test_multiple_pairs(self):
        diff = (
            "<<<<<<< SEARCH\nold1\n=======\nnew1\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nold2\n=======\nnew2\n>>>>>>> REPLACE\n"
        )
        changes = derive_session_changes(
            [_tool_use("replace_in_file", {"path": "a.py", "diff": diff})],
            None,
        )
        f = changes["files"][0]
        assert "-old1" in f["diff"] and "+new1" in f["diff"]
        assert "-old2" in f["diff"] and "+new2" in f["diff"]
        assert f["added_lines"] == 2
        assert f["removed_lines"] == 2


class TestClineUiMessagesFallback:
    def test_non_unified_diff_shows_not_available(self):
        changes = derive_session_changes(
            [_tool_use("editedExistingFile", {"path": "a.py", "diff": "just some text"})],
            None,
        )
        f = changes["files"][0]
        assert f["operation"] == "edited"
        assert f["diff"] == _DIFF_NOT_AVAILABLE
        assert f["added_lines"] == 0
        assert f["removed_lines"] == 0


# --- Copilot create accepts file_text ---------------------------------------

class TestCopilotCreate:
    def test_file_text_accepted(self):
        changes = derive_session_changes(
            [_tool_use("create", {"path": "c.py", "file_text": "y = 2"})],
            None,
        )
        f = changes["files"][0]
        assert f["operation"] == "created"
        assert f["diff"] == "--- /dev/null\n+++ b/c.py\n+y = 2"


# --- Lone UTF-16 surrogates must not 500 derivation --------------------------

class TestLoneSurrogates:
    def test_sanitize_replaces_lone_surrogate(self):
        assert _sanitize("a\ud800b") == "a�b"
        # A lone high surrogate and a lone low surrogate are both replaced.
        assert _sanitize("\udfff") == "�"
        # Valid text is returned unchanged (and stays the same object).
        assert _sanitize("plain ascii") == "plain ascii"
        assert _sanitize("café 日本語") == "café 日本語"

    def test_edit_old_string_with_lone_surrogate(self):
        changes = derive_session_changes(
            [_tool_use("Edit", {"file_path": "a.py", "old_string": "a\ud800b", "new_string": "c"})],
            None,
        )
        f = changes["files"][0]
        assert "\ud800" not in f["diff"]
        assert "�" in f["diff"]
        assert "-a�b" in f["diff"]
        f["diff"].encode("utf-8")  # must not raise

    def test_write_content_with_lone_surrogate(self):
        changes = derive_session_changes(
            [_tool_use("Write", {"file_path": "new.py", "content": "x\ud800y"})],
            None,
        )
        f = changes["files"][0]
        assert "\ud800" not in f["diff"]
        assert "+x�y" in f["diff"]
        f["diff"].encode("utf-8")

    def test_file_path_with_lone_surrogate(self):
        changes = derive_session_changes(
            [_tool_use("Write", {"file_path": "a\ud800.py", "content": "x"})],
            None,
        )
        f = changes["files"][0]
        assert f["path"] == "a�.py"
        assert "\ud800" not in f["path"]
        f["diff"].encode("utf-8")

    def test_git_ref_with_lone_surrogate(self):
        changes = derive_session_changes(
            [],
            {"git": {"branch": "feat\ud800x", "commit_sha": "abc"}},
        )
        assert changes["git_ref"] == "feat�x@abc"
        changes["git_ref"].encode("utf-8")
