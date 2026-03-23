# /brief — Generate a change brief since last release

Summarize everything that changed since the last tagged version. Only report what's new — not the full project state.

## Steps

### 1. Find the last release
```bash
git describe --tags --abbrev=0
```
This gives the last tag (e.g., `v0.1.0`).

### 2. Get all commits since that tag on develop
```bash
git log <last-tag>..develop --oneline --no-merges
```

### 3. Get the diff stats
```bash
git diff <last-tag>..develop --stat
```

### 4. Generate the brief

Format as:

```
## SessionFS — Changes since <last-tag>

### Summary
<2-3 sentence overview of what shipped>

### New Features
- <feature>: <one-line description>

### New Tool Support
| Tool | Capture | Resume | Tests |
(only list NEW tools added since last tag)

### Infrastructure
- <any infra changes>

### Bug Fixes
- <any fixes>

### Stats
- Tests: <old count> → <new count>
- Files changed: <N>
- Insertions: <N>, Deletions: <N>

### Files Affected
<grouped list of changed files by category: converters, watchers, server, CLI, dashboard, docs, infra, tests>
```

### 5. Output
Print the brief directly. Do NOT save it to a file unless the user asks.

### Rules
- Only report changes since the last tag, not the full project state
- Group by category, not chronologically
- Commit messages should read as human-authored (per project rules)
- Do not mention AI, Claude, or any tooling in the brief
- Include test count before and after
- Include the git diff stat summary
