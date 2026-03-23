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

### 3. Bump version

Only TWO files hold the version — everything else reads dynamically:
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/sessionfs/__init__.py` → fallback in the `except` block

DO NOT change `SFS_FORMAT_VERSION` in `src/sessionfs/spec/version.py` — that's the .sfs format version, independent of the package version. Only bump it if the .sfs spec itself changed.

### 4. Update CHANGELOG.md
- Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top
- List everything added/changed/fixed since last release
- Follow Keep a Changelog format (Added, Changed, Fixed, Removed)

### 5. Update ALL documentation

**Every release, verify and update each of these:**

| File | What to check |
|------|---------------|
| `README.md` | Version in Status section, test count, tool count, feature list, roadmap |
| `CLAUDE.md` | Current Phase, test count, feature list |
| `pyproject.toml` | `description` field matches current tool count |
| `docs/quickstart.md` | Prerequisites, expected CLI output, tool list |
| `docs/cli-reference.md` | All commands documented, flags up to date |
| `docs/pricing.md` | Shipped features, tier matrix, tool list |
| `docs/sync-guide.md` | Server URLs, examples |
| `landing/index.html` | Tool count in meta descriptions, hero text, tool cards, pricing section |
| `dashboard/` | No stale content (usually code-only, but check) |
| `.env.example` | Matches docker-compose.yml variables |

**Grep to catch strays:**
```bash
grep -rn "OLD_TOOL_COUNT\|old version" README.md docs/ landing/ pyproject.toml
```

### 6. Update landing page content
- Verify tool count in all `<meta>` descriptions (og, twitter)
- Verify pricing section matches current tiers
- Verify feature cards are current
- Deploy: `cd landing && npx vercel --yes --prod`

### 7. Rebuild and deploy dashboard (if frontend changed)
```bash
cd dashboard && npm run build && npx vercel --yes --prod
```

### 8. Commit on develop

Note: files in `.gitignore` (landing/, .claude/, .release/, etc.) need `git add -f` to track on develop.

```bash
git add -A
git add -f landing/ .claude/commands/ .release/  # force-add gitignored private files
git commit --author="sessionfsbot <bot@sessionfs.dev>" -m "Release vX.Y.Z"
git push origin develop
```

### 9. Merge to main with sanitization

**This is the critical step.** Use `.release/private-files.txt` as the definitive list.

```bash
git checkout main
git merge develop --no-edit
```

Then remove ALL private files listed in `.release/private-files.txt`:
```bash
while IFS= read -r line; do
  [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
  line="${line%%#*}"
  line="${line%% *}"
  [ -z "$line" ] && continue
  git rm -rf "$line" 2>/dev/null
done < .release/private-files.txt
```

**VERIFY nothing leaked** (zero tolerance):
```bash
git ls-tree -r HEAD --name-only | while read f; do
  grep -qF "$f" .release/private-files.txt 2>/dev/null && echo "LEAK: $f"
done
```
If any leaks found, STOP and scrub with `git-filter-repo`.

Commit:
```bash
git commit --author="sessionfsbot <bot@sessionfs.dev>" -m "vX.Y.Z public release"
git push origin main
```

### 10. Tag
```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main --tags
git checkout develop
```

The release workflow (release.yml) will:
- Build wheel + sdist
- Publish to PyPI
- Create GitHub Release with changelog notes

### 11. Update GitHub repo description
```bash
gh repo edit --description "CURRENT DESCRIPTION WITH CORRECT TOOL COUNT"
```

### 12. Deploy API (if server code changed)
```bash
cd /Users/ola/Documents/Repo/sessionfs
docker build -t us-central1-docker.pkg.dev/sessionfs-prod/sessionfs/sessionfs-api:latest .
docker push us-central1-docker.pkg.dev/sessionfs-prod/sessionfs/sessionfs-api:latest
gcloud run deploy sessionfs-api --image us-central1-docker.pkg.dev/sessionfs-prod/sessionfs/sessionfs-api:latest --region us-central1
```
Or trigger via: `gh workflow run "Deploy API" --repo alwaysnix/sessionfs --ref main`

### 13. Run migrations (if new migration added)
```bash
gcloud run jobs execute sessionfs-migrate --region us-central1 --wait
```

### 14. Post-deploy verification
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
gh release view vX.Y.Z --repo alwaysnix/sessionfs
```

### 15. Update memory
- Update `project_status.md` with new version, test count, features

### 16. Report
Print summary table:

| Item | Status |
|------|--------|
| Version | vX.Y.Z |
| Tests | N passing |
| PyPI | published / pending |
| GitHub Release | created / pending |
| API | healthy at api.sessionfs.dev |
| Landing | deployed at sessionfs.dev |
| Dashboard | deployed at app.sessionfs.dev |
| Tag | vX.Y.Z pushed |

## Reference Files

| File | Purpose |
|------|---------|
| `.release/private-files.txt` | Files to strip from main — the single source of truth |
| `CHANGELOG.md` | Release notes — Keep a Changelog format |
| `.github/workflows/release.yml` | Tag → PyPI + GitHub Release automation |
| `.github/workflows/deploy-api.yml` | Push to main → Cloud Run deploy automation |
