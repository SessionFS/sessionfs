#!/bin/sh
#===============================================================================
# SessionFS Installer — https://get.sessionfs.dev
#
# Intended hosting:  curl -fsSL https://get.sessionfs.dev | sh
#
# This file is the source of truth.  A byte-identical copy lives at
# site/public/install.sh for serving via the Astro static site.
# CI enforces they stay in sync (see .github/workflows/ci.yml install-sync job).
#
# To serve at get.sessionfs.dev:
#   1. Create a Vercel project or alias for the get.sessionfs.dev domain.
#   2. Configure a rewrite so that requests to get.sessionfs.dev/install.sh
#      resolve to site/public/install.sh (the Astro + Vercel deploy serves
#      public/ files at the domain root).
#   3. If the domain is a separate Vercel project, set its output directory
#      to site/public/ and deploy only that file.
#
# Strategy (tried in order):
#   pipx  →  uv tool install  →  python3 -m venv + symlink
#
# Safety:
#   - set -eu (exit on error / unset variable)
#   - No sudo.  Refuses to run as root unless --allow-root is passed.
#   - Idempotent: re-running upgrades in place.
#   - This installer script itself sends no telemetry and phones nothing home.
#     The installed product collects anonymous usage telemetry (see
#     docs/telemetry.md in the repo; opt out with SFS_NO_TELEMETRY=1).
#===============================================================================
set -eu

# --- constants ---------------------------------------------------------------
readonly PACKAGE="sessionfs"
readonly VENV_DIR="${HOME}/.sessionfs/venv"
readonly BIN_DIR="${HOME}/.local/bin"
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=10

# --- helpers -----------------------------------------------------------------
err() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; }
info() { printf '\033[1;34m→\033[0m %s\n' "$*"; }
success() { printf '\033[1;32m✓\033[0m %s\n' "$*"; }

usage() {
	cat <<'EOF'
Usage: curl -fsSL https://get.sessionfs.dev | sh
       curl -fsSL https://get.sessionfs.dev | sh -s -- --allow-root

Options:
  --allow-root   Bypass the root-user guard (not recommended).
  --help         Show this message.
EOF
}

need_cmd() {
	if ! command -v "$1" >/dev/null 2>&1; then
		err "Required command not found: $1"
		return 1
	fi
}

python_ok() {
	# Returns 0 if $1 is a usable Python >= 3.10.
	"$1" -c "
import sys
vi = sys.version_info
ok = (vi.major, vi.minor) >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR})
sys.exit(0 if ok else 1)
" 2>/dev/null
}

find_python3() {
	# Return the first usable python3 on PATH.
	for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
		if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
			printf '%s' "$candidate"
			return 0
		fi
	done
	return 1
}

# --- guards ------------------------------------------------------------------
ALLOW_ROOT=0
for arg in "$@"; do
	case "$arg" in
		--allow-root) ALLOW_ROOT=1 ;;
		--help) usage; exit 0 ;;
		*) err "Unknown option: $arg"; usage; exit 2 ;;
	esac
done

if [ "$(id -u)" -eq 0 ] && [ "$ALLOW_ROOT" -ne 1 ]; then
	err "Refusing to run as root.  Re-run with --allow-root if you are sure."
	exit 1
fi

# Verify the installed `sfs` entry point. PATH may not yet include the
# installer's bin dir in THIS shell (pipx/uv put it in ~/.local/bin), so fall
# back to the well-known locations before declaring failure.
verify_sfs() {
	if command -v sfs >/dev/null 2>&1; then
		sfs --help >/dev/null 2>&1 && return 0
	fi
	for _cand in "${HOME}/.local/bin/sfs" "${HOME}/.local/share/uv/tools/sessionfs/bin/sfs"; do
		if [ -x "${_cand}" ]; then
			"${_cand}" --help >/dev/null 2>&1 && return 0
		fi
	done
	return 1
}

# --- strategy 1: pipx --------------------------------------------------------
try_pipx() {
	command -v pipx >/dev/null 2>&1 || return 1
	info "pipx found — installing with pipx"
	if pipx list --short 2>/dev/null | grep -q "^${PACKAGE} "; then
		info "${PACKAGE} is already installed — upgrading"
		pipx upgrade "${PACKAGE}" 2>/dev/null || pipx install --force "${PACKAGE}"
	else
		pipx install "${PACKAGE}"
	fi
	verify_sfs
}

# --- strategy 2: uv ----------------------------------------------------------
try_uv() {
	command -v uv >/dev/null 2>&1 || return 1
	info "uv found — installing with uv tool install"
	uv tool install "${PACKAGE}" --reinstall 2>/dev/null || {
		# Fall back to pip if uv tool install isn't available.
		uv pip install "${PACKAGE}" --user 2>/dev/null
	}
	verify_sfs
}

# --- strategy 3: venv + symlink ----------------------------------------------
try_venv() {
	_python3="$(find_python3)" || {
		err "No Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} found on PATH."
		err "Install Python 3.10+ first, or use pipx/uv for a self-contained install."
		return 1
	}

	info "Using ${_python3} — creating venv at ${VENV_DIR}"

	if [ -d "${VENV_DIR}" ]; then
		info "venv already exists — upgrading package"
		"${VENV_DIR}/bin/python" -m pip install --upgrade "${PACKAGE}" >/dev/null
	else
		"${_python3}" -m venv "${VENV_DIR}"
		"${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
		"${VENV_DIR}/bin/python" -m pip install "${PACKAGE}" >/dev/null
	fi

	# Create symlink in ~/.local/bin.
	mkdir -p "${BIN_DIR}"
	ln -sf "${VENV_DIR}/bin/sfs" "${BIN_DIR}/sfs"
	ln -sf "${VENV_DIR}/bin/sfsd" "${BIN_DIR}/sfsd"

	# Warn if ~/.local/bin is not on PATH.
	case ":${PATH}:" in
		*:"${BIN_DIR}":*) ;;
		*)
			printf '\n\033[1;33m⚠\033[0m  %s is not on your PATH.\n' "${BIN_DIR}"
			printf '   Add this to your shell profile:\n\n'
			printf '     export PATH="%s:$PATH"\n\n' "${BIN_DIR}"
			printf '   Then restart your shell or run:\n\n'
			printf '     export PATH="%s:$PATH"\n\n' "${BIN_DIR}"
			;;
	esac

	"${BIN_DIR}/sfs" --help >/dev/null 2>&1
}

# --- main --------------------------------------------------------------------
main() {
	info "Installing ${PACKAGE}..."

	if try_pipx; then
		:
	elif try_uv; then
		:
	elif try_venv; then
		:
	else
		err "All install strategies failed."
		err "Install pipx (https://pipx.pypa.io), uv (https://docs.astral.sh/uv),"
		err "or Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} and try again."
		exit 1
	fi

	success "${PACKAGE} installed successfully"
	printf '\n\033[1mNext step:\033[0m  sfs init\n\n'
}

main "$@"
