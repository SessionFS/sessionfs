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

### 3b. MANDATORY: Run Shield Security Review

**This step is NON-NEGOTIABLE. No release ships without a clean security review.**

Load the Shield-SR agent from `.agents/shield-security-review.md` and run the full review. Use the Agent tool to launch it:

```
Prompt: "You are the Shield security review agent. Load your persona from .agents/shield-security-review.md and run the COMPLETE pre-release security review for v{VERSION}. Fix all CRITICAL and HIGH findings. Report results in the specified format."
```

The agent will:
1. Run pip-audit — fix all vulnerabilities
2. Run npm audit — fix all vulnerabilities
3. Run bandit — fix HIGH/MEDIUM findings
4. Scan for hardcoded secrets
5. Review all new code since last release for OWASP issues
6. Check config, Helm, and LLM-specific security
7. Run full test suite after any fixes

**STOP if the agent reports any unresolved CRITICAL or HIGH finding.**
Only proceed to step 4 when the agent reports: "Zero critical/high findings. Release approved."

### 4. Bump version

Only TWO files hold the version — everything else reads dynamically:
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/sessionfs/__init__.py` → fallback in the `except` block

Also bump:
- `charts/sessionfs/Chart.yaml` → `version` and `appVersion`
- `dashboard/VERSION` → single-line `X.Y.Z` (the dashboard footer reads this; Vercel's build container can't reach repo-root `pyproject.toml`)

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
grep -rn "$OLD_VERSION" README.md CLAUDE.md charts/sessionfs/Chart.yaml pyproject.toml src/sessionfs/__init__.py dashboard/VERSION
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
| `LICENSE` | MIT license present at root |
| `ee/LICENSE` | FSL-1.1-Apache-2.0 license present in ee/ |
| `ee/sessionfs_ee/__init__.py` | ee package importable |
| `site/src/pages/index.astro` | Version, test count, feature cards, meta tags |
| `site/src/pages/changelog.astro` | New version section added |
| `site/src/pages/pricing.astro` | Tiers match server-side definitions |

#### 6f. MANDATORY: Run Scribe-Site Sync (when `site/` has changes)

**The product site must NEVER be stale after a release.**

**Gate first.** Skip Scribe-Site invocation when `site/` is untouched
since the last tag — saves the agent invocation (~80k tokens of work)
on code-only releases like v0.10.11. Check before invoking:

```bash
LAST_TAG=$(git describe --abbrev=0 --tags)
SITE_CHANGES=$(git diff --name-only "${LAST_TAG}..HEAD" -- site/ | wc -l | tr -d ' ')
echo "site/ changes since ${LAST_TAG}: ${SITE_CHANGES}"
```

If `SITE_CHANGES == 0`, **skip step 6f entirely** and proceed to 6g.
Even the site deploy below is skipped — Vercel's Deploy Site
pipeline only fires when `site/` files change in the same push, so
there's nothing to deploy.

If `SITE_CHANGES > 0`, run the Scribe-Site agent — every changed
`site/` file (changelog, version refs, test counts, meta tags,
feature/pricing pages) needs a sync pass:

Load the Scribe-Site agent from `.agents/scribe-site-sync.md` and run the full site sync:

```
Prompt: "You are the Scribe-Site agent. Load your persona from .agents/scribe-site-sync.md and sync the product site (site/) for v{VERSION}. Update version numbers, test counts, changelog, feature pages, pricing, MCP docs, and meta tags. Verify zero stale references remain."
```

After the agent completes, deploy the site:
```bash
cd site && npx vercel --yes --prod
```

**Note: this is still MANDATORY when `SITE_CHANGES > 0`.** The gate
only short-circuits the zero-change case — never skip Scribe-Site on
a release that actually touched `site/` files.

#### 6g. Forbidden strings
```bash
grep -rn "sfs pull --handoff\|alwaysnix\|Dropbox" README.md docs/ landing/ src/ dashboard/src/
```
Must return zero results (except troubleshooting doc warning).

#### 6h. Public docs positioning + operator-leak audit

The public site must sell the hosted cloud product and explain customer-facing setup. It must not expose internal operator runbooks or imply that customers should wire SessionFS billing infrastructure themselves.

Run this scan against public-facing docs and site pages:

```bash
rg -n \
  "Free during beta|pricing at v1\.0|Beta mode|all features are available regardless of tier|Stripe|stripe|webhooks/stripe|checkout\.session\.completed|customer\.subscription\.(updated|deleted)|SFS_STRIPE_|billing portal|hosted checkout|checkout session|Customer Portal|connect(ed)? Stripe to a self-hosted|self-hosted deployments that have not connected Stripe|Without all five variables" \
  README.md site/src docs/
