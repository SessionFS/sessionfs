# Windows support — scoping spike (tk_6c0e9ee76e7c439c, v0.15 P1)

**Method:** static code audit (no Windows machine in this environment — a real
Windows CI run is the follow-up validation step). **Verdict at bottom.**

## Gap inventory (by blast radius)

### 1. Hard blockers (import-time or first-use crashes)

| Site | Problem | Fix shape | Size |
|---|---|---|---|
| `store/deleted.py` | `fcntl.flock` for deleted.json locking — module doesn't exist on Windows | `msvcrt.locking` fallback or the `portalocker` dep (adds a dep) or an atomic-rename lock-file protocol (no dep, cross-platform) | M |
| `sync/hooks_installer.py` | same `fcntl` usage | same shared helper | S (after the helper exists) |
| `cli/cmd_daemon.py` / `daemon/main.py` | `os.kill(pid, 0)` liveness probe works on Windows, but `SIGTERM`/`SIGHUP` semantics don't — Windows has no SIGHUP; `signal.SIGTERM` maps to TerminateProcess-ish behavior via `os.kill` only for the calling process's own handlers | drop SIGHUP reload on Windows (poll config mtime instead — the telemetry cache already re-reads daily); stop via `taskkill`/`proc.terminate()` handle | M |
| `subprocess.Popen(..., start_new_session=True)` (daemon spawn) | POSIX-only session detach | `creationflags=DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` on Windows | S |

### 2. Native tool storage paths (capture coverage)

`daemon/config.py`, `cli/cmd_init.py`, `cli/cmd_watcher.py` all branch
`Darwin` **else Linux** — Windows falls into the Linux branch and every path
is wrong. Actual Windows locations (to verify on a real machine):

- Claude Code: `%USERPROFILE%\.claude\projects\` (same dotdir convention — likely fine)
- Codex: `%USERPROFILE%\.codex\` (likely fine)
- VS Code globalStorage (Cline/Roo/Kilo): `%APPDATA%\Code\User\globalStorage\` — **wrong today**
- Cursor: `%APPDATA%\Cursor\` — **wrong today**
- Amp: `%LOCALAPPDATA%\amp\` (or XDG override) — **wrong today**
- Gemini/Copilot: dotdirs — likely fine

Fix: a third `Windows` branch in ONE shared path-provider (the audit found the
same path logic duplicated in 3 files — consolidate first, then add the branch). Size: M.

### 3. Quiet correctness hazards

- `0600`/`0700` chmod calls: no-ops on Windows (ACLs differ) — acceptable
  initially; document that key-file permissions are POSIX-only hardening.
- `os.open(..., O_EXCL)` atomic claims: work on Windows — fine.
- `os.rename` atomic install (init magic moment): works on same volume; fails
  overwriting on Windows where POSIX overwrites — our usage never overwrites
  (exists-precheck), so **fine**, but keep the test.
- Path handling: the codebase consistently uses `pathlib` (good); risk is in
  converters writing tool-native configs with `/` joins — grep found string
  interpolation of paths in `sfs_to_codex.py` / `sfs_to_gemini.py` resume
  writers. Audit each converter's write path. Size: S–M.
- watchdog: supports Windows (ReadDirectoryChangesW observer) — no code change
  expected, but event-coalescing behavior differs; needs soak testing.

### 4. CI matrix

- Add `windows-latest` to the test matrix for the unit suite (converters,
  store, telemetry) with `fcntl`-dependent tests skipped/behind the new lock
  helper. Full daemon integration on Windows = follow-up.
- PyPI wheel is pure-python — no build work.

## Effort estimate

- **Cross-platform lock helper + daemon spawn/stop + config-reload fallback:** ~3–4 days
- **Path provider consolidation + Windows branch + converter write audit:** ~3 days
- **CI matrix + skips + green-up:** ~2 days
- **Real-machine validation of native storage paths + daemon soak:** ~3 days (needs a Windows box/VM)

**Total: roughly 2 engineer-weeks to credible beta**, dominated by
validation, not code.

## Recommendation (decision output)

**GO for v0.16 as a headline, gated on acquiring a Windows test machine/VM
first.** The blockers are shallow (locking, spawn semantics, paths) and the
architecture (pathlib + watchdog + pure-python wheel) was already
Windows-friendly. Until then, docs state platform support honestly:
**macOS and Linux today; Windows planned** (the honest-platform-docs edit
should ship with v0.15).
