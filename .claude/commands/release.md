# /release — Prepare a feature release

Run this skill when the CEO says to release, tag, or go public with new features.

## Steps

### 1. Determine the new version
- Read the current version from `pyproject.toml` (field: `version`)
- If no version argument given, bump the MINOR version (e.g., 0.1.0 → 0.2.0)
- If the user specified a version, use that

### 2. Update version in ALL of these files

**Source of truth (bump the version string):**
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/sessionfs/__init__.py` → `__version__ = "X.Y.Z"`

**Server/API:**
- `src/sessionfs/server/app.py` → FastAPI `version="X.Y.Z"`
- `src/sessionfs/server/routes/health.py` → health response version
- `src/sessionfs/daemon/status.py` → `version: str = "X.Y.Z"`
- `src/sessionfs/sync/client.py` → User-Agent header

**DO NOT change these (sfs format version is independent):**
- `sfs_version` in converters, watchers, specs, examples, tests — this is the `.sfs` FORMAT version, not the package version. Only change if the .sfs spec itself changed.

### 3. Update documentation files

- `README.md` → Status section version, test count
- `CLAUDE.md` → Current Phase section with test count
- `docs/pricing.md` → Shipped version reference (if mentioned)

### 4. Update memory

- Update `project_status.md` in memory with new version and test count

### 5. Run tests
```bash
.venv/bin/pytest tests/ -x -q
```
All tests must pass. If a test asserts on the version string (like test_health.py), update it.

### 6. Commit on develop
```bash
git add -A
git commit --author="sessionfsbot <bot@sessionfs.dev>" -m "Bump version to X.Y.Z"
git push origin develop
```

### 7. Merge to main (strip internal files)
- `git checkout main`
- `git merge develop --no-edit`
- Remove internal files: `.agents/`, `DOGFOOD.md`, `docs/positioning.md`, `docs/security/`, `src/spikes/`
- Strip internal sections from CLAUDE.md (Agent Team, Commit Rules, Branch Policy, monetization)
- Commit and push

### 8. Tag and push
```bash
git tag -a vX.Y.Z -m "vX.Y.Z release"
git push origin main --tags
git checkout develop
```

### 9. Deploy
- Rebuild and push Docker image
- Redeploy landing page if content changed
- Redeploy dashboard if frontend changed

### 10. Update GitHub repo description if needed
```bash
gh repo edit --description "new description"
```

### 11. Report
List: version, test count, files changed, tag, deploy status.
