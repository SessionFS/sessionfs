"""SessionFS — Portable AI coding sessions."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("sessionfs")
except PackageNotFoundError:
    __version__ = "0.9.9"  # fallback for development