```

This must return zero results unless the match is code/dependency metadata or private/internal release material that will never ship to `main`. If it finds a public page:
- Rewrite the page from the buyer/customer point of view.
- Drive users to hosted signup: `https://app.sessionfs.dev/login?mode=signup`.
- Keep self-hosted copy enterprise-oriented: license / contract / support boundary, not payment processor setup or internal SaaS operations.
- Move true operator details to private docs or keep them out of the public site.

Manually inspect these high-risk pages every release:

| File | What to check |
|------|---------------|
| `site/src/content/docs/billing.mdx` | Cloud signup, plans, customer billing only; no payment-processor setup |
| `site/src/content/docs/environment.mdx` | No hosted-only secrets presented as customer self-host requirements |
| `site/src/content/docs/self-hosted.mdx` | Deployment guidance only; no SaaS billing internals |
| `site/src/content/docs/organizations.mdx` | Customer-facing org billing language; no webhook/customer-transfer implementation details |
| `site/src/pages/pricing.astro` | Current prices, no beta pricing copy, clear cloud CTA |
| `site/src/pages/index.astro` | Cloud signup CTA visible above the fold |

Also search for implementation-flavored billing terms and rewrite them unless they are code/dependency metadata:

```bash
rg -n "Stripe customer|org-first webhook|subscription_id disambiguation|webhook handling" README.md site/src docs/
```

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

**This is the critical step.** `.release/private-files.txt` is the single source of truth for which paths must NOT appear on main.

```bash
git checkout main
git merge develop --no-edit
```

**Expected merge noise** (do not panic): main has historically deleted some private files (e.g. `.claude/commands/release.md`, `CLAUDE.md`) that develop still modifies. You'll see `CONFLICT (modify/delete)` for those exact paths. Resolve by re-deleting:
```bash
git rm -f .claude/commands/release.md CLAUDE.md 2>/dev/null
```
Anything else conflicting should be investigated manually — it's likely a real change collision, not the expected private-file mismatch.

