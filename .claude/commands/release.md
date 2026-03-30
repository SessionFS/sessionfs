# /release — Prepare a feature release

Run this skill when the CEO says to release, tag, or ship.

## Steps

### 1. Determine the new version
- Read current version from `pyproject.toml` (field: `version`)
- If no version argument given, bump MINOR (e.g., 0.2.0 → 0.3.0)
- If the user specified a version, use that

### 2. Run tests first
```bash
.venv/bin/python -m pytest tests/ -x -q
```
Record the test count. Stop if tests fail.

### 3. Run lint
```bash
ruff check src/
helm lint charts/sessionfs
```
Stop if lint fails. Auto-fix with `ruff check src/ --fix` if needed.

### 4. Bump version

Only TWO files hold the version — everything else reads dynamically:
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/sessionfs/__init__.py` → fallback in the `except` block

Also bump:
- `charts/sessionfs/Chart.yaml` → `version` and `appVersion`

DO NOT change `SFS_FORMAT_VERSION` in `src/sessionfs/spec/version.py` — that's the .sfs format version, independent of the package version. Only bump it if the .sfs spec itself changed.

### 5. Update CHANGELOG.md
- Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top
- List everything added/changed/fixed since last release
- Follow Keep a Changelog format (Added, Changed, Fixed, Removed)

### 6. Documentation Audit (CRITICAL — do not skip)

This step verifies ALL documentation matches the actual codebase. Run these checks programmatically:

#### 6a. CLI Reference completeness
```bash
# Extract all registered commands from main.py
grep -E "app\.command|app\.add_typer" src/sessionfs/cli/main.py

# Extract all subcommands from each cmd_*.py
grep -E "@.*\.command|@.*\.callback" src/sessionfs/cli/cmd_*.py

# Verify each command appears in docs/cli-reference.md
# Every command from main.py MUST have a ## section in cli-reference.md
```

If any command is missing from `docs/cli-reference.md`, add it with:
- Usage syntax
- Arguments and options (from typer decorators)
- Brief description
- Example if non-obvious

#### 6b. README commands table
```bash
# Count commands in README table vs actual CLI
grep -c "| \`sfs " README.md
grep -c "app\.command\|app\.add_typer" src/sessionfs/cli/main.py
```

Every command group and top-level command must appear in the README commands table.

#### 6c. Environment variables
```bash
# Extract all env vars from server config
grep -E "^\s+\w+:" src/sessionfs/server/config.py | head -30

# Extract all SFS_ references in code
grep -rn "SFS_" src/sessionfs/server/ --include="*.py" | grep -oP "SFS_\w+" | sort -u

# Verify each appears in docs/environment-variables.md
```

Every `SFS_*` env var used in the code must be documented.

#### 6d. Test count + version consistency
```bash
# Verify test count matches across files
grep -n "tests passing" README.md CLAUDE.md

