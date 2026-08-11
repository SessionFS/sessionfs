# Static-Binary Spike — SessionFS CLI + Daemon

**Author:** Forge · **Date:** 2026-08-11 · **Status:** Recommendation

## Context

`pip install sessionfs` bounces users without a working Python environment.
A self-contained static binary (no Python, no venv, no pip) would remove the
top funnel-stage-1 blocker.  This doc answers: *can we ship a static `sfs`
binary in v0.16?*

## Options Evaluated

### PyInstaller

| Concern | Verdict |
|---------|---------|
| **watchdog (fsevents/inotify)** | **Hard block.** `watchdog` loads the native observer at import time via C extension. PyInstaller can bundle `.so`/`.dylib` files, but each platform needs its own binary AND the extension must be loadable from the tempdir. macOS codesigning with `--codesign-identity` helps but notarization + hardened runtime complicates this further. |
| **aiosqlite / SQLite** | Works — PyInstaller bundles `sqlite3` from the host Python. |
| **httpx / certifi** | Requires `--collect-data certifi` for the CA bundle. |
| **Binary size** | A quick dry-run produced ~28 MB (one platform, no compression). Three platforms → three binaries. |
| **CI matrix** | macOS-arm64, macOS-x64, Linux-x64, Linux-arm64 — 4 build runners with platform-specific PyInstaller invocations. |
| **Signing/notarization** | macOS requires Developer ID + notarytool for distribution. CI must handle this per binary. Significant infra cost. |

### PyOxidizer

| Concern | Verdict |
|---------|---------|
| **Rust-based, one-file** | Produces a true single-file binary with embedded Python. |
| **watchdog** | Same native-extension challenge as PyInstaller. PyOxidizer's extension loading story is less mature. |
| **Maturity** | Project is in low-maintenance mode. Fewer community resources. |
| **Cross-compilation** | Not supported — must build on each target platform. |

### shiv / zipapp

| Concern | Verdict |
|---------|---------|
| **Self-contained zip** | Produces a `.pyz` archive — still needs a system Python. |
| **Meets goal?** | No. The goal is to eliminate the Python dependency. |

## Dry-Run: PyInstaller (macOS, arm64)

Attempted in a throwaway venv (Python 3.12):

```bash
python3 -m venv /tmp/sfs-binary-test
source /tmp/sfs-binary-test/bin/activate
pip install sessionfs==0.14.0 pyinstaller
pyinstaller --onefile --name sfs \
  --hidden-import=watchdog.observers.fsevents \
  --hidden-import=watchdog.observers.inotify \
  --collect-data certifi \
  --collect-data httpx \
  "$(which sfs)"
```

Result:
- Build succeeded (~45s).
- Binary produced at `dist/sfs` — 28 MB.
- `dist/sfs --version` works.
- `dist/sfs daemon start` **fails on fsevents import** (`ModuleNotFoundError:
  watchdog.observers.fsevents` even with `--hidden-import`). The native
  `_fsevents.cpython-312-darwin.so` is bundled but the import path resolution
  from PyInstaller's tempdir fails.

Resolving this requires a custom PyInstaller hook for watchdog — not a
one-liner, but solvable (~1 day of hook work). The larger issue: every new
native dep adds fragility.

## Effort Estimate

| Task | Days |
|------|------|
| PyInstaller watchdog hook (both fsevents + inotify) | 1–2 |
| Multi-platform CI matrix (4 targets) | 2–3 |
| macOS signing + notarization pipeline | 3–5 |
| Binary-size optimization (UPX, exclusions) | 1–2 |
| Integration testing (daemon capture on each platform) | 2–3 |
| Release workflow (upload bins to GitHub Releases, brew tap, curl installer integration) | 2 |
| **Total** | **11–17 days** |

## Recommendation: GO for v0.16 (not v0.15)

**Go.** The Python-env requirement is the #1 silent bounce in our funnel.
PyInstaller is the right tool (largest community, most docs, GitHub release
CI templates exist). The native-extension challenge is a known, solved problem.

**But NOT for v0.15.** v0.15 is the adoption release — P0 already has
five workstreams and the static-binary work is heavy on infra (CI matrix +
signing pipeline) with little user-visible payoff beyond what pipx/brew/curl
already deliver.  Shipping pipx + brew + curl first (P0.3) removes most of the
friction; the static binary is the last 5% and isn't worth delaying the release.

**Recommendation:** Commit to a v0.16 static-binary beta after v0.15 ships.
Start the signing-infra work in parallel during v0.15 stabilization so the
CI pipeline is ready.

## Follow-Up Tickets

- `static-binary-pyinstaller-hook`: watchdog fsevents + inotify hooks.
- `static-binary-ci-matrix`: 4-platform PyInstaller CI with artifact upload.
- `static-binary-macos-signing`: Developer ID + notarytool pipeline.
- `static-binary-installer-integration`: curl installer detects and prefers the static binary.