Then run the deterministic sanitizer (the file is on develop, so we need to fetch it first if it's not still in the working tree after the merge):
```bash
# Make sure the helper is reachable. The merge brought it in
# unless we're in a clean-checkout flow.
ls .release/sanitize_main.py >/dev/null || git checkout develop -- .release/sanitize_main.py

# Dry run first (exits 1 if any private path is tracked — expected after merge):
.venv/bin/python .release/sanitize_main.py

# Apply: runs git rm on every leak, then re-verifies. Exits 0 only if
# the branch is leak-clean post-sweep.
.venv/bin/python .release/sanitize_main.py --apply
```

The helper reads `.release/private-files.txt`, runs `git rm -rf` on each tracked match, and re-verifies before exiting. It replaces the prior ad-hoc bash loop (which the sandbox occasionally flagged as risky because it looked like an exfiltration prelude). See `tests/unit/test_sanitize_main.py` for the unit-test coverage of the parser + leak-finder.

If the helper exits non-zero, STOP — the branch is not safe to push. Diagnose the residual leak before continuing.

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

**Always use cache-busted URLs.** Vercel's edge cache can serve stale content
from a healthy CDN even when a freshly-promoted deployment is broken or
empty. Hit `?nocache=$(date +%s)` to bypass.

Set the release version once so the rest of the block uses it:

```bash
VERSION="X.Y.Z"   # e.g. "0.9.9.10"

# API health
curl -s https://api.sessionfs.dev/health

# Landing page reachable + non-empty
SITE_HTML=$(curl -s "https://sessionfs.dev/?nocache=$(date +%s)")
echo "$SITE_HTML" | grep -o "<title>[^<]*</title>"

# STRICT version check — the changelog page MUST contain the literal
# `v${VERSION}` string. Anything else (loose major.minor prefix, the
# standing "Public Beta" badge, etc.) lets a stale build through —
# e.g. when releasing 0.9.9.10 a build still on 0.9.7.2 would have
# satisfied v0.9. / Public Beta. The changelog is also what
# Scribe-Site updates every release, so its presence proves both
# (a) the deploy isn't empty AND (b) the site sync ran.
CHANGELOG_HTML=$(curl -s "https://sessionfs.dev/changelog?nocache=$(date +%s)")
if echo "$CHANGELOG_HTML" | grep -F "v${VERSION}" >/dev/null 2>&1; then
  echo "Site changelog contains v${VERSION} — deploy is current."
else
  echo "WARN: https://sessionfs.dev/changelog does NOT contain v${VERSION}."
  echo "      Likely an empty/stale Vercel deploy or Scribe-Site didn't run."
  echo "      Inspect with the live-alias check below and fix before tagging."
fi

# Dashboard
curl -s -o /dev/null -w "%{http_code}\n" "https://app.sessionfs.dev/?nocache=$(date +%s)"

# Vercel deployment health check — authoritative via the live alias URL.
# `vercel inspect <alias-url>` resolves the alias server-side and returns
# the exact deployment serving traffic, with no race against
# `vercel ls` ordering or duplicate alias rows. The x-vercel-id header
# is captured for the log trail so we have a trace id if support is needed.
SITE_VID=$(curl -sI "https://sessionfs.dev/?nocache=$(date +%s)" | awk -F': ' '/^x-vercel-id/{print $2}' | tr -d '\r')
echo "Live site deploy x-vercel-id (trace): $SITE_VID"
# Capture inspect output + exit code BEFORE piping into grep — otherwise
# a piped grep masks any auth/network/CLI failure as exit-0 with empty
# output (`set -o pipefail` is not portable across the readers of this
# skill, so we check the exit code explicitly).
if VCL_INSPECT=$(cd site && npx vercel inspect https://sessionfs.dev 2>&1); then
  echo "$VCL_INSPECT" | grep -E "Builds|status|Aliases|url\s" | head -10
else
  echo "WARN: vercel inspect https://sessionfs.dev failed — release is" \
    "unverified at the Vercel layer:"
  echo "$VCL_INSPECT" | head -10
fi

# PyPI (after release workflow completes)
curl -s https://pypi.org/pypi/sessionfs/json | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"

# GitHub Release
gh release view "v${VERSION}" --repo SessionFS/sessionfs
```

### 12b. Post-PyPI smoke test

Verify the wheel actually works end-to-end. Checking PyPI's `/json` only confirms the metadata uploaded; if the wheel didn't ship the right files (e.g. a new CLI command or a new MCP tool registration), users discover it. This step catches that before anyone files a bug.

PyPI's index can lag a minute or two behind the release workflow — the loop retries until the wheel is installable.

```bash
VERSION=<the version you just shipped, e.g. 0.10.12>

# Throwaway venv so the install doesn't touch the dev env.
# MUST use python3.10+ — sessionfs requires-python >= 3.10. On macOS,
# `python3` alone is 3.9 which silently fails ALL pip resolves with the
# misleading "no matching distribution" (real cause: the wheel's
# python_requires excludes 3.9). Pick the highest 3.x explicitly.
SMOKE_DIR=$(mktemp -d)
PY=$(command -v python3.12 || command -v python3.11 || command -v python3.10)
[ -z "$PY" ] && { echo "ERROR: need python3.10+ on PATH for the smoke test"; rm -rf "$SMOKE_DIR"; exit 1; }
"$PY" -m venv "$SMOKE_DIR/venv"
"$SMOKE_DIR/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1

# Retry until the published wheel is installable (~3 min ceiling).
# Important: capture pip's actual exit code, not `tail`'s — piping pip
# output through `| tail` breaks the conditional and makes the retry
# loop short-circuit on a "successful tail" of a failed install. We
# learned this the hard way during the v0.10.12 release.
INSTALL_OK=
for attempt in 1 2 3 4 5 6; do
  if "$SMOKE_DIR/venv/bin/pip" install --quiet "sessionfs==${VERSION}" 2>/tmp/pip_smoke_err; then
    echo "PyPI install ok (attempt $attempt)"
    INSTALL_OK=1
    break
  fi
  echo "PyPI not ready (attempt $attempt): $(tail -1 /tmp/pip_smoke_err)"
  [ "$attempt" -lt 6 ] && sleep 30
done
[ -z "$INSTALL_OK" ] && { echo "SMOKE FAIL: install never succeeded after 6 attempts"; rm -rf "$SMOKE_DIR"; exit 1; }

# Smoke: version reports right + sfs --help shape + new command help.
"$SMOKE_DIR/venv/bin/python" -c "import sessionfs; assert sessionfs.__version__ == '${VERSION}', f'expected ${VERSION}, got {sessionfs.__version__}'; print('version ok')"
"$SMOKE_DIR/venv/bin/sfs" --help 2>&1 | grep -q "Portable AI coding sessions" || { echo "sfs --help shape regressed"; rm -rf "$SMOKE_DIR"; exit 1; }

# If this release added a new command/subcommand, check its help renders.
# Replace the example with the actual new commands shipped this cycle.
# Example for v0.10.12:
#   "$SMOKE_DIR/venv/bin/sfs" project promote-eligible --help 2>&1 | grep -q "min-length"

rm -rf "$SMOKE_DIR"
echo "Post-PyPI smoke: clean."
```

**If smoke fails:** the wheel is broken on PyPI but PyPI doesn't support unpublish-and-replace at the same version. You must (a) yank the broken release with `gh release edit v${VERSION} --draft` + cut a fast patch release, or (b) accept a bad release. Catching this before announcing externally is the whole point of this step.

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