# Verify no stale version numbers
OLD_VERSION=$(git tag --sort=-version:refname | head -1 | sed 's/v//')
grep -rn "$OLD_VERSION" README.md CLAUDE.md charts/sessionfs/Chart.yaml pyproject.toml src/sessionfs/__init__.py
```

Fix any stale test counts or version numbers.

#### 6e. Verify specific files

| File | What to check |
|------|---------------|
| `README.md` | Version in Status section, test count, commands table complete, feature list current |
| `CLAUDE.md` | Current Phase, test count, feature list, migration count |
| `docs/cli-reference.md` | ALL commands and subcommands documented with flags |
| `docs/environment-variables.md` | ALL `SFS_*` vars documented, no non-existent vars |
| `docs/quickstart.md` | "What's Next" section mentions key features |
| `docs/self-hosted.md` | Architecture, Helm values, GitLab, nginx proxy, troubleshooting |
| `docs/troubleshooting.md` | Covers common issues for each tool (Cursor, Codex, etc.) |
| `docs/project-context.md` | All project commands documented |
| `pyproject.toml` | `description` field matches current tool count |
| `charts/sessionfs/Chart.yaml` | `version` and `appVersion` bumped |
| `charts/sessionfs/values.yaml` | Comments accurate, no stale defaults |
| `landing/index.html` | Test count, feature cards, tool count in meta tags |

#### 6f. Forbidden strings
```bash
grep -rn "sfs pull --handoff\|alwaysnix\|Dropbox" README.md docs/ landing/ src/ dashboard/src/
```
Must return zero results (except troubleshooting doc warning).

### 7. Update landing page content (if features changed)
- Verify tool count in all `<meta>` descriptions (og, twitter)
- Verify pricing section matches current tiers
- Verify feature cards are current
- Deploy: `cd landing && npx vercel --yes --prod`

### 8. Rebuild and deploy dashboard (if frontend changed)
```bash
cd dashboard && npm run build && npx vercel --yes --prod
```

### 9. Commit on develop (LOCAL ONLY)

**NEVER push develop to origin.** Develop is local only — it contains internal files.

```bash
git add -A
git add -f landing/ .claude/commands/ .release/ brand/  # force-add gitignored private files
git commit --author="sessionfsbot <bot@sessionfs.dev>" -m "Release vX.Y.Z"
# DO NOT push develop. Only main goes to origin.
```

### 10. Merge to main with sanitization

**This is the critical step.** Use `.release/private-files.txt` as the definitive list.

```bash
git checkout main
git merge develop --no-edit
```

Then remove ALL private files listed in `.release/private-files.txt`:
```bash
git show develop:.release/private-files.txt | while IFS= read -r line; do
  [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
  line="${line%%#*}"; line="${line%% *}"
  [ -z "$line" ] && continue
  git rm -rf "$line" 2>/dev/null
done
```

**VERIFY nothing leaked** (zero tolerance):
```bash
for pattern in .agents/ src/spikes/ docs/security/ docs/positioning.md docs/pricing.md DOGFOOD.md brand/ landing/ packaging/ .claude/commands/ CLAUDE.md github-app-manifest.json .release/; do
  git ls-files | grep "^${pattern}" && echo "LEAK: $pattern"
done
```
Must print only "CLEAN" with no LEAK lines.

Commit:
```bash
git commit --author="sessionfsbot <bot@sessionfs.dev>" -m "vX.Y.Z public release"
git push origin main
```

### 11. Tag
```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main --tags
git checkout develop
```

The release workflow (release.yml) will:
- Build wheel + sdist
- Publish to PyPI
- Create GitHub Release with changelog notes

### 12. Post-deploy verification
```bash
# API health
curl -s https://api.sessionfs.dev/health

# Landing page
curl -s https://sessionfs.dev | grep -o "<title>[^<]*</title>"

# Dashboard
curl -s -o /dev/null -w "%{http_code}" https://app.sessionfs.dev

# PyPI (after release workflow completes)
curl -s https://pypi.org/pypi/sessionfs/json | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"

# GitHub Release
gh release view vX.Y.Z --repo SessionFS/sessionfs
```

### 13. Wait for all pipelines
```bash
gh run list --repo SessionFS/sessionfs --limit 10
```
Wait until ALL of these show `completed success`:
- **CI** — tests + mypy
- **Release** — PyPI + GitHub Release
- **Deploy API** — Cloud Run
- **Deploy MCP Server** — Cloud Run
- **Deploy Dashboard** — Vercel (triggers when `dashboard/` changes)
- **Deploy Site** — Vercel (triggers when `site/` changes)
- **Publish Container Images** — GHCR (triggers after Release)

If Deploy Dashboard or Deploy Site didn't trigger (no changes in those dirs), that's fine — they only run when their files change. But verify the current deployments are healthy:
```bash
curl -s https://api.sessionfs.dev/health
curl -s -o /dev/null -w "%{http_code}" https://app.sessionfs.dev
curl -s -o /dev/null -w "%{http_code}" https://sessionfs.dev
```

### 14. Update memory
- Update `project_status.md` with new version, test count, features, migration count
- Update `project_architecture.md` if architecture changed
- Update `MEMORY.md` index if new memory files added

### 15. Report
Print summary table:

| Item | Status |
|------|--------|
| Version | vX.Y.Z |
| Tests | N passing |
| Lint | clean |
| Helm lint | clean |
| Docs audit | complete |
| PyPI | published / pending |
| GitHub Release | created / pending |
| API | healthy at api.sessionfs.dev |
| Site | deployed at sessionfs.dev (auto via pipeline) |
| Dashboard | deployed at app.sessionfs.dev (auto via pipeline) |
| Tag | vX.Y.Z pushed |
| Leak check | clean |

## Reference Files

| File | Purpose |
|------|---------|
| `.release/private-files.txt` | Files to strip from main — the single source of truth |
| `CHANGELOG.md` | Release notes — Keep a Changelog format |
| `.github/workflows/release.yml` | Tag → PyPI + GitHub Release automation |
| `.github/workflows/deploy-api.yml` | Push to main → Cloud Run deploy (API server) |
| `.github/workflows/deploy-mcp.yml` | Push to main → Cloud Run deploy (MCP server) |
| `.github/workflows/deploy-dashboard.yml` | Push to main → Vercel deploy (dashboard, when `dashboard/` changes) |
| `.github/workflows/deploy-site.yml` | Push to main → Vercel deploy (product site, when `site/` changes) |
| `.github/workflows/publish-images.yml` | Release → GHCR images (with VITE_API_URL build arg) |
