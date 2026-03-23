# /release — Prepare a feature release

Run this skill when the CEO says to release, tag, or ship.

## Steps

### 1. Determine the new version
- Read current version from `pyproject.toml` (field: `version`)
- If no version argument given, bump MINOR (e.g., 0.2.0 → 0.3.0)
- If the user specified a version, use that

### 2. Bump version

Only TWO files hold the version — everything else reads dynamically:
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/sessionfs/__init__.py` → fallback in the `except` block

DO NOT change `SFS_FORMAT_VERSION` in `src/sessionfs/spec/version.py` — that's the .sfs format version, independent of the package version. Only bump it if the .sfs spec itself changed.

### 3. Update CHANGELOG.md
- Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top
- Move unreleased items into it
- Follow Keep a Changelog format (Added, Changed, Fixed, Removed)

### 4. Update documentation
- `README.md` → version in Status section, test count
- `CLAUDE.md` → Current Phase test count and feature list

### 5. Run tests
```bash
.venv/bin/python -m pytest tests/ -x -q
```
Update test count in docs if changed.

### 6. Commit on develop
```bash
git add -A
git commit --author="sessionfsbot <bot@sessionfs.dev>" -m "Release vX.Y.Z"
git push origin develop
```

### 7. Merge to main with sanitization

**This is the critical step.** Use `.release/private-files.txt` as the definitive list.

```bash
git checkout main
git merge develop --no-edit
```

Then remove ALL private files listed in `.release/private-files.txt`:
```bash
# Read the manifest and remove each entry
while IFS= read -r line; do
  [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
  line="${line%%#*}"  # strip inline comments
  line="${line%% *}"  # strip trailing spaces
  [ -z "$line" ] && continue
  git rm -rf "$line" 2>/dev/null
done < .release/private-files.txt
```

CLAUDE.md is in the private files list — it gets removed entirely. No sanitization needed.

Commit:
```bash
git commit --author="sessionfsbot <bot@sessionfs.dev>" -m "vX.Y.Z public release"
git push origin main
```

### 8. Tag
```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main --tags
git checkout develop
```

The release workflow (release.yml) will:
- Build wheel + sdist
- Publish to PyPI
- Create GitHub Release with changelog notes

### 9. Deploy (if needed)
- Rebuild Docker image → push to Artifact Registry → `gcloud run deploy`
- Redeploy landing page → `cd landing && npx vercel --yes --prod`
- Redeploy dashboard → `cd dashboard && npm run build && npx vercel --yes --prod`

### 10. Update memory
- Update `project_status.md` with new version and test count

### 11. Report
Print: version, test count, changelog summary, tag, PyPI status, deploy status.

## Private File Manifest

The definitive list of private files is at `.release/private-files.txt`. If a new private file category is added to the project, update that file — not this skill.

## CLAUDE.md

CLAUDE.md is private — lives on develop only. It's in the private files manifest and gets removed during merge to main. The README serves as the public project overview.
