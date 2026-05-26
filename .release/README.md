# .release/ — internal release tooling

Internal scripts and reference data the `/release` skill uses to ship a
public version of SessionFS without leaking strategy files. Local-only
on `develop`; never reaches `main`.

## Files

| File | Purpose |
|---|---|
| `private-files.txt` | Single source of truth for which paths get stripped at sanitize time. The release skill iterates this list and runs `git rm -rf` against each. |
| `sanitize_main.py` | Deterministic Python helper that consumes `private-files.txt` with `--dry-run` default + `--apply`. Use this instead of the inline bash loop when you want preview output before mutating. |

## Expected develop → main merge conflicts

When the `/release` skill merges `develop` into `main`, `git merge`
will surface conflicts on the small set of paths where develop and
main both legitimately diverge:

| Path | Why it conflicts |
|---|---|
| `.claude/commands/release.md` | The skill itself lives only on develop. Main has no such file. Conflict is "modify on develop, delete on main". |
| `CLAUDE.md` | Carries internal current-phase notes + memory pointers on develop. Main has a public-facing version (or none at all). |
| `.agents/*.md` | Persona files exist only on develop. Conflict is "modify on develop, delete on main". |
| `.agents/README.md` | Same as above. |

**This is expected.** The sanitize step (run immediately after the
merge in step 10 of the release skill) consumes
`.release/private-files.txt` and `git rm -rf`'s every entry, so the
conflicted private files get removed regardless of which side won the
merge. The skill's leak-check loop at the end of step 10 verifies
zero private paths remain.

### Why not `.gitattributes merge=ours` for these paths

A repo-wide `merge=ours` on these paths would silently drop legitimate
develop-side changes too — the conflict surface is the only signal
that a release-skill edit (or a CLAUDE.md update) made it from develop
without being intentionally re-resolved. The current ad-hoc approach
(let the conflict happen → sanitize strips it → leak check verifies)
keeps the audit trail visible.

If the conflict ever becomes painful enough to revisit:

1. First try narrowing `.gitattributes` rules to just the
   develop-only paths (`.agents/**`, `.claude/commands/**`,
   `CLAUDE.md`) with `merge=ours`. **Do not** use a broad rule.
2. Verify the sanitize step still runs correctly with the new rule —
   `merge=ours` makes the conflict invisible, so the leak check
   becomes the only backstop.
3. Update this README + the release skill to reflect the new behavior.

For now, the conflict-then-sanitize pattern is the documented contract.

## Quick reference

To preview what sanitize would strip on the current main checkout:

```bash
.venv/bin/python .release/sanitize_main.py --dry-run
```

To apply (call only after merging develop into main):

```bash
.venv/bin/python .release/sanitize_main.py --apply
```

Both modes read `.release/private-files.txt`; edit that file to add or
remove paths from the sanitize list, never inline a path here or in
the release skill.
