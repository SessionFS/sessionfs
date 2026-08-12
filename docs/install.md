# Install SessionFS

> **Platforms:** macOS and Linux. Windows support is planned.

Choose the path that fits your setup.

## Recommended: pipx

[pipx](https://pipx.pypa.io) installs SessionFS in an isolated environment so it
never conflicts with other Python packages on your system.

```bash
pipx install sessionfs
```

**Upgrade:**

```bash
pipx upgrade sessionfs
```

**Uninstall:**

```bash
pipx uninstall sessionfs
```

## Homebrew (macOS)

```bash
brew tap sessionfs/tap
brew install sessionfs
```

**Upgrade:**

```bash
brew upgrade sessionfs
```

**Uninstall:**

```bash
brew uninstall sessionfs
brew untap sessionfs/tap
```

## Curl installer (all platforms)

A single command that detects the best available install method on your machine
(pipx → uv → python3 venv + symlink).

```bash
curl -fsSL https://get.sessionfs.dev | sh
```

**Upgrade:** re-run the same command — it upgrades in place.

**Uninstall:**

```bash
# If installed via venv path:
rm -rf ~/.sessionfs/venv ~/.local/bin/sfs ~/.local/bin/sfsd

# If installed via pipx:
pipx uninstall sessionfs

# If installed via uv:
uv tool uninstall sessionfs
```

## pip (fallback)

If you already have a Python environment you manage yourself:

```bash
pip install sessionfs
```

**Upgrade:**

```bash
pip install --upgrade sessionfs
```

**Uninstall:**

```bash
pip uninstall sessionfs
```

## Verify

```bash
sfs --version
```

## Next step

```bash
sfs init
```

The wizard auto-detects your installed AI tools, starts the daemon, and walks
you through setup in under a minute.

---

See the [Quickstart Guide](/quickstart/) for the full walkthrough.
See [Troubleshooting](/troubleshooting/) if you run into issues.
